"""SQLite storage: settings, watchlist, signal history, alerts, paper trading and
journal. Standard library only; one file in data/trader.db."""

import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY, added_at REAL NOT NULL, note TEXT DEFAULT '', mode TEXT DEFAULT 'swing');
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, mode TEXT NOT NULL,
  rating TEXT NOT NULL, score REAL, confidence REAL, price REAL, setups TEXT, data_ts REAL);
CREATE INDEX IF NOT EXISTS signals_sym ON signals(symbol, ts);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT, kind TEXT NOT NULL,
  message TEXT NOT NULL, level TEXT DEFAULT 'info', dedupe TEXT UNIQUE, seen INTEGER DEFAULT 0,
  telegram INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS paper_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  qty INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target1 REAL, target2 REAL,
  mode TEXT NOT NULL, status TEXT NOT NULL, risk_verdict TEXT, approval TEXT, reason TEXT,
  plan TEXT);
CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
  entry_price REAL NOT NULL, stop REAL NOT NULL, target1 REAL, target2 REAL, mode TEXT NOT NULL,
  opened_at REAL NOT NULL, closed_at REAL, exit_price REAL, exit_reason TEXT, costs REAL DEFAULT 0,
  pnl REAL, status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at REAL NOT NULL);
"""

DEFAULT_SETTINGS = {
    "capital": 100000.0,          # total trading capital, INR
    "cash": 100000.0,             # available cash for paper trading, INR
    "risk_per_trade_pct": 1.0,    # max loss per trade as % of capital
    "max_daily_loss_pct": 2.0,
    "max_trades_per_day": 3,
    "max_position_pct": 20.0,     # max % of capital in one position
    "min_reward_risk": 1.5,
    "style": "swing",             # intraday | swing | longterm
    "paper": True,                # paper trading only (no live broker in this app)
    "allow_short": False,
    "allow_margin": False,
    "allow_options": False,
    "intraday_no_entry_before": "09:30",
    "intraday_no_entry_after": "14:45",
    "intraday_flat_by": "15:15",
    "slippage_pct": 0.05,
    "brokerage_per_order": 20.0,  # flat discount-broker fee cap, INR
}


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


_conn = None


def db():
    global _conn
    with _lock:
        if _conn is None:
            _conn = conn()
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def q(sql, args=(), one=False):
    with _lock:
        cur = db().execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        cur = db().execute(sql, args)
        db().commit()
        return cur.lastrowid


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass
    # Safety switches that this app never turns on (analysis + paper only)
    s["paper"] = True
    return s


def save_settings(updates):
    allowed = set(DEFAULT_SETTINGS) - {"paper"}
    for k, v in updates.items():
        if k in allowed:
            x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (k, json.dumps(v)))
    return get_settings()


def add_alert(symbol, kind, message, level="info", dedupe=None):
    """Insert once per dedupe key ("rings once"). Returns the id or None if it already rang."""
    try:
        return x("INSERT INTO alerts(ts, symbol, kind, message, level, dedupe) VALUES(?,?,?,?,?,?)",
                 (time.time(), symbol, kind, message, level, dedupe))
    except sqlite3.IntegrityError:
        return None
