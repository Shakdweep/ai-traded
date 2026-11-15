"""The "Rs 5,000 intraday test": a PAPER account that trades the app's own intraday rules
by itself for a fixed number of trading days, logging every decision. No real orders.

Rules (the same opening-range breakout plan the Intraday tab shows):
  * Universe NIFTY 50. Every 5 minutes (after each 5-min candle closes) the intraday scan
    runs; a stock is bought only when its plan is ACTIONABLE: a completed 5-min close above
    the opening-range high, bullish rating, reward:risk >= 1.5, fresh data.
  * Entry at the latest price + 0.05% slippage, and only if the price hasn't run more than
    0.15R past the plan's entry. Stop = opening-range midpoint; target = 2R.
  * One position at a time, at most 3 trades a day, the same stock not twice in a day,
    no new trades after a 2% loss on the day, entries 09:30-14:45, everything closed 15:15.
  * 5x intraday leverage (MIS-style): it may buy up to 5x its free cash, but the size is
    still capped so the loss at the stop, incl. slippage and charges, is at most 1% of the
    account. Margin used = position value / 5. Brokers' actual leverage varies by stock.
  * Exits on completed 5-min candles: stop first if a candle touches both (conservative),
    a gap below the stop fills at the candle's open.
  * Indian intraday charges on every trade (brokerage min(Rs 20, 0.03%), STT, exchange,
    SEBI, stamp, GST).
Prices are Yahoo's delayed 5-minute data, so real fills would differ.

Run by GitHub Actions on trading days (.github/workflows/intraday-bot.yml):
  python -m backend.bot.intraday run        # trade today's session, 09:25-15:20 IST
  python -m backend.bot.intraday status     # print the account
"""

import datetime
import json
import math
import sys
import time
import traceback

from .. import db, services
from ..analysis import risk as rk
from ..market import calendar as cal
from ..market import yahoo

ACCOUNT = "intraday-5k"
CONFIG = {
    "name": "₹5,000 intraday test", "capital": 5000.0, "start_date": "2026-11-16", "trading_days": 5,
    "universe": "nifty50", "risk_per_trade_pct": 1.0, "max_open": 1, "max_trades_per_day": 3,
    "daily_loss_limit_pct": 2.0, "slippage_pct": 0.05, "chase_limit_r": 0.15, "leverage": 5,
    "no_entry_before": "09:30", "no_entry_after": "14:45", "flat_by": "15:15",
}
CYCLE_SECONDS = 300
BAR_DELAY = 75           # wait this long after a 5-min candle closes before reading it (Yahoo delay)


# ---------------------------------------------------------------- storage

def ensure_account():
    db.x("INSERT INTO bot_accounts(id, name, start_capital, start_date, trading_days, config, created_at, status) "
         "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
         (ACCOUNT, CONFIG["name"], CONFIG["capital"], CONFIG["start_date"], CONFIG["trading_days"],
          json.dumps(CONFIG), time.time(), "scheduled"))
    # rules can still change until the test starts; after that they're frozen with its history
    db.x("UPDATE bot_accounts SET config=?, name=? WHERE id=? AND status='scheduled'",
         (json.dumps(CONFIG), CONFIG["name"], ACCOUNT))
    return db.q("SELECT * FROM bot_accounts WHERE id=?", (ACCOUNT,), one=True)


def log(kind, message, data=None, symbol=None):
    line = "[%s] %s %s" % (cal.now_ist().strftime("%H:%M:%S"), kind, message)
    try:  # a console that can't show "Rs" symbols must never stop the trading loop
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.replace("₹", "Rs ").encode("ascii", "replace").decode(), flush=True)
    db.x("INSERT INTO bot_events(account, ts, day, kind, symbol, message, data) VALUES(?,?,?,?,?,?,?)",
         (ACCOUNT, time.time(), cal.now_ist().date().isoformat(), kind, symbol, message,
          json.dumps(data, default=str) if data else None))


def trades(day=None, status=None):
    sql, args = "SELECT * FROM bot_trades WHERE account=?", [ACCOUNT]
    if day:
        sql += " AND day=?"
        args.append(str(day))
    if status:
        sql += " AND status=?"
        args.append(status)
    return db.q(sql + " ORDER BY entry_time", tuple(args))


