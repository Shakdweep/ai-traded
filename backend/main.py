"""FastAPI app: JSON API under /api, the dashboard as static files at /.

Run:  .venv/Scripts/python -m backend   (or: uvicorn backend.main:app)
"""

import asyncio
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, config, db, paper, services
from .analysis import fundamentals
from .market import calendar as cal
from .market import universe, yahoo
from .news import feeds

WEB = config.ROOT / "frontend"
_bg = {"last_scan": None, "last_news": None, "last_intraday": None, "errors": [], "cycles": 0}


def _log_err(where, e):
    _bg["errors"] = (_bg["errors"] + [{"ts": time.time(), "where": where, "error": str(e)[:300]}])[-20:]
    traceback.print_exc()


def _background_loop():
    """Refresh news, scans, alerts and paper positions on a market-aware cadence."""
    next_scan = next_news = next_intra = 0
    while True:
        try:
            now = time.time()
            st = cal.market_status()
            settings = db.get_settings()
            if now >= next_news:
                feeds.refresh()
                alerts.evaluate_news(feeds.latest(hours=24))
                _bg["last_news"] = now
                next_news = now + config.NEWS_EVERY_MIN * 60
            if now >= next_scan:
                res = services.run_scan(config.BACKGROUND_UNIVERSE, "swing")
                alerts.evaluate_scan(res.get("rows", []))
                watch = [r["symbol"] for r in db.q("SELECT symbol FROM watchlist")]
                if watch:
                    wres = services.run_scan("watchlist", "swing", symbols=watch)
                    alerts.evaluate_scan(wres.get("rows", []))
                _bg["last_scan"] = now
                next_scan = now + (config.SCAN_EVERY_MIN_OPEN if st["open"] else config.SCAN_EVERY_MIN_CLOSED) * 60
            if st["open"] and now >= next_intra:
                res = services.run_scan("nifty50", "intraday")
                alerts.evaluate_scan(res.get("rows", []))
                _bg["last_intraday"] = now
                next_intra = now + 5 * 60
            if st["open"] or st["state"] == "closed":
                paper.mark_to_market(settings, notify=lambda s, k, m: alerts.notify(s, k, m, "caution"))
            _bg["cycles"] += 1
        except Exception as e:  # noqa: BLE001 - the loop must survive any single failure
            _log_err("background", e)
        time.sleep(60)


@asynccontextmanager
async def lifespan(app):
    db.db()
    cal.holidays()
    services.load_saved()
    if not config.DISABLE_BACKGROUND:
        threading.Thread(target=_background_loop, daemon=True, name="refresh").start()
    yield


app = FastAPI(title="AI Traded – Indian market intelligence (analysis + paper trading)", lifespan=lifespan)


