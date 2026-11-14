"""News and announcements monitor for Indian markets (replaces the trader-news
skill's Yahoo-US RSS + SEC 8-K sources, keeping its approach: tag every item,
dedupe, newest first, never repeat, and only show what is recent).

Sources (all public RSS, no key):
  NSE corporate announcements  nsearchives.nseindia.com/content/RSS/Online_announcements.xml
  NSE financial results        nsearchives.nseindia.com/content/RSS/Financial_Results.xml
  SEBI                         www.sebi.gov.in/sebirss.xml
  RBI press releases           www.rbi.org.in/pressreleases_rss.xml
  Economic Times markets, Business Standard markets, Mint markets RSS
  Google News search RSS       per stock, for the watchlist / detail page
BSE announcements: BSE's announcement API refuses non-browser clients, so it is
NOT used; the UI says so instead of pretending.

Sentiment is a transparent keyword lexicon (positive / negative / neutral), not
language understanding; each item shows the words that decided it.
"""

import email.utils
import datetime
import hashlib
import html
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..market import calendar as cal

UA = "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"
MAX_AGE_H = 72

FEEDS = [
    {"id": "nse_ann", "name": "NSE announcements", "kind": "exchange",
     "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"id": "nse_results", "name": "NSE financial results", "kind": "results",
     "url": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"},
    {"id": "sebi", "name": "SEBI", "kind": "regulator", "url": "https://www.sebi.gov.in/sebirss.xml"},
    {"id": "rbi", "name": "RBI press releases", "kind": "regulator",
     "url": "https://www.rbi.org.in/pressreleases_rss.xml"},
    {"id": "et", "name": "Economic Times – Markets", "kind": "media",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"id": "bs", "name": "Business Standard – Markets", "kind": "media",
     "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"id": "mint", "name": "Mint – Markets", "kind": "media", "url": "https://www.livemint.com/rss/markets"},
]
UNAVAILABLE = [{"name": "BSE announcements", "why": "BSE's API blocks non-browser clients; not used."}]

POS = ["beats", "beat estimates", "surge", "surges", "soar", "jumps", "rallies", "rally", "record high", "upgrade",
       "upgraded", "outperform", "buy rating", "profit rises", "profit jumps", "net profit up", "order win",
       "wins order", "bags order", "secures order", "approval", "approved", "dividend", "bonus", "buyback",
       "stake buy", "expansion", "acquires", "strong demand", "raises guidance", "rate cut", "gains", "higher",
       "all-time high", "robust", "growth", "turnaround", "inflows"]
NEG = ["misses", "miss estimates", "plunge", "plunges", "slump", "crash", "tumbles", "falls", "declines", "drops",
       "downgrade", "downgraded", "underperform", "sell rating", "loss widens", "net loss", "profit falls",
       "profit declines", "penalty", "fine", "probe", "raid", "fraud", "default", "resigns", "resignation",
       "insolvency", "bankruptcy", "pledge", "lower", "weak", "cut guidance", "rate hike", "outflows",
       "show cause", "debarred", "ban", "suspension", "recall", "losses", "selloff", "sell-off"]
TAGS = [
    ("results", r"\b(q[1-4]|quarter|results?|earnings|net profit|revenue|ebitda)\b"),
    ("corporate action", r"\b(dividend|bonus|split|buyback|rights issue|record date)\b"),
    ("deal", r"\b(acqui|merger|stake|takeover|amalgamation|demerger)\w*"),
    ("order", r"\b(order|contract|tender)\b"),
    ("rating", r"\b(upgrade|downgrade|target price|rating|brokerage)\b"),
    ("RBI / monetary", r"\b(rbi|repo|monetary policy|inflation|cpi)\b"),
    ("SEBI / regulatory", r"\b(sebi|regulator|penalty|show cause|adjudication|debarred)\b"),
    ("government policy", r"\b(budget|gst|tax|ministry|government|cabinet|pli|tariff)\b"),
    ("global", r"\b(fed|us market|wall street|nasdaq|dow|china|crude|oil|dollar|global)\b"),
    ("IPO", r"\b(ipo|listing|drhp|rhp)\b"),
]
GENERIC = {"bank", "general", "national", "state", "power", "steel", "finance", "capital", "energy", "global",
            "united", "indian", "bharat", "hindustan", "oil", "gas", "life", "insurance", "housing", "motors",
            "cement", "chemicals", "pharma", "infra", "metals", "textiles", "trust", "services"}
HIGH_IMPACT = {"results", "deal", "RBI / monetary", "SEBI / regulatory"}

_state = {"items": {}, "last_fetch": {}, "errors": {}}
_lock = threading.Lock()


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=cal.IST)
        return d.timestamp()
    except (TypeError, ValueError):
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d %b, %Y %z", "%d %b, %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=cal.IST)
            return d.timestamp()
        except ValueError:
            continue
    return None


def sentiment(text):
    t = " " + text.lower() + " "
    p = [w for w in POS if re.search(r"\b%s\b" % re.escape(w), t)]
    n = [w for w in NEG if re.search(r"\b%s\b" % re.escape(w), t)]
    score = len(p) - len(n)
    label = "positive" if score > 0 else "negative" if score < 0 else "neutral"
    return label, p, n


def tags_for(text):
    t = text.lower()
    return [name for name, rx in TAGS if re.search(rx, t)]


def _name_index():
    """Company-name patterns -> symbol, from the index lists (cached by universe)."""
    from ..market import universe
    idx = {}
    for sym, m in universe.stock_meta().items():
        name = re.sub(r"\b(ltd|limited|the|india|of|and|company|corporation|industries)\b\.?", "",
                      m["name"], flags=re.I)
        name = re.sub(r"[^A-Za-z0-9& ]", " ", name).strip()
        words = name.split()
        if len(words) >= 2:
            idx[" ".join(words[:2]).lower()] = sym
        elif words and len(words[0]) >= 5 and words[0].lower() not in GENERIC:
            idx[words[0].lower()] = sym
        idx[sym.lower()] = sym
    return idx


