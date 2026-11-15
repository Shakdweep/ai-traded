"""Indicators for one stock on one date, using ONLY bars dated on or before that date.

Reuses the app's own engine (EMA 20/50/200, RSI, MACD, ATR, VWAP, support/resistance,
setups, score, confidence) and adds SMA 20/50/100/200, EMA 100, Bollinger Bands,
delivery %, daily volatility and named yes/no signals used by the accuracy dashboard
("which indicators actually helped").
"""

import bisect
import math

from .. import db
from ..analysis import engine
from ..analysis import indicators as ind
from . import data

MIN_BARS = 220          # EMA 200 needs ~200 sessions of history
WINDOW = 320            # bars handed to the indicator engine
MIN_TRADED_CR = 10.0    # skip stocks trading less than Rs 10 crore a day on average (illiquid)

# yes/no signals tracked by the dashboard (name -> plain-English label)
SIGNALS = {
    "above_ema200": "Price above EMA 200",
    "above_sma100": "Price above SMA 100",
    "ema20_over_50": "EMA 20 above EMA 50",
    "rsi_over_60": "RSI above 60",
    "rsi_under_40": "RSI below 40",
    "macd_bull": "MACD above its signal",
    "bb_upper_break": "Close above upper Bollinger Band",
    "bb_lower_break": "Close below lower Bollinger Band",
    "volume_breakout": "Volume breakout (1.5x average with a breakout setup)",
    "delivery_high": "Delivery % well above its average",
    "rs_positive": "Stronger than NIFTY over 60 days",
    "near_52w_high": "Within 5% of the 52-week high",
    "overextended": "Overextended (more than 3 ATR above EMA 20)",
}


class Store:
    """All adjusted daily series in memory (one database read), sliced by date without look-ahead."""

    def __init__(self, symbols=None):
        """symbols=None loads everything (GitHub Actions jobs); a small set keeps memory low on the web server."""
        sql = "SELECT symbol, date, open, high, low, close, prev_close, volume, deliv_pct FROM daily_prices"
        args = ()
        if symbols is not None and len(symbols) <= 60:
            want = sorted(set(symbols) | {"^NSEI"})
            sql += " WHERE symbol IN (%s)" % ",".join("?" * len(want))
            args = tuple(want)
        rows = db.q(sql + " ORDER BY symbol, date", args)
        by = {}
        for r in rows:
            if symbols is None or r["symbol"] in symbols or r["symbol"].startswith("^"):
                by.setdefault(r["symbol"], []).append(r)
        self.series = {s: data.adjust(rs) for s, rs in by.items()}
        self.dates = {s: [b["date"] for b in bars] for s, bars in self.series.items()}

    def upto(self, symbol, date, limit=WINDOW):
        bars = self.series.get(symbol) or []
        i = bisect.bisect_right(self.dates.get(symbol) or [], str(date))
        return bars[max(0, i - limit):i]

    def after(self, symbol, date, n):
        """The first n bars strictly AFTER date (used only to judge outcomes, never to predict)."""
        bars = self.series.get(symbol) or []
        i = bisect.bisect_right(self.dates.get(symbol) or [], str(date))
        return bars[i:i + n]

    def close_on(self, symbol, date):
        bars = self.upto(symbol, date, 1)
        return bars[-1] if bars and bars[-1]["date"] == str(date) else None


def _sma(xs, n):
    return ind.last(ind.sma(xs, n))


def _std(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def compute(store, symbol, as_of, bench_symbol="^NSEI"):
    """Feature dict for symbol at the close of as_of, or None if there isn't enough clean data."""
    bars = store.upto(symbol, as_of)
    if len(bars) < MIN_BARS or bars[-1]["date"] != str(as_of):
        return None  # not traded that day, or too little history
    bench = {b["date"]: b["c"] for b in store.upto(bench_symbol, as_of, WINDOW + 30)}
    bench_closes = [bench[b["date"]] for b in bars if b["date"] in bench]
    f = engine.daily_features(bars, bench_closes if len(bench_closes) > 60 else None)
    series_ = f.pop("series", None)
    if f.get("traded_value_cr") is not None and f["traded_value_cr"] < MIN_TRADED_CR:
        return None
    c = [b["c"] for b in bars]
    close = c[-1]
    f["sma20"], f["sma50"], f["sma100"], f["sma200"] = _sma(c, 20), _sma(c, 50), _sma(c, 100), _sma(c, 200)
    f["ema100"] = ind.last(ind.ema(c, 100))
    mid, sd = f["sma20"], _std(c[-20:])
    f["bb_upper"], f["bb_lower"] = (mid + 2 * sd, mid - 2 * sd) if mid and sd else (None, None)
    f["bb_pct_b"] = (close - f["bb_lower"]) / (f["bb_upper"] - f["bb_lower"]) if sd else None
    logret = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 60, len(c)) if c[i - 1] > 0]
    f["sigma_d"] = _std(logret) or 0.02
    dl = [b.get("deliv_pct") for b in bars[-21:-1] if b.get("deliv_pct") is not None]
    f["deliv_pct"] = bars[-1].get("deliv_pct")
    f["deliv_avg20"] = sum(dl) / len(dl) if dl else None
    s, reasons = engine.score_daily(f)
    flags = engine.risk_flags(f)
    rating, why = engine.rate(s, flags, f["setups"])
    missing = sum(1 for k in ("ema20", "ema50", "ema200", "rsi", "macd", "atr", "vol20", "rs20") if f.get(k) is None)
    f.update(score=s, reasons=reasons, flags=flags, rating=rating,
             confidence=engine.confidence(s, f, missing)["value"], symbol=symbol, as_of=str(as_of))
    f["signals"] = signals(f)
    return f


def signals(f):
    close = f["close"]
    atr = f.get("atr") or 0
    vr = f.get("vol_ratio") or 0
    breakout = any(s in (f.get("setups") or []) for s in ("breakout_20d", "resistance_breakout"))
    return {
        "above_ema200": bool(f.get("ema200") and close > f["ema200"]),
        "above_sma100": bool(f.get("sma100") and close > f["sma100"]),
        "ema20_over_50": bool(f.get("ema20") and f.get("ema50") and f["ema20"] > f["ema50"]),
        "rsi_over_60": bool(f.get("rsi") and f["rsi"] > 60),
        "rsi_under_40": bool(f.get("rsi") and f["rsi"] < 40),
        "macd_bull": bool(f.get("macd") is not None and f.get("macd_signal") is not None
                          and f["macd"] > f["macd_signal"]),
        "bb_upper_break": bool(f.get("bb_upper") and close > f["bb_upper"]),
        "bb_lower_break": bool(f.get("bb_lower") and close < f["bb_lower"]),
        "volume_breakout": bool(vr > 1.5 and breakout),
        "delivery_high": bool(f.get("deliv_pct") and f.get("deliv_avg20") and f["deliv_pct"] > 1.2 * f["deliv_avg20"]),
        "rs_positive": bool(f.get("rs60") is not None and f["rs60"] > 0),
        "near_52w_high": bool(f.get("high52") and close >= 0.95 * f["high52"]),
        "overextended": bool(atr and f.get("ema20") and close - f["ema20"] > 3 * atr),
    }
