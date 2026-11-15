"""Orchestration: universe scans, the market overview, the per-stock deep dive and
the daily briefing ("WHAT SHOULD I DO TODAY?"). Everything here is assembled from
real fetched data; when data is missing the output says so instead of filling in."""

import json
import threading
import time

from . import db
from .config import BACKGROUND_UNIVERSE, DATA_DIR
from .analysis import backtest, engine, fundamentals, tradeplan
from .market import calendar as cal
from .market import universe, yahoo
from .news import feeds

SCANS = {}      # (universe, mode) -> result
_scan_lock = threading.Lock()
SIGNAL_KEEP_DAYS = 90  # rating history kept per stock
_running = set()


def _bench(rng="2y", interval="1d"):
    try:
        return yahoo.chart("^NSEI", rng, interval)
    except yahoo.DataError:
        return None


def _row(a, plan, meta):
    f = a.get("features") or {}
    q = a["quote"]
    sup = (f.get("supports") or [{}])[0].get("price") if f.get("supports") else None
    res = (f.get("resistances") or [{}])[0].get("price") if f.get("resistances") else None
    return {
        "symbol": a["symbol"], "name": meta.get("name") or a.get("name"), "sector": meta.get("sector"),
        "cap": meta.get("cap"), "price": q["price"], "change_pct": q["change_pct"], "volume": q["volume"],
        "vol_ratio": f.get("vol_ratio"), "rsi": f.get("rsi"), "macd": f.get("macd"),
        "macd_signal": f.get("macd_signal"), "vwap": f.get("vwap20") or f.get("vwap"),
        "ema20": f.get("ema20"), "ema50": f.get("ema50"), "ema200": f.get("ema200"),
        "support": sup, "resistance": res, "atr_pct": f.get("atr_pct"), "atr": f.get("atr"),
        "traded_value_cr": f.get("traded_value_cr"),
        "trend": a.get("trend"), "setups": [s["key"] for s in a.get("setups", [])],
        "setup_labels": [s["label"] for s in a.get("setups", [])], "rating": a["rating"], "score": a.get("score"),
        "confidence": (a.get("confidence") or {}).get("value"), "flags": a.get("flags", []),
        "why": a.get("why", []), "freshness": q["freshness"], "breakout_trigger": f.get("breakout_trigger"),
        "or_high": f.get("or_high"), "or_low": f.get("or_low"),
        "plan": {k: plan.get(k) for k in ("status", "actionable", "setup", "entry", "entry_low", "entry_high", "stop",
                                          "target1", "target2", "reward_risk", "message")} if plan else None,
        "qty": ((plan or {}).get("position") or {}).get("qty"),
        "max_loss": ((plan or {}).get("position") or {}).get("max_loss"),
    }


