"""The analysis engine: indicators -> setups -> scored evidence -> rating, with the
reason for every point. It is a transparent rules engine, not a black box and not
a prediction: every rating can be traced to the numbers shown next to it.

Ratings: STRONG BULLISH, BULLISH, WAIT, HIGH RISK, BEARISH, AVOID, plus
INSUFFICIENT DATA when there is not enough history to say anything.

Swing/daily setups reuse the trader-scanner skill's `find_setups()` (20-day closing
-high breakout, bull flag, reclaimed 20-day average, oversold, oversold bounce)
and add Indian-market equivalents of the rest (breakdown, bear flag, MACD and EMA
crosses, VWAP, unusual volume, momentum, S/R breaks, trend reversals).
"""

import datetime

from ..market import calendar as cal
from ..vendor.day_trader_skill import scan as skill_scan
from . import indicators as ind

RATINGS = ["STRONG BULLISH", "BULLISH", "WAIT", "HIGH RISK", "BEARISH", "AVOID", "INSUFFICIENT DATA"]

SETUP_LABELS = {
    "breakout_20d": ("20-day closing-high breakout", +1),
    "bull_flag": ("Bull flag", +1),
    "reclaim_ma20": ("Reclaimed 20-day average", +1),
    "oversold": ("RSI oversold (<30)", 0),
    "oversold_bounce": ("Oversold bounce", +1),
    "breakdown_20d": ("20-day closing-low breakdown", -1),
    "bear_flag": ("Bear flag", -1),
    "overbought": ("RSI overbought (>70)", 0),
    "macd_bull_cross": ("MACD bullish crossover", +1),
    "macd_bear_cross": ("MACD bearish crossover", -1),
    "ema_golden_20_50": ("EMA20 crossed above EMA50", +1),
    "ema_death_20_50": ("EMA20 crossed below EMA50", -1),
    "golden_cross_50_200": ("Golden cross (EMA50 > EMA200)", +1),
    "death_cross_50_200": ("Death cross (EMA50 < EMA200)", -1),
    "vwap_breakout": ("Closed above 20-day VWAP", +1),
    "vwap_breakdown": ("Closed below 20-day VWAP", -1),
    "unusual_volume": ("Unusual volume (>2x 20-day avg)", 0),
    "momentum": ("Momentum leader", +1),
    "resistance_breakout": ("Resistance breakout", +1),
    "support_breakdown": ("Support breakdown", -1),
    "trend_reversal_up": ("Trend reversal up", +1),
    "trend_reversal_down": ("Trend reversal down", -1),
    "near_breakout": ("Approaching breakout level", +1),
    # intraday
    "orb_breakout": ("Opening-range breakout (15 min)", +1),
    "orb_breakdown": ("Opening-range breakdown (15 min)", -1),
    "vwap_reclaim": ("Reclaimed session VWAP", +1),
    "vwap_loss": ("Lost session VWAP", -1),
    "volume_spike": ("5-min volume spike (>3x)", 0),
    "intraday_momentum": ("Intraday momentum", +1),
}
BULL_CONFIRMING = {"breakout_20d", "resistance_breakout", "bull_flag", "momentum", "golden_cross_50_200",
                   "trend_reversal_up", "ema_golden_20_50", "orb_breakout", "intraday_momentum"}


def _crossed_up(a, b, within):
    n = len(a)
    for i in range(max(1, n - within), n):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i - 1] <= b[i - 1] and a[i] > b[i]:
            return True
    return False


def _crossed_down(a, b, within):
    n = len(a)
    for i in range(max(1, n - within), n):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i - 1] >= b[i - 1] and a[i] < b[i]:
            return True
    return False


def completed_daily(bars, now=None):
    """Drop today's still-forming daily bar while the market is open."""
    status = cal.market_status(now)
    if bars and status["open"]:
        if cal.to_ist(bars[-1]["t"]).date() == cal.now_ist().date():
            return bars[:-1], bars[-1]
    return bars, None


