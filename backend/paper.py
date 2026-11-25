"""Paper trading. No broker is connected and nothing here can place a real order.

Flow (same three-gate idea as the trader-broker skill, adapted for paper):
  1. propose: a fresh analysis builds the plan, sizes it and runs the risk gate.
  2. confirm: the user types an explicit "yes" for THAT order; the reply is checked
     with the trader-broker skill's own `parse_yes()` (vendored). "ok", "sure",
     "yes to all", or a yes naming another stock/amount are refused.
  3. the risk gate runs again at confirm time; then the paper fill happens at the
     latest (delayed) quote plus slippage, only if it is still inside the entry range.
Exits: stop, Target 1 (half off, stop to break-even), Target 2, the intraday
square-off time, or a manual close. Checked against the latest quote each refresh.

Every query is scoped to db.owner(): each person has their own paper account.
"""

import json
import time

from . import db
from .analysis import exitcheck
from .analysis import risk as rk
from .market import calendar as cal
from .market import yahoo
from .vendor.day_trader_skill import ticket as skill_ticket


def _alpaca_like(o):
    """The order in the shape the skill's yes-parser understands."""
    return {"symbol": o["symbol"], "side": o["side"], "qty": str(o["qty"]), "type": "market",
            "time_in_force": "day", "order_class": "bracket", "stop_loss_stop_price": o["stop"],
            "take_profit_limit_price": o["target1"],
            "plan": {"entry": o["entry"], "stop": o["stop"], "max_loss": json.loads(o["plan"] or "{}").get("max_loss")}}


def ticket_text(o):
    plan = json.loads(o["plan"] or "{}")
    return ("PAPER ORDER #%d: BUY %d %s (%s), entry about ₹%.2f (range %s), stop-loss ₹%.2f, target 1 ₹%.2f, "
            "target 2 ₹%s, max loss about ₹%s at the stop incl. charges. Reply \"yes\" to place this paper order."
            % (o["id"], o["qty"], o["symbol"], o["mode"], o["entry"],
               "%.2f-%.2f" % (plan.get("entry_low") or o["entry"], plan.get("entry_high") or o["entry"]),
               o["stop"], o["target1"] or 0, "%.2f" % o["target2"] if o["target2"] else "-",
               "{:,.0f}".format(plan.get("max_loss") or 0)))


def _today_state():
    start, _ = rk.today_bounds()
    me = db.owner()
    opened = db.q("SELECT * FROM paper_positions WHERE owner=? AND opened_at >= ? AND order_id IS NOT NULL", (me, start))
    openpos = db.q("SELECT * FROM paper_positions WHERE owner=? AND status='open'", (me,))
    closed = db.q("SELECT pnl FROM paper_positions WHERE owner=? AND status='closed' AND closed_at >= ?", (me, start))
    return opened, openpos, sum(r["pnl"] or 0 for r in closed)


def propose(plan, settings):
    if not plan.get("entry") or not plan.get("stop"):
        return {"ok": False, "error": plan.get("message") or "No trade plan with an entry and a stop."}
    pos = plan.get("position") or {}
    if not pos.get("qty"):
        return {"ok": False, "error": pos.get("refused") or "Position size is 0 under your limits."}
    order = {"symbol": plan["symbol"], "side": "buy", "qty": pos["qty"], "entry": plan["entry"], "stop": plan["stop"],
             "target1": plan.get("target1"), "mode": plan["mode"]}
    opened, openpos, pnl = _today_state()
    verdict = rk.check_order(order, settings, opened, openpos, pnl)
    pj = {k: plan.get(k) for k in ("entry_low", "entry_high", "setup", "reward_risk", "status", "actionable",
                                   "blockers")}
    pj["max_loss"] = pos.get("max_loss")
    oid = db.x("INSERT INTO paper_orders(ts, symbol, side, qty, entry, stop, target1, target2, mode, status, "
               "risk_verdict, plan, owner) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (time.time(), order["symbol"], "buy", order["qty"], order["entry"], order["stop"], order["target1"],
                plan.get("target2"), plan["mode"], "proposed" if verdict["verdict"] == "PASS" else "blocked",
                json.dumps(verdict), json.dumps(pj), db.owner()))
    o = db.q("SELECT * FROM paper_orders WHERE id=? AND owner=?", (oid, db.owner()), one=True)
    out = {"ok": verdict["verdict"] == "PASS", "order": o, "risk": verdict, "ticket": ticket_text(o)}
    if not plan.get("actionable"):
        out["warning"] = "This plan is not marked actionable: %s" % "; ".join(plan.get("blockers") or [])
    return out


