"""Official end-of-day market data for research.

Sources (public files, plain downloads, no logins or cookies):
  * NSE "full bhavcopy" sec_bhavdata_full_DDMMYYYY.csv: official open/high/low/close,
    volume and DELIVERY % for every NSE stock, one file per trading day.
  * NSE bulk deals (bulk.csv, latest day only).
  * Yahoo Finance daily bars for indices (NIFTY 50, BANK NIFTY, sector indices), labelled.

Split/bonus adjustment: on an ex-date NSE reports PREV_CLOSE already adjusted, so
PREV_CLOSE / (our stored previous close) is the adjustment ratio; history before that date
is multiplied by it when a series is read. Nothing is ever invented: a missing day stays
missing and is reported by the quality check.
"""

import csv
import datetime
import io
import time
import urllib.error
import urllib.request

from .. import db
from ..market import calendar as cal
from ..market import universe, yahoo

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_%s.csv"
BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
PAUSE = 0.4                      # polite gap between downloads
SERIES_OK = {"EQ", "BE", "BZ"}   # equity series kept (BE/BZ = trade-for-trade segments)
ADJ_THRESHOLD = 0.015            # a reported prev-close differing by more than 1.5% = corporate action
INDEX_SYMBOLS = {"^NSEI": "NIFTY 50", "^NSEBANK": "BANK NIFTY", **{v: k for k, v in yahoo.INDEX_SYMBOLS.items()}}

SOURCE_BHAV = "NSE bhavcopy (official end-of-day)"
SOURCE_YAHOO = "Yahoo Finance daily (unofficial)"