def ret(closes, n):
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return (closes[-1] / closes[-n - 1] - 1) * 100


def daily_features(bars, bench_closes=None):
    """All daily indicators and setup tags for one stock (completed bars only)."""
    c = [b["c"] for b in bars]
    v = [b["v"] or 0 for b in bars]
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    rs = ind.rsi_series(c)
    ml, ms, mh = ind.macd(c)
    at = ind.atr(bars)
    vw = ind.rolling_vwap(bars, 20)
    sup, res = ind.levels(bars)
    sup_prev, res_prev = ind.levels(bars[:-1]) if len(bars) > 20 else ([], [])
    last = c[-1]
    vol20 = sum(v[-21:-1]) / 20 if len(v) >= 21 else None
    f = {
        "close": last, "ema20": ind.last(e20), "ema50": ind.last(e50), "ema200": ind.last(e200),
        "rsi": ind.last(rs), "macd": ind.last(ml), "macd_signal": ind.last(ms), "macd_hist": ind.last(mh),
        "macd_hist_prev": mh[-2] if len(mh) > 1 else None, "atr": ind.last(at),
        "vwap20": ind.last(vw), "vol": v[-1], "vol20": vol20,
        "vol_ratio": (v[-1] / vol20) if vol20 else None,
        "traded_value_cr": (sum(c[i] * v[i] for i in range(-20, 0)) / 20 / 1e7) if len(c) >= 20 else None,
        "high20": max(c[-21:-1]) if len(c) >= 21 else None, "low20": min(c[-21:-1]) if len(c) >= 21 else None,
        "high52": max(b["h"] for b in bars[-250:]), "low52": min(b["l"] for b in bars[-250:]),
        "ret5": ret(c, 5), "ret20": ret(c, 20), "ret60": ret(c, 60),
        "supports": sup, "resistances": res, "bars": len(bars),
        "series": {"ema20": e20, "ema50": e50, "ema200": e200, "rsi": rs, "macd": ml, "signal": ms, "hist": mh,
                   "vwap20": vw},
    }
    f["atr_pct"] = f["atr"] / last * 100 if f["atr"] else None
    if bench_closes and len(bench_closes) > 61:
        b20 = ret(bench_closes, 20)
        b60 = ret(bench_closes, 60)
        f["rs20"] = (f["ret20"] - b20) if f["ret20"] is not None and b20 is not None else None
        f["rs60"] = (f["ret60"] - b60) if f["ret60"] is not None and b60 is not None else None
    else:
        f["rs20"] = f["rs60"] = None

    tags = []
    if len(c) >= 30:
        t, nums = skill_scan.find_setups(c)  # reused from the trader-scanner skill
        tags += t
        run = c[-6] / c[-16] - 1
        rng5 = (max(c[-5:]) - min(c[-5:])) / last
        if last < min(c[-21:-1]):
            tags.append("breakdown_20d")
        if run < -0.10 and rng5 < 0.05:
            tags.append("bear_flag")
    if f["rsi"] is not None and f["rsi"] > 70:
        tags.append("overbought")
    if _crossed_up(ml, ms, 3):
        tags.append("macd_bull_cross")
    if _crossed_down(ml, ms, 3):
        tags.append("macd_bear_cross")
    if _crossed_up(e20, e50, 5):
        tags.append("ema_golden_20_50")
    if _crossed_down(e20, e50, 5):
        tags.append("ema_death_20_50")
    if _crossed_up(e50, e200, 10):
        tags.append("golden_cross_50_200")
    if _crossed_down(e50, e200, 10):
        tags.append("death_cross_50_200")
    if len(c) > 2 and vw[-1] and vw[-2]:
        if c[-2] <= vw[-2] and last > vw[-1]:
            tags.append("vwap_breakout")
        if c[-2] >= vw[-2] and last < vw[-1]:
            tags.append("vwap_breakdown")
    if f["vol_ratio"] and f["vol_ratio"] > 2:
        tags.append("unusual_volume")
    if (f["ret20"] or 0) > 8 and f["ema20"] and f["ema50"] and last > f["ema20"] > f["ema50"] \
            and f["rsi"] and 55 <= f["rsi"] <= 75:
        tags.append("momentum")
    if res_prev and len(c) > 1:
        lvl = res_prev[0]["price"]
        if c[-2] <= lvl < last:
            tags.append("resistance_breakout")
            f["broken_level"] = lvl
    if sup_prev and len(c) > 1:
        lvl = sup_prev[0]["price"]
        if c[-2] >= lvl > last:
            tags.append("support_breakdown")
    if f["ema50"] and len(c) > 25:
        below = sum(1 for i in range(-21, -1) if e50[i] and c[i] < e50[i])
        above = sum(1 for i in range(-21, -1) if e50[i] and c[i] > e50[i])
        if last > f["ema50"] and below >= 15 and (f["macd_hist"] or 0) > 0:
            tags.append("trend_reversal_up")
        if last < f["ema50"] and above >= 15 and (f["macd_hist"] or 0) < 0:
            tags.append("trend_reversal_down")
    trigger = None
    if f["high20"] and "breakout_20d" not in tags:
        cand = [f["high20"]] + ([res[0]["price"]] if res else [])
        trigger = min(x for x in cand if x > last) if any(x > last for x in cand) else None
        if trigger and (trigger - last) / last <= 0.02 and f["ema20"] and last > f["ema20"]:
            tags.append("near_breakout")
            f["breakout_trigger"] = round(trigger, 2)
    f["setups"] = sorted(set(tags))
    return f


