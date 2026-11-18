"""Intraday opening-range-breakout (ORB) backtest on 5-minute bars, used to test rule changes
for the Rs 5,000 paper bot BEFORE adopting them.

Mirrors the bot: opening range 09:15-09:30, entry when a completed 5-min candle closes above
the range high (and above session VWAP), entries 09:30-14:45, buy at that close + slippage,
stop = range midpoint, target = k x R,
stop checked before target inside a candle, gaps below the stop fill at the open, flat at
15:15. Size and Indian intraday charges exactly as the bot (Rs 5,000, 1% risk, 5x buying
power). One trade per stock per day. Results in R (1R = the risk per share) and rupees.

Honest-testing rules: a small set of variants decided BEFORE looking at results; the best is
chosen on the first half of the days only and then reported on the second half (unseen).
Free data keeps only ~60 days of 5-minute bars, so samples are small: treat results as
evidence to test forward, not proof.
"""

import datetime
import statistics
import sys

from ..analysis import risk as rk
from ..db import DEFAULT_SETTINGS
from ..market import calendar as cal

OPEN_RANGE_BARS = 3              # 09:15, 09:20, 09:25 -> range complete at 09:30
SLIP = 0.0005
FLAT = datetime.time(15, 15)
ENTRY_FROM, ENTRY_TO = datetime.time(9, 30), datetime.time(14, 45)

BASE = {"buffer_pct": 0.0, "vol_mult": 0.0, "max_or_pct": None, "target_r": 2.0, "time_stop_bars": None,
        "time_stop_progress_r": 0.5, "vwap": True, "stop_mode": "mid", "index_filter": False}

# Decided before running: one change at a time, then all three ideas from Day 1 together.
VARIANTS = {
    "v0 current rules": {},
    "v1 strong breakout (+0.15%)": {"buffer_pct": 0.15},
    "v2 volume spike (1.5x)": {"vol_mult": 1.5},
    "v3 skip wide range (>1%)": {"max_or_pct": 1.0},
    "v4 closer target (1.5R)": {"target_r": 1.5},
    "v5 time stop (90 min, +0.5R)": {"time_stop_bars": 18},
    "v6 = v1+v2+v3+v5": {"buffer_pct": 0.15, "vol_mult": 1.5, "max_or_pct": 1.0, "time_stop_bars": 18},
    "v7 = v1+v3+v5": {"buffer_pct": 0.15, "max_or_pct": 1.0, "time_stop_bars": 18},
}

# Round 2 (decided after round 1 showed ORB has no edge before costs, BEFORE running these):
# structurally different risk and a market filter, not more tuning of the same trigger.
VARIANTS_2 = {
    "v0 current rules": {},
    "A wide stop (range low), 1R": {"stop_mode": "low", "target_r": 1.0},
    "B NIFTY above its VWAP": {"index_filter": True},
    "C = A + B": {"stop_mode": "low", "target_r": 1.0, "index_filter": True},
}


def _t(b):
    return cal.to_ist(b["t"]).time()


def bot_qty(entry, stop, target):
    s = dict(DEFAULT_SETTINGS)
    s.update(capital=5000.0, cash=25000.0, risk_per_trade_pct=1.0, max_position_pct=500.0, slippage_pct=0.05)
    return rk.size_position(entry, stop, s, target=target, product="intraday").get("qty") or 0


def _vwap_by_time(bars):
    """{bar time: (close, session VWAP)}. Indices have no volume on Yahoo, so for them the
    average of typical prices so far (time-weighted) is used instead."""
    pv = vv = tp_sum = 0.0
    out = {}
    for n, b in enumerate(bars, 1):
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * (b["v"] or 0)
        vv += b["v"] or 0
        tp_sum += tp
        out[b["t"]] = (b["c"], pv / vv if vv else tp_sum / n)
    return out


