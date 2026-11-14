"""Structured trade plans. Long only by default (short selling is off).

Swing: entry range from the setup (breakout trigger, fresh breakout, or a pullback
to EMA 20), stop below the nearest support with an ATR buffer, Target 1 at 2R or
the nearest resistance if it comes first, Target 2 at 3R or the next resistance.

Intraday: the trader-exit skill's defaults on the opening range: stop at the
range midpoint, Target 1 at 2R (Target 2 at 3R), flat by your square-off time
(15:15 IST default).

A plan is only marked ACTIONABLE when the rating is bullish, reward:risk meets
your minimum, the data is not stale and the size fits your limits. Otherwise
the plan says exactly what is missing.
"""

from . import risk as rk

NO_TRADE = "NO TRADE – Market conditions are not favourable. Protect your capital."


def _r2(x):
    return None if x is None else round(x, 2)


def swing_plan(a, settings):
    sym = a["symbol"]
    if a["rating"] == "INSUFFICIENT DATA":
        return {"symbol": sym, "actionable": False, "status": "NO DATA", "message": "Not enough data for a plan."}
    f = a["features"]
    last, atr = f["close"], f["atr"]
    plan = {"symbol": sym, "name": a.get("name"), "mode": "swing", "side": "buy", "timeframe": "daily"}
    if a["rating"] in ("BEARISH", "AVOID"):
        plan.update(actionable=False, status="NO TRADE",
                    message="No long trade: the structure is %s. Short selling is disabled in your settings."
                            % a["rating"].lower())
        return plan
    keys = {s["key"] for s in a.get("setups", [])}
    sups = [s["price"] for s in f.get("supports") or []]
    ress = [r["price"] for r in f.get("resistances") or []]
    if "near_breakout" in keys and f.get("breakout_trigger"):
        trig = f["breakout_trigger"]
        lo, hi = rk.tick_up(trig * 1.001), rk.tick_up(trig * 1.001 + 0.5 * atr)
        plan["setup"] = "Breakout above %.2f" % trig
        plan["confirmation"] = ["Daily close above %.2f" % trig,
                                "Breakout-day volume at least 1.5x the 20-day average (%s shares)" % (
                                    format(int(1.5 * f["vol20"]), ",") if f.get("vol20") else "n/a"),
                                "NIFTY 50 not in a sharp sell-off on the day"]
    elif keys & {"breakout_20d", "resistance_breakout"}:
        lvl = f.get("broken_level") or f.get("high20") or last
        lo, hi = rk.tick_up(last), rk.tick_up(last + 0.5 * atr)
        plan["setup"] = "Fresh breakout (level %.2f)" % lvl
        plan["confirmation"] = ["Next session holds above the breakout level %.2f" % lvl,
                                "Do not chase an opening gap above %.2f; wait for a retest" % hi]
    elif f.get("ema20") and last <= f["ema20"] * 1.03 and last >= f["ema20"] * 0.99:
        lo, hi = rk.tick_up(f["ema20"]), rk.tick_up(f["ema20"] + 0.5 * atr)
        plan["setup"] = "Pullback to EMA 20 in an uptrend"
        plan["confirmation"] = ["A bullish daily candle closing above the prior day's high",
                                "Price holds above EMA 20 (%.2f)" % f["ema20"]]
    else:
        lo = hi = None
        plan["setup"] = "No defined entry"
    if lo is None:
        plan.update(actionable=False, status="WAIT",
                    message="No clean entry right now: price is neither at a breakout trigger nor on a pullback "
                            "to EMA 20. Wait.")
        return plan
    entry = hi  # size from the worst fill inside the range
    cands = [s for s in sups if s < lo]
    if f.get("ema50") and f["ema50"] < lo:
        cands.append(f["ema50"])
    stop = None
    for s in sorted(cands, reverse=True):
        st = s - 0.25 * atr
        if atr <= entry - st <= 3 * atr:
            stop = st
            break
    if stop is None:
        stop = entry - 2 * atr
        plan["stop_basis"] = "2 x ATR below entry (no support in a sensible range)"
    else:
        plan["stop_basis"] = "below support/EMA 50 with a 0.25 x ATR buffer"
    stop = rk.tick_down(stop)
    risk = entry - stop
    t1 = entry + 2 * risk
    t1_basis = "2R"
    near = [r for r in ress if entry + 0.5 * risk < r < t1]
    if near:
        t1, t1_basis = near[0], "nearest resistance (comes before 2R)"
    t2 = max(entry + 3 * risk, next((r for r in ress if r > t1 + 0.5 * risk), 0))
    t1, t2 = rk.tick_down(t1), rk.tick_down(t2)
    rr = (t1 - entry) / risk if risk > 0 else 0
    days = max(2, min(25, round((t1 - entry) / (0.5 * atr)))) if atr else None
    size = rk.size_position(entry, stop, settings, target=t1, product="delivery")
    plan.update(
        entry_low=_r2(lo), entry_high=_r2(hi), entry=_r2(entry), stop=_r2(stop), target1=_r2(t1),
        target2=_r2(t2), target1_basis=t1_basis, risk_per_share=_r2(risk), reward_risk=round(rr, 2),
        holding_period="about %d-%d trading days (estimate from ATR; not a promise)" % (
            max(1, days // 2), days) if days else "unknown",
        position=size,
        invalidation=["Daily close below the stop %.2f" % stop,
                      "Daily close back below EMA 50 (%.2f)" % f["ema50"] if f.get("ema50") else
                      "Daily close below the entry range low",
                      "Breakout fails: close back below the trigger within 2 sessions",
                      "Gap down below the stop at the open: exit, do not wait"],
        reasoning=[r["text"] for r in a["reasons"] if r["points"] > 0][:6],
    )
    missing = []
    if a["rating"] not in ("STRONG BULLISH", "BULLISH"):
        missing.append("rating is %s (needs BULLISH or STRONG BULLISH)" % a["rating"])
    if rr < float(settings["min_reward_risk"]):
        missing.append("reward:risk %.2f is below your minimum %.2f" % (rr, float(settings["min_reward_risk"])))
    if not size.get("qty"):
        missing.append(size.get("refused") or "position size is 0")
    if a["quote"]["freshness"]["label"] in ("STALE", "UNAVAILABLE"):
        missing.append("price data is %s" % a["quote"]["freshness"]["label"].lower())
    plan["actionable"] = not missing
    plan["status"] = "ACTIONABLE (after confirmation)" if not missing else "WAIT"
    plan["blockers"] = missing
    if missing:
        plan["message"] = "Not actionable yet: " + "; ".join(missing) + "."
    return plan


def intraday_plan(a, settings, target_r=2.0):
    sym = a["symbol"]
    if a["rating"] == "INSUFFICIENT DATA":
        return {"symbol": sym, "actionable": False, "status": "NO DATA", "message": "Opening range not formed yet."}
    f = a["features"]
    plan = {"symbol": sym, "name": a.get("name"), "mode": "intraday", "side": "buy", "timeframe": "5-minute",
            "flat_by": settings["intraday_flat_by"]}
    keys = {s["key"] for s in a.get("setups", [])}
    if a["rating"] in ("BEARISH", "AVOID") or "orb_breakdown" in keys:
        plan.update(actionable=False, status="NO TRADE",
                    message="No long trade: the stock is below its opening range / VWAP. Shorting is disabled.")
        return plan
    oh, om = f["or_high"], f["or_mid"]
    if "orb_breakout" in keys and f.get("trigger_close"):
        entry = f["trigger_close"]
        plan["setup"] = "Opening-range breakout, triggered %s" % f["trigger_bar"]
        plan["confirmation"] = ["5-min close above %.2f (done at %s)" % (oh, f["trigger_bar"]),
                                "Price stays above session VWAP (%.2f)" % (f["vwap"] or 0)]
    else:
        entry = rk.tick_up(oh * 1.0005)
        plan["setup"] = "Waiting for an opening-range breakout above %.2f" % oh
        plan["confirmation"] = ["A completed 5-min candle closing above %.2f" % oh,
                                "Above session VWAP, volume on the breakout bar above average"]
    stop = rk.tick_down(om)
    risk = entry - stop
    if risk <= 0:
        plan.update(actionable=False, status="WAIT", message="Price is below the range midpoint; no valid stop.")
        return plan
    t1, t2 = rk.tick_down(entry + target_r * risk), rk.tick_down(entry + (target_r + 1) * risk)
    size = rk.size_position(entry, stop, settings, target=t1, product="intraday")
    rr = (t1 - entry) / risk
    plan.update(entry=_r2(entry), entry_low=_r2(entry), entry_high=_r2(rk.tick_up(entry + 0.15 * risk)),
                stop=_r2(stop), stop_basis="opening-range midpoint (trader-exit default)", target1=_r2(t1),
                target2=_r2(t2), target1_basis="%gR" % target_r, reward_risk=round(rr, 2),
                risk_per_share=_r2(risk),
                holding_period="same day; square off by %s IST" % settings["intraday_flat_by"], position=size,
                invalidation=["5-min close back inside the opening range below %.2f" % oh,
                              "Price loses session VWAP", "Stop %.2f hit" % stop,
                              "Square-off time %s reached" % settings["intraday_flat_by"]],
                reasoning=[r["text"] for r in a["reasons"] if r["points"] > 0][:6])
    missing = []
    if "orb_breakout" not in keys:
        missing.append("no opening-range breakout yet")
    if a["rating"] not in ("STRONG BULLISH", "BULLISH"):
        missing.append("rating is %s" % a["rating"])
    if rr < float(settings["min_reward_risk"]):
        missing.append("reward:risk %.2f below your minimum" % rr)
    if not size.get("qty"):
        missing.append(size.get("refused") or "size is 0")
    if not a.get("session_is_today"):
        missing.append("this is the last completed session, not a live one")
    if a["quote"]["freshness"]["label"] in ("STALE", "UNAVAILABLE"):
        missing.append("price data is %s" % a["quote"]["freshness"]["label"].lower())
    plan["actionable"] = not missing
    plan["blockers"] = missing
    plan["status"] = "ACTIONABLE (after confirmation)" if not missing else "WAIT"
    if missing:
        plan["message"] = "Not actionable: " + "; ".join(missing) + "."
    return plan