def _pts(reasons, pts, text):
    reasons.append({"points": pts, "text": text})
    return pts


def score_daily(f):
    """Evidence -> score in about [-100, 100] with one reason per point."""
    R = []
    s = 0
    last = f["close"]
    # 1. Trend structure (max +/-35)
    for key, name in (("ema20", "EMA 20"), ("ema50", "EMA 50"), ("ema200", "EMA 200")):
        v = f[key]
        if v is None:
            R.append({"points": 0, "text": "%s not available (needs more history)" % name})
            continue
        if last > v:
            s += _pts(R, 10, "Price %.2f is above %s (%.2f)" % (last, name, v))
        else:
            s += _pts(R, -10, "Price %.2f is below %s (%.2f)" % (last, name, v))
    if f["ema20"] and f["ema50"] and f["ema200"]:
        if f["ema20"] > f["ema50"] > f["ema200"]:
            s += _pts(R, 5, "EMAs stacked bullish (20 > 50 > 200): uptrend structure")
        elif f["ema20"] < f["ema50"] < f["ema200"]:
            s += _pts(R, -5, "EMAs stacked bearish (20 < 50 < 200): downtrend structure")
    # 2. Momentum (max +/-20)
    r = f["rsi"]
    if r is not None:
        if 55 <= r <= 70:
            s += _pts(R, 8, "RSI %.1f: healthy bullish momentum" % r)
        elif 70 < r <= 80:
            s += _pts(R, 3, "RSI %.1f: strong but overbought; chasing is risky" % r)
        elif r > 80:
            s += _pts(R, -5, "RSI %.1f: extremely overbought, pullback risk" % r)
        elif 45 <= r < 55:
            R.append({"points": 0, "text": "RSI %.1f: neutral" % r})
        elif 30 <= r < 45:
            s += _pts(R, -6, "RSI %.1f: weak momentum" % r)
        else:
            s += _pts(R, -8, "RSI %.1f: oversold, trend is weak (a bounce is possible but unconfirmed)" % r)
    if f["macd"] is not None and f["macd_signal"] is not None:
        if f["macd"] > f["macd_signal"]:
            s += _pts(R, 6, "MACD (%.2f) above its signal line (%.2f)" % (f["macd"], f["macd_signal"]))
        else:
            s += _pts(R, -6, "MACD (%.2f) below its signal line (%.2f)" % (f["macd"], f["macd_signal"]))
        if f["macd_hist"] is not None and f["macd_hist_prev"] is not None:
            if f["macd_hist"] > f["macd_hist_prev"]:
                s += _pts(R, 4, "MACD histogram rising")
            else:
                s += _pts(R, -4, "MACD histogram falling")
    # 3. Volume (max +/-15)
    vr = f["vol_ratio"]
    if vr is not None and f["bars"] > 1:
        day_chg = f.get("day_change_pct")
        if vr >= 1.5 and day_chg is not None:
            if day_chg > 0:
                s += _pts(R, 10, "Up day on %.1fx average volume: buyers in control" % vr)
            elif day_chg < 0:
                s += _pts(R, -10, "Down day on %.1fx average volume: distribution" % vr)
        elif vr < 0.6:
            R.append({"points": 0, "text": "Volume only %.1fx average: weak participation" % vr})
    # 4. Setups (max +/-20)
    bull = [t for t in f["setups"] if SETUP_LABELS.get(t, ("", 0))[1] > 0]
    bear = [t for t in f["setups"] if SETUP_LABELS.get(t, ("", 0))[1] < 0]
    if bull:
        s += _pts(R, min(20, 7 * len(bull)), "Bullish setups: " + ", ".join(SETUP_LABELS[t][0] for t in bull))
    if bear:
        s += _pts(R, -min(20, 7 * len(bear)), "Bearish setups: " + ", ".join(SETUP_LABELS[t][0] for t in bear))
    # 5. Relative strength vs NIFTY 50 (max +/-10)
    rs = f.get("rs20")
    if rs is not None:
        if rs > 5:
            s += _pts(R, 10, "Outperformed NIFTY 50 by %.1f pts over 20 days" % rs)
        elif rs > 2:
            s += _pts(R, 5, "Outperformed NIFTY 50 by %.1f pts over 20 days" % rs)
        elif rs < -5:
            s += _pts(R, -10, "Underperformed NIFTY 50 by %.1f pts over 20 days" % -rs)
        elif rs < -2:
            s += _pts(R, -5, "Underperformed NIFTY 50 by %.1f pts over 20 days" % -rs)
    return max(-100, min(100, s)), R


