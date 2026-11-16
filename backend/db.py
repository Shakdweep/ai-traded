"""Storage: settings, watchlist, signal history, alerts, paper trading, journal and
accounts.

- On your own PC: SQLite, one file in data/trader.db (standard library only).
- When DATABASE_URL is set (the hosted site): Postgres, e.g. a Neon project created
  just for AI Traded. Data then survives restarts and redeploys.

All SQL is written once with "?" placeholders; for Postgres it is translated to "%s"
and the schema types are mapped (REAL -> DOUBLE PRECISION, AUTOINCREMENT -> BIGSERIAL).
"""

import contextvars
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.RLock()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USING_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# tables whose INSERTs return the new row id
_ID_TABLES = {"signals", "alerts", "paper_orders", "paper_positions", "journal", "email_codes", "invites",
              "data_runs", "cohorts", "predictions", "bot_trades", "bot_events"}

# Whose data a request (or a background step) works on: the signed-in account id, or ""
# on your own PC where there is no login. Set once per request by the login middleware.
_owner = contextvars.ContextVar("owner", default="")


def owner():
    return _owner.get()


def set_owner(value):
    return _owner.set(value or "")


@contextmanager
def as_owner(value):
    token = _owner.set(value or "")
    try:
        yield
    finally:
        _owner.reset(token)

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
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS accounts (
  email TEXT PRIMARY KEY, name TEXT NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
  created_at REAL NOT NULL, last_login REAL, pw_changed_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS email_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL,
  payload TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER DEFAULT 0,
  used INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS email_codes_lookup ON email_codes(email, purpose, created_at);
CREATE TABLE IF NOT EXISTS user_settings (
  owner TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (owner, key));
CREATE TABLE IF NOT EXISTS user_watchlist (
  owner TEXT NOT NULL, symbol TEXT NOT NULL, added_at REAL NOT NULL, note TEXT DEFAULT '', mode TEXT DEFAULT 'swing',
  PRIMARY KEY (owner, symbol));
CREATE TABLE IF NOT EXISTS daily_prices (
  symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL NOT NULL, prev_close REAL,
  volume REAL, deliv_qty REAL, deliv_pct REAL, source TEXT NOT NULL, PRIMARY KEY (symbol, date));
CREATE INDEX IF NOT EXISTS daily_prices_date ON daily_prices(date);
CREATE TABLE IF NOT EXISTS bulk_deals (
  date TEXT NOT NULL, symbol TEXT NOT NULL, client TEXT NOT NULL, side TEXT NOT NULL, qty REAL, price REAL,
  PRIMARY KEY (date, symbol, client, side, qty, price));
CREATE TABLE IF NOT EXISTS data_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, started_at REAL, finished_at REAL, status TEXT,
  rows INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS model_versions (
  version TEXT PRIMARY KEY, created_at REAL NOT NULL, description TEXT, params TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS cohorts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, week_of TEXT NOT NULL, as_of TEXT NOT NULL, kind TEXT NOT NULL,
  model_version TEXT NOT NULL, universe TEXT, scored INTEGER, created_at REAL NOT NULL, notes TEXT,
  UNIQUE (week_of, kind, model_version));
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id INTEGER NOT NULL, symbol TEXT NOT NULL, as_of TEXT NOT NULL,
  model_version TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS predictions_cohort ON predictions(cohort_id);
CREATE TABLE IF NOT EXISTS outcomes (
  prediction_id INTEGER NOT NULL, horizon INTEGER NOT NULL, eval_date TEXT, actual_close REAL, actual_ret_pct REAL,
  window_high REAL, window_low REAL, predicted_dir TEXT, actual_dir TEXT, dir_correct INTEGER, in_range INTEGER,
  abs_err REAL, pct_err REAL, support_held INTEGER, resistance_broken INTEGER, stop_hit INTEGER, nifty_ret_pct REAL,
  evaluated_at REAL, PRIMARY KEY (prediction_id, horizon));
CREATE TABLE IF NOT EXISTS bot_accounts (
  id TEXT PRIMARY KEY, name TEXT, start_capital REAL NOT NULL, start_date TEXT, trading_days INTEGER, config TEXT,
  created_at REAL, status TEXT);
CREATE TABLE IF NOT EXISTS bot_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
  qty INTEGER NOT NULL, entry_time REAL, entry_bar_t REAL, entry_price REAL, invested REAL, stop REAL, target REAL,
  status TEXT NOT NULL, signal TEXT, last_checked_t REAL, exit_time REAL, exit_price REAL, exit_reason TEXT,
  received REAL, charges REAL, charges_detail TEXT, gross_pnl REAL, net_pnl REAL);
CREATE INDEX IF NOT EXISTS bot_trades_account ON bot_trades(account, day);
CREATE TABLE IF NOT EXISTS bot_days (
  account TEXT NOT NULL, day TEXT NOT NULL, start_equity REAL, start_cash REAL, end_equity REAL, trades INTEGER,
  wins INTEGER, invested REAL, received REAL, charges REAL, net_pnl REAL, closed_at REAL, PRIMARY KEY (account, day));
CREATE TABLE IF NOT EXISTS bot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, ts REAL NOT NULL, day TEXT, kind TEXT, symbol TEXT,
  message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS bot_events_account ON bot_events(account, ts);
CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT NOT NULL UNIQUE, hint TEXT, note TEXT,
  created_at REAL NOT NULL, used_by TEXT, used_at REAL, revoked INTEGER DEFAULT 0);