def confirm(order_id, reply, settings):
    o = db.q("SELECT * FROM paper_orders WHERE id=? AND owner=?", (order_id, db.owner()), one=True)
    if not o:
        return {"ok": False, "error": "No such order."}
    if o["status"] != "proposed":
        return {"ok": False, "error": "Order is %s, not awaiting your yes." % o["status"]}
    others = [r["symbol"] for r in db.q("SELECT symbol FROM paper_orders WHERE owner=? AND status='proposed'",
                                        (db.owner(),))]
    ok, why = skill_ticket.parse_yes(reply, _alpaca_like(o), others)
    why = why.replace("$", "₹")
    if not ok:
        return {"ok": False, "error": "Not recorded: %s. Type yes for this one order." % why}
    if not cal.market_status()["open"]:
        if o["mode"] == "intraday":
            return {"ok": False, "error": "Market is closed: an intraday order cannot be approved now. Re-run the "
                                          "analysis during market hours."}
        db.x("UPDATE paper_orders SET status='queued', approval=? WHERE id=?", (reply, order_id))
        return {"ok": True, "queued": True,
                "note": "Your yes is recorded. Market is closed, so this paper order is queued: at the next open it "
                        "fills only if the price is inside the entry range and the risk check passes again; "
                        "otherwise it expires at that day's close."}
    return _fill(o, reply, settings)


def _fill(o, reply, settings):
    order_id = o["id"]
    opened, openpos, pnl = _today_state()
    verdict = rk.check_order(dict(o), settings, opened, openpos, pnl)
    if verdict["verdict"] != "PASS":
        db.x("UPDATE paper_orders SET status='blocked', risk_verdict=?, approval=? WHERE id=?",
             (json.dumps(verdict), reply, order_id))
        return {"ok": False, "error": "Blocked by the risk check at fill time: " + "; ".join(verdict["failed"]),
                "risk": verdict}
    try:
        q = yahoo.quote_from_chart(yahoo.chart(yahoo.to_yahoo(o["symbol"]), "5d", "5m", ttl=30))
    except yahoo.DataError as e:
        return {"ok": False, "error": "No price available to fill against: %s" % e}
    if q["freshness"]["label"] in ("STALE", "UNAVAILABLE"):
        return {"ok": False, "error": "Price is %s; refusing to fill a paper order on it." % q["freshness"]["label"]}
    plan = json.loads(o["plan"] or "{}")
    px = q["price"] * (1 + float(settings.get("slippage_pct", 0.05)) / 100)
    hi = plan.get("entry_high") or o["entry"]
    lo = plan.get("entry_low") or o["entry"]
    if px > hi * 1.002:
        if o["status"] == "queued":
            return {"ok": False, "waiting": True, "error": "Price %.2f above the entry range; still waiting." % px}
        db.x("UPDATE paper_orders SET status='expired', approval=?, reason=? WHERE id=?",
             (reply, "price %.2f above the entry range" % px, order_id))
        return {"ok": False, "error": "Price %.2f has moved above the entry range (max %.2f). Not filled: do not "
                                      "chase. Re-run the analysis." % (px, hi)}
    if o["status"] == "queued" and px < lo * 0.998:
        return {"ok": False, "waiting": True, "error": "Price %.2f below the entry range; trigger not reached." % px}
    if px <= o["stop"]:
        db.x("UPDATE paper_orders SET status='expired', approval=?, reason=? WHERE id=?",
             (reply, "price at or below the stop", order_id))
        return {"ok": False, "error": "Price %.2f is at or below the stop %.2f. Setup invalid." % (px, o["stop"])}
    db.x("UPDATE paper_orders SET status='filled', approval=? WHERE id=?", (reply, order_id))
    pid = db.x("INSERT INTO paper_positions(order_id, symbol, qty, entry_price, stop, target1, target2, mode, "
               "opened_at, owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (order_id, o["symbol"], o["qty"], round(px, 2), o["stop"], o["target1"], o["target2"], o["mode"],
                time.time(), db.owner()))
    s = dict(settings)
    db.save_settings({"cash": round(float(s["cash"]) - o["qty"] * px, 2)}, who=o["owner"])
    db.x("INSERT INTO journal(ts, symbol, text, owner) VALUES(?,?,?,?)",
         (time.time(), o["symbol"], "Paper BUY %d @ %.2f (stop %.2f, T1 %s). Approval: %r" % (
             o["qty"], px, o["stop"], o["target1"], reply), db.owner()))
    return {"ok": True, "position_id": pid, "fill_price": round(px, 2),
            "note": "Paper fill at the latest %s quote + slippage. No real order was sent." % q["freshness"]["label"]}


