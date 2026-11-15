"""Model versions, weekly 10-stock selection and 1/3/5/7-trading-day forecasts.

rules-v1 (the baseline every future model must beat on unseen data):
  * Selection score = the app's evidence score (trend, momentum, setups, volume, relative
    strength) + bonuses for strength vs NIFTY, a strong sector, high delivery %, and a volume
    breakout - penalties for being overextended or up more than 15% in 20 days (so stocks
    aren't picked just because they already rose). Liquid NIFTY 500 stocks only, at most
    two per sector.
  * Direction: Bullish if score >= 25, Bearish if <= -20, else Sideways.
  * Price range: an 80% range from the stock's own daily volatility (sigma) over the last
    60 sessions, scaled by sqrt(days), with a deliberately small tilt in the predicted
    direction. The range should contain the actual price about 80% of the time; the
    dashboard checks whether it really does ("coverage").
Nothing here is investment advice; every prediction is a hypothesis to be tested.
"""

import json
import math

from .. import db
from ..analysis import risk as rk
from . import features as fx

HORIZONS = (1, 3, 5, 7)
Z80 = 1.2816                 # 80% two-sided range under a normal approximation
TILT = 0.15                  # how far the centre leans in the predicted direction (in sigma*sqrt(h))
SIDEWAYS_K = 0.3             # |move| below 0.3 sigma*sqrt(h) counts as "sideways"
PICKS = 10
MAX_PER_SECTOR = 2

MODELS = {
    "rules-v1": {
        "description": "Baseline: the app's rule score + relative strength, sector, delivery and volume bonuses; "
                       "volatility-based 80% ranges with a small directional tilt.",
        "params": {"tilt": TILT, "z": Z80, "sideways_k": SIDEWAYS_K, "picks": PICKS, "max_per_sector": MAX_PER_SECTOR,
                   "bull_score": 25, "bear_score": -20},
    },
}
CHAMPION = "rules-v1"


def ensure_models():
    for v, m in MODELS.items():
        db.x("INSERT INTO model_versions(version, created_at, description, params, status) VALUES(?,?,?,?,?) "
             "ON CONFLICT(version) DO NOTHING",
             (v, __import__("time").time(), m["description"], json.dumps(m["params"]),
              "champion" if v == CHAMPION else "challenger"))


def direction_of(score):
    return "Bullish" if score >= 25 else "Bearish" if score <= -20 else "Sideways"


def selection_score(f, sector_rs):
    s = f["score"]
    parts = [("Rule score (trend, momentum, setups, volume)", s)]
    sig = f["signals"]

    def add(cond, pts, text):
        if cond:
            parts.append((text, pts))
    add(sig["rs_positive"], 10, "Stronger than NIFTY over 60 days")
    add(sector_rs is not None and sector_rs > 0, 5, "Its sector is beating NIFTY")
    add(sig["delivery_high"], 5, "Delivery % well above its average (genuine buying)")
    add(sig["volume_breakout"], 5, "Breakout on high volume")
    add(sig["overextended"], -15, "Overextended above EMA 20 (chasing risk)")
    add((f.get("ret20") or 0) > 15, -10, "Already up more than 15% in 20 days")
    return sum(p for _, p in parts), parts


def sector_strength(feats, meta, nifty_ret20):
    by = {}
    for f in feats:
        sec = (meta.get(f["symbol"]) or {}).get("sector") or "Other"
        if f.get("ret20") is not None:
            by.setdefault(sec, []).append(f["ret20"])
    return {sec: (sum(v) / len(v) - (nifty_ret20 or 0)) for sec, v in by.items() if len(v) >= 3}


