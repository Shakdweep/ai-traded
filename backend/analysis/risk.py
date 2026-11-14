"""Position sizing and the risk gate, in INR, for NSE cash equities.

Indian-market adaptation of two skills:
  * trader-budget `size_trade`: whole shares that fit inside every limit (max loss
    per trade, position cap, cash), and it says which limit decided. A stop too
    far away for the max loss is a plain refusal, never a smaller-but-wrong order.
  * trader-risk `check`: stop required, loss at the stop within the per-trade
    limit, reward:risk minimum, daily loss limit, trades per day, entry window,
    no shorting / margin / options unless enabled. Getting OUT is never blocked.

Costs are APPROXIMATE Indian charges (verify with your broker's calculator):
  brokerage  min(flat fee, 0.03% of turnover) per executed order
  STT        intraday 0.025% on the sell side; delivery 0.1% on buy and sell
  exchange   0.00297% of turnover (NSE)          SEBI fee 0.0001%
  stamp duty intraday 0.003% on buy; delivery 0.015% on buy
  GST        18% on brokerage + exchange + SEBI fees
  DP charge  about ₹15.93 per scrip on delivery sells
"""

import datetime
import math

from ..market import calendar as cal

RATES = {
    "intraday": {"stt_buy": 0.0, "stt_sell": 0.00025, "stamp_buy": 0.00003, "dp_sell": 0.0},
    "delivery": {"stt_buy": 0.001, "stt_sell": 0.001, "stamp_buy": 0.00015, "dp_sell": 15.93},
}
EXCH = 0.0000297
SEBI = 0.000001
GST = 0.18


def tick_down(x, tick=0.05):
    return math.floor(round(x / tick, 6)) * tick


def tick_up(x, tick=0.05):
    return math.ceil(round(x / tick, 6)) * tick


def round_trip_costs(qty, buy, sell, product="delivery", flat=20.0):
    r = RATES["intraday" if product == "intraday" else "delivery"]
    tb, ts = qty * buy, qty * sell
    brok = min(flat, 0.0003 * tb) + min(flat, 0.0003 * ts)
    if product != "intraday":
        brok = min(brok, 2 * flat)
    exch = EXCH * (tb + ts)
    sebi = SEBI * (tb + ts)
    stt = r["stt_buy"] * tb + r["stt_sell"] * ts
    stamp = r["stamp_buy"] * tb
    gst = GST * (brok + exch + sebi)
    total = brok + exch + sebi + stt + stamp + gst + r["dp_sell"]
    return {"total": round(total, 2), "brokerage": round(brok, 2), "stt": round(stt, 2),
            "exchange": round(exch, 2), "stamp": round(stamp, 2), "gst": round(gst, 2),
            "dp": r["dp_sell"], "sebi": round(sebi, 2)}


