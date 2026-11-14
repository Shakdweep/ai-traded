"""Live smoke test against a running server and real data sources.
Start the app first, then:  .venv/Scripts/python tests/smoke_live.py [http://127.0.0.1:8000]
It only reads, except one watchlist add/remove of a test symbol. No paper orders."""

import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def call(path, method="GET", body=None, timeout=300):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(name, fn):
    t = time.time()
    try:
        msg = fn()
        print("PASS  %-28s %5.1fs  %s" % (name, time.time() - t, msg or ""))
        return True
    except Exception as e:  # noqa: BLE001
        print("FAIL  %-28s %5.1fs  %s" % (name, time.time() - t, e))
        return False


def status():
    s = call("/api/status")
    assert s["paper_only"] is True
    return s["market"]["label"]


def overview():
    o = call("/api/overview")
    n = next(q for q in o["indices"] if q["label"] == "NIFTY 50")
    assert n["price"] and n["freshness"]["label"] != "LIVE"
    return "NIFTY 50 %.2f (%s), trend %s" % (n["price"], n["freshness"]["label"], o["market_trend"]["verdict"])


def scan():
    s = call("/api/scan?universe=nifty50&mode=swing")
    for _ in range(60):
        if s.get("rows"):
            break
        time.sleep(3)
        s = call("/api/scan?universe=nifty50&mode=swing")
    assert len(s["rows"]) >= 45, len(s["rows"])
    return "%d rows, %d actionable after backtest" % (len(s["rows"]), len(s["top"]["actionable"]))


def stock():
    d = call("/api/stock/RELIANCE?mode=swing")
    assert d["daily"]["rating"] and d["plan"]
    return "RELIANCE %s, plan %s" % (d["daily"]["rating"], d["plan"]["status"])


def intraday():
    d = call("/api/stock/INFY?mode=intraday")
    mc = (d.get("backtest") or {}).get("monte_carlo") or {}
    return "INFY %s, time machine %s" % (d["intraday"]["rating"], "ok" if mc.get("replays") else "n/a")


def news():
    n = call("/api/news?hours=72")
    assert all(i["age_hours"] <= 72 for i in n["items"])
    ok = sum(1 for f in n["status"]["feeds"] if not f["error"])
    return "%d items, %d/%d feeds OK" % (len(n["items"]), ok, len(n["status"]["feeds"]))


def briefing():
    b = call("/api/briefing")
    return "%s: %s" % (b["decision"], b["summary"][:80])


def watch():
    call("/api/watchlist", "POST", {"symbol": "SBIN"})
    assert any(w["symbol"] == "SBIN" for w in call("/api/watchlist"))
    call("/api/watchlist/SBIN", "DELETE")
    return "add/remove ok"


def guards():
    try:
        call("/api/settings", "PUT", {"allow_margin": True})
        raise AssertionError("margin could be enabled")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    return "margin/short/options cannot be enabled"


if __name__ == "__main__":
    import urllib.error  # noqa: F401
    results = [check(n, f) for n, f in [("status", status), ("overview", overview), ("scan nifty50", scan),
                                         ("stock swing", stock), ("stock intraday", intraday), ("news", news),
                                         ("briefing", briefing), ("watchlist", watch), ("safety guards", guards)]]
    print("\n%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)