def balances():
    """equity = capital + realised net P&L; margin = open position value / leverage;
    cash = free margin (equity - margin); buying power = cash x leverage."""
    acct = ensure_account()
    lev = float(json.loads(acct["config"] or "{}").get("leverage") or 1)
    closed = trades(status="closed")
    open_ = trades(status="open")
    realised = sum(t["net_pnl"] or 0 for t in closed)
    tied = sum(t["invested"] for t in open_)
    equity = acct["start_capital"] + realised
    cash = equity - tied / lev
    return {"capital": acct["start_capital"], "realised": round(realised, 2), "cash": round(cash, 2),
            "tied": round(tied, 2), "margin": round(tied / lev, 2), "leverage": lev,
            "buying_power": round(cash * lev, 2), "equity": round(equity, 2)}


def trading_days_of_test():
    d, out = datetime.date.fromisoformat(CONFIG["start_date"]), []
    while len(out) < CONFIG["trading_days"]:
        if cal.is_trading_day(d):
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- trading

def bot_settings(cash, equity):
    """Sizing limits: buying power = free cash x leverage; max loss at the stop = 1% of equity."""
    lev = float(CONFIG.get("leverage") or 1)
    s = dict(db.DEFAULT_SETTINGS)
    s.update(capital=equity, cash=cash * lev, risk_per_trade_pct=CONFIG["risk_per_trade_pct"],
             max_position_pct=100.0 * lev,
             slippage_pct=CONFIG["slippage_pct"], intraday_flat_by=CONFIG["flat_by"],
             intraday_no_entry_before=CONFIG["no_entry_before"], intraday_no_entry_after=CONFIG["no_entry_after"])
    return s


def _bars5(symbol):
    return yahoo.chart(yahoo.to_yahoo(symbol), "5d", "5m", ttl=30)["bars"]


def _completed(bars, now_ts):
    return [b for b in bars if b["t"] + 300 <= now_ts]


def close_trade(t, price, when_ts, reason):
    price = round(price, 2)  # the stored price is the one every rupee figure is calculated from
    qty = t["qty"]
    costs = rk.round_trip_costs(qty, t["entry_price"], price, "intraday")
    gross = qty * (price - t["entry_price"])
    net = gross - costs["total"]
    received = qty * price - costs["total"]
    db.x("UPDATE bot_trades SET status='closed', exit_time=?, exit_price=?, exit_reason=?, received=?, charges=?, "
         "charges_detail=?, gross_pnl=?, net_pnl=? WHERE id=? AND status='open'",
         (when_ts, round(price, 2), reason, round(received, 2), costs["total"], json.dumps(costs), round(gross, 2),
          round(net, 2), t["id"]))
    log("sell", "SOLD %d %s @ ₹%.2f (%s): got back ₹%.2f after ₹%.2f charges, net P&L ₹%+.2f" % (
        qty, t["symbol"], price, reason, received, costs["total"], net), {"costs": costs}, t["symbol"])


def manage_exits(now_ts, flat_now):
    for t in trades(status="open"):
        try:
            bars = _completed(_bars5(t["symbol"]), now_ts)
        except Exception as e:  # noqa: BLE001
            log("error", "No price for open %s: %s" % (t["symbol"], e), symbol=t["symbol"])
            continue
        after = [b for b in bars if b["t"] >= t["entry_bar_t"] + 300 and b["t"] > (t["last_checked_t"] or 0)]
        done = False
        for b in after:
            if b["l"] <= t["stop"]:
                fill = min(t["stop"], b["o"])  # a gap below the stop fills at the open
                close_trade(t, fill * (1 - CONFIG["slippage_pct"] / 100), b["t"] + 300, "stop-loss")
                done = True
                break
            if b["h"] >= t["target"]:
                close_trade(t, t["target"] * (1 - CONFIG["slippage_pct"] / 100), b["t"] + 300, "target")
                done = True
                break
        if not done and after:
            db.x("UPDATE bot_trades SET last_checked_t=? WHERE id=?", (after[-1]["t"], t["id"]))
        if not done and flat_now and bars:
            close_trade(t, bars[-1]["c"] * (1 - CONFIG["slippage_pct"] / 100), now_ts, "square-off %s" % CONFIG["flat_by"])


