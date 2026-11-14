"""Backtesting and probability analysis.

INTRADAY uses the trader-timemachine skill's own code (vendored, unchanged):
`build_pool`, `simulate` (10,000 Monte Carlo replays of the rest of the session
drawn from the stock's own 5-minute bars) and `history_replay` (the same
opening-range rule re-run on every past full session). The only adaptation is
the session geometry: NSE has 75 five-minute bars (09:15-15:30 IST) instead of
NYSE's 78, set through the module's constants.

SWING is an event study on up to 5 years of daily bars: every past day the same
setup rule fired, entered at the NEXT day's open, with the plan's stop distance
(in ATR) and target R, a time exit after `max_hold` sessions, the stop checked
before the target inside a bar (conservative), and Indian delivery charges plus
slippage subtracted from every trade.

Neither is a forecast. Small samples are flagged. Past frequencies are not
probabilities of future success.
"""

import statistics

from ..market import calendar as cal
from ..market import yahoo
from ..vendor.day_trader_skill import timemachine as tm
from . import indicators as ind
from . import risk as rk

# NSE session geometry for the vendored Time Machine
tm.SESSION_BARS = cal.SESSION_BARS_5M
tm.OPEN_MIN = cal.OPEN_MIN
tm.CLOSE_MIN = 15 * 60 + 30

# The gate a plan must pass before it is called actionable: enough history, and an
# average result after costs that is clearly above zero (not noise around it).
MIN_OCCURRENCES = 15
MIN_AVG_R = 0.10

DISCLAIMER = ("Historical and simulated frequencies describe the past and a model of it. "
              "They are not guaranteed probabilities of future success.")


def _max_drawdown(rs):
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return round(dd, 2)


def _summ(trades, key="net_R"):
    n = len(trades)
    if not n:
        return {"occurrences": 0}
    rs = [t[key] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "occurrences": n,
        "target_hit_pct": round(100 * sum(1 for t in trades if t["outcome"] == "target") / n, 1),
        "stop_hit_pct": round(100 * sum(1 for t in trades if t["outcome"] == "stop") / n, 1),
        "neither_pct": round(100 * sum(1 for t in trades if t["outcome"] not in ("target", "stop")) / n, 1),
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "avg_R": round(statistics.mean(rs), 3),
        "avg_win_R": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss_R": round(statistics.mean(losses), 2) if losses else None,
        "max_drawdown_R": _max_drawdown(rs),
        "sample_warning": ("Small sample (%d): treat as anecdotal" % n) if n < 20 else None,
    }


# ---------------------------------------------------------------- intraday (Time Machine)

MAX_FILL = 3