def decline(order_id):
    db.x("UPDATE paper_orders SET status='declined' WHERE id=? AND owner=? AND status IN ('proposed', 'queued')",
         (order_id, db.owner()))
    return {"ok": True}


def _close(p, qty, price, reason, settings):
    product = "intraday" if p["mode"] == "intraday" else "delivery"
    costs = rk.round_trip_costs(qty, p["entry_price"], price, product, float(settings.get("brokerage_per_order", 20)))
    pnl = qty * (price - p["entry_price"]) - costs["total"]
    now = time.time()
    if qty >= p["qty"]:
        db.x("UPDATE paper_positions SET status='closed', closed_at=?, exit_price=?, exit_reason=?, costs=?, pnl=? "
             "WHERE id=?", (now, price, reason, costs["total"], round(pnl, 2), p["id"]))
    else:
        db.x("INSERT INTO paper_positions(order_id, symbol, qty, entry_price, stop, target1, target2, mode, opened_at,"
             " closed_at, exit_price, exit_reason, costs, pnl, status, owner) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,"
             "'closed',?)", (p["symbol"], qty, p["entry_price"], p["stop"], p["target1"], p["target2"], p["mode"],
                             p["opened_at"], now, price, reason, costs["total"], round(pnl, 2), p["owner"]))
        db.x("UPDATE paper_positions SET qty=?, stop=?, target1=NULL WHERE id=?",
             (p["qty"] - qty, p["entry_price"], p["id"]))  # rest rides with the stop at break-even
    cash = float(db.get_settings(p["owner"])["cash"]) + qty * price - costs["total"]
    db.save_settings({"cash": round(cash, 2)}, who=p["owner"])
    db.x("INSERT INTO journal(ts, symbol, text, owner) VALUES(?,?,?,?)",
         (now, p["symbol"], "Paper SELL %d @ %.2f (%s), P&L ₹%.2f after ₹%.2f charges" % (
             qty, price, reason, pnl, costs["total"]), p["owner"]))
    return pnl


def process_queue(settings, notify=None):
    """Fill queued (approved after hours) paper orders at the open, or expire them."""
    st = cal.market_status()
    now = cal.now_ist()
    for o in db.q("SELECT * FROM paper_orders WHERE owner=? AND status='queued'", (db.owner(),)):
        queued_day = cal.to_ist(o["ts"]).date()
        if st["open"]:
            r = _fill(o, o["approval"], settings)
            if r.get("ok") and notify:
                notify(o["symbol"], "paper_fill", "Queued paper order #%d filled: %d %s at %.2f" % (
                    o["id"], o["qty"], o["symbol"], r["fill_price"]))
        elif now.date() > queued_day and now.time() >= cal.CLOSE and cal.is_trading_day(now.date()):
            db.x("UPDATE paper_orders SET status='expired', reason=? WHERE id=?",
                 ("not filled during the next session", o["id"]))


# ---- database-free watch while the site sleeps (nobody logged in): an in-memory copy of every
# open paper position, checked against live prices without touching the database. Only when a
# stop, target, square-off or queued fill is actually due does the background loop wake it.
WATCH = {"positions": [], "queued": False, "loaded": False}


def snapshot(flat_by_of=None):
    """Refresh the in-memory copy (call while the database is awake anyway)."""
    rows = db.q("SELECT owner, symbol, qty, stop, target1, target2, mode, opened_at FROM paper_positions "
                "WHERE status='open'")
    for r in rows:
        r["flat_by"] = (flat_by_of or {}).get(r["owner"]) or db.DEFAULT_SETTINGS.get("intraday_flat_by", "15:15")
    q = db.q("SELECT COUNT(*) AS n FROM paper_orders WHERE status='queued'", one=True)
    WATCH.update(positions=rows, queued=bool(q and q["n"]), loaded=True)