def try_entry(now):
    day = now.date().isoformat()
    today = trades(day=day)
    if len([t for t in today if t["status"] == "open"]) >= CONFIG["max_open"]:
        return
    if len(today) >= CONFIG["max_trades_per_day"]:
        return
    b = balances()
    day_row = db.q("SELECT * FROM bot_days WHERE account=? AND day=?", (ACCOUNT, day), one=True)
    day_start = day_row["start_equity"] if day_row else b["equity"]
    day_pnl = sum(t["net_pnl"] or 0 for t in today if t["status"] == "closed")
    if day_pnl <= -day_start * CONFIG["daily_loss_limit_pct"] / 100:
        return
    res = services.run_scan(CONFIG["universe"], "intraday")
    rows = res.get("rows", [])
    traded = {t["symbol"] for t in today}
    cands = [r for r in rows if (r.get("plan") or {}).get("actionable") and r["symbol"] not in traded]
    cands.sort(key=lambda r: (r.get("score") or 0, r.get("confidence") or 0), reverse=True)
    if not cands:
        return
    s = bot_settings(b["cash"], b["equity"])
    for r in cands:
        p = r["plan"]
        price = r["price"]
        risk = p["entry"] - p["stop"]
        if price > p["entry"] + CONFIG["chase_limit_r"] * risk:
            log("skip", "%s: price ₹%.2f has run past the entry ₹%.2f (don't chase)" % (r["symbol"], price, p["entry"]),
                symbol=r["symbol"])
            continue
        if price <= p["stop"]:
            continue
        fill = round(price * (1 + CONFIG["slippage_pct"] / 100), 2)
        size = rk.size_position(fill, p["stop"], s, target=p["target1"], product="intraday")
        qty = size.get("qty") or 0
        if qty < 1:
            log("skip", "%s at ₹%.2f: %s" % (r["symbol"], fill, size.get("refused") or "0 shares fit"), symbol=r["symbol"])
            continue
        bars = _completed(_bars5(r["symbol"]), time.time())
        invested = qty * fill
        db.x("INSERT INTO bot_trades(account, day, symbol, qty, entry_time, entry_bar_t, entry_price, invested, stop, "
             "target, status, signal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (ACCOUNT, day, r["symbol"], qty, time.time(), bars[-1]["t"] if bars else int(time.time()), round(fill, 2),
              round(invested, 2), p["stop"], p["target1"], "open",
              json.dumps({"setup": p.get("setup"), "rating": r["rating"], "score": r.get("score"),
                          "confidence": r.get("confidence"), "or_high": r.get("or_high"), "entry_plan": p["entry"],
                          "reasons": p.get("reasoning"), "decided_by": size.get("decided_by"),
                          "max_loss": size.get("max_loss")})))
        lev = float(CONFIG.get("leverage") or 1)
        log("buy", "BOUGHT %d %s @ ₹%.2f = ₹%.2f position (margin ₹%.2f at %gx; stop ₹%.2f, target ₹%.2f; max loss "
            "≈ ₹%.2f). Free cash left ₹%.2f" % (qty, r["symbol"], fill, invested, invested / lev, lev, p["stop"],
                                                p["target1"], size.get("max_loss") or 0, b["cash"] - invested / lev),
            {"plan": {k: p.get(k) for k in ("entry", "stop", "target1", "reward_risk", "setup")}}, r["symbol"])
        return


def _hhmm(text):
    return cal.hhmm(text)


def start_day(today):
    b = balances()
    db.x("INSERT INTO bot_days(account, day, start_equity, start_cash) VALUES(?,?,?,?) ON CONFLICT(account, day) "
         "DO NOTHING", (ACCOUNT, today.isoformat(), b["equity"], b["cash"]))


def end_day(today):
    ts = trades(day=today.isoformat())
    closed = [t for t in ts if t["status"] == "closed"]
    b = balances()
    row = db.q("SELECT * FROM bot_days WHERE account=? AND day=?", (ACCOUNT, today.isoformat()), one=True) or {}
    net = sum(t["net_pnl"] or 0 for t in closed)
    db.x("UPDATE bot_days SET end_equity=?, trades=?, wins=?, invested=?, received=?, charges=?, net_pnl=?, "
         "closed_at=? WHERE account=? AND day=?",
         (b["equity"], len(ts), sum(1 for t in closed if (t["net_pnl"] or 0) > 0),
          round(sum(t["invested"] for t in ts), 2), round(sum(t["received"] or 0 for t in closed), 2),
          round(sum(t["charges"] or 0 for t in closed), 2), round(net, 2), time.time(), ACCOUNT, today.isoformat()))
    log("day", "Day closed: %d trade(s), net ₹%+.2f, balance ₹%.2f (started the day at ₹%.2f)" % (
        len(ts), net, b["equity"], row.get("start_equity") or b["equity"]))


