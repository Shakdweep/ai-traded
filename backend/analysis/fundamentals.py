"""Fundamental analysis for NSE companies from Screener.in's public company pages
(the Indian-market replacement for the trader-analyst skill's SEC EDGAR source).

Source: https://www.screener.in/company/<SYMBOL>/consolidated/ (falls back to the
standalone page). Company pages are allowed by Screener's robots.txt; this module
fetches one page per stock and caches it for 12 hours. It is an unofficial source:
figures can lag filings. Toggle with TRADER_ENABLE_SCREENER=0.

Classification (only from fields that are actually present, and it says which
are missing): Strong / Average / Weak fundamentals, and a separate valuation
call (Undervalued / Fairly valued / Overvalued vs the peer median P/E).
"""

import re
import threading
import time
import urllib.error
import urllib.request

from ..config import ENABLE_SCREENER

UA = "Mozilla/5.0 (compatible; personal-trading-dashboard/1.0)"
TTL = 12 * 3600
_cache = {}
_lock = threading.Lock()
_last_req = [0.0]


class FundamentalsError(Exception):
    pass


def _get(url):
    with _lock:  # one request at a time, at most one per 1.5 s
        wait = 1.5 - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _txt(s):
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def _num(s):
    if s is None:
        return None
    s = s.replace(",", "").replace("%", "").replace("₹", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _section(h, sid):
    i = h.find('id="%s"' % sid)
    if i < 0:
        return ""
    j = h.find("</section>", i)
    return h[i:j if j > 0 else i + 60000]


def _table(seg):
    """First data table of a section -> (headers, {row label: [values]})."""
    m = re.search(r'<table class="data-table[^"]*"[^>]*>(.*?)</table>', seg, re.S)
    if not m:
        return [], {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
    head, out = [], {}
    for r in rows:
        cells = [_txt(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
        if not cells:
            continue
        if not head:
            head = cells[1:]
            continue
        out[cells[0].rstrip(" +").strip()] = cells[1:]
    return head, out


def _ranges(seg):
    out = {}
    for m in re.findall(r'<table class="ranges-table">(.*?)</table>', seg, re.S):
        rows = [[_txt(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", m, re.S)]
        if not rows:
            continue
        title = rows[0][0]
        out[title] = {r[0].rstrip(":"): _num(r[1]) for r in rows[1:] if len(r) >= 2}
    return out


def _top_ratios(h):
    seg = h[h.find('id="top-ratios"'):]
    seg = seg[:seg.find("</ul>")]
    out = {}
    for li in re.findall(r"<li[^>]*>(.*?)</li>", seg, re.S):
        name = _txt(re.search(r'class="name"[^>]*>(.*?)</span>', li, re.S).group(1)) if 'class="name"' in li else ""
        nums = [_num(_txt(x)) for x in re.findall(r'<span class="number">(.*?)</span>', li, re.S)]
        if name:
            out[name] = nums
    return out


def _last(vals, n=1):
    v = [_num(x) for x in vals if _num(x) is not None]
    return v[-n:] if v else []


def fetch(symbol):
    if not ENABLE_SCREENER:
        raise FundamentalsError("Screener.in source is disabled (TRADER_ENABLE_SCREENER=0)")
    sym = symbol.upper().replace(".NS", "")
    with _lock:
        hit = _cache.get(sym)
    if hit and time.time() - hit["fetched"] < TTL:
        return hit
    page, basis = None, None
    for basis_try, url in (("consolidated", "https://www.screener.in/company/%s/consolidated/" % sym),
                           ("standalone", "https://www.screener.in/company/%s/" % sym)):
        try:
            h = _get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise FundamentalsError("Screener.in returned HTTP %s" % e.code)
        except Exception as e:  # noqa: BLE001
            raise FundamentalsError("Screener.in unreachable: %s" % str(e)[:100])
        _, q = _table(_section(h, "quarters"))
        if q.get("Sales") or q.get("Revenue") or q.get("Net Profit"):
            page, basis = h, basis_try
            break
        page, basis = h, basis_try
    if not page:
        raise FundamentalsError("No Screener.in page for %s" % sym)
    doc = parse(page, sym)
    doc.update(basis=basis, fetched=time.time(),
               source="Screener.in (%s figures, unofficial; verify against exchange filings)" % basis,
               source_url="https://www.screener.in/company/%s/%s" % (sym, "consolidated/" if basis ==
                                                                     "consolidated" else ""))
    wid = re.findall(r'data-warehouse-id="(\d+)"', page)
    if wid:
        try:
            doc["peers"] = parse_peers(_get("https://www.screener.in/api/company/%s/peers/" % wid[0]))
        except Exception as e:  # noqa: BLE001
            doc["peers"] = {"error": "peer comparison unavailable: %s" % str(e)[:80]}
    classify(doc)
    with _lock:
        _cache[sym] = doc
    return doc


def parse(h, sym):
    tr = _top_ratios(h)
    qh, q = _table(_section(h, "quarters"))
    ph, pl = _table(_section(h, "profit-loss"))
    rng = _ranges(_section(h, "profit-loss"))
    bh, bs = _table(_section(h, "balance-sheet"))
    ch, cf = _table(_section(h, "cash-flow"))
    rh, ra = _table(_section(h, "ratios"))
    sh_h, sh = _table(_section(h, "shareholding"))
    sales_key = "Sales" if "Sales" in q else "Revenue" if "Revenue" in q else None
    d = {"is_financial": "Financing Profit" in q or "Financing Margin %" in q, "symbol": sym, "name": _txt(re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S).group(1)) if "<h1" in h else sym}
    g = lambda k, i=0: (tr.get(k) or [None])[i] if tr.get(k) and len(tr.get(k)) > i else None  # noqa: E731
    d["market_cap_cr"] = g("Market Cap")
    d["price"] = g("Current Price")
    d["pe"] = g("Stock P/E")
    d["book_value"] = g("Book Value")
    d["pb"] = round(d["price"] / d["book_value"], 2) if d["price"] and d["book_value"] else None
    d["dividend_yield"] = g("Dividend Yield")
    d["roce"] = g("ROCE")
    d["roe"] = g("ROE")
    # quarterly results (last 5 quarters shown)
    quarters = []
    if qh:
        for i, label in enumerate(qh[-5:]):
            idx = len(qh) - 5 + i if len(qh) >= 5 else i
            row = {"quarter": label}
            for key, name in ((sales_key, "sales"), ("Operating Profit", "operating_profit"), ("OPM %", "opm"),
                              ("Net Profit", "net_profit"), ("EPS in Rs", "eps"), ("Financing Profit", "op_fin")):
                if key and key in q and idx < len(q[key]):
                    row[name] = _num(q[key][idx])
            quarters.append(row)
    d["quarters"] = quarters
    if quarters and len(qh) >= 5 and sales_key:
        def yoy(key):
            vals = q.get(key) or []
            if len(vals) >= 5 and _num(vals[-1]) is not None and _num(vals[-5]) not in (None, 0):
                return round((_num(vals[-1]) / abs(_num(vals[-5])) - 1) * 100, 1)
            return None
        d["qtr_sales_yoy"] = yoy(sales_key)
        d["qtr_profit_yoy"] = yoy("Net Profit")
    d["sales_growth_3y"] = (rng.get("Compounded Sales Growth") or {}).get("3 Years")
    d["sales_growth_5y"] = (rng.get("Compounded Sales Growth") or {}).get("5 Years")
    d["profit_growth_3y"] = (rng.get("Compounded Profit Growth") or {}).get("3 Years")
    d["profit_growth_5y"] = (rng.get("Compounded Profit Growth") or {}).get("5 Years")
    d["roe_3y"] = (rng.get("Return on Equity") or {}).get("3 Years")
    eps = _last(pl.get("EPS in Rs", []), 1)
    d["eps_ttm"] = eps[0] if eps else None
    opm = _last(pl.get("OPM %", []), 2)
    d["opm"] = opm[-1] if opm else None
    eq, res_, borr = _last(bs.get("Equity Capital", [])), _last(bs.get("Reserves", [])), _last(bs.get("Borrowings", []))
    if eq and res_ and borr:
        d["debt_to_equity"] = round(borr[0] / (eq[0] + res_[0]), 2) if (eq[0] + res_[0]) > 0 else None
    else:
        d["debt_to_equity"] = None
    cfo = _last(cf.get("Cash from Operating Activity", []), 3)
    fcf = _last(cf.get("Free Cash Flow", []), 3)
    d["cfo_last3"] = cfo
    d["fcf_last3"] = fcf
    np_ = _last(pl.get("Net Profit", []), 3)
    d["cfo_to_profit"] = round(sum(cfo) / sum(np_), 2) if cfo and np_ and len(cfo) == len(np_) and sum(np_) > 0 \
        else None
    roce_hist = _last(ra.get("ROCE %", []), 1)
    if d["roce"] is None and roce_hist:
        d["roce"] = roce_hist[0]
    # shareholding (latest quarter + change over the 4 quarters before)
    hold = {}
    for key, name in (("Promoters", "promoters"), ("FIIs", "fii"), ("DIIs", "dii"), ("Public", "public")):
        vals = _last(sh.get(key, []), 5)
        if vals:
            hold[name] = vals[-1]
            hold[name + "_change_1y"] = round(vals[-1] - vals[0], 2) if len(vals) >= 5 else None
    hold["as_of"] = sh_h[-1] if sh_h else None
    d["shareholding"] = hold
    cons = _txt(_section(h, "analysis") or h[h.find('class="cons"'):h.find('class="cons"') + 3000])
    m = re.search(r"[Pp]romoters? ha(?:ve|s) pledged ([\d.]+)%", h)
    d["promoter_pledge_pct"] = float(m.group(1)) if m else None
    d["pledge_note"] = ("Screener reports promoters have pledged %.2f%% of holding" % d["promoter_pledge_pct"]
                        if m else "No pledge disclosed on the source page (not independently verified)")
    pros = re.findall(r'<div class="pros"[^>]*>.*?<ul>(.*?)</ul>', h, re.S)
    conss = re.findall(r'<div class="cons"[^>]*>.*?<ul>(.*?)</ul>', h, re.S)
    d["source_pros"] = [_txt(x) for x in re.findall(r"<li>(.*?)</li>", pros[0], re.S)] if pros else []
    d["source_cons"] = [_txt(x) for x in re.findall(r"<li>(.*?)</li>", conss[0], re.S)] if conss else []
    del cons
    return d


def parse_peers(h):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S)
    table = [[_txt(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)] for r in rows]
    table = [r for r in table if r]
    if not table:
        return {"error": "no peer table"}
    head = table[0]
    peers, median = [], None
    for r in table[1:]:
        rec = dict(zip(head, r))
        if rec.get("Company", "").startswith("Median"):
            median = rec
        else:
            peers.append(rec)

    def pick(rec):
        return {"company": rec.get("Company"), "pe": _num(rec.get("P/E")), "mcap_cr": _num(rec.get("Mar Cap Rs.Cr.")),
                "roce": _num(rec.get("ROCE %")), "qtr_profit_var": _num(rec.get("Qtr Profit Var %")),
                "qtr_sales_var": _num(rec.get("Qtr Sales Var %"))}
    return {"peers": [pick(p) for p in peers[:10]], "median": pick(median) if median else None}


def classify(d):
    """Score each available metric; the class comes only from what is present."""
    pts, notes, missing = 0, [], []

    def rule(val, name, good, bad, fmt="%.1f"):
        nonlocal pts
        if val is None:
            missing.append(name)
            return
        if good(val):
            pts += 1
            notes.append({"ok": True, "text": "%s %s" % (name, fmt % val)})
        elif bad(val):
            pts -= 1
            notes.append({"ok": False, "text": "%s %s" % (name, fmt % val)})
        else:
            notes.append({"ok": None, "text": "%s %s" % (name, fmt % val)})

    rule(d.get("sales_growth_3y"), "Sales growth (3y CAGR, %)", lambda v: v >= 12, lambda v: v < 5)
    rule(d.get("profit_growth_3y"), "Profit growth (3y CAGR, %)", lambda v: v >= 12, lambda v: v < 0)
    rule(d.get("roe_3y") or d.get("roe"), "ROE (%)", lambda v: v >= 15, lambda v: v < 10)
    if not d.get("is_financial"):  # ROCE is not meaningful for lenders
        rule(d.get("roce"), "ROCE (%)", lambda v: v >= 15, lambda v: v < 10)
    if not d.get("is_financial"):
        rule(d.get("debt_to_equity"), "Debt / equity", lambda v: v <= 0.5, lambda v: v > 1.5, "%.2f")
        rule(d.get("opm"), "Operating margin (%)", lambda v: v >= 15, lambda v: v < 7)
    rule(d.get("cfo_to_profit"), "Operating cash flow / net profit (3y)", lambda v: v >= 0.8, lambda v: v < 0.5,
         "%.2f")
    rule(d.get("qtr_profit_yoy"), "Latest quarter profit YoY (%)", lambda v: v >= 10, lambda v: v < -10)
    if d.get("promoter_pledge_pct") is not None:
        rule(d["promoter_pledge_pct"], "Promoter pledge (%)", lambda v: v == 0, lambda v: v > 10)
    sh = d.get("shareholding") or {}
    if sh.get("promoters") is not None:
        # low promoter holding is normal for professionally run companies: noted, never penalised
        rule(sh["promoters"], "Promoter holding (%)", lambda v: v >= 50, lambda v: False)
    if d.get("is_financial"):
        missing[:] = [m for m in missing if m not in ("Debt / equity", "Operating margin (%)")]
        d["financial_note"] = ("Bank/NBFC: debt/equity and operating margin are not meaningful and are skipped; "
                               "check NIM, GNPA/NNPA and CASA in the results filing.")
    n = len(notes)
    bad = sum(1 for x in notes if x["ok"] is False)
    good = sum(1 for x in notes if x["ok"] is True)
    if n < 4:
        cls = "Insufficient data"
    elif good >= -(-0.6 * n // 1) and bad <= 1:
        cls = "Strong fundamentals"
    elif pts <= -2 or bad >= 0.4 * n:
        cls = "Weak fundamentals"
    else:
        cls = "Average fundamentals"
    val, vnote = None, None
    med = ((d.get("peers") or {}).get("median") or {}).get("pe") if isinstance(d.get("peers"), dict) else None
    pe = d.get("pe")
    if pe and med and med > 0:
        ratio = pe / med
        if pe < 0:
            val, vnote = "Loss-making", "negative P/E"
        elif ratio > 1.5:
            val = "Overvalued vs peers"
        elif ratio < 0.7:
            val = "Undervalued vs peers"
        else:
            val = "Fairly valued vs peers"
        vnote = vnote or "P/E %.1f vs peer median %.1f (%.0f%% of median)" % (pe, med, ratio * 100)
    else:
        val, vnote = "Not assessed", "peer median P/E unavailable"
    d["classification"] = cls
    d["score"] = pts
    d["checks"] = notes
    d["missing"] = missing
    d["valuation"] = val
    d["valuation_note"] = vnote
    d["disclaimer"] = ("Fundamental classification is for investment research, separate from short-term "
                       "trading signals. Based only on the listed fields; verify with annual reports.")
    return d
