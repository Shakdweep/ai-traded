"""Exit check for an open (paper) position: HOLD / TIGHTEN STOP / BOOK PARTIAL / EXIT.

A rules-based reading of delayed data, re-run every time the paper page loads and by the
background refresh. It never sells anything by itself: the paper engine only acts on the
stop, the targets and the square-off time; this check tells you what the chart says now.

Swing / delivery (completed daily bars):
  EXIT          price at/below the stop; target 2 reached; daily close below EMA 50 while
                the trade is losing; rating-style breakdown (close below EMA 50 and EMA 20
                falling with MACD below its signal)
  BOOK PARTIAL  target 1 reached and more than one share left; >= 2R with RSI above 75
  TIGHTEN STOP  a higher trailing stop is available (break-even at +1R, 3 x ATR below the
                highest high since entry, just below EMA 20 once in profit); close below
                EMA 20; momentum fading (MACD histogram falling 3 sessions from RSI > 65);
                time stop (10+ sessions held and still under +0.5R)
  HOLD          none of the above
Intraday (5-minute bars of today's session):
  EXIT near the square-off time or at the stop; TIGHTEN below session VWAP or when a
  1.5 x ATR(5m) trail is higher than the stop; BOOK PARTIAL at target 1.
"""

import datetime

from ..market import calendar as cal
from . import indicators as ind
from . import risk as rk
from .engine import completed_daily, session_of

ORDER = ["HOLD", "TIGHTEN STOP", "BOOK PARTIAL", "EXIT"]
TONE = {"HOLD": "good", "TIGHTEN STOP": "warn", "BOOK PARTIAL": "info", "EXIT": "bad"}


def _r(x):
    return None if x is None else round(x, 2)


class _Verdict:
    def __init__(self):
        self.level = "HOLD"
        self.reasons = []  # (tone, text)

    def add(self, level, text, tone=None):
        if ORDER.index(level) > ORDER.index(self.level):
            self.level = level
        self.reasons.append((tone or TONE[level], text))

    def note(self, text, tone="muted"):
        self.reasons.append((tone, text))


def _trail(candidates, stop, price):
    """Highest sensible stop: above the current stop, below the price, rounded down to the tick."""
    ok = [(rk.tick_down(v), why) for v, why in candidates if v is not None and stop < v < price * 0.995]
    return max(ok) if ok else (None, None)


def evaluate(pos, price, daily_chart=None, bars5=None, settings=None, orig_stop=None, now=None):
    now = now or cal.now_ist()
    entry, stop = float(pos["entry_price"]), float(pos["stop"])
    t1, t2, qty = pos.get("target1"), pos.get("target2"), int(pos["qty"])
    out = {"verdict": None, "reasons": [], "suggested_stop": None, "suggested_basis": None,
           "r_multiple": None, "pnl_pct": None, "basis": None}
    if price is None:
        out["reasons"] = [("muted", "No price available right now, so no exit check.")]
        return out
    first_stop = orig_stop if orig_stop and orig_stop < entry else (stop if stop < entry else None)
    v = _Verdict()
    out["pnl_pct"] = round((price / entry - 1) * 100, 2)

    if pos["mode"] == "intraday":
        _intraday(pos, price, entry, stop, t1, t2, qty, first_stop, bars5, settings or {}, now, v, out)
    else:
        _swing(pos, price, entry, stop, t1, t2, qty, first_stop, daily_chart, now, v, out)

    out["verdict"] = v.level
    out["tone"] = TONE[v.level]
    out["reasons"] = v.reasons
    return out