def close_stale(today):
    """Positions left open from an earlier day (the runner stopped) are closed at that day's last price."""
    for t in trades(status="open"):
        if t["day"] < today.isoformat():
            try:
                bars = [b for b in _bars5(t["symbol"]) if cal.to_ist(b["t"]).date().isoformat() == t["day"]]
            except Exception:  # noqa: BLE001
                bars = []
            price = bars[-1]["c"] if bars else t["entry_price"]
            close_trade(t, price, bars[-1]["t"] + 300 if bars else time.time(),
                        "squared off at that day's close (the runner had stopped)")


def run(now_fn=None):
    now_fn = now_fn or cal.now_ist
    db.db()
    acct = ensure_account()
    today = now_fn().date()
    days = trading_days_of_test()
    if today not in days:
        msg = "Not a test day (test runs %s to %s)." % (days[0], days[-1]) if cal.is_trading_day(today) \
            else "Market holiday or weekend: no trading."
        log("info", msg)
        if today > days[-1] and acct["status"] != "finished":
            db.x("UPDATE bot_accounts SET status='finished' WHERE id=?", (ACCOUNT,))
        return msg
    db.x("UPDATE bot_accounts SET status='running' WHERE id=?", (ACCOUNT,))
    close_stale(today)
    start_day(today)
    log("start", "Day %d of %d. Balance ₹%.2f. Rules: %s" % (days.index(today) + 1, len(days), balances()["equity"],
        "ORB on NIFTY 50, 1 position, max 3 trades, 1%% max loss per trade, stop at 2%% day loss, entries "
        "09:30-14:45, flat 15:15, %gx leverage" % CONFIG["leverage"]))
    entry_from, entry_to, flat = _hhmm(CONFIG["no_entry_before"]), _hhmm(CONFIG["no_entry_after"]), _hhmm(CONFIG["flat_by"])
    last_beat = 0
    while True:
        now = now_fn()
        t = now.time()
        flat_now = t >= flat
        try:
            manage_exits(time.time(), flat_now)
            if not flat_now and entry_from <= t <= entry_to:
                try_entry(now)
        except Exception as e:  # noqa: BLE001 - keep trading the session
            traceback.print_exc()
            log("error", "%s: %s" % (type(e).__name__, str(e)[:200]))
        if flat_now:
            break
        if time.time() - last_beat > 1800:
            b = balances()
            log("heartbeat", "Watching. Open positions: %d. Cash ₹%.2f, balance ₹%.2f" % (
                len(trades(status="open")), b["cash"], b["equity"]))
            last_beat = time.time()
        # wake up just after the next 5-minute candle closes (plus Yahoo's delay)
        nxt = (math.floor(time.time() / CYCLE_SECONDS) + 1) * CYCLE_SECONDS + BAR_DELAY
        time.sleep(max(5, nxt - time.time()))
    end_day(today)
    if today == days[-1]:
        db.x("UPDATE bot_accounts SET status='finished' WHERE id=?", (ACCOUNT,))
    return "day done"


# ---------------------------------------------------------------- admin view

def report():
    acct = ensure_account()
    b = balances()
    ts = trades()
    closed = [t for t in ts if t["status"] == "closed"]
    for t in ts:
        t["signal"] = json.loads(t["signal"] or "{}")
        t["charges_detail"] = json.loads(t["charges_detail"]) if t.get("charges_detail") else None
    days = db.q("SELECT * FROM bot_days WHERE account=? ORDER BY day", (ACCOUNT,))
    events = db.q("SELECT * FROM bot_events WHERE account=? ORDER BY ts DESC LIMIT 300", (ACCOUNT,))
    return {"account": dict(acct, config=json.loads(acct["config"])), "balances": b,
            "return_pct": round((b["equity"] / acct["start_capital"] - 1) * 100, 2),
            "test_days": [d.isoformat() for d in trading_days_of_test()],
            "totals": {"trades": len(ts), "closed": len(closed), "wins": sum(1 for t in closed if t["net_pnl"] > 0),
                       "charges": round(sum(t["charges"] or 0 for t in closed), 2),
                       "gross": round(sum(t["gross_pnl"] or 0 for t in closed), 2),
                       "net": round(sum(t["net_pnl"] or 0 for t in closed), 2)},
            "days": days, "trades": ts[::-1], "events": events,
            "data_note": "Paper trades on Yahoo's delayed 5-minute prices; real fills would differ. No real orders."}


def main(argv):
    db.db()
    if argv and argv[0] == "run":
        print(run())
        return 0
    if argv and argv[0] == "status":
        print(json.dumps(report(), indent=1, default=str)[:4000])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