def _nse_bars(chart, stats=None):
    """5-minute bars -> the Time Machine's {date, slot, o, h, l, c} rows.
    Yahoo routinely omits the 15:20 IST bar (and occasionally one or two others);
    up to MAX_FILL missing slots in a session are filled with a flat bar at the
    previous close, and the number filled is reported."""
    by_day = {}
    for b in chart["bars"]:
        dt = cal.to_ist(b["t"])
        m = dt.hour * 60 + dt.minute
        slot = (m - cal.OPEN_MIN) // 5
        if 0 <= slot < cal.SESSION_BARS_5M:
            by_day.setdefault(dt.date().isoformat(), {})[slot] = {
                "date": dt.date().isoformat(), "slot": slot, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
    out, filled = [], 0
    for d in sorted(by_day):
        S = by_day[d]
        missing = [i for i in range(cal.SESSION_BARS_5M) if i not in S]
        if 0 not in S or len(missing) > MAX_FILL:
            out.extend(S.values())  # left short: full_sessions() will skip it
            continue
        for i in missing:
            pc = S[i - 1]["c"]
            S[i] = {"date": d, "slot": i, "o": pc, "h": pc, "l": pc, "c": pc}
            filled += 1
        out.extend(S[i] for i in range(cal.SESSION_BARS_5M))
    if stats is not None:
        stats["filled_bars"] = filled
    return out


def intraday(symbol, plan, settings, replays=10000, seed=19):
    ysym = yahoo.to_yahoo(symbol)
    ch = yahoo.chart(ysym, "60d", "5m")
    st = {}
    bars = _nse_bars(ch, st)
    days = tm.split_sessions(bars)
    today = cal.now_ist().date().isoformat()
    sessions, short = tm.full_sessions(days, today)
    res = {"method": "trader-timemachine (vendored) on NSE 5-minute bars", "symbol": symbol,
           "disclaimer": DISCLAIMER, "sessions_used": len(sessions), "sessions_skipped": len(short),
           "filled_bars": st.get("filled_bars", 0),
           "data_note": "Yahoo omits some 5-min bars (usually 15:20 IST); %d were filled flat at the previous "
                        "close." % st.get("filled_bars", 0)}
    if len(sessions) < tm.MIN_SESSIONS:
        res["error"] = "Only %d full sessions in the data; at least %d needed." % (len(sessions), tm.MIN_SESSIONS)
        return res
    res["period"] = "%s to %s" % (sessions[0][0], sessions[-1][0])
    flat = cal.hhmm(settings["intraday_flat_by"])
    last = min(cal.SESSION_BARS_5M - 1, (flat.hour * 60 + flat.minute - cal.OPEN_MIN) // 5 - 1)
    target_r = round((plan["target1"] - plan["entry"]) / (plan["entry"] - plan["stop"]), 2)
    rule = {"or_minutes": 15, "stop_rule": "or_mid", "target_r": target_r}
    hist = tm.history_replay(sessions, rule, last)
    # costs per trade in R (intraday charges + slippage on both fills)
    slip = float(settings.get("slippage_pct", 0.05)) / 100
    trades = []
    for t in hist["trades"]:
        risk = t["entry"] - t["stop"]
        exit_px = t["target"] if t["outcome"] == "target" else t["stop"] if t["outcome"] == "stop" else \
            t["entry"] + t["R"] * risk
        q = max(1, int(100000 // t["entry"]))
        c = rk.round_trip_costs(q, t["entry"], exit_px, "intraday", float(settings.get("brokerage_per_order", 20)))
        cost_R = (c["total"] / q + slip * (t["entry"] + exit_px)) / risk if risk > 0 else 0
        trades.append(dict(t, gross_R=t["R"], net_R=round(t["R"] - cost_R, 3), cost_R=round(cost_R, 3)))
    res["history"] = dict(_summ(trades), rule="15-min opening-range breakout, stop at range midpoint, "
                                             "target %.2fR, flat by %s" % (target_r, settings["intraday_flat_by"]),
                          sessions_tested=len(sessions), trades=trades[-40:],
                          costs="intraday charges + %.2f%% slippage per fill, deducted" % (slip * 100))
    # Monte Carlo from the plan's entry
    k = cal.SESSION_BARS_5M
    if plan.get("trigger_slot") is not None:
        k = plan["trigger_slot"]
    else:
        k = 3  # first bar after a 15-minute range: the typical ORB entry time
    if k >= last:
        res["monte_carlo"] = {"error": "Too late in the session to replay."}
        return res
    pool = tm.build_pool(sessions)
    mc = tm.simulate(pool, plan["entry"], plan["stop"], plan["target1"], k, last, replays=replays, seed=seed)
    mc["until"] = settings["intraday_flat_by"]
    res["monte_carlo"] = {k2: mc[k2] for k2 in ("replays", "seed", "p_target", "p_stop", "p_neither", "p_green",
                                                 "median_close", "p5_close", "p95_close", "avg_R", "r_at_target",
                                                 "bars_left", "bands", "until")}
    res["monte_carlo"]["verdict"] = tm.verdict(mc)
    res["monte_carlo"]["notes"] = tm.verdict_notes(mc)
    res["monte_carlo"]["entry_bar"] = "slot %d (%s IST)" % (k, tm.clock12(cal.OPEN_MIN + 5 * (k + 1)))
    return res


# ---------------------------------------------------------------- swing (event study)

def _rule_hits(bars, rule):
    c = [b["c"] for b in bars]
    v = [b["v"] or 0 for b in bars]
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    rs = ind.rsi_series(c)
    ml, ms, _ = ind.macd(c)
    hits = []
    for i in range(201, len(bars) - 1):
        if None in (e20[i], e50[i], e200[i], rs[i], ml[i], ms[i]):
            continue
        vol20 = sum(v[i - 20:i]) / 20
        if rule == "breakout":
            ok = c[i] > max(c[i - 20:i]) and c[i] > e50[i] and vol20 and v[i] >= 1.5 * vol20
        elif rule == "pullback":
            ok = c[i] > e50[i] > e200[i] and bars[i]["l"] <= e20[i] * 1.01 and c[i] > e20[i] \
                and c[i] > bars[i - 1]["h"]
        else:  # trend-following
            ok = c[i] > e20[i] > e50[i] and ml[i] > ms[i] and 50 <= rs[i] <= 70 and not (
                c[i - 1] > e20[i - 1] > e50[i - 1] and ml[i - 1] > ms[i - 1] and 50 <= rs[i - 1] <= 70)
        if ok:
            hits.append(i)
    return hits


def swing(symbol, plan, settings, max_hold=20):
    ysym = yahoo.to_yahoo(symbol)
    ch = yahoo.chart(ysym, "5y", "1d", ttl=6 * 3600)
    bars = ch["bars"]
    setup = (plan.get("setup") or "").lower()
    rule = "breakout" if "breakout" in setup else "pullback" if "pullback" in setup else "trend"
    rule_text = {"breakout": "close above the prior 20-day closing high, above EMA 50, on >= 1.5x volume",
                 "pullback": "uptrend (close > EMA50 > EMA200), dip to EMA 20, close above prior day's high",
                 "trend": "first day of: close > EMA20 > EMA50, MACD above signal, RSI 50-70"}[rule]
    res = {"method": "event study on daily bars", "symbol": symbol, "rule": rule_text, "disclaimer": DISCLAIMER}
    if len(bars) < 260:
        res["error"] = "Only %d daily bars; at least 260 needed." % len(bars)
        return res
    at = ind.atr(bars)
    risk_atr = (plan["entry"] - plan["stop"]) / plan["features_atr"] if plan.get("features_atr") else 2.0
    target_r = (plan["target1"] - plan["entry"]) / (plan["entry"] - plan["stop"])
    slip = float(settings.get("slippage_pct", 0.05)) / 100
    trades = []
    i_free = 0
    for i in _rule_hits(bars, rule):
        if i < i_free or i + 1 >= len(bars) or not at[i]:
            continue
        e = bars[i + 1]["o"]
        s = e - risk_atr * at[i]
        t = e + target_r * (e - s)
        risk = e - s
        oc, exit_px, j_exit = None, None, None
        for j in range(i + 1, min(len(bars), i + 1 + max_hold)):
            b = bars[j]
            if b["l"] <= s:
                oc, exit_px = "stop", min(s, b["o"])  # a gap through the stop fills at the open
                j_exit = j
                break
            if b["h"] >= t:
                oc, exit_px = "target", max(t, b["o"]) if j > i + 1 else t
                j_exit = j
                break
        if oc is None:
            j_exit = min(len(bars) - 1, i + max_hold)
            if j_exit == len(bars) - 1 and j_exit < i + max_hold:
                continue  # still open today: not a completed trade
            oc, exit_px = "time exit", bars[j_exit]["c"]
        q = max(1, int(100000 // e))
        c = rk.round_trip_costs(q, e, exit_px, "delivery", float(settings.get("brokerage_per_order", 20)))
        gross = (exit_px - e) / risk
        cost_R = (c["total"] / q + slip * (e + exit_px)) / risk
        trades.append({"date": cal.to_ist(bars[i]["t"]).date().isoformat(), "entry": round(e, 2),
                       "stop": round(s, 2), "target": round(t, 2), "outcome": oc, "exit": round(exit_px, 2),
                       "days": j_exit - i, "gross_R": round(gross, 2), "net_R": round(gross - cost_R, 3)})
        i_free = j_exit + 1
    res.update(_summ(trades))
    res.update(
        period="%s to %s" % (cal.to_ist(bars[0]["t"]).date(), cal.to_ist(bars[-1]["t"]).date()),
        bars=len(bars), stop_atr_multiple=round(risk_atr, 2), target_R=round(target_r, 2), max_hold_days=max_hold,
        costs="delivery charges (STT 0.1%% each side, stamp, exchange, GST, DP) + %.2f%% slippage per fill"
              % (slip * 100),
        avg_days_held=round(statistics.mean(t["days"] for t in trades), 1) if trades else None,
        trades=trades[-40:],
    )
    return res