def _swing(pos, price, entry, stop, t1, t2, qty, first_stop, chart, now, v, out):
    bars, _ = completed_daily((chart or {}).get("bars") or [])
    if len(bars) < 60:
        v.note("Not enough daily history for the trend checks (need 60 sessions).")
        if price <= stop:
            v.add("EXIT", "Price ₹%.2f is at or below your stop ₹%.2f." % (price, stop))
        return
    closes = [b["c"] for b in bars]
    e20s, e50s = ind.ema(closes, 20), ind.ema(closes, 50)
    e20, e50 = ind.last(e20s), ind.last(e50s)
    rsis = ind.rsi_series(closes)
    rsi = ind.last(rsis)
    _, sig, hist = ind.macd(closes)
    macd_below = hist[-1] is not None and hist[-1] < 0
    atr = ind.last(ind.atr(bars))
    day0 = cal.to_ist(pos["opened_at"]).date()
    since = [b for b in bars if cal.to_ist(b["t"]).date() >= day0]
    held = len(since)
    high_since = max([b["h"] for b in since] + [price, entry])
    r_unit = (entry - first_stop) if first_stop else 2 * (atr or entry * 0.02)
    r_mult = (price - entry) / r_unit if r_unit else None
    out.update(r_multiple=_r(r_mult), basis="daily, completed sessions", sessions_held=held,
               ema20=_r(e20), ema50=_r(e50), rsi=_r(rsi), atr=_r(atr), high_since_entry=_r(high_since))
    last_close = closes[-1]

    # --- exit
    if price <= stop:
        v.add("EXIT", "Price ₹%.2f is at or below your stop ₹%.2f. Exit; don't wait for it to come back." % (price, stop))
    if t2 and price >= t2:
        v.add("EXIT", "Target 2 ₹%.2f reached. The plan's job is done: take the profit." % t2)
    if e50 and last_close < e50 and price < entry:
        v.add("EXIT", "Closed below EMA 50 (₹%.2f) and the trade is losing: the uptrend that justified it is broken."
              % e50)
    if e50 and e20 and last_close < e50 and e20s[-1] is not None and e20s[-2] is not None and \
            e20s[-1] < e20s[-2] and macd_below and price >= entry:
        v.add("EXIT", "Below EMA 50 with EMA 20 turning down and MACD below its signal: trend breakdown. "
                      "Lock in what's left.")

    # --- book partial
    if t1 and price >= t1 and qty > 1:
        v.add("BOOK PARTIAL", "Target 1 ₹%.2f reached: sell about half (%d shares) and move the stop to break-even "
                              "(₹%.2f)." % (t1, qty // 2, entry))
    if r_mult is not None and r_mult >= 2 and rsi and rsi > 75:
        v.add("BOOK PARTIAL", "Up %.1fR with RSI %.0f (stretched). Taking some profit protects the gain." % (r_mult, rsi))

    # --- tighten
    cands = []
    if r_mult is not None and r_mult >= 1:
        cands.append((entry, "break-even (trade is up %.1fR)" % r_mult))
    if atr:
        cands.append((high_since - 3 * atr, "3 x ATR below the highest high since entry (₹%.2f)" % high_since))
    if e20 and price > e20 and price > entry and atr:
        cands.append((e20 - 0.25 * atr, "just below EMA 20 (₹%.2f)" % e20))
    new_stop, why = _trail(cands, stop, price)
    if new_stop and new_stop - stop >= max(0.05, 0.002 * price):
        out["suggested_stop"], out["suggested_basis"] = round(new_stop, 2), why
        v.add("TIGHTEN STOP", "Raise your stop from ₹%.2f to ₹%.2f (%s)." % (stop, new_stop, why))
    if e20 and last_close < e20 and (not e50 or last_close >= e50):
        v.add("TIGHTEN STOP", "Closed below EMA 20 (₹%.2f): the short-term trend is weakening. Keep the stop tight; "
                              "a close below EMA 50 (₹%.2f) would be an exit." % (e20, e50 or 0))
    recent_hist = [h for h in hist[-4:] if h is not None]
    recent_rsi = [r for r in rsis[-6:] if r is not None]
    if len(recent_hist) == 4 and recent_hist[0] > recent_hist[1] > recent_hist[2] > recent_hist[3] and \
            recent_rsi and max(recent_rsi) > 65:
        v.add("TIGHTEN STOP", "Momentum is fading: MACD histogram has fallen 3 sessions in a row after RSI went "
                              "above 65.")
    if held >= 10 and r_mult is not None and r_mult < 0.5 and price > stop:
        v.add("TIGHTEN STOP", "Held %d sessions and only %+.1fR: the setup isn't working. Consider freeing the money "
                              "(time stop)." % (held, r_mult))

    # --- context for HOLD
    if v.level == "HOLD":
        trend = []
        if e20 and price > e20:
            trend.append("above EMA 20")
        if e50 and price > e50:
            trend.append("above EMA 50")
        v.note("Trend intact: price is %s." % " and ".join(trend) if trend else "No exit signal right now.", "good")
    gap = (price - stop) / price * 100
    v.note("Stop ₹%.2f is %.1f%% below the price%s." % (stop, gap, " (break-even or better)" if stop >= entry else ""))
    nxt = t1 if t1 and price < t1 else (t2 if t2 and price < t2 else None)
    if nxt:
        v.note("Next target ₹%.2f is %.1f%% away." % (nxt, (nxt / price - 1) * 100))


def _intraday(pos, price, entry, stop, t1, t2, qty, first_stop, bars5, settings, now, v, out):
    out["basis"] = "5-minute bars, today's session"
    flat_by = cal.hhmm(settings.get("intraday_flat_by", "15:15"))
    mins_left = (datetime.datetime.combine(now.date(), flat_by) - now.replace(tzinfo=None)).total_seconds() / 60 \
        if now.tzinfo else (datetime.datetime.combine(now.date(), flat_by) - now).total_seconds() / 60
    if price <= stop:
        v.add("EXIT", "Price ₹%.2f is at or below your stop ₹%.2f." % (price, stop))
    if cal.to_ist(pos["opened_at"]).date() == now.date() and mins_left <= 15:
        v.add("EXIT", "Square-off time %s is %s: close intraday positions now." % (
            flat_by.strftime("%H:%M"), "here" if mins_left <= 0 else "in %d min" % mins_left))
    if t2 and price >= t2:
        v.add("EXIT", "Target 2 ₹%.2f reached." % t2)
    if t1 and price >= t1 and qty > 1:
        v.add("BOOK PARTIAL", "Target 1 ₹%.2f reached: sell about half and move the stop to ₹%.2f (break-even)."
              % (t1, entry))
    _, sess = session_of(bars5 or [])
    since = [b for b in sess if b["t"] >= pos["opened_at"] - 300]
    r_unit = (entry - first_stop) if first_stop else None
    r_mult = (price - entry) / r_unit if r_unit else None
    out["r_multiple"] = _r(r_mult)
    if len(sess) >= 3:
        vwap = ind.last(ind.vwap_session(sess))
        atr5 = ind.last(ind.atr(sess, min(14, len(sess) - 1))) if len(sess) > 2 else None
        out.update(vwap=_r(vwap), atr=_r(atr5))
        if vwap and price < vwap:
            v.add("TIGHTEN STOP", "Price is below today's VWAP (₹%.2f): buyers are losing control." % vwap)
        cands = []
        if r_mult is not None and r_mult >= 1:
            cands.append((entry, "break-even (up %.1fR)" % r_mult))
        if atr5 and since:
            hi = max([b["h"] for b in since] + [price])
            cands.append((hi - 1.5 * atr5, "1.5 x 5-min ATR below today's high since entry (₹%.2f)" % hi))
        new_stop, why = _trail(cands, stop, price)
        if new_stop and new_stop - stop >= 0.05:
            out["suggested_stop"], out["suggested_basis"] = round(new_stop, 2), why
            v.add("TIGHTEN STOP", "Raise your stop from ₹%.2f to ₹%.2f (%s)." % (stop, new_stop, why))
    else:
        v.note("Not enough 5-minute bars yet today for VWAP and trailing checks.")
    if v.level == "HOLD":
        v.note("No exit signal right now. Square-off by %s." % flat_by.strftime("%H:%M"), "good")