def risk_flags(f, mode="swing"):
    flags = []
    if f.get("atr_pct") and f["atr_pct"] > 4.0:
        flags.append("High volatility: daily ATR is %.1f%% of price" % f["atr_pct"])
    if f.get("rsi") and f["rsi"] > 78:
        flags.append("Overextended: RSI %.0f" % f["rsi"])
    if f.get("ema20") and f["close"] > f["ema20"] * 1.12:
        flags.append("Price %.0f%% above EMA 20: extended from support" % ((f["close"] / f["ema20"] - 1) * 100))
    tv = f.get("traded_value_cr")
    min_tv = 25 if mode == "intraday" else 5
    if tv is not None and tv < min_tv:
        flags.append("Low liquidity: average traded value ₹%.1f cr/day (< ₹%d cr)" % (tv, min_tv))
    if f.get("bars", 0) < 200:
        flags.append("Short history (%d daily bars): EMA 200 and long-term context unavailable" % f.get("bars", 0))
    return flags


def confidence(score, f, n_fields_missing, total_fields=8, extra_support=None):
    """Confidence 0-100 = 50% strength of evidence + 30% indicator agreement +
    20% data completeness. Shown with its parts so it can be checked."""
    sign = 1 if score >= 0 else -1
    checks = []
    last = f["close"]
    for k in ("ema20", "ema50", "ema200"):
        if f.get(k):
            checks.append((last > f[k]) == (sign > 0))
    if f.get("macd") is not None and f.get("macd_signal") is not None:
        checks.append((f["macd"] > f["macd_signal"]) == (sign > 0))
    if f.get("rsi") is not None:
        checks.append((f["rsi"] > 50) == (sign > 0))
    if f.get("rs20") is not None:
        checks.append((f["rs20"] > 0) == (sign > 0))
    agree = sum(checks) / len(checks) if checks else 0
    complete = max(0.0, 1 - n_fields_missing / float(total_fields))
    strength = min(abs(score), 100) / 100.0
    val = 100 * (0.5 * strength + 0.3 * agree + 0.2 * complete)
    return {
        "value": round(val), "strength": round(strength * 100), "agreement": round(agree * 100),
        "agreement_detail": "%d of %d indicators agree with the %s reading" % (
            sum(checks), len(checks), "bullish" if sign > 0 else "bearish"),
        "completeness": round(complete * 100),
        "formula": "confidence = 50% x evidence strength (|score|) + 30% x indicator agreement + "
                   "20% x data completeness",
        "note": "Confidence measures how consistent the available evidence is, not the probability "
                "that the price will rise or fall.",
    }