@app.middleware("http")
async def no_stale_assets(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # revalidate (ETag) so updates always show
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(yahoo.DataError)
async def data_error(_, exc):
    return JSONResponse(status_code=503, content={"error": "Market data unavailable: %s" % exc,
                                                  "data_status": "UNAVAILABLE"})


async def run(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


# ---------------------------------------------------------------- status

@app.get("/api/status")
async def status():
    return {
        "market": cal.market_status(), "server_time_ist": cal.now_ist().isoformat(),
        "providers": {
            "prices": {"name": "Yahoo Finance chart API", "type": "unofficial, free, may be delayed",
                       "label_rule": "DELAYED if < %d min old while open, STALE if older, CLOSED outside hours; "
                                     "never labelled LIVE" % yahoo.STALE_AFTER_MIN, **yahoo.status()},
            "index_lists": {"name": "NSE Indices (niftyindices.com) CSV", "type": "official public files, daily"},
            "fundamentals": {"name": "Screener.in company pages", "type": "unofficial, cached 12h",
                             "enabled": config.ENABLE_SCREENER},
            "news": feeds.status(),
            "brokers": {k: ("configured (not used for data in this version)" if v else "not configured")
                        for k, v in config.public()["brokers"].items()},
        },
        "background": {k: v for k, v in _bg.items()},
        "config": config.public(),
        "scans": [{"universe": k, "mode": m, "finished_ist": v.get("finished_ist"), "finished": v.get("finished"),
                   "restored": bool(v.get("restored_from_disk")), "analysed": v.get("analysed"),
                   "errors": len(v.get("errors", {}))} for (k, m), v in services.SCANS.items()],
        "running": [list(x) for x in services._running],
        "paper_only": True,
    }


# ---------------------------------------------------------------- market

@app.get("/api/overview")
async def overview():
    return await run(services.overview)


@app.get("/api/briefing")
async def briefing():
    return await run(services.briefing)


@app.get("/api/universes")
async def universes():
    return [{"key": k, "name": v[0]} for k, v in universe.UNIVERSES.items()]


@app.get("/api/search")
async def search(q: str = Query("", min_length=1)):
    return await run(universe.search, q)


@app.get("/api/scan")
async def scan(universe_key: str = Query("nifty100", alias="universe"), mode: str = "swing",
               sector: str = None, cap: str = None, min_price: float = None, max_price: float = None,
               min_volume: float = None, setup: str = None, rating: str = None, q: str = None,
               refresh: bool = False):
    if mode not in ("swing", "intraday"):
        raise HTTPException(400, "mode must be swing or intraday")
    if universe_key not in universe.UNIVERSES and universe_key != "watchlist":
        raise HTTPException(400, "unknown universe")
    tag = (universe_key, mode)
    res = services.SCANS.get(tag)
    if universe_key == "watchlist":
        syms = [r["symbol"] for r in db.q("SELECT symbol FROM watchlist")]
        if not syms:
            return {"universe": "watchlist", "mode": mode, "rows": [], "message": "Your watchlist is empty."}
        if refresh or not res:
            res = await run(services.run_scan, "watchlist", mode, syms)
    elif refresh or not res:
        if not services.is_running(universe_key, mode):
            services.scan_async(universe_key, mode)
        if not res:
            return {"universe": universe_key, "mode": mode, "running": True, "rows": [],
                    "message": "Scan started. Large universes take a few minutes (polite rate limits)."}
    rows = services.filter_rows(res["rows"], sector, cap, min_price, max_price, min_volume, setup, rating, q)
    rows = sorted(rows, key=lambda r: (services.ORDER.get(r["rating"], 99), -(r.get("confidence") or 0)))
    sectors = sorted({r["sector"] for r in res["rows"] if r.get("sector")})

    def tops():
        return {k: services.top(res["rows"], k) for k in ("bullish", "bearish", "actionable", "near_breakout",
                                                           "volume")}
    return dict({k: v for k, v in res.items() if k != "rows"}, rows=rows, total=len(res["rows"]), sectors=sectors,
                running=services.is_running(universe_key, mode), top=await run(tops))


@app.get("/api/stock/{symbol}")
async def stock(symbol: str, mode: str = "swing", backtest: bool = True):
    if mode not in ("swing", "intraday", "longterm"):
        raise HTTPException(400, "bad mode")
    try:
        return await run(services.stock_detail, symbol, mode, backtest)
    except yahoo.DataError as e:
        raise HTTPException(503, "No market data for %s: %s" % (symbol, e))


@app.get("/api/chart/{symbol}")
async def chart(symbol: str, interval: str = "5m"):
    """Candles for the chart switcher: 1m / 5m / 15m (latest session, with VWAP), 1d, 1wk."""
    from .analysis import engine, indicators as ind
    ranges = {"1m": "1d", "5m": "5d", "15m": "5d", "1d": "2y", "1wk": "5y"}
    if interval not in ranges:
        raise HTTPException(400, "interval must be one of %s" % ", ".join(ranges))
    ch = await run(yahoo.chart, yahoo.to_yahoo(symbol), ranges[interval], interval)
    bars = ch["bars"]
    out = {"interval": interval, "freshness": yahoo.quote_from_chart(ch)["freshness"]}
    if interval in ("1m", "5m", "15m"):
        day, bars = engine.session_of(bars)
        out["session"] = day.isoformat() if day else None
        out["vwap"] = [{"time": b["t"], "value": v} for b, v in zip(bars, ind.vwap_session(bars)) if v]
    out["candles"] = [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
                       "volume": b["v"]} for b in bars]
    return out


@app.get("/api/fundamentals/{symbol}")
async def fund(symbol: str):
    try:
        return await run(fundamentals.fetch, symbol)
    except fundamentals.FundamentalsError as e:
        raise HTTPException(503, str(e))


@app.get("/api/longterm")
async def longterm():
    """Fundamentals for the watchlist (long-term investing view)."""
    syms = [r["symbol"] for r in db.q("SELECT symbol FROM watchlist")]
    out = []
    for s in syms:
        try:
            d = await run(fundamentals.fetch, s)
            out.append({k: d.get(k) for k in ("symbol", "name", "classification", "valuation", "valuation_note", "pe",
                                              "pb", "roe", "roce", "debt_to_equity", "sales_growth_3y",
                                              "profit_growth_3y", "market_cap_cr", "promoter_pledge_pct",
                                              "qtr_profit_yoy", "is_financial")})
            out[-1]["promoters"] = (d.get("shareholding") or {}).get("promoters")
        except fundamentals.FundamentalsError as e:
            out.append({"symbol": s, "error": str(e)})
    return {"rows": out, "note": "Long-term view uses fundamentals only; it is separate from trading signals."}


@app.get("/api/news")
async def news(symbol: str = None, kind: str = None, hours: int = 72):
    items = feeds.latest(hours=min(hours, 168), symbol=symbol, kind=kind)
    return {"items": items, "status": feeds.status()}


# ---------------------------------------------------------------- watchlist & alerts

@app.get("/api/watchlist")
async def watchlist():
    rows = db.q("SELECT * FROM watchlist ORDER BY added_at DESC")
    scan = {r["symbol"]: r for r in (services.SCANS.get(("watchlist", "swing")) or {}).get("rows", [])}
    for r in rows:
        r["analysis"] = scan.get(r["symbol"])
    return rows


@app.post("/api/watchlist")
async def watch_add(body: dict = Body(...)):
    sym = (body.get("symbol") or "").upper().strip().replace(".NS", "")
    if not sym:
        raise HTTPException(400, "symbol required")
    try:
        await run(yahoo.chart, yahoo.to_yahoo(sym), "5d", "1d")
    except yahoo.DataError:
        raise HTTPException(404, "No market data for %s on NSE" % sym)
    db.x("INSERT OR REPLACE INTO watchlist(symbol, added_at, note, mode) VALUES(?,?,?,?)",
         (sym, time.time(), body.get("note", ""), body.get("mode", "swing")))
    syms = [r["symbol"] for r in db.q("SELECT symbol FROM watchlist")]
    threading.Thread(target=services.run_scan, args=("watchlist", "swing", syms), daemon=True).start()
    return {"ok": True}


@app.delete("/api/watchlist/{symbol}")
async def watch_del(symbol: str):
    db.x("DELETE FROM watchlist WHERE symbol=?", (symbol.upper(),))
    return {"ok": True}


@app.get("/api/alerts")
async def get_alerts(unseen: bool = False):
    return {"alerts": alerts.recent(100, unseen), "telegram": config.public()["telegram_configured"]}


@app.post("/api/alerts/seen")
async def alerts_seen(body: dict = Body(default={})):
    return alerts.mark_seen(body.get("ids"))


@app.post("/api/alerts/test-telegram")
async def test_telegram():
    ok, msg = await run(alerts.telegram, "AI Traded: test notification. Alerts are working.")
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- settings & paper trading

@app.get("/api/settings")
async def get_settings():
    return db.get_settings()


@app.put("/api/settings")
async def put_settings(body: dict = Body(...)):
    clean = {}
    num = {"capital": (1000, 1e10), "cash": (0, 1e10), "risk_per_trade_pct": (0.1, 5), "max_daily_loss_pct": (0.1, 10),
           "max_trades_per_day": (1, 50), "max_position_pct": (1, 100), "min_reward_risk": (0.5, 10),
           "slippage_pct": (0, 2), "brokerage_per_order": (0, 100)}
    for k, (lo, hi) in num.items():
        if k in body:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(400, "%s must be a number" % k)
            if not lo <= v <= hi:
                raise HTTPException(400, "%s must be between %g and %g" % (k, lo, hi))
            clean[k] = int(v) if k == "max_trades_per_day" else v
    if "style" in body and body["style"] in ("intraday", "swing", "longterm"):
        clean["style"] = body["style"]
    for k in ("intraday_no_entry_before", "intraday_no_entry_after", "intraday_flat_by"):
        if k in body:
            try:
                cal.hhmm(body[k])
                clean[k] = body[k]
            except (ValueError, AttributeError):
                raise HTTPException(400, "%s must be HH:MM" % k)
    for k in ("allow_short", "allow_margin", "allow_options"):
        if body.get(k):
            raise HTTPException(400, "%s cannot be enabled: this platform is long-only cash equity, paper only" % k)
    return db.save_settings(clean)


@app.post("/api/size")
async def size(body: dict = Body(...)):
    from .analysis import risk as rk
    try:
        e, s = float(body["entry"]), float(body["stop"])
        t = float(body["target"]) if body.get("target") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "entry and stop are required numbers")
    return rk.size_position(e, s, db.get_settings(), target=t, product=body.get("product", "delivery"))