def run_scan(key="nifty100", mode="swing", symbols=None):
    """Scan a universe (or an explicit symbol list). Blocking; returns the result."""
    tag = (key, mode)
    with _scan_lock:
        if tag in _running:
            return SCANS.get(tag) or {"running": True, "universe": key, "mode": mode, "rows": []}
        _running.add(tag)
    t0 = time.time()
    try:
        settings = db.get_settings()
        meta = universe.stock_meta()
        if symbols:
            stocks = [{"symbol": s.upper(), **meta.get(s.upper(), {})} for s in symbols]
            title = "Custom list"
        else:
            u = universe.constituents(key)
            stocks = [dict(s, cap=meta.get(s["symbol"], {}).get("cap")) for s in u["stocks"]]
            title = u["name"]
        ysyms = [yahoo.to_yahoo(s["symbol"]) for s in stocks]
        rows, errors = [], {}
        if mode == "intraday":
            bench = _bench("5d", "5m")
            charts, bad = yahoo.charts(ysyms, "5d", "5m")
            daily = {r["symbol"]: r for r in (SCANS.get((key, "swing")) or {}).get("rows", [])}
        else:
            bench = _bench()
            charts, bad = yahoo.charts(ysyms, "2y", "1d")
        errors.update(bad)
        for s, ys in zip(stocks, ysyms):
            ch = charts.get(ys)
            if not ch:
                continue
            m = dict(s, **meta.get(s["symbol"], {}))
            try:
                if mode == "intraday":
                    d = daily.get(s["symbol"])
                    ctx = {"trend": d["trend"], "features": {"traded_value_cr": d["traded_value_cr"]}} if d else None
                    a = engine.analyse_intraday(ch, ctx, bench)
                    plan = tradeplan.intraday_plan(a, settings) if a["rating"] != "INSUFFICIENT DATA" else None
                else:
                    a = engine.analyse_daily(ch, bench, mode="swing")
                    plan = tradeplan.swing_plan(a, settings) if a["rating"] != "INSUFFICIENT DATA" else None
                rows.append(_row(a, plan, m))
            except Exception as e:  # noqa: BLE001 - one bad series never sinks the scan
                errors[ys] = "analysis failed: %s" % str(e)[:120]
        now = time.time()
        db.xmany("INSERT INTO signals(ts, symbol, mode, rating, score, confidence, price, setups, data_ts, "
                 "entry, stop, target1, target2) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [(now, r["symbol"], mode, r["rating"], r["score"], r["confidence"], r["price"],
                   ",".join(r["setups"]), now) + tuple((r.get("plan") or {}).get(k) for k in
                                                       ("entry", "stop", "target1", "target2"))
                  for r in rows if r["rating"] != "INSUFFICIENT DATA"])
        db.x("DELETE FROM signals WHERE ts < ?", (now - SIGNAL_KEEP_DAYS * 86400,))  # keep the history small
        res = {"universe": key, "title": title, "mode": mode, "rows": rows, "errors": errors,
               "scanned": len(stocks), "analysed": len(rows), "started": t0, "finished": time.time(),
               "finished_ist": cal.now_ist().strftime("%d %b %Y %H:%M:%S IST"),
               "market": cal.market_status(), "running": False,
               "source": "Yahoo Finance (unofficial, delayed). Index lists: niftyindices.com."}
        SCANS[tag] = res
        _save(tag, res)
        return res
    finally:
        with _scan_lock:
            _running.discard(tag)


SCAN_DIR = DATA_DIR / "scans"
SCAN_DIR.mkdir(exist_ok=True)