class DataUnavailable(Exception):
    pass


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _num(x):
    x = (x or "").strip().replace(",", "")
    if x in ("", "-"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


# ---------------------------------------------------------------- runs log

def log_run(job, status, rows=0, message="", started=None):
    db.x("INSERT INTO data_runs(job, started_at, finished_at, status, rows, message) VALUES(?,?,?,?,?,?)",
         (job, started or time.time(), time.time(), status, rows, (message or "")[:500]))


def last_runs(limit=20):
    return db.q("SELECT * FROM data_runs ORDER BY finished_at DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- bhavcopy

def parse_bhavcopy(raw, keep=None):
    """CSV bytes -> list of row dicts for equity series (optionally only symbols in `keep`)."""
    rows = []
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")))
    for r in reader:
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        sym, series = r.get("SYMBOL"), r.get("SERIES")
        if not sym or series not in SERIES_OK or (keep is not None and sym not in keep):
            continue
        try:
            d = datetime.datetime.strptime(r["DATE1"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        close = _num(r.get("CLOSE_PRICE"))
        if not close:
            continue
        rows.append({"symbol": sym, "date": d.isoformat(), "open": _num(r.get("OPEN_PRICE")),
                     "high": _num(r.get("HIGH_PRICE")), "low": _num(r.get("LOW_PRICE")), "close": close,
                     "prev_close": _num(r.get("PREV_CLOSE")), "volume": _num(r.get("TTL_TRD_QNTY")),
                     "deliv_qty": _num(r.get("DELIV_QTY")), "deliv_pct": _num(r.get("DELIV_PER")),
                     "source": SOURCE_BHAV})
    return rows


def fetch_bhavcopy(day, keep=None):
    """One trading day's official file. Raises DataUnavailable if NSE has no file (holiday / not yet out)."""
    try:
        raw = _get(BHAV_URL % day.strftime("%d%m%Y"))
    except urllib.error.HTTPError as e:
        raise DataUnavailable("NSE bhavcopy for %s: HTTP %d" % (day, e.code)) from None
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable("NSE bhavcopy for %s: %s" % (day, type(e).__name__)) from None
    rows = parse_bhavcopy(raw, keep)
    if not rows:
        raise DataUnavailable("NSE bhavcopy for %s had no usable rows" % day)
    return rows


def save_prices(rows):
    db.xmany("INSERT INTO daily_prices(symbol, date, open, high, low, close, prev_close, volume, deliv_qty, "
             "deliv_pct, source) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, date) DO NOTHING",
             [(r["symbol"], r["date"], r["open"], r["high"], r["low"], r["close"], r.get("prev_close"),
               r.get("volume"), r.get("deliv_qty"), r.get("deliv_pct"), r["source"]) for r in rows])
    return len(rows)


def loaded_dates(source=SOURCE_BHAV):
    return {r["date"] for r in db.q("SELECT DISTINCT date FROM daily_prices WHERE source=?", (source,))}


def research_universe():
    """NIFTY 500 symbols (today's list: historical membership isn't freely available -> survivorship bias)."""
    return {s["symbol"]: s for s in universe.constituents("nifty500")["stocks"]}


def prev_trading_day(d):
    d -= datetime.timedelta(days=1)
    while not cal.is_trading_day(d):
        d -= datetime.timedelta(days=1)
    return d


def trading_days(start, end):
    d, out = start, []
    while d <= end:
        if cal.is_trading_day(d):
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def load_bhavcopy_range(start, end, keep=None, on_progress=None):
    """Download every missing trading day in [start, end]. Returns (days loaded, days unavailable)."""
    have = loaded_dates()
    loaded, missing = 0, []
    days = [d for d in trading_days(start, end) if d.isoformat() not in have]
    for i, d in enumerate(days):
        try:
            save_prices(fetch_bhavcopy(d, keep))
            loaded += 1
        except DataUnavailable as e:
            missing.append(str(e))
        if on_progress:
            on_progress(i + 1, len(days))
        time.sleep(PAUSE)
    return loaded, missing


def load_indices(rng="2y"):
    """Daily index bars from Yahoo (NSE's own index files need a browser session)."""
    rows, errors = [], []
    for ysym in INDEX_SYMBOLS:
        try:
            ch = yahoo.chart(ysym, rng, "1d", ttl=600)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (ysym, e))
            continue
        bars, _ = _completed(ch["bars"])
        for b in bars:
            rows.append({"symbol": ysym, "date": cal.to_ist(b["t"]).date().isoformat(), "open": b["o"],
                         "high": b["h"], "low": b["l"], "close": b["c"], "volume": b.get("v"),
                         "source": SOURCE_YAHOO})
    if rows:
        # index bars can be revised by Yahoo during the day: replace rather than keep the first copy
        db.xmany("INSERT INTO daily_prices(symbol, date, open, high, low, close, volume, source) "
                 "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET open=excluded.open, "
                 "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
                 [(r["symbol"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["source"])
                  for r in rows])
    return len(rows), errors


def _completed(bars):
    from ..analysis.engine import completed_daily
    return completed_daily(bars)


def load_bulk_deals():
    try:
        raw = _get(BULK_URL)
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable("NSE bulk deals: %s" % type(e).__name__) from None
    rows = []
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace"))):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        try:
            d = datetime.datetime.strptime(r["Date"], "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        if not r.get("Symbol") or not r.get("Client Name"):
            continue
        rows.append((d, r["Symbol"], r["Client Name"], r.get("Buy/Sell") or "-",
                     _num(r.get("Quantity Traded")) or 0, _num(r.get("Trade Price / Wght. Avg. Price")) or 0))
    db.xmany("INSERT INTO bulk_deals(date, symbol, client, side, qty, price) VALUES(?,?,?,?,?,?) "
             "ON CONFLICT DO NOTHING", rows)
    return len(rows)


# ---------------------------------------------------------------- reading series

def series(symbol, upto=None, limit=None):
    """Adjusted daily bars for one symbol, oldest first, only dates <= upto (no look-ahead).
    -> list of {date, t, o, h, l, c, v, deliv_pct}."""
    sql = "SELECT date, open, high, low, close, prev_close, volume, deliv_pct FROM daily_prices WHERE symbol=?"
    args = [symbol]
    if upto:
        sql += " AND date <= ?"
        args.append(str(upto))
    rows = db.q(sql + " ORDER BY date", tuple(args))
    return adjust(rows)[-limit:] if limit else adjust(rows)


def adjust(rows):
    """Back-adjust prices for splits/bonuses using NSE's adjusted PREV_CLOSE on each ex-date."""
    factor = 1.0
    out = []
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        d = datetime.date.fromisoformat(r["date"])
        t = int(datetime.datetime.combine(d, datetime.time(15, 30), cal.IST).timestamp())
        out.append({"date": r["date"], "t": t,
                    "o": (r["open"] or r["close"]) * factor, "h": (r["high"] or r["close"]) * factor,
                    "l": (r["low"] or r["close"]) * factor, "c": r["close"] * factor,
                    "v": (r["volume"] or 0) / factor if factor else r["volume"], "deliv_pct": r.get("deliv_pct")})
        # only compare against the row of the PREVIOUS trading day: across a data gap the
        # difference would be a normal price move, not a corporate action
        if i > 0 and r.get("prev_close") and rows[i - 1]["close"] and \
                rows[i - 1]["date"] == prev_trading_day(d).isoformat():
            ratio = r["prev_close"] / rows[i - 1]["close"]
            if abs(ratio - 1) > ADJ_THRESHOLD:
                factor *= ratio
    out.reverse()
    return out


def latest_date(symbol="^NSEI"):
    row = db.q("SELECT MAX(date) AS d FROM daily_prices WHERE symbol=?", (symbol,), one=True)
    return row["d"] if row else None


def latest_equity_date():
    row = db.q("SELECT MAX(date) AS d FROM daily_prices WHERE source=?", (SOURCE_BHAV,), one=True)
    return row["d"] if row else None


# ---------------------------------------------------------------- quality

def quality_report(symbols=None, days=60):
    """Gaps, stale symbols and suspicious jumps in the last `days` trading days."""
    last = latest_equity_date()
    if not last:
        return {"ok": False, "message": "No official end-of-day data loaded yet.", "issues": []}
    last_d = datetime.date.fromisoformat(last)
    expected = [d.isoformat() for d in trading_days(last_d - datetime.timedelta(days=int(days * 1.6)), last_d)][-days:]
    have = loaded_dates()
    missing_days = [d for d in expected if d not in have]
    issues = []
    if missing_days:
        issues.append({"kind": "missing_days", "detail": "%d trading day(s) without an NSE file: %s" % (
            len(missing_days), ", ".join(missing_days[-5:]))})
    stale = db.q("SELECT symbol, MAX(date) AS d FROM daily_prices WHERE source=? GROUP BY symbol HAVING MAX(date) < ?",
                 (SOURCE_BHAV, expected[max(0, len(expected) - 5)]))
    if symbols is not None:
        stale = [s for s in stale if s["symbol"] in symbols]
    if stale:
        issues.append({"kind": "stale_symbols", "detail": "%d stock(s) not traded / missing in the last 5 sessions: %s"
                       % (len(stale), ", ".join(s["symbol"] for s in stale[:8]))})
    return {"ok": not issues, "latest": last, "days_checked": len(expected), "issues": issues}


def coverage():
    eq = db.q("SELECT COUNT(DISTINCT symbol) AS s, COUNT(DISTINCT date) AS d, MIN(date) AS a, MAX(date) AS b "
              "FROM daily_prices WHERE source=?", (SOURCE_BHAV,), one=True) or {}
    ix = db.q("SELECT COUNT(DISTINCT symbol) AS s, MAX(date) AS b FROM daily_prices WHERE source=?",
              (SOURCE_YAHOO,), one=True) or {}
    return {"equities": eq.get("s") or 0, "days": eq.get("d") or 0, "from": eq.get("a"), "to": eq.get("b"),
            "indices": ix.get("s") or 0, "indices_to": ix.get("b"), "sources": [SOURCE_BHAV, SOURCE_YAHOO]}