def due_without_db(price_of=None, now=None):
    """True if any open paper position would act on the latest price (same rules as
    mark_to_market), or a queued order can fill. Uses prices only, never the database."""
    st = cal.market_status(now)
    if not st["open"]:
        return False
    if WATCH["queued"]:
        return True
    now = now or cal.now_ist()
    for p in WATCH["positions"]:
        if p["mode"] == "intraday" and now.time() >= cal.hhmm(p["flat_by"]) and                 cal.to_ist(p["opened_at"]).date() == now.date():
            return True
        try:
            px = price_of(p["symbol"]) if price_of else                 yahoo.quote_from_chart(yahoo.chart(yahoo.to_yahoo(p["symbol"]), "5d", "5m"))["price"]
        except Exception:  # noqa: BLE001 - no price, nothing to decide
            continue
        if px is None:
            continue
        if (p["stop"] and px <= p["stop"]) or (p["target1"] and px >= p["target1"]) or                 (p["target2"] and px >= p["target2"]):
            return True
    return False


def active_owners():
    """Everyone with an open paper position or a queued paper order (the background job checks each)."""
    rows = db.q("SELECT DISTINCT owner FROM paper_positions WHERE status='open' "
                "UNION SELECT DISTINCT owner FROM paper_orders WHERE status='queued'")
    return [r["owner"] for r in rows]


def mark_to_market(settings, notify=None):
    """Check every open paper position against the latest quote."""
    process_queue(settings, notify)
    events = []
    now = cal.now_ist()
    for p in db.q("SELECT * FROM paper_positions WHERE owner=? AND status='open'", (db.owner(),)):
        try:
            q = yahoo.quote_from_chart(yahoo.chart(yahoo.to_yahoo(p["symbol"]), "5d", "5m"))
        except yahoo.DataError:
            continue
        px = q["price"]
        if px is None or q["freshness"]["label"] == "UNAVAILABLE":
            continue
        if px <= p["stop"]:
            pnl = _close(p, p["qty"], round(min(px, p["stop"]), 2), "stop-loss", settings)
            events.append((p["symbol"], "stop", "Paper stop hit on %s at %.2f, P&L ₹%.0f" % (p["symbol"], px, pnl)))
        elif p["target1"] and px >= p["target1"] and p["qty"] > 1:
            half = p["qty"] // 2
            pnl = _close(p, half, round(px, 2), "target 1", settings)
            events.append((p["symbol"], "target", "Target 1 reached on %s at %.2f: half closed (₹%.0f), stop moved to "
                                                  "break-even" % (p["symbol"], px, pnl)))
        elif (p["target2"] and px >= p["target2"]) or (p["target1"] and px >= p["target1"] and p["qty"] <= 1):
            pnl = _close(p, p["qty"], round(px, 2), "target", settings)
            events.append((p["symbol"], "target", "Target reached on %s at %.2f, P&L ₹%.0f" % (p["symbol"], px, pnl)))
        elif p["mode"] == "intraday" and now.time() >= cal.hhmm(settings["intraday_flat_by"]) and \
                cal.to_ist(p["opened_at"]).date() == now.date():
            pnl = _close(p, p["qty"], round(px, 2), "intraday square-off", settings)
            events.append((p["symbol"], "squareoff", "Squared off %s at %.2f, P&L ₹%.0f" % (p["symbol"], px, pnl)))
        elif p["stop"] and (px - p["stop"]) / px < 0.01:
            events.append((p["symbol"], "stop_near", "%s at %.2f is within 1%% of its stop %.2f" % (
                p["symbol"], px, p["stop"])))
    if notify:
        for sym, kind, msg in events:
            notify(sym, kind, msg)
    return events


def close_manual(pid, settings):
    p = db.q("SELECT * FROM paper_positions WHERE id=? AND owner=? AND status='open'", (pid, db.owner()), one=True)
    if not p:
        return {"ok": False, "error": "No open position with that id."}
    try:
        q = yahoo.quote_from_chart(yahoo.chart(yahoo.to_yahoo(p["symbol"]), "5d", "5m", ttl=30))
    except yahoo.DataError as e:
        return {"ok": False, "error": "No price to close against: %s" % e}
    pnl = _close(p, p["qty"], round(q["price"], 2), "manual close", settings)  # exits are never blocked
    return {"ok": True, "pnl": round(pnl, 2), "price": q["price"]}