def _save(tag, res):
    try:
        (SCAN_DIR / ("%s_%s.json" % tag)).write_text(json.dumps(res), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def load_saved():
    """Reload the last scans after a restart. They keep their original timestamps
    and are replaced by the first fresh scan; nothing is presented as new."""
    for p in SCAN_DIR.glob("*.json"):
        try:
            res = json.loads(p.read_text(encoding="utf-8"))
            res["restored_from_disk"] = True
            SCANS[(res["universe"], res["mode"])] = res
        except (OSError, ValueError, KeyError):
            continue


def scan_async(key, mode):
    threading.Thread(target=run_scan, args=(key, mode), daemon=True).start()


def is_running(key, mode):
    return (key, mode) in _running


def filter_rows(rows, sector=None, cap=None, min_price=None, max_price=None, min_volume=None, setup=None,
                rating=None, q=None):
    out = []
    for r in rows:
        if sector and (r.get("sector") or "") != sector:
            continue
        if cap and (r.get("cap") or "Other") != cap:
            continue
        if min_price and (r["price"] or 0) < min_price:
            continue
        if max_price and (r["price"] or 0) > max_price:
            continue
        if min_volume and (r["volume"] or 0) < min_volume:
            continue
        if setup and setup not in r["setups"]:
            continue
        if rating and r["rating"] != rating:
            continue
        if q and q.upper() not in r["symbol"] and q.upper() not in (r.get("name") or "").upper():
            continue
        out.append(r)
    return out


ORDER = {k: i for i, k in enumerate(engine.RATINGS)}
_valid = {}


def validate(r):
    """Backtest gate for a scanner row's plan (cached per symbol, plan and day).
    Swing plans: 5-year event study; intraday: Time Machine history. A plan is only
    called actionable when the setup has >= 10 past occurrences and a positive
    average R after costs on this stock."""
    p = r.get("plan") or {}
    mode = "intraday" if r.get("or_high") is not None else "swing"
    key = (r["symbol"], mode, p.get("entry"), p.get("stop"), cal.now_ist().date().isoformat())
    if key in _valid:
        return _valid[key]
    settings = db.get_settings()
    try:
        if mode == "intraday":
            bt = backtest.intraday(r["symbol"], dict(p, symbol=r["symbol"]), settings, replays=2000)
            h = bt.get("history") or {}
        else:
            bt = backtest.swing(r["symbol"], dict(p, features_atr=r.get("atr")), settings)
            h = bt
        n, avg = h.get("occurrences", 0), h.get("avg_R")
        if bt.get("error"):
            v = {"ok": False, "why": bt["error"]}
        elif n < backtest.MIN_OCCURRENCES:
            v = {"ok": False, "why": "only %d historical occurrences" % n}
        elif (avg or 0) < backtest.MIN_AVG_R:
            v = {"ok": False, "why": "weak history after costs (avg %+.2fR over %d; need >= %+.2fR)" % (avg or 0, n, backtest.MIN_AVG_R)}
        else:
            v = {"ok": True, "why": "%d past occurrences, avg %+.2fR net, target hit %.0f%%" % (
                n, avg, h.get("target_hit_pct") or 0)}
        v.update(occurrences=n, avg_R=avg, target_hit_pct=h.get("target_hit_pct"), period=bt.get("period"))
    except Exception as e:  # noqa: BLE001
        v = {"ok": False, "why": "backtest failed: %s" % str(e)[:80]}
    _valid[key] = v
    return v


def top(rows, kind, n=10):
    if kind == "bullish":
        sel = [r for r in rows if r["rating"] in ("STRONG BULLISH", "BULLISH")]
        return sorted(sel, key=lambda r: (ORDER[r["rating"]], -(r["confidence"] or 0)))[:n]
    if kind == "bearish":
        sel = [r for r in rows if r["rating"] in ("AVOID", "BEARISH")]
        return sorted(sel, key=lambda r: (r["score"] or 0))[:n]
    if kind == "actionable":
        sel = [r for r in rows if r["plan"] and r["plan"].get("actionable")]
        sel = sorted(sel, key=lambda r: -(r["confidence"] or 0))[:max(n, 12)]
        out = []
        for r in sel:
            v = validate(r)
            if v["ok"]:
                out.append(dict(r, backtest=v))
        return out[:n]
    if kind == "near_breakout":
        sel = [r for r in rows if "near_breakout" in r["setups"] or
               (r.get("or_high") and r["price"] and 0 < (r["or_high"] - r["price"]) / r["price"] < 0.005)]
        return sorted(sel, key=lambda r: -(r["score"] or 0))[:n]
    if kind == "volume":
        sel = [r for r in rows if r.get("vol_ratio")]
        return sorted(sel, key=lambda r: -(r["vol_ratio"] or 0))[:n]
    return rows[:n]


# ---------------------------------------------------------------- overview

def _quote(ys, rng="1y"):
    try:
        ch = yahoo.chart(ys, rng, "1d")
        return yahoo.quote_from_chart(ch), ch
    except yahoo.DataError as e:
        return {"symbol": ys, "price": None, "error": str(e),
                "freshness": {"label": "UNAVAILABLE", "source": "Yahoo Finance"}}, None


def overview():
    out = {"market": cal.market_status(), "indices": [], "global": [], "sectors": []}
    main = {}
    for name, ys in yahoo.INDEX_SYMBOLS.items():
        q, ch = _quote(ys, "2y" if name in ("NIFTY 50", "BANK NIFTY") else "1mo")
        q["label"] = name
        (out["indices"] if name in ("NIFTY 50", "BANK NIFTY", "SENSEX", "INDIA VIX") else out["sectors"]).append(q)
        if ch and name in ("NIFTY 50", "BANK NIFTY"):
            main[name] = ch
    for name, ys in yahoo.GLOBAL_SYMBOLS.items():
        q, _ = _quote(ys, "1mo")
        q["label"] = name
        out["global"].append(q)
    out["sectors"].sort(key=lambda q: -(q.get("change_pct") or -999))
    trends = {}
    for name, ch in main.items():
        a = engine.analyse_daily(ch, None)
        f = a.get("features") or {}
        trends[name] = {
            "rating": a["rating"], "score": a.get("score"), "trend": a.get("trend"),
            "rsi": f.get("rsi"), "ema20": f.get("ema20"), "ema50": f.get("ema50"), "ema200": f.get("ema200"),
            "supports": f.get("supports"), "resistances": f.get("resistances"),
            "why": [r["text"] for r in a.get("reasons", [])][:6],
            "history": [{"time": b["t"], "close": b["c"]} for b in ch["bars"][-120:]],
        }
    out["trends"] = trends
    scan = SCANS.get((BACKGROUND_UNIVERSE, "swing")) or \
        next((v for (k, m), v in SCANS.items() if m == "swing"), None)
    breadth = None
    if scan and scan["rows"]:
        rows = scan["rows"]
        adv = sum(1 for r in rows if (r["change_pct"] or 0) > 0)
        dec = sum(1 for r in rows if (r["change_pct"] or 0) < 0)
        above50 = sum(1 for r in rows if r["ema50"] and r["price"] and r["price"] > r["ema50"])
        breadth = {"universe": scan["title"], "advancers": adv, "decliners": dec, "count": len(rows),
                   "pct_above_ema50": round(100 * above50 / len(rows), 1), "as_of": scan["finished_ist"]}
    out["breadth"] = breadth
    n = trends.get("NIFTY 50")
    verdict, why = "Unknown", ["NIFTY 50 data unavailable"]
    if n and n["ema20"] and n["ema50"]:
        px = main["NIFTY 50"]["bars"][-1]["c"]
        b = breadth["pct_above_ema50"] if breadth else None
        why = ["NIFTY 50 %.0f vs EMA20 %.0f / EMA50 %.0f" % (px, n["ema20"], n["ema50"])]
        if b is not None:
            why.append("%.0f%% of %s stocks above their EMA 50" % (b, breadth["universe"]))
        if px > n["ema20"] > n["ema50"] and (b is None or b >= 55):
            verdict = "Bullish"
        elif px < n["ema20"] < n["ema50"] and (b is None or b <= 45):
            verdict = "Bearish"
        else:
            verdict = "Sideways"
    vix = next((q for q in out["indices"] if q.get("label") == "INDIA VIX"), None)
    if vix and vix.get("price"):
        why.append("India VIX %.1f (%s)" % (vix["price"], "elevated: expect wider swings" if vix["price"] > 18
                                            else "calm" if vix["price"] < 13 else "normal"))
    out["market_trend"] = {"verdict": verdict, "why": why}
    return out


# ---------------------------------------------------------------- the deep dive

def stock_detail(symbol, mode="swing", with_backtest=True, with_fundamentals=None):
    symbol = symbol.upper().replace(".NS", "")
    settings = db.get_settings()
    meta = universe.stock_meta().get(symbol, {})
    ys = yahoo.to_yahoo(symbol)
    out = {"symbol": symbol, "mode": mode, "meta": meta, "generated_ist": cal.now_ist().isoformat()}
    daily = yahoo.chart(ys, "2y", "1d")
    bench = _bench()
    fund = None
    if with_fundamentals is None:
        with_fundamentals = mode in ("swing", "longterm")
    if with_fundamentals:
        try:
            fund = fundamentals.fetch(symbol)
        except Exception as e:  # noqa: BLE001
            fund = {"error": str(e)}
    fclass = fund.get("classification") if fund and not fund.get("error") else None
    a = engine.analyse_daily(daily, bench, mode="swing" if mode != "intraday" else "intraday",
                             fundamentals_class=fclass if mode != "intraday" else None)
    out["daily"] = engine.strip_private(a)
    s = a.get("_series") or {}
    out["chart_daily"] = {
        "candles": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"]}
                    for b in daily["bars"]],
        "ema20": _line(daily["bars"], a, s.get("ema20")), "ema50": _line(daily["bars"], a, s.get("ema50")),
        "ema200": _line(daily["bars"], a, s.get("ema200")), "vwap20": _line(daily["bars"], a, s.get("vwap20")),
        "rsi": _line(daily["bars"], a, s.get("rsi")), "macd": _line(daily["bars"], a, s.get("macd")),
        "signal": _line(daily["bars"], a, s.get("signal")), "hist": _line(daily["bars"], a, s.get("hist")),
    }
    try:
        weekly = yahoo.chart(ys, "5y", "1wk", ttl=6 * 3600)
        out["chart_weekly"] = [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]}
                               for b in weekly["bars"]]
    except yahoo.DataError:
        out["chart_weekly"] = []
    out["fundamentals"] = fund
    if mode == "intraday":
        ch5 = yahoo.chart(ys, "5d", "5m")
        ia = engine.analyse_intraday(ch5, a, _bench("5d", "5m"))
        out["intraday"] = engine.strip_private(ia)
        S = ia.get("_session") or []
        out["chart_intraday"] = {
            "candles": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
                         "volume": b["v"]} for b in S],
            "vwap": [{"time": b["t"], "value": v} for b, v in zip(S, ia.get("_vwap") or []) if v],
        }
        plan = tradeplan.intraday_plan(ia, settings)
        if ia.get("session_is_today") and ia.get("features", {}).get("trigger_bar"):
            h, m = map(int, ia["features"]["trigger_bar"].split(":"))
            plan["trigger_slot"] = (h * 60 + m - cal.OPEN_MIN) // 5 - 1
        rating_src = ia
    else:
        plan = tradeplan.swing_plan(a, settings)
        rating_src = a
    plan["features_atr"] = (a.get("features") or {}).get("atr")
    bt = None
    if with_backtest and plan.get("entry") and plan.get("stop"):
        try:
            bt = backtest.intraday(symbol, plan, settings) if mode == "intraday" else backtest.swing(symbol, plan,
                                                                                                    settings)
        except Exception as e:  # noqa: BLE001
            bt = {"error": "backtest failed: %s" % str(e)[:150]}
    out["backtest"] = bt
    # A plan needs statistical support too: a negative or tiny history blocks "actionable"
    if plan.get("actionable") and bt and not bt.get("error"):
        h = bt.get("history") if mode == "intraday" else bt
        n = (h or {}).get("occurrences", 0)
        if n < backtest.MIN_OCCURRENCES:
            plan["actionable"] = False
            plan.setdefault("blockers", []).append("only %d historical occurrences of this setup (need %d+)"
                                                   % (n, backtest.MIN_OCCURRENCES))
        elif (h.get("avg_R") or 0) < backtest.MIN_AVG_R:
            plan["actionable"] = False
            plan.setdefault("blockers", []).append(
                "this setup's history on this stock is too weak after costs (avg %+.2fR over %d trades; need >= %+.2fR)"
                % (h.get("avg_R") or 0, n, backtest.MIN_AVG_R))
        if not plan["actionable"]:
            plan["status"] = "WAIT"
            plan["message"] = "Not actionable: " + "; ".join(plan["blockers"]) + "."
    out["plan"] = plan
    out["rating"] = rating_src["rating"]
    try:
        out["news"] = feeds.stock_news(symbol, meta.get("name") or a.get("name"))[:15]
    except Exception as e:  # noqa: BLE001
        out["news"] = []
        out["news_error"] = str(e)
    out["history"] = db.q("SELECT ts, mode, rating, score, confidence, price FROM signals WHERE symbol=? "
                          "ORDER BY ts DESC LIMIT 30", (symbol,))
    return out


