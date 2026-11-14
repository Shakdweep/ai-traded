"""Yahoo Finance chart client for NSE (.NS) and BSE (.BO) symbols and indices.

Same endpoints the day-trader skills use (v8 chart, v7 spark), with the same
politeness rules (host fallback, one backoff on 429, small pause), plus:
  * an in-memory TTL cache so the dashboard does not hammer Yahoo,
  * a freshness label on every quote. Yahoo's feed is UNOFFICIAL and is never
    labelled LIVE here: it is DELAYED (fresh enough), STALE (too old while the
    market is open), CLOSED (last close) or UNAVAILABLE.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..config import FETCH_WORKERS
from . import calendar as cal

UA = "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"
HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
PAUSE = 0.15
BACKOFF = 3.0
STALE_AFTER_MIN = 20

INDEX_SYMBOLS = {
    "NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK", "SENSEX": "^BSESN", "INDIA VIX": "^INDIAVIX",
    "NIFTY IT": "^CNXIT", "NIFTY MIDCAP 50": "^NSEMDCP50", "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA", "NIFTY FMCG": "^CNXFMCG", "NIFTY METAL": "^CNXMETAL",
    "NIFTY REALTY": "^CNXREALTY", "NIFTY ENERGY": "^CNXENERGY", "NIFTY PSU BANK": "^CNXPSUBANK",
}
GLOBAL_SYMBOLS = {
    "S&P 500": "^GSPC", "NASDAQ": "^IXIC", "Nikkei 225": "^N225", "Hang Seng": "^HSI",
    "Brent crude": "BZ=F", "Gold": "GC=F", "USD/INR": "USDINR=X", "US 10Y yield": "^TNX",
}

_cache = {}
_cache_lock = threading.Lock()
_errors = {}  # last error per symbol, for the data-status page
_stats = {"requests": 0, "failures": 0, "last_success": None, "last_failure": None}


class DataError(Exception):
    pass


def to_yahoo(symbol, exchange="NSE"):
    s = symbol.strip().upper()
    if s.startswith("^") or "=" in s or s.endswith(".NS") or s.endswith(".BO"):
        return s
    if exchange.upper() == "BSE" or s.isdigit():
        return s + ".BO"
    return s + ".NS"


def plain(ysym):
    return ysym.replace(".NS", "").replace(".BO", "")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _fetch(path):
    last = None
    for attempt, host in enumerate(HOSTS * 2):
        url = "https://%s%s" % (host, path)
        _stats["requests"] += 1
        try:
            text = _get(url)
            _stats["last_success"] = time.time()
            return json.loads(text)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code == 404:
                break
            if e.code == 429:
                time.sleep(BACKOFF)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = str(e)[:120]
        time.sleep(PAUSE)
    _stats["failures"] += 1
    _stats["last_failure"] = time.time()
    raise DataError(last or "no response")


def _ttl_for(interval):
    status = cal.market_status()
    if not status["open"]:
        return 1800
    return {"1m": 45, "5m": 90, "15m": 180}.get(interval, 600)


def chart(ysym, rng="1y", interval="1d", ttl=None):
    """-> {"symbol", "meta", "bars": [{t,o,h,l,c,v}], "fetched": ts}. Null bars dropped."""
    key = (ysym, rng, interval)
    ttl = _ttl_for(interval) if ttl is None else ttl
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit["fetched"] < ttl:
        return hit
    path = "/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (
        urllib.parse.quote(ysym), rng, interval)
    try:
        doc = _fetch(path)
        res = (doc.get("chart") or {}).get("result")
        if not res:
            err = (doc.get("chart") or {}).get("error") or {}
            raise DataError(err.get("description") or "no data for %s" % ysym)
        res = res[0]
        q = (res.get("indicators") or {}).get("quote") or [{}]
        q = q[0]
        bars = []
        for i, t in enumerate(res.get("timestamp") or []):
            o, h, l, c, v = (q.get(k, [None])[i] if i < len(q.get(k) or []) else None
                             for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
        meta = res.get("meta") or {}
        if interval == "1d":
            _patch_latest_session(bars, meta)
        out = {"symbol": ysym, "meta": meta, "bars": bars, "fetched": time.time(),
               "range": rng, "interval": interval}
        with _cache_lock:
            _cache[key] = out
        _errors.pop(ysym, None)
        return out
    except DataError as e:
        _errors[ysym] = {"error": str(e), "ts": time.time()}
        if hit:  # serve the older copy, but say how old it is
            return dict(hit, served_from_cache=True)
        raise


def xdate(ts, meta):
    """Calendar date of a timestamp in the instrument's own exchange time zone."""
    import datetime
    return datetime.datetime.fromtimestamp(ts + int(meta.get("gmtoffset") or 0), datetime.timezone.utc).date()


