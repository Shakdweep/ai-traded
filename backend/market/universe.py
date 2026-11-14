"""Index constituents from NSE Indices Ltd (niftyindices.com) public CSVs, cached
daily in data/universe/. Replaces the skill's Nasdaq Trader US symbol directory.
Also the full NSE equity list (EQUITY_L.csv) for searching individual stocks."""

import csv
import io
import time
import urllib.request

from ..config import DATA_DIR

UA = "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"
BASE = "https://niftyindices.com/IndexConstituent/%s.csv"
EQUITY_L = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
CACHE = DATA_DIR / "universe"
CACHE.mkdir(parents=True, exist_ok=True)
MAX_AGE = 24 * 3600

UNIVERSES = {
    "nifty50": ("NIFTY 50", "ind_nifty50list"),
    "niftynext50": ("NIFTY NEXT 50", "ind_niftynext50list"),
    "nifty100": ("NIFTY 100", "ind_nifty100list"),
    "nifty200": ("NIFTY 200", "ind_nifty200list"),
    "nifty500": ("NIFTY 500", "ind_nifty500list"),
    "niftybank": ("BANK NIFTY", "ind_niftybanklist"),
    "midcap100": ("NIFTY MIDCAP 100", "ind_niftymidcap100list"),
    "midcap150": ("NIFTY MIDCAP 150", "ind_niftymidcap150list"),
    "smallcap100": ("NIFTY SMALLCAP 100", "ind_niftysmallcap100list"),
    "smallcap250": ("NIFTY SMALLCAP 250", "ind_niftysmallcap250list"),
}


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    dest.write_bytes(data)
    return data


def _cached(name, url):
    dest = CACHE / (name + ".csv")
    if dest.exists() and time.time() - dest.stat().st_mtime < MAX_AGE:
        return dest.read_bytes(), dest.stat().st_mtime
    try:
        return _download(url, dest), time.time()
    except Exception:
        if dest.exists():  # stale list is better than none, and its age is reported
            return dest.read_bytes(), dest.stat().st_mtime
        raise


def constituents(key):
    """-> {"key", "name", "as_of", "stocks": [{symbol, name, sector, isin}]}"""
    if key not in UNIVERSES:
        raise KeyError("unknown universe %r" % key)
    title, fname = UNIVERSES[key]
    data, mtime = _cached(fname, BASE % fname)
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace")))
    stocks = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("Symbol"):
            stocks.append({"symbol": r["Symbol"], "name": r.get("Company Name", ""),
                           "sector": r.get("Industry", ""), "isin": r.get("ISIN Code", "")})
    return {"key": key, "name": title, "as_of": mtime, "stocks": stocks}


_meta_cache = {"t": 0, "map": {}}


def stock_meta():
    """symbol -> {name, sector, cap_bucket} from the broad index lists (cached)."""
    if time.time() - _meta_cache["t"] < 3600 and _meta_cache["map"]:
        return _meta_cache["map"]
    out = {}
    for key, bucket in (("smallcap250", "Smallcap"), ("midcap150", "Midcap"), ("nifty500", None),
                        ("nifty100", "Largecap")):
        try:
            for s in constituents(key)["stocks"]:
                cur = out.setdefault(s["symbol"], {"name": s["name"], "sector": s["sector"], "cap": None})
                if bucket:
                    cur["cap"] = bucket
        except Exception:
            continue
    _meta_cache.update(t=time.time(), map=out)
    return out


def all_equities():
    data, _ = _cached("EQUITY_L", EQUITY_L)
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace")))
    out = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") in ("EQ", "BE"):
            out.append({"symbol": r["SYMBOL"], "name": r.get("NAME OF COMPANY", "")})
    return out


def search(qtext, limit=15):
    qtext = qtext.strip().upper()
    if not qtext:
        return []
    meta = stock_meta()
    try:
        eq = all_equities()
    except Exception:
        eq = [{"symbol": k, "name": v["name"]} for k, v in meta.items()]
    starts = [e for e in eq if e["symbol"].startswith(qtext)]
    contains = [e for e in eq if e not in starts and (qtext in e["symbol"] or qtext in e["name"].upper())]
    res = (starts + contains)[:limit]
    for r in res:
        m = meta.get(r["symbol"], {})
        r["sector"] = m.get("sector", "")
        r["cap"] = m.get("cap")
    return res