def rate(score, flags, setups, fundamentals_class=None, mode="swing"):
    severe = [x for x in flags if x.startswith(("High volatility", "Overextended", "Low liquidity", "Price"))]
    confirming = BULL_CONFIRMING & set(setups)
    why = []
    if score >= 55 and confirming and not severe:
        r = "STRONG BULLISH"
        why.append("score %+d is >= 55 with a confirming setup and no major risk flag" % score)
    elif score >= 25:
        if severe:
            r = "HIGH RISK"
            why.append("score %+d is bullish but risk flags apply: %s" % (score, "; ".join(severe)))
        else:
            r = "BULLISH"
            why.append("score %+d is >= 25%s" % (score, "" if confirming else " but no confirmed breakout yet"))
    elif score <= -45:
        r = "AVOID"
        why.append("score %+d is <= -45: weak structure across trend, momentum and setups" % score)
    elif score <= -20:
        if "oversold_bounce" in setups:
            r = "HIGH RISK"
            why.append("score %+d is bearish, but an oversold bounce is in progress (counter-trend)" % score)
        elif any(x.startswith("Low liquidity") for x in flags):
            r = "AVOID"
            why.append("score %+d is bearish and the stock is illiquid" % score)
        else:
            r = "BEARISH"
            why.append("score %+d is <= -20" % score)
    else:
        r = "WAIT"
        why.append("score %+d is between -20 and +25: no clear edge, wait for confirmation" % score)
    if fundamentals_class and mode != "intraday":
        if fundamentals_class == "Weak fundamentals":
            if r in ("STRONG BULLISH", "BULLISH"):
                r = "HIGH RISK"
                why.append("downgraded: weak fundamentals undermine a swing/long hold")
            elif r == "BEARISH":
                r = "AVOID"
                why.append("downgraded: bearish technicals and weak fundamentals")
    return r, why


def analyse_daily(chart, bench_chart=None, mode="swing", fundamentals_class=None):
    """Full swing-mode analysis of one daily chart (as returned by yahoo.chart)."""
    from ..market.yahoo import quote_from_chart
    q = quote_from_chart(chart)
    bars, forming = completed_daily(chart["bars"])
    out = {"symbol": q["symbol"], "name": q["name"], "quote": q, "mode": mode,
           "timeframe": "daily (completed sessions)", "forming_bar": bool(forming)}
    if len(bars) < 60:
        out.update(rating="INSUFFICIENT DATA", score=None, reasons=[],
                   why=["only %d daily bars available; at least 60 needed" % len(bars)],
                   confidence=None, features=None, flags=[])
        return out
    bench = None
    if bench_chart:
        bb, _ = completed_daily(bench_chart["bars"])
        bench = [b["c"] for b in bb]
    f = daily_features(bars, bench)
    if len(bars) > 1:
        f["day_change_pct"] = (bars[-1]["c"] / bars[-2]["c"] - 1) * 100
    s, reasons = score_daily(f)
    flags = risk_flags(f, mode)
    if q["freshness"]["label"] in ("STALE", "UNAVAILABLE"):
        flags.append("Price data is %s: do not act on it" % q["freshness"]["label"].lower())
    missing = sum(1 for k in ("ema20", "ema50", "ema200", "rsi", "macd", "atr", "vol20", "rs20") if f.get(k) is None)
    rating, why = rate(s, flags, f["setups"], fundamentals_class, mode)
    trend = "Uptrend" if f["ema50"] and f["close"] > f["ema50"] and (not f["ema200"] or f["ema50"] > f["ema200"]) \
        else "Downtrend" if f["ema50"] and f["close"] < f["ema50"] and (not f["ema200"] or f["ema50"] < f["ema200"]) \
        else "Sideways / transitioning"
    series = f.pop("series")
    out.update(
        rating=rating, score=s, reasons=reasons, why=why, flags=flags, trend=trend,
        confidence=confidence(s, f, missing),
        setups=[{"key": t, "label": SETUP_LABELS.get(t, (t, 0))[0], "bias": SETUP_LABELS.get(t, ("", 0))[1]}
                for t in f["setups"]],
        features={k: (ind.safe_round(v, 2) if isinstance(v, float) else v) for k, v in f.items()},
        last_completed=cal.to_ist(bars[-1]["t"]).date().isoformat(),
    )
    out["_series"] = series
    out["_bars"] = bars
    return out