def size_position(entry, stop, settings, target=None, product="delivery"):
    """Whole shares inside every limit. Long only (shorting is off by default)."""
    cap = float(settings["capital"])
    cash = float(settings["cash"])
    max_loss = cap * float(settings["risk_per_trade_pct"]) / 100.0
    pos_cap = cap * float(settings["max_position_pct"]) / 100.0
    slip = float(settings.get("slippage_pct", 0.05)) / 100.0
    risk_ps = entry - stop
    out = {"entry": entry, "stop": stop, "max_loss_allowed": round(max_loss, 2), "position_cap": round(pos_cap, 2),
           "cash": round(cash, 2)}
    if risk_ps <= 0:
        out.update(qty=0, refused="The stop must be below the entry for a long trade.")
        return out
    # loss per share at the stop including slippage on both fills
    eff_risk = risk_ps + slip * entry + slip * stop
    limits = {
        "max loss per trade": int(max_loss // eff_risk),
        "position cap": int(pos_cap // entry),
        "available cash": int(cash // entry),
    }
    qty = max(0, min(limits.values()))
    decided = min(limits, key=limits.get)
    if qty <= 0:
        why = ("the stop is ₹%.2f away (plus slippage), so even 1 share risks more than your max loss ₹%.0f"
               % (risk_ps, max_loss)) if limits["max loss per trade"] <= 0 else \
            "%s allows 0 shares at ₹%.2f" % (decided, entry)
        out.update(qty=0, refused="No position: " + why, limits=limits)
        return out
    costs = round_trip_costs(qty, entry, stop, product, float(settings.get("brokerage_per_order", 20)))
    loss = qty * eff_risk + costs["total"]
    out.update(
        qty=qty, decided_by=decided, limits=limits, capital_required=round(qty * entry, 2),
        risk_per_share=round(risk_ps, 2), est_costs=costs,
        max_loss=round(loss, 2),
        max_loss_pct_capital=round(loss / cap * 100, 2),
        note="Max loss is at the stop price incl. slippage and charges. A gap can fill worse than the stop.",
    )
    if loss > max_loss * 1.0001 and qty > 1:
        # charges pushed it over: drop shares until it fits
        while qty > 0:
            qty -= 1
            costs = round_trip_costs(qty, entry, stop, product, float(settings.get("brokerage_per_order", 20)))
            loss = qty * eff_risk + costs["total"]
            if loss <= max_loss:
                break
        out.update(qty=qty, capital_required=round(qty * entry, 2), est_costs=costs, max_loss=round(loss, 2),
                   max_loss_pct_capital=round(loss / cap * 100, 2), decided_by="max loss per trade (after charges)")
    if target:
        tc = round_trip_costs(qty, entry, target, product, float(settings.get("brokerage_per_order", 20)))
        reward = qty * (target - entry) - tc["total"]
        out.update(potential_reward=round(reward, 2), net_reward_risk=round(reward / loss, 2) if loss else None)
    return out


def check_order(order, settings, opened_today, open_positions, today_closed_pnl, now=None):
    """The risk gate for a paper entry. -> {"verdict": PASS|BLOCK, "checks": [...]}"""
    now = now or cal.now_ist()
    checks = []

    def add(ok, text):
        checks.append({"ok": bool(ok), "text": text})

    side = order.get("side", "buy")
    entry, stop, qty = float(order["entry"]), float(order.get("stop") or 0), int(order["qty"])
    t1 = order.get("target1")
    cap = float(settings["capital"])
    add(side == "buy" or settings.get("allow_short"), "Long only (short selling is disabled)" if side != "buy"
        else "Side: buy (long)")
    add(stop > 0, "A stop-loss is set" if stop > 0 else "No stop-loss: every trade needs one")
    if stop > 0:
        add(stop < entry, "Stop %.2f is below entry %.2f" % (stop, entry))
    add(qty > 0, "Quantity %d" % qty)
    max_loss = cap * float(settings["risk_per_trade_pct"]) / 100
    slip = float(settings.get("slippage_pct", 0.05)) / 100
    loss = qty * ((entry - stop) + slip * (entry + stop)) if stop else float("inf")
    add(loss <= max_loss + 0.01, "Loss at stop ₹%.0f vs your max ₹%.0f per trade" % (loss, max_loss))
    if t1 and stop and entry > stop:
        rr = (float(t1) - entry) / (entry - stop)
        add(rr >= float(settings["min_reward_risk"]) - 1e-9,
            "Reward:risk %.2f vs your minimum %.2f" % (rr, float(settings["min_reward_risk"])))
    else:
        add(False, "A target is required to check reward:risk")
    add(qty * entry <= float(settings["cash"]) + 0.01, "Capital ₹%.0f vs cash ₹%.0f" % (qty * entry,
                                                                                       float(settings["cash"])))
    add(qty * entry <= cap * float(settings["max_position_pct"]) / 100 + 0.01,
        "Position ₹%.0f vs cap %.0f%% of capital" % (qty * entry, float(settings["max_position_pct"])))
    open_risk = sum(p["qty"] * max(0.0, p["entry_price"] - p["stop"]) for p in open_positions)
    daily_lim = cap * float(settings["max_daily_loss_pct"]) / 100
    worst = -min(0.0, today_closed_pnl) + open_risk + loss
    add(worst <= daily_lim + 0.01, "Today's realised loss + open risk + this trade ₹%.0f vs daily limit ₹%.0f"
        % (worst, daily_lim))
    n_today = len(opened_today)
    add(n_today < int(settings["max_trades_per_day"]),
        "Trades today %d vs limit %d" % (n_today, int(settings["max_trades_per_day"])))
    if order.get("mode") == "intraday":
        st = cal.market_status(now)
        t = now.time()
        a, b = cal.hhmm(settings["intraday_no_entry_before"]), cal.hhmm(settings["intraday_no_entry_after"])
        add(st["open"] and a <= t <= b, "Intraday entry window %s-%s IST (now %s, %s)" % (
            settings["intraday_no_entry_before"], settings["intraday_no_entry_after"], now.strftime("%H:%M"),
            st["label"]))
    add(not order.get("margin") and not order.get("option"), "No margin, leverage or options")
    ok = all(c["ok"] for c in checks)
    return {"verdict": "PASS" if ok else "BLOCK", "checks": checks,
            "failed": [c["text"] for c in checks if not c["ok"]], "checked_at": now.isoformat()}


def today_bounds():
    d = cal.now_ist().date()
    start = datetime.datetime.combine(d, datetime.time(0, 0), cal.IST).timestamp()
    return start, start + 86400