def simulate_day(bars, params, index_bars=None):
    """bars: one session's 5-min bars in time order; index_bars: NIFTY's bars that session.
    -> trade dict or None."""
    p = dict(BASE, **params)
    if len(bars) < OPEN_RANGE_BARS + 2:
        return None
    rng = bars[:OPEN_RANGE_BARS]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    om = (oh + ol) / 2
    if oh <= 0 or (p["max_or_pct"] and (oh - ol) / oh * 100 > p["max_or_pct"]):
        return None
    trigger = oh * (1 + p["buffer_pct"] / 100)
    ix = _vwap_by_time(index_bars) if (p["index_filter"] and index_bars) else None
    if p["index_filter"] and not ix:
        return None
    pv = vv = 0.0
    entry_i = None
    for i, b in enumerate(bars):
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * (b["v"] or 0)
        vv += b["v"] or 0
        vwap = pv / vv if vv else b["c"]
        if i < OPEN_RANGE_BARS:
            continue
        close_t = (datetime.datetime.combine(datetime.date.today(), _t(b)) + datetime.timedelta(minutes=5)).time()
        if close_t < ENTRY_FROM or close_t > ENTRY_TO:
            continue
        if b["c"] <= trigger or (p["vwap"] and b["c"] < vwap):
            continue
        if ix is not None:
            ic, iv = ix.get(b["t"], (None, None))
            if ic is None or iv is None or ic <= iv:   # NIFTY not above its own VWAP at that candle
                continue
        if p["vol_mult"]:
            prior = [x["v"] or 0 for x in bars[:i]]
            if not prior or (b["v"] or 0) < p["vol_mult"] * (sum(prior) / len(prior)):
                continue
        entry_i = i
        break
    if entry_i is None:
        return None
    entry = bars[entry_i]["c"] * (1 + SLIP)
    stop = ol if p["stop_mode"] == "low" else om
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + p["target_r"] * risk
    qty = bot_qty(round(entry, 2), stop, target)
    if qty < 1:
        return None
    exit_px = reason = None
    exit_i = None
    for j in range(entry_i + 1, len(bars)):
        b = bars[j]
        if _t(b) >= FLAT:
            exit_px, reason, exit_i = bars[j - 1]["c"] * (1 - SLIP), "square-off", j
            break
        if b["l"] <= stop:
            exit_px, reason, exit_i = min(stop, b["o"]) * (1 - SLIP), "stop", j
            break
        if b["h"] >= target:
            exit_px, reason, exit_i = target * (1 - SLIP), "target", j
            break
        if p["time_stop_bars"] and j - entry_i >= p["time_stop_bars"] and b["c"] < entry + p["time_stop_progress_r"] * risk:
            exit_px, reason, exit_i = b["c"] * (1 - SLIP), "time stop", j
            break
    if exit_px is None:
        exit_px, reason = bars[-1]["c"] * (1 - SLIP), "square-off"
    costs = rk.round_trip_costs(qty, entry, exit_px, "intraday")["total"]
    net = qty * (exit_px - entry) - costs
    return {"entry": round(entry, 2), "exit": round(exit_px, 2), "stop": round(stop, 2), "target": round(target, 2),
            "qty": qty, "reason": reason, "net": round(net, 2), "r": (exit_px - entry) / risk,
            "net_r": net / (qty * risk), "or_pct": (oh - ol) / oh * 100, "entry_time": _t(bars[entry_i]).strftime("%H:%M")}


def sessions(chart_bars):
    by = {}
    for b in chart_bars:
        d = cal.to_ist(b["t"]).date()
        if cal.is_trading_day(d):
            by.setdefault(d, []).append(b)
    return {d: v for d, v in by.items() if len(v) >= 60}  # skip half/missing sessions


def stats(trades):
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x <= 0]
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"trades": len(trades), "win_pct": round(100 * len(wins) / len(trades), 1),
            "avg_net_r": round(statistics.mean(t["net_r"] for t in trades), 3),
            "avg_net_rs": round(statistics.mean(nets), 2), "total_rs": round(sum(nets), 2),
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) else None,
            "max_drawdown_rs": round(dd, 2), "exits": reasons}