@app.post("/api/paper/propose")
async def paper_propose(body: dict = Body(...)):
    sym, mode = body.get("symbol"), body.get("mode", "swing")
    if not sym:
        raise HTTPException(400, "symbol required")
    d = await run(services.stock_detail, sym, mode, True)
    return paper.propose(d["plan"], db.get_settings())


@app.post("/api/paper/confirm")
async def paper_confirm(body: dict = Body(...)):
    return await run(paper.confirm, int(body.get("order_id", 0)), body.get("reply", ""), db.get_settings())


@app.post("/api/paper/decline")
async def paper_decline(body: dict = Body(...)):
    return paper.decline(int(body.get("order_id", 0)))


@app.post("/api/paper/close")
async def paper_close(body: dict = Body(...)):
    return await run(paper.close_manual, int(body.get("position_id", 0)), db.get_settings())


@app.get("/api/paper")
async def paper_summary():
    return await run(paper.summary, db.get_settings())


@app.post("/api/journal")
async def journal(body: dict = Body(...)):
    if not body.get("text"):
        raise HTTPException(400, "text required")
    db.x("INSERT INTO journal(ts, symbol, text) VALUES(?,?,?)", (time.time(), body.get("symbol"), body["text"][:2000]))
    return {"ok": True}


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(WEB) / "index.html")