"""

# Columns added after the first version. Each is added once if missing (safe on every start).
MIGRATIONS = [
    ("alerts", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("paper_orders", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("paper_positions", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("journal", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "status", "TEXT NOT NULL DEFAULT 'active'"),
    ("accounts", "contact_email", "TEXT"),
    ("accounts", "invite_id", "INTEGER"),
    ("accounts", "approved_at", "REAL"),
    ("accounts", "last_seen", "REAL"),
    ("signals", "entry", "REAL"),
    ("signals", "stop", "REAL"),
    ("signals", "target1", "REAL"),
    ("signals", "target2", "REAL"),
    ("bot_accounts", "runner", "TEXT"),
    ("bot_accounts", "lease_until", "REAL"),
]
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS alerts_owner ON alerts(owner, ts);
CREATE INDEX IF NOT EXISTS paper_orders_owner ON paper_orders(owner, status);
CREATE INDEX IF NOT EXISTS paper_positions_owner ON paper_positions(owner, status);
CREATE INDEX IF NOT EXISTS journal_owner ON journal(owner, ts);
CREATE INDEX IF NOT EXISTS accounts_contact ON accounts(contact_email);
INSERT INTO user_settings(owner, key, value) SELECT '', key, value FROM settings WHERE true ON CONFLICT DO NOTHING;
INSERT INTO user_watchlist(owner, symbol, added_at, note, mode) SELECT '', symbol, added_at, note, mode FROM watchlist
  WHERE true ON CONFLICT DO NOTHING;
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


def pg_schema(schema=SCHEMA):
    """The SQLite schema in Postgres types (REAL is only 4 bytes in Postgres, so timestamps need DOUBLE)."""
    out = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", out)


def pg_sql(sql):
    """'?' placeholders -> '%s' (and escape any literal '%')."""
    return sql.replace("%", "%%").replace("?", "%s")


def _returns_id(sql):
    m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    return bool(m and m.group(1).lower() in _ID_TABLES and "RETURNING" not in sql.upper())


if USING_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    IntegrityError = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)
    _RETRY = (psycopg.OperationalError, psycopg.InterfaceError)
else:
    IntegrityError = (sqlite3.IntegrityError,)
    _RETRY = ()


def conn():
    if USING_POSTGRES:
        # autocommit: every statement stands alone, like the SQLite code expects;
        # prepare_threshold=None keeps it safe behind Neon's pooled (PgBouncer) endpoint
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None,
                               connect_timeout=15, keepalives=1, keepalives_idle=60)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


_conn = None


def db():
    global _conn
    with _lock:
        if _conn is None:
            c = conn()
            if USING_POSTGRES:
                for stmt in filter(str.strip, pg_schema().split(";")):
                    c.execute(stmt)
            else:
                c.executescript(SCHEMA)
                c.commit()
            _migrate(c)
            _conn = c
        return _conn


def _migrate(c):
    """Add the newer columns to older databases, then copy single-user data (settings and
    watchlist from before accounts existed) to the local owner "" once."""
    for table, col, decl in MIGRATIONS:
        if USING_POSTGRES:
            c.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s" % (table, col, pg_schema(decl)))
        elif col not in {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}:
            c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    if USING_POSTGRES:
        for stmt in filter(str.strip, POST_MIGRATION.split(";")):
            c.execute(stmt)
    else:
        c.executescript(POST_MIGRATION)
        c.commit()


def _run(fn):
    """Run fn(connection); on Postgres, reconnect once if the connection dropped
    (Neon closes idle connections when its compute sleeps)."""
    global _conn
    with _lock:
        try:
            return fn(db())
        except _RETRY:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001 - it is already broken
                pass
            _conn = None
            return fn(db())


def q(sql, args=(), one=False):
    def go(c):
        cur = c.execute(pg_sql(sql) if USING_POSTGRES else sql, args)
        return [dict(r) for r in cur.fetchall()]
    rows = _run(go)
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    def go(c):
        if USING_POSTGRES:
            want_id = _returns_id(sql)
            cur = c.execute(pg_sql(sql + (" RETURNING id" if want_id else "")), args)
            return cur.fetchone()["id"] if want_id else None
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    return _run(go)


def xmany(sql, rows):
    """Insert/update many rows in one round trip (scans write ~100 signal rows at once)."""
    rows = list(rows)
    if not rows:
        return

    def go(c):
        if USING_POSTGRES:
            with c.cursor() as cur:
                cur.executemany(pg_sql(sql), rows)
        else:
            c.executemany(sql, rows)
            c.commit()
    _run(go)


def backend_name():
    return "postgres" if USING_POSTGRES else "sqlite"


def get_settings(who=None):
    who = owner() if who is None else who
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM user_settings WHERE owner=?", (who,)):
        try:
            s[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass
    # Safety switches that this app never turns on (analysis + paper only)
    s["paper"] = True
    return s


def save_settings(updates, who=None):
    who = owner() if who is None else who
    allowed = set(DEFAULT_SETTINGS) - {"paper"}
    for k, v in updates.items():
        if k in allowed:
            x("INSERT INTO user_settings(owner, key, value) VALUES(?, ?, ?) "
              "ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value", (who, k, json.dumps(v)))
    return get_settings(who)


def add_alert(symbol, kind, message, level="info", dedupe=None, who=None):
    """Insert once per dedupe key ("rings once") for one owner. Returns the id or None if it already rang."""
    who = owner() if who is None else who
    try:
        return x("INSERT INTO alerts(ts, symbol, kind, message, level, dedupe, owner) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), symbol, kind, message, level, "%s|%s" % (who, dedupe) if dedupe else None, who))
    except IntegrityError:
        return None