def _line(bars, a, series):
    if not series:
        return []
    done = a.get("_bars") or bars
    return [{"time": b["t"], "value": round(v, 4)} for b, v in zip(done, series) if v is not None]


# ---------------------------------------------------------------- the briefing

def briefing():
    ov = overview()
    settings = db.get_settings()
    swing = next((v for (k, m), v in SCANS.items() if m == "swing" and v.get("rows")), None)
    intra = next((v for (k, m), v in SCANS.items() if m == "intraday" and v.get("rows")), None)
    rows = swing["rows"] if swing else []
    act = top(rows, "actionable", 5)
    bull = top(rows, "bullish", 5)
    avoid = top(rows, "bearish", 5)
    mt = ov["market_trend"]
    lines = []
    lines.append("Market: %s. %s." % (mt["verdict"], "; ".join(mt["why"])))
    nifty = ov["trends"].get("NIFTY 50") or {}
    bank = ov["trends"].get("BANK NIFTY") or {}
    levels = []
    for name, t in (("NIFTY 50", nifty), ("BANK NIFTY", bank)):
        if t.get("supports") or t.get("resistances"):
            levels.append({"index": name,
                           "supports": [x["price"] for x in (t.get("supports") or [])[:2]],
                           "resistances": [x["price"] for x in (t.get("resistances") or [])[:2]]})
    news = feeds.latest(hours=24)
    events = [n for n in news if n["impact"] == "high"][:8]
    glob = [q for q in ov["global"] if q.get("change_pct") is not None]
    risks = []
    vix = next((q for q in ov["indices"] if q.get("label") == "INDIA VIX"), {})
    if vix.get("price") and vix["price"] > 18:
        risks.append("India VIX is elevated at %.1f: size down, expect wider stops." % vix["price"])
    for q in glob:
        if q["label"] in ("S&P 500", "NASDAQ") and q["change_pct"] <= -1.5:
            risks.append("%s fell %.1f%% in its last session: weak global cue." % (q["label"], q["change_pct"]))
        if q["label"] == "Brent crude" and abs(q["change_pct"]) >= 3:
            risks.append("Brent crude moved %+.1f%%: watch OMCs, paints, aviation." % q["change_pct"])
    nxt = ov["market"]["next_open"]
    upcoming_holidays = [{"date": d, "name": n} for d, n in sorted(cal.holidays().items())
                         if d >= cal.now_ist().date().isoformat()][:3]
    if not swing:
        decision = "WAIT"
        summary = "The first scan has not finished yet. Data is loading; check back in a minute."
    elif mt["verdict"] == "Bearish" and not act:
        decision = "NO TRADE"
        summary = tradeplan.NO_TRADE
    elif not act:
        decision = "WAIT"
        summary = ("No setup currently passes every check (rating, reward:risk, sizing). It is better to wait "
                   "than to force a trade. " + tradeplan.NO_TRADE)
    elif mt["verdict"] == "Bearish":
        decision = "CAUTION"
        summary = ("The broad market is bearish. %d stock(s) still show valid long setups; if you trade, use smaller "
                   "size and only after confirmation." % len(act))
    else:
        decision = "TRADE SELECTIVELY"
        summary = ("%d setup(s) pass the rules (rating, reward:risk, sizing) and a positive 5-year backtest on the "
                   "same stock. Investigate them; act only after the listed confirmation, with the "
                   "stop in place, and within your limits (max ₹%.0f risk per trade)." % (
                       len(act), settings["capital"] * settings["risk_per_trade_pct"] / 100))
    return {
        "generated_ist": cal.now_ist().strftime("%a %d %b %Y, %H:%M IST"), "market": ov["market"],
        "next_open": nxt, "decision": decision, "summary": summary, "outlook": lines,
        "market_trend": mt, "levels": levels,
        "opportunities": [{"symbol": r["symbol"], "rating": r["rating"], "confidence": r["confidence"],
                           "setup": (r["plan"] or {}).get("setup"), "entry": (r["plan"] or {}).get("entry"),
                           "stop": (r["plan"] or {}).get("stop"), "target1": (r["plan"] or {}).get("target1"),
                           "rr": (r["plan"] or {}).get("reward_risk"),
                           "backtest": (r.get("backtest") or {}).get("why")} for r in (act or bull)],
        "opportunities_are_actionable": bool(act),
        "avoid": [{"symbol": r["symbol"], "rating": r["rating"], "why": r["why"][:1]} for r in avoid],
        "events": events, "risks": risks, "upcoming_holidays": upcoming_holidays,
        "intraday_note": ("Intraday scan from %s." % intra["finished_ist"]) if intra else
        "Intraday scan not run yet (it runs during market hours).",
        "scan_as_of": swing["finished_ist"] if swing else None,
        "method": "Composed from the scan, index trend, breadth, VIX, global cues and the last 24h of high-impact "
                  "news. Rules-based, not a prediction. Not investment advice.",
    }
