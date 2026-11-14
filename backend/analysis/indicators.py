"""Technical indicators on plain lists (standard library only).

RSI uses Wilder's smoothing, identical to the trader-scanner skill's `rsi()`
(which is vendored and used as a cross-check in the tests)."""

import math


def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, v in enumerate(xs):
        s += v
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(xs[:n]) / n
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = xs[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig = [None] * (len(line) - len(valid)) + sig_valid
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def atr(bars, n=14):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def vwap_session(bars):
    """Cumulative VWAP over the given (single-session) bars."""
    pv = vv = 0.0
    out = []
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * (b["v"] or 0)
        vv += b["v"] or 0
        out.append(pv / vv if vv else None)
    return out


def rolling_vwap(bars, n=20):
    """Volume-weighted average price over the last n bars (used on daily charts)."""
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = bars[i - n + 1:i + 1]
        vv = sum(b["v"] or 0 for b in w)
        if vv:
            out[i] = sum((b["h"] + b["l"] + b["c"]) / 3.0 * (b["v"] or 0) for b in w) / vv
    return out


def pivots(bars, left=3, right=3):
    """Swing highs and lows (fractals)."""
    highs, lows = [], []
    for i in range(left, len(bars) - right):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h >= bars[j]["h"] for j in range(i - left, i + right + 1)):
            highs.append((i, h))
        if all(l <= bars[j]["l"] for j in range(i - left, i + right + 1)):
            lows.append((i, l))
    return highs, lows


def levels(bars, lookback=120, tol=0.015, max_levels=4):
    """Support / resistance levels from clustered swing points, with touch counts.
    Returns (supports below price, resistances above price), nearest first."""
    if len(bars) < 15:
        return [], []
    window = bars[-lookback:]
    hs, ls = pivots(window)
    pts = sorted([p for _, p in hs] + [p for _, p in ls])
    clusters = []
    for p in pts:
        if clusters and abs(p - clusters[-1]["mean"]) / clusters[-1]["mean"] <= tol:
            c = clusters[-1]
            c["pts"].append(p)
            c["mean"] = sum(c["pts"]) / len(c["pts"])
        else:
            clusters.append({"pts": [p], "mean": p})
    price = bars[-1]["c"]
    lv = [{"price": round(c["mean"], 2), "touches": len(c["pts"])} for c in clusters]
    sup = sorted([x for x in lv if x["price"] < price], key=lambda x: -x["price"])[:max_levels]
    res = sorted([x for x in lv if x["price"] > price], key=lambda x: x["price"])[:max_levels]
    # Add 52-week style extremes if nothing found
    if not res:
        hi = max(b["h"] for b in window)
        if hi > price:
            res = [{"price": round(hi, 2), "touches": 1}]
    if not sup:
        lo = min(b["l"] for b in window)
        if lo < price:
            sup = [{"price": round(lo, 2), "touches": 1}]
    return sup, res


def floor_pivots(prev_bar):
    """Classic floor-trader pivots from the previous session (used for intraday)."""
    h, l, c = prev_bar["h"], prev_bar["l"], prev_bar["c"]
    p = (h + l + c) / 3
    return {"P": round(p, 2), "R1": round(2 * p - l, 2), "S1": round(2 * p - h, 2),
            "R2": round(p + (h - l), 2), "S2": round(p - (h - l), 2)}


def last(xs):
    for v in reversed(xs):
        if v is not None:
            return v
    return None


def safe_round(v, n=2):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, n)