def run(symbol_charts, variants=None, index_chart=None):
    """symbol_charts: {symbol: 5-min chart bars}; index_chart: NIFTY 5-min bars.
    -> per-variant stats on first half, second half, all."""
    variants = variants or VARIANTS
    days = sorted({d for bars in symbol_charts.values() for d in sessions(bars)})
    if len(days) < 10:
        raise ValueError("only %d sessions of 5-minute data" % len(days))
    cut = days[len(days) // 2]
    per_symbol = {s: sessions(b) for s, b in symbol_charts.items()}
    index_sess = sessions(index_chart) if index_chart else {}
    out = {"days": len(days), "first_half": "%s to %s" % (days[0], days[len(days) // 2 - 1]),
           "second_half": "%s to %s" % (cut, days[-1]), "stocks": len(symbol_charts), "variants": {}}
    for name, params in variants.items():
        trades = []
        for sym, sess in per_symbol.items():
            for d in sorted(sess):
                t = simulate_day(sess[d], params, index_sess.get(d))
                if t:
                    t.update(symbol=sym, day=d)
                    trades.append(t)
        trades.sort(key=lambda t: (t["day"], t["entry_time"]))
        out["variants"][name] = {"params": params, "first": stats([t for t in trades if t["day"] < cut]),
                                 "second": stats([t for t in trades if t["day"] >= cut]), "all": stats(trades)}
    return out


def merge_bars(*lists):
    by = {}
    for bars in lists:
        for b in bars:
            by[int(b["t"])] = b
    return [by[t] for t in sorted(by)]


def verdict(res):
    """APPROVED only if the best variant chosen on the FIRST half is profitable after costs on the SECOND
    half with a profit factor above 1.2 and at least 100 trades; otherwise REJECTED."""
    name, v = max(res["variants"].items(), key=lambda kv: kv[1]["first"].get("avg_net_r") or -9)
    s = v["second"]
    ok = (s.get("trades") or 0) >= 100 and (s.get("avg_net_r") or -1) > 0 and (s.get("profit_factor") or 0) > 1.2
    return ("APPROVED" if ok else "REJECTED"), name, s


def run_and_store():
    """Fetch 60 days of NIFTY 50 5-min bars, test both variant rounds, store one report (for the
    Research lab and the Today's 5 warning)."""
    import json
    import time
    from .. import db
    from ..market import universe, yahoo
    from . import data as rdata
    syms = [s["symbol"] for s in universe.constituents("nifty50")["stocks"]]
    charts, bad = yahoo.charts([yahoo.to_yahoo(s) for s in syms], "60d", "5m")
    nifty = yahoo.chart("^NSEI", "60d", "5m")["bars"]
    data = {yahoo.plain(k): v["bars"] for k, v in charts.items()}
    try:   # our own saved history (grows every day) + the fresh 60 days
        stored = rdata.stored_intraday(set(syms) | {"^NSEI"})
    except Exception:  # noqa: BLE001
        stored = {}
    data = {s: merge_bars(stored.get(s, []), data.get(s, [])) for s in set(data) | (set(stored) - {"^NSEI"})}
    nifty = merge_bars(stored.get("^NSEI", []), nifty)
    res = run(data, dict(VARIANTS, **VARIANTS_2), nifty)
    res["fetch_errors"] = len(bad)
    v, best, s = verdict(res)
    summary = ("%s: the best version on the first half was '%s'; on the unseen second half it made %s trades, "
               "%s%% winners, %sR per trade after charges, profit factor %s."
               % (v, best, s.get("trades"), s.get("win_pct"), s.get("avg_net_r"), s.get("profit_factor")))
    db.x("INSERT INTO strategy_reports(strategy, created_at, verdict, summary, report) VALUES(?,?,?,?,?)",
         ("intraday-orb-nifty50", time.time(), v, summary, json.dumps(res, default=str)))
    return summary


def latest_report(strategy="intraday-orb-nifty50"):
    import json
    from .. import db
    row = db.q("SELECT * FROM strategy_reports WHERE strategy=? ORDER BY created_at DESC LIMIT 1", (strategy,),
               one=True)
    if row:
        row["report"] = json.loads(row["report"])
    return row


def main(argv):
    from ..market import universe, yahoo
    syms = [s["symbol"] for s in universe.constituents("nifty50")["stocks"]]
    charts, bad = yahoo.charts([yahoo.to_yahoo(s) for s in syms], "60d", "5m")
    nifty = yahoo.chart("^NSEI", "60d", "5m")["bars"]
    res = run({yahoo.plain(k): v["bars"] for k, v in charts.items()},
              VARIANTS_2 if argv and argv[0] == "round2" else VARIANTS, nifty)
    print("ORB backtest: %d stocks, %d sessions | tune on %s | test on %s | fetch errors %d" % (
        res["stocks"], res["days"], res["first_half"], res["second_half"], len(bad)))
    for name, v in res["variants"].items():
        f, s = v["first"], v["second"]
        print("%-30s FIRST n=%-4s win %-5s avgR %-7s PF %-5s total %-9s | SECOND n=%-4s win %-5s avgR %-7s PF %-5s total %-9s dd %s" % (
            name, f.get("trades"), f.get("win_pct"), f.get("avg_net_r"), f.get("profit_factor"), f.get("total_rs"),
            s.get("trades"), s.get("win_pct"), s.get("avg_net_r"), s.get("profit_factor"), s.get("total_rs"),
            s.get("max_drawdown_rs")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