def _patch_latest_session(bars, meta):
    """Yahoo's daily series for NSE stocks often lags a session behind the quote.
    When the quote is from a later session than the last daily bar, append that
    session built from the quote (high, low, last, volume; open unknown, so the
    previous close stands in) and mark it synthetic."""
    t, px = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if not (t and px and bars):
        return
    last = bars[-1]
    if xdate(t, meta) > xdate(last["t"], meta):
        hi = meta.get("regularMarketDayHigh") or max(px, last["c"])
        lo = meta.get("regularMarketDayLow") or min(px, last["c"])
        bars.append({"t": t - (t + int(meta.get("gmtoffset") or 0)) % 86400 + 9 * 3600 + 15 * 60
                     if meta.get("exchangeTimezoneName") == "Asia/Kolkata" else t,
                     "o": last["c"], "h": max(hi, px), "l": min(lo, px), "c": px,
                     "v": meta.get("regularMarketVolume") or 0, "synthetic": True})


def charts(ysyms, rng="1y", interval="1d", workers=FETCH_WORKERS):
    """Fetch many charts politely in a small thread pool. -> ({sym: chart}, {sym: error})."""
    ok, bad = {}, {}

    def one(s):
        try:
            ok[s] = chart(s, rng, interval)
        except Exception as e:  # noqa: BLE001 - every failure is reported, none is fatal
            bad[s] = str(e)
        time.sleep(PAUSE)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, ysyms))
    return ok, bad


def freshness(meta, fetched=None):
    """Label how current a quote is. Never 'LIVE' for this unofficial source."""
    t = meta.get("regularMarketTime")
    if not t:
        return {"label": "UNAVAILABLE", "age_min": None, "as_of": None, "source": "Yahoo Finance (unofficial)"}
    age = (time.time() - t) / 60.0
    status = cal.market_status()
    if status["open"]:
        label = "DELAYED" if age <= STALE_AFTER_MIN else "STALE"
    else:
        label = "CLOSED"
    return {"label": label, "age_min": round(age, 1), "as_of": cal.to_ist(t).isoformat(),
            "source": "Yahoo Finance (unofficial, may be delayed)",
            "fetched": cal.to_ist(fetched).isoformat() if fetched else None}


def quote_from_chart(ch):
    m = ch["meta"]
    bars = ch["bars"]
    price = m.get("regularMarketPrice") or (bars[-1]["c"] if bars else None)
    prev = m.get("previousClose") or m.get("chartPreviousClose")
    if bars and m.get("regularMarketTime"):
        # previous close = the last bar from an EARLIER session than the quote,
        # with dates taken in the exchange's own time zone
        today = xdate(m["regularMarketTime"], m)
        earlier = [b for b in bars if xdate(b["t"], m) < today]
        if earlier:
            prev = earlier[-1]["c"]
        elif ch.get("interval") != "1d":
            prev = m.get("chartPreviousClose") if ch.get("range") == "1d" else prev
    chg = (price - prev) if (price is not None and prev) else None
    return {
        "symbol": plain(ch["symbol"]), "ysymbol": ch["symbol"], "name": m.get("longName") or m.get("shortName"),
        "price": price, "prev_close": prev, "change": chg,
        "change_pct": (chg / prev * 100) if chg is not None and prev else None,
        "day_high": m.get("regularMarketDayHigh"), "day_low": m.get("regularMarketDayLow"),
        "volume": m.get("regularMarketVolume"), "high52": m.get("fiftyTwoWeekHigh"),
        "low52": m.get("fiftyTwoWeekLow"), "currency": m.get("currency"),
        "exchange": m.get("fullExchangeName"), "freshness": freshness(m, ch.get("fetched")),
    }


def status():
    return {"stats": dict(_stats), "recent_errors": dict(list(_errors.items())[-20:]),
            "cached_series": len(_cache)}