def exit_check(p, ch5, settings):
    """HOLD / TIGHTEN STOP / BOOK PARTIAL / EXIT for one open position (never fails the page)."""
    try:
        daily = None if p["mode"] == "intraday" else yahoo.chart(yahoo.to_yahoo(p["symbol"]), "1y", "1d")
        o = db.q("SELECT stop FROM paper_orders WHERE id=?", (p["order_id"],), one=True) if p.get("order_id") else None
        return exitcheck.evaluate(p, p.get("price"), daily_chart=daily, bars5=(ch5 or {}).get("bars"),
                                  settings=settings, orig_stop=o["stop"] if o else None)
    except Exception as e:  # noqa: BLE001 - a data hiccup must not break the paper page
        return {"verdict": None, "reasons": [("muted", "Exit check unavailable right now (%s)." % type(e).__name__)]}


def raise_stop(pid, new_stop):
    """Move a paper stop UP (never down, never above the latest price)."""
    p = db.q("SELECT * FROM paper_positions WHERE id=? AND owner=? AND status='open'", (pid, db.owner()), one=True)
    if not p:
        return {"ok": False, "error": "No open position with that id."}
    try:
        new_stop = round(float(new_stop), 2)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Enter a stop price."}
    if new_stop <= p["stop"]:
        return {"ok": False, "error": "A stop can only be raised (it's ₹%.2f now)." % p["stop"]}
    try:
        px = yahoo.quote_from_chart(yahoo.chart(yahoo.to_yahoo(p["symbol"]), "5d", "5m", ttl=30))["price"]
    except yahoo.DataError as e:
        return {"ok": False, "error": "No price to check against: %s" % e}
    if px is None or new_stop >= px:
        return {"ok": False, "error": "The stop must be below the current price (₹%s)." % ("%.2f" % px if px else "?")}
    db.x("UPDATE paper_positions SET stop=? WHERE id=?", (new_stop, pid))
    db.x("INSERT INTO journal(ts, symbol, text, owner) VALUES(?,?,?,?)",
         (time.time(), p["symbol"], "Raised the paper stop from %.2f to %.2f" % (p["stop"], new_stop), db.owner()))
    return {"ok": True, "stop": new_stop}


def summary(settings):
    me = db.owner()
    openpos = db.q("SELECT * FROM paper_positions WHERE owner=? AND status='open' ORDER BY opened_at DESC", (me,))
    closed = db.q("SELECT * FROM paper_positions WHERE owner=? AND status='closed' ORDER BY closed_at", (me,))
    for p in openpos:
        ch5 = None
        try:
            ch5 = yahoo.chart(yahoo.to_yahoo(p["symbol"]), "5d", "5m")
            q = yahoo.quote_from_chart(ch5)
            p["price"] = q["price"]
            p["freshness"] = q["freshness"]
            p["unrealised"] = round(p["qty"] * (q["price"] - p["entry_price"]), 2)
        except yahoo.DataError:
            p["price"], p["unrealised"] = None, None
        p["exit_check"] = exit_check(p, ch5, settings)
    eq, cum = [], 0.0
    for c in closed:
        cum += c["pnl"] or 0
        eq.append({"time": int(c["closed_at"]), "value": round(cum, 2)})
    wins = [c for c in closed if (c["pnl"] or 0) > 0]
    return {
        "paper_only": True, "cash": settings["cash"], "capital": settings["capital"],
        "open": openpos, "closed": closed[::-1][:100],
        "orders": db.q("SELECT * FROM paper_orders WHERE owner=? ORDER BY ts DESC LIMIT 50", (me,)),
        "stats": {"trades": len(closed), "wins": len(wins),
                  "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                  "realised_pnl": round(cum, 2),
                  "unrealised_pnl": round(sum(p.get("unrealised") or 0 for p in openpos), 2),
                  "charges": round(sum(c["costs"] or 0 for c in closed), 2)},
        "equity_curve": eq,
        "journal": db.q("SELECT * FROM journal WHERE owner=? ORDER BY ts DESC LIMIT 50", (me,)),
    }