# ---------------------------------------------------------------- intraday

def session_of(bars5, day=None):
    """Bars of one IST session (default: the latest day in the data)."""
    if not bars5:
        return None, []
    days = sorted({cal.to_ist(b["t"]).date() for b in bars5})
    d = day or days[-1]
    return d, [b for b in bars5 if cal.to_ist(b["t"]).date() == d]


def analyse_intraday(chart5, daily_analysis=None, bench5=None, or_minutes=15):
    """Intraday analysis on 5-minute bars: opening range, VWAP, volume spikes,
    intraday momentum. Uses the latest session in the data (today if open)."""
    from ..market.yahoo import quote_from_chart
    q = quote_from_chart(chart5)
    day, S = session_of(chart5["bars"])
    out = {"symbol": q["symbol"], "name": q["name"], "quote": q, "mode": "intraday",
           "timeframe": "5-minute bars", "session": day.isoformat() if day else None}
    status = cal.market_status()
    out["session_is_today"] = bool(day and day == cal.now_ist().date())
    n = or_minutes // 5
    if len(S) < n + 1:
        out.update(rating="INSUFFICIENT DATA", score=None, reasons=[], flags=[],
                   why=["the opening range is still forming or there are no bars for this session"],
                   confidence=None)
        return out
    # drop a still-forming last bar when the market is open
    if status["open"] and out["session_is_today"] and S:
        if cal.now_ist().timestamp() < S[-1]["t"] + 300:
            S = S[:-1]
    c = [b["c"] for b in S]
    vw = ind.vwap_session(S)
    e9, e21 = ind.ema(c, 9), ind.ema(c, 21)
    oh = max(b["h"] for b in S[:n])
    ol = min(b["l"] for b in S[:n])
    last = c[-1]
    vols = [b["v"] or 0 for b in S]
    avgv = sum(vols[:-1]) / max(1, len(vols) - 1)
    spikes = [i for i in range(1, len(S)) if avgv and vols[i] > 3 * avgv]
    tags = []
    trig = next((i for i in range(n, len(S)) if S[i]["c"] > oh), None)
    brk = next((i for i in range(n, len(S)) if S[i]["c"] < ol), None)
    if trig is not None and last > oh:
        tags.append("orb_breakout")
    if brk is not None and last < ol:
        tags.append("orb_breakdown")
    if len(S) > 2 and vw[-2] and vw[-1]:
        if c[-2] <= vw[-2] and last > vw[-1]:
            tags.append("vwap_reclaim")
        if c[-2] >= vw[-2] and last < vw[-1]:
            tags.append("vwap_loss")
    if spikes and spikes[-1] >= len(S) - 3:
        tags.append("volume_spike")
    if e9[-1] and e21[-1] and last > vw[-1] and e9[-1] > e21[-1] and last > oh:
        tags.append("intraday_momentum")
    R, s = [], 0
    if vw[-1]:
        if last > vw[-1]:
            s += _pts(R, 20, "Price %.2f above session VWAP %.2f" % (last, vw[-1]))
        else:
            s += _pts(R, -20, "Price %.2f below session VWAP %.2f" % (last, vw[-1]))
    if last > oh:
        s += _pts(R, 20, "Above the %d-min opening range high %.2f" % (or_minutes, oh))
    elif last < ol:
        s += _pts(R, -20, "Below the %d-min opening range low %.2f" % (or_minutes, ol))
    else:
        R.append({"points": 0, "text": "Inside the opening range %.2f-%.2f" % (ol, oh)})
    if e9[-1] and e21[-1]:
        s += _pts(R, 15 if e9[-1] > e21[-1] else -15,
                  "5-min EMA 9 %s EMA 21" % ("above" if e9[-1] > e21[-1] else "below"))
    if "volume_spike" in tags:
        s += _pts(R, 10 if c[-1] >= S[-1]["o"] else -10, "Recent 5-min volume spike on a %s bar" %
                  ("green" if c[-1] >= S[-1]["o"] else "red"))
    if daily_analysis and daily_analysis.get("trend"):
        t = daily_analysis["trend"]
        s += _pts(R, 15 if t == "Uptrend" else -15 if t == "Downtrend" else 0, "Daily trend context: %s" % t)
    if bench5:
        _, B = session_of(bench5["bars"], day)
        if len(B) > 1 and S[0]["o"]:
            rs = (last / S[0]["o"] - 1) * 100 - (B[-1]["c"] / B[0]["o"] - 1) * 100
            s += _pts(R, 10 if rs > 0.5 else -10 if rs < -0.5 else 0,
                      "Relative to NIFTY today: %+.2f pts" % rs)
    s = max(-100, min(100, s))
    flags = []
    rng_pct = (oh - ol) / last * 100
    if rng_pct > 2.5:
        flags.append("Wide opening range (%.1f%%): stop at the range midpoint is far" % rng_pct)
    if daily_analysis and daily_analysis.get("features"):
        tv = daily_analysis["features"].get("traded_value_cr")
        if tv is not None and tv < 25:
            flags.append("Low liquidity for intraday: ₹%.1f cr/day average" % tv)
    if q["freshness"]["label"] in ("STALE", "UNAVAILABLE"):
        flags.append("Price data is %s: do not act on it" % q["freshness"]["label"].lower())
    if not out["session_is_today"]:
        flags.append("Showing the last completed session (%s), not today" % day.isoformat())
    rating, why = rate(s, flags, tags, mode="intraday")
    if rating == "STRONG BULLISH" and "orb_breakout" not in tags:
        rating = "BULLISH"
    out.update(
        rating=rating, score=s, reasons=R, why=why, flags=flags,
        confidence=confidence(s, {"close": last, "ema20": e9[-1], "ema50": e21[-1], "rsi": None,
                                  "macd": None, "rs20": None}, 0, 8),
        setups=[{"key": t, "label": SETUP_LABELS[t][0], "bias": SETUP_LABELS[t][1]} for t in tags],
        features={"or_high": round(oh, 2), "or_low": round(ol, 2), "or_mid": round((oh + ol) / 2, 2),
                  "vwap": ind.safe_round(vw[-1]), "ema9": ind.safe_round(e9[-1]), "ema21": ind.safe_round(e21[-1]),
                  "last": round(last, 2), "bars": len(S), "volume_spikes": len(spikes),
                  "trigger_bar": cal.to_ist(S[trig]["t"] + 300).strftime("%H:%M") if trig is not None else None,
                  "trigger_close": round(S[trig]["c"], 2) if trig is not None else None},
        trend="Intraday up" if s > 20 else "Intraday down" if s < -20 else "Range-bound",
    )
    out["_session"] = S
    out["_vwap"] = vw
    return out


def strip_private(a):
    return {k: v for k, v in a.items() if not k.startswith("_")}


def today_str():
    return datetime.date.today().isoformat()