_idx_cache = {"t": 0, "idx": {}}


def affected(text, link=""):
    if time.time() - _idx_cache["t"] > 3600:
        try:
            _idx_cache.update(t=time.time(), idx=_name_index())
        except Exception:
            pass
    idx = _idx_cache["idx"]
    out = set()
    m = re.search(r"/corporate/([A-Z0-9&\-]+)_\d{8}", link or "")
    if m:
        out.add(m.group(1))
    low = " " + re.sub(r"[^a-z0-9& ]", " ", text.lower()) + " "
    for key, sym in idx.items():
        if len(key) >= 4 and (" %s " % key) in low:
            out.add(sym)
    return sorted(out)[:6]


def _item(feed, title, link, desc, ts):
    text = "%s. %s" % (title, desc)
    lab, p, n = sentiment(text)
    tg = tags_for(text)
    if feed["kind"] == "results" and "results" not in tg:
        tg.insert(0, "results")
    if feed["kind"] == "regulator":
        tg.insert(0, "RBI / monetary" if feed["id"] == "rbi" else "SEBI / regulatory")
    impact = "high" if HIGH_IMPACT & set(tg) else "medium" if tg else "low"
    return {
        "id": hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16],
        "title": title, "summary": desc[:280], "link": link, "source": feed["name"], "source_kind": feed["kind"],
        "published": ts, "published_ist": cal.to_ist(ts).strftime("%d %b %Y, %H:%M IST") if ts else None,
        "sentiment": lab, "sentiment_words": {"positive": p, "negative": n}, "tags": list(dict.fromkeys(tg)),
        "impact": impact, "symbols": affected(text, link),
    }


def parse_feed(feed, data):
    root = ET.fromstring(data.lstrip(b"\xef\xbb\xbf").strip())
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        desc = _clean(it.findtext("description"))
        link = (it.findtext("link") or "").strip()
        ts = parse_date(it.findtext("pubDate"))
        if feed["id"] == "nse_ann":
            if re.search(r"\b(mutual fund|etf)\b", title, re.I):
                continue  # fund/ETF housekeeping filings are noise for stock analysis
            if desc:
                title, desc = "%s: %s" % (title, desc), ""
        if feed["id"] == "nse_results":
            title = "%s – financial results filed" % title
        out.append(_item(feed, title, link, desc, ts))
    return out


def refresh():
    now = time.time()
    got = 0
    for f in FEEDS:
        try:
            items = parse_feed(f, _get(f["url"]))
            with _lock:
                for it in items:
                    _state["items"].setdefault(it["id"], it)
                _state["last_fetch"][f["id"]] = now
                _state["errors"].pop(f["id"], None)
            got += len(items)
        except Exception as e:  # noqa: BLE001
            _state["errors"][f["id"]] = "%s: %s" % (f["name"], str(e)[:100])
    with _lock:  # forget anything older than a week
        for k in [k for k, v in _state["items"].items() if v["published"] and now - v["published"] > 7 * 86400]:
            del _state["items"][k]
    return got


def stock_news(symbol, name=None, hours=MAX_AGE_H):
    """Google News search for one stock (cached 15 min)."""
    key = "g:" + symbol
    with _lock:
        hit = _state.get(key)
    if hit and time.time() - hit["t"] < 900:
        items = hit["items"]
    else:
        q = '"%s" OR "%s" share' % (name or symbol, symbol) if name else '"%s" share NSE' % symbol
        url = "https://news.google.com/rss/search?q=%s+when:3d&hl=en-IN&gl=IN&ceid=IN:en" % urllib.parse.quote_plus(q)
        feed = {"id": "gnews", "name": "Google News", "kind": "media"}
        try:
            items = parse_feed(feed, _get(url))
            for it in items:
                it["symbols"] = sorted(set(it["symbols"]) | {symbol})
                m = re.search(r" - ([^-]+)$", it["title"])
                if m:
                    it["source"] = "%s (via Google News)" % m.group(1).strip()
        except Exception as e:  # noqa: BLE001
            items = []
            _state["errors"]["gnews"] = "Google News: %s" % str(e)[:100]
        with _lock:
            _state[key] = {"t": time.time(), "items": items}
    own = [v for v in list(_state["items"].values()) if symbol in v["symbols"]]
    return select(items + own, hours)


def select(items, hours=MAX_AGE_H, symbol=None, kind=None):
    now = time.time()
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -(x["published"] or 0)):
        if not it["published"] or now - it["published"] > hours * 3600:
            continue  # undated or older than the window: never shown as current
        if symbol and symbol not in it["symbols"]:
            continue
        if kind and it["source_kind"] != kind:
            continue
        k = re.sub(r"\W+", "", it["title"].lower())[:80]
        if k in seen:
            continue
        seen.add(k)
        it = dict(it, age_hours=round((now - it["published"]) / 3600, 1))
        out.append(it)
    return out


def latest(hours=MAX_AGE_H, symbol=None, kind=None, limit=100):
    with _lock:
        items = list(_state["items"].values())
    return select(items, hours, symbol, kind)[:limit]


def status():
    return {"feeds": [{"name": f["name"], "url": f["url"],
                       "last_fetch": _state["last_fetch"].get(f["id"]),
                       "error": _state["errors"].get(f["id"])} for f in FEEDS],
            "unavailable": UNAVAILABLE, "items_cached": len(_state["items"]),
            "max_age_hours": MAX_AGE_H}