def select(store, as_of, meta):
    """Score every eligible stock at the close of as_of -> (top 10 rows, number scored)."""
    feats = [f for f in (fx.compute(store, s, as_of) for s in meta) if f]
    nb = store.upto("^NSEI", as_of, 25)
    nifty_ret20 = (nb[-1]["c"] / nb[-21]["c"] - 1) * 100 if len(nb) > 21 else None
    srs = sector_strength(feats, meta, nifty_ret20)
    ranked = []
    for f in feats:
        sec = (meta.get(f["symbol"]) or {}).get("sector") or "Other"
        total, parts = selection_score(f, srs.get(sec))
        ranked.append((total, f["traded_value_cr"] or 0, f, sec, srs.get(sec), parts))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picks, per_sector = [], {}
    for total, _, f, sec, s_rs, parts in ranked:
        if per_sector.get(sec, 0) >= MAX_PER_SECTOR:
            continue
        per_sector[sec] = per_sector.get(sec, 0) + 1
        picks.append({"features": f, "sector": sec, "sector_rs": s_rs, "selection_score": total, "parts": parts})
        if len(picks) == PICKS:
            break
    return picks, len(feats), nifty_ret20


def forecast(pick, nifty_atr_pct=None):
    """1/3/5/7-day ranges, direction, levels, stop, reward/risk, reasons and invalidation for one pick."""
    f = pick["features"]
    price, sigma, score = f["close"], f["sigma_d"], f["score"]
    direction = direction_of(score)
    tilt = TILT * max(-1.0, min(1.0, score / 50.0))
    ranges = {}
    for h in HORIZONS:
        spread = sigma * math.sqrt(h)
        mu = tilt * spread
        ranges[str(h)] = {"low": round(price * math.exp(mu - Z80 * spread), 2), "mid": round(price * math.exp(mu), 2),
                          "high": round(price * math.exp(mu + Z80 * spread), 2),
                          "ret_pct": round((math.exp(mu) - 1) * 100, 2),
                          "flat_band_pct": round(SIDEWAYS_K * spread * 100, 2)}
    atr = f.get("atr") or price * sigma
    sup = [s["price"] for s in (f.get("supports") or [])][:3]
    res = [r["price"] for r in (f.get("resistances") or [])][:3]
    if direction == "Bearish":
        raw = (res[0] if res else price + 2 * atr) + 0.25 * atr
        stop = round(rk.tick_up(min(max(raw, price + atr), price + 3 * atr)), 2)
        target = ranges["7"]["low"]
        rr = (price - target) / (stop - price) if stop > price else None
    else:
        raw = (sup[0] if sup else price - 2 * atr) - 0.25 * atr
        stop = round(rk.tick_down(max(min(raw, price - atr), price - 3 * atr)), 2)
        target = max(ranges["7"]["high"], res[0]) if res and res[0] < ranges["7"]["high"] * 1.05 else ranges["7"]["high"]
        rr = (target - price) / (price - stop) if price > stop else None
    reasons = [r["text"] for r in sorted(f["reasons"], key=lambda r: -abs(r["points"])) if r["points"]][:5]
    why = ["%s (%+d)" % (t, p) for t, p in pick["parts"][1:]]
    invalid = ["A daily close %s the stop ₹%.2f" % ("above" if direction == "Bearish" else "below", stop)]
    if nifty_atr_pct:
        invalid.append("NIFTY 50 falls more than %.1f%% in one session (a market-wide shock)" % (2 * nifty_atr_pct))
    if sup and direction != "Bearish":
        invalid.append("Support ₹%.2f breaks on high volume" % sup[0])
    invalid.append("Company results, a regulatory action or other unexpected news inside the 7 days")
    if f.get("vol20"):
        invalid.append("Volume dries up below half its 20-day average while the price stalls")
    return {
        "price": round(price, 2), "direction": direction, "confidence": f["confidence"], "score": score,
        "rating": f["rating"], "ranges": ranges, "supports": sup, "resistances": res, "stop": stop,
        "target": round(target, 2), "rr": round(rr, 2) if rr else None, "reasons": reasons,
        "why_selected": why, "invalidation": invalid,
        "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()
                     if k in ("rsi", "macd", "macd_signal", "atr", "atr_pct", "ema20", "ema50", "ema100", "ema200",
                              "sma20", "sma50", "sma100", "sma200", "bb_upper", "bb_lower", "bb_pct_b", "vol_ratio",
                              "deliv_pct", "deliv_avg20", "rs20", "rs60", "ret20", "ret60", "sigma_d", "high52",
                              "low52", "traded_value_cr", "setups", "flags")},
        "signals": f["signals"], "sector": pick["sector"], "sector_rs": pick["sector_rs"],
        "selection_score": pick["selection_score"],
    }
