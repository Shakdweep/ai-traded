"""Owner-only admin panel: people, activity, system health and how accurate the site's
calls have been, per segment.

Accuracy is measured two ways, both with the SAME rating rules the site uses:
  * Live tracking: every rating the site actually gave (signals table, first call per
    stock, segment and day) checked against what the price did next.
  * Historical replay: the swing rating rules re-run day by day over the last year of
    NIFTY 50 history (data up to that day only), then checked the same way.

A call is "right" when the price moved its way over the horizon: bullish calls (STRONG
BULLISH, BULLISH) right if the price went up, bearish calls (BEARISH, AVOID) right if it
went down. WAIT and HIGH RISK make no direction call, so they only show the average move.
"Base rate" = how often ANY stock went up over the same windows: a call only adds value
if its hit rate beats that. Past accuracy says nothing certain about the future.
"""

import threading
import time
import traceback

from . import auth, db, mailer, services
from .analysis import engine
from .market import calendar as cal
from .market import universe, yahoo

BULL = ("STRONG BULLISH", "BULLISH")
BEAR = ("BEARISH", "AVOID")
RATINGS = ("STRONG BULLISH", "BULLISH", "HIGH RISK", "WAIT", "BEARISH", "AVOID")
SWING_HORIZONS = (5, 10, 20)        # trading days
MIN_SAMPLE = 30                     # below this a hit rate is flagged as "too few to judge"
REPLAY_DAYS = 250                   # about one trading year
REFRESH_AFTER = 6 * 3600

_acc = {"status": "idle", "result": None, "started": None, "finished": None, "error": None}
_acc_lock = threading.Lock()


# ---------------------------------------------------------------- scoring helpers

def _direction_ok(rating, ret):
    if rating in BULL:
        return ret > 0
    if rating in BEAR:
        return ret < 0
    return None


class _Bucket:
    def __init__(self):
        self.n = self.hits = self.calls = self.beat = 0
        self.ret = self.excess = 0.0

    def add(self, rating, ret, bench_ret):
        self.n += 1
        self.ret += ret
        if bench_ret is not None:
            self.excess += ret - bench_ret
            if rating in BULL and ret > bench_ret or rating in BEAR and ret < bench_ret:
                self.beat += 1
        ok = _direction_ok(rating, ret)
        if ok is not None:
            self.calls += 1
            self.hits += ok

    def out(self):
        return {"n": self.n, "calls": self.calls,
                "hit_pct": round(100 * self.hits / self.calls, 1) if self.calls else None,
                "beat_nifty_pct": round(100 * self.beat / self.calls, 1) if self.calls else None,
                "avg_ret_pct": round(100 * self.ret / self.n, 2) if self.n else None,
                "avg_vs_nifty_pct": round(100 * self.excess / self.n, 2) if self.n else None,
                "enough": self.calls >= MIN_SAMPLE}


class _Segment:
    def __init__(self, name, horizon_note):
        self.name, self.note = name, horizon_note
        self.by_rating = {r: _Bucket() for r in RATINGS}
        self.direction = _Bucket()   # all bullish + bearish calls together
        self.up = self.all = 0       # base rate: share of all observations that went up

    def add(self, rating, ret, bench_ret):
        if rating not in self.by_rating:
            return
        self.by_rating[rating].add(rating, ret, bench_ret)
        if rating in BULL or rating in BEAR:
            self.direction.add(rating, ret, bench_ret)
        self.all += 1
        self.up += ret > 0

    def out(self):
        d = self.direction.out()
        base = round(100 * self.up / self.all, 1) if self.all else None
        bull = [self.by_rating[r] for r in BULL]
        bull_n = sum(b.calls for b in bull)
        bull_hit = round(100 * sum(b.hits for b in bull) / bull_n, 1) if bull_n else None
        return {"name": self.name, "note": self.note, "observations": self.all, "base_up_pct": base,
                "direction": d, "bullish_hit_pct": bull_hit,
                "bullish_edge_pct": round(bull_hit - base, 1) if bull_hit is not None and base is not None else None,
                "rows": [dict(rating=r, **self.by_rating[r].out()) for r in RATINGS if self.by_rating[r].n]}


def _closes_by_date(bars):
    return {cal.to_ist(b["t"]).date(): b["c"] for b in bars}


# ---------------------------------------------------------------- historical replay (swing rules)

def replay(symbols, bench_chart, charts, days=REPLAY_DAYS):
    segs = {h: _Segment("Swing · %d trading days" % h, "rating at a day's close vs the close %d trading days later" % h)
            for h in SWING_HORIZONS}
    bench = _closes_by_date(bench_chart["bars"])
    first = last = None
    used = 0
    for sym in symbols:
        ch = charts.get(sym)
        if not ch or "bars" not in ch:
            continue
        bars, _ = engine.completed_daily(ch["bars"])
        if len(bars) < 260 + max(SWING_HORIZONS):
            continue
        used += 1
        dates = [cal.to_ist(b["t"]).date() for b in bars]
        start = max(259, len(bars) - days - max(SWING_HORIZONS))
        for i in range(start, len(bars) - min(SWING_HORIZONS)):
            sub = bars[max(0, i - 299):i + 1]
            bc = [bench[d] for d in dates[max(0, i - 299):i + 1] if d in bench]
            f = engine.daily_features(sub, bc)
            s, _ = engine.score_daily(f)
            rating, _ = engine.rate(s, engine.risk_flags(f), f["setups"])
            first = min(first or dates[i], dates[i])
            last = max(last or dates[i], dates[i])
            for h in SWING_HORIZONS:
                if i + h >= len(bars):
                    continue
                ret = bars[i + h]["c"] / bars[i]["c"] - 1
                b0, b1 = bench.get(dates[i]), bench.get(dates[i + h])
                segs[h].add(rating, ret, (b1 / b0 - 1) if b0 and b1 else None)
    return {"universe": "NIFTY 50", "stocks": used, "period": "%s to %s" % (first, last) if first else None,
            "segments": [segs[h].out() for h in SWING_HORIZONS]}


# ---------------------------------------------------------------- live tracking (what the site actually said)

def live(charts, bench_chart):
    rows = db.q("SELECT ts, symbol, mode, rating, price FROM signals ORDER BY ts")
    first_call = {}
    for r in rows:  # scans repeat every few minutes: keep the first call per stock, segment and day
        d = cal.to_ist(r["ts"]).date()
        first_call.setdefault((r["symbol"], r["mode"], d), r)
    segs = {"intraday": _Segment("Intraday · same day", "rating during the session vs that day's close")}
    for h in SWING_HORIZONS:
        segs[h] = _Segment("Swing · %d trading days" % h, "first rating of the day vs the close %d trading days later" % h)
    bench = _closes_by_date(bench_chart["bars"]) if bench_chart else {}
    bench_dates = sorted(bench)
    today = cal.now_ist().date()
    open_now = cal.market_status()["open"]
    pending = 0
    for (sym, mode, d), r in first_call.items():
        ch = charts.get(sym)
        if not ch or not r["price"]:
            pending += 1
            continue
        bars, _ = engine.completed_daily(ch["bars"])
        closes = [(cal.to_ist(b["t"]).date(), b["c"]) for b in bars]
        idx = next((i for i, (bd, _) in enumerate(closes) if bd >= d), None)
        if idx is None:
            pending += 1
            continue
        if mode == "intraday":
            if closes[idx][0] != d or (d == today and open_now):
                pending += 1
                continue
            ret = closes[idx][1] / r["price"] - 1
            b0 = bench.get(_prev(bench_dates, d))
            b1 = bench.get(d)
            segs["intraday"].add(r["rating"], ret, (b1 / b0 - 1) if b0 and b1 else None)
            continue
        matured = False
        for h in SWING_HORIZONS:
            j = idx + h
            if j < len(closes):
                matured = True
                ret = closes[j][1] / r["price"] - 1
                b0, b1 = bench.get(closes[idx][0]), bench.get(closes[j][0])
                segs[h].add(r["rating"], ret, (b1 / b0 - 1) if b0 and b1 else None)
        if not matured:
            pending += 1
    first = min((k[2] for k in first_call), default=None)
    return {"since": str(first) if first else None, "calls": len(first_call), "pending": pending,
            "segments": [segs["intraday"].out()] + [segs[h].out() for h in SWING_HORIZONS]}


def _prev(dates, d):
    prior = [x for x in dates if x < d]
    return prior[-1] if prior else None


# ---------------------------------------------------------------- predictions: did each one come true?

SWING_WINDOW = 20      # trading days a swing target/stop prediction has to play out
DIRECTION_DAYS = 5     # trading days for predictions without a target and stop


def _first_calls():
    """First call per stock, segment and IST day (scans repeat every few minutes)."""
    first = {}
    for r in db.q("SELECT * FROM signals ORDER BY ts"):
        first.setdefault((r["symbol"], r["mode"], cal.to_ist(r["ts"]).date()), r)
    return first


def judge(call, daily_bars, bars5=None, now=None):
    """One prediction -> outcome dict. outcome: met | missed | expired | open | not_triggered | none.
    Breakout plans (entry above the price at the time of the call) only count once the price
    reaches the entry; a plan whose entry never triggers is "not_triggered", not a miss."""
    now = now or time.time()
    d = cal.to_ist(call["ts"]).date()
    rating, price = call["rating"], call["price"]
    base = {"ts": call["ts"], "date": str(d), "symbol": call["symbol"], "segment": call["mode"], "rating": rating,
            "price": price, "target": call.get("target1"), "stop": call.get("stop")}
    if rating not in BULL and rating not in BEAR or not price:
        return dict(base, kind="none", outcome="none", text="No up/down prediction")
    entry = call.get("entry") if call.get("entry") and call["entry"] > price * 1.001 else None
    with_plan = rating in BULL and call.get("target1") and call.get("stop") and call["stop"] < price < call["target1"] \
        and (entry is None or entry < call["target1"])
    base["entry"] = entry
    if call["mode"] == "intraday":
        t = cal.to_ist(call["ts"]).time()
        if not cal.is_trading_day(d) or not cal.OPEN <= t < cal.CLOSE:
            return dict(base, kind="none", outcome="none", text="Made while the market was closed")
        bars = [b for b in (bars5 or []) if b["t"] >= call["ts"] and cal.to_ist(b["t"]).date() == d]
        closed_day = d < cal.now_ist().date() or not cal.market_status()["open"] and d == cal.now_ist().date()
        if with_plan:
            live = entry is None
            for b in bars:
                if not live:
                    if b["h"] < entry:
                        continue
                    live = True
                hit_t, hit_s = b["h"] >= call["target1"], b["l"] <= call["stop"]
                if hit_s:  # if both in one 5-minute bar, count the stop (conservative)
                    return dict(base, kind="target/stop", outcome="missed", move_pct=_pct(call["stop"], price),
                                text="Stop ₹%.2f hit at %s" % (call["stop"], cal.to_ist(b["t"]).strftime("%H:%M")))
                if hit_t:
                    return dict(base, kind="target/stop", outcome="met", move_pct=_pct(call["target1"], price),
                                text="Target ₹%.2f hit at %s" % (call["target1"], cal.to_ist(b["t"]).strftime("%H:%M")))
        if not bars or not closed_day:
            return dict(base, kind="target/stop" if with_plan else "direction", outcome="open",
                        text="Waiting for today's close")
        move = _pct(bars[-1]["c"], price)
        if with_plan and entry and max(b["h"] for b in bars) < entry:
            return dict(base, kind="target/stop", outcome="not_triggered", move_pct=move,
                        text="Entry ₹%.2f never reached today" % entry)
        if with_plan:
            return dict(base, kind="target/stop", outcome="expired", move_pct=move,
                        text="Neither hit by the close (%+.2f%%)" % move)
        ok = move > 0 if rating in BULL else move < 0
        return dict(base, kind="direction", outcome="met" if ok else "missed", move_pct=move,
                    text="%s %.2f%% by the close" % ("Up" if move > 0 else "Down", abs(move)))

    completed, _ = engine.completed_daily(daily_bars or [])
    after = [b for b in completed if cal.to_ist(b["t"]).date() > d]  # sessions AFTER the call
    if with_plan:
        live = entry is None
        for i, b in enumerate(after[:SWING_WINDOW]):
            when = cal.to_ist(b["t"]).strftime("%d %b")
            if not live:
                if b["h"] < entry:
                    continue
                live = True  # entry reached: from this day the target and stop count
            if b["l"] <= call["stop"]:  # if both in one day, count the stop (conservative)
                return dict(base, kind="target/stop", outcome="missed", days=i + 1, move_pct=_pct(call["stop"], price),
                            text="Stop ₹%.2f hit on %s (day %d)" % (call["stop"], when, i + 1))
            if b["h"] >= call["target1"]:
                return dict(base, kind="target/stop", outcome="met", days=i + 1, move_pct=_pct(call["target1"], price),
                            text="Target ₹%.2f hit on %s (day %d)" % (call["target1"], when, i + 1))
        if len(after) < SWING_WINDOW:
            move = _pct(after[-1]["c"], price) if after else None
            waiting = " for entry ₹%.2f" % entry if not live else ", neither hit yet"
            return dict(base, kind="target/stop", outcome="open", days=len(after), move_pct=move,
                        text="Day %d of %d%s%s" % (len(after), SWING_WINDOW, waiting,
                                                   " (%+.2f%%)" % move if move is not None else ""))
        move = _pct(after[SWING_WINDOW - 1]["c"], price)
        if not live:
            return dict(base, kind="target/stop", outcome="not_triggered", days=SWING_WINDOW, move_pct=move,
                        text="Entry ₹%.2f never reached in %d trading days (%+.2f%%)" % (entry, SWING_WINDOW, move))
        return dict(base, kind="target/stop", outcome="expired", days=SWING_WINDOW, move_pct=move,
                    text="Neither hit in %d trading days (%+.2f%%)" % (SWING_WINDOW, move))
    if len(after) < DIRECTION_DAYS:
        move = _pct(after[-1]["c"], price) if after else None
        return dict(base, kind="direction", outcome="open", days=len(after), move_pct=move,
                    text="Day %d of %d%s" % (len(after), DIRECTION_DAYS, " (%+.2f%%)" % move if move is not None else ""))
    move = _pct(after[DIRECTION_DAYS - 1]["c"], price)
    ok = move > 0 if rating in BULL else move < 0
    return dict(base, kind="direction", outcome="met" if ok else "missed", days=DIRECTION_DAYS, move_pct=move,
                text="%s %.2f%% after %d trading days" % ("Up" if move > 0 else "Down", abs(move), DIRECTION_DAYS))


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


def summarize(items):
    """Counts per segment and rating: met / missed / expired / open, and the success rate of finished ones."""
    out = {}
    for it in items:
        if it["outcome"] == "none":
            continue
        for key in (it["segment"], "%s|%s" % (it["segment"], it["rating"])):
            s = out.setdefault(key, {"met": 0, "missed": 0, "expired": 0, "open": 0, "not_triggered": 0})
            s[it["outcome"]] += 1
    for s in out.values():
        done = s["met"] + s["missed"] + s["expired"]
        s["finished"] = done
        s["met_pct"] = round(100 * s["met"] / done, 1) if done else None
    return out


def _compute_predictions():
    calls = list(_first_calls().values())
    syms = sorted({c["symbol"] for c in calls})
    daily, _ = yahoo.charts([yahoo.to_yahoo(s) for s in syms], "6mo", "1d")
    intra_syms = sorted({c["symbol"] for c in calls if c["mode"] == "intraday"})
    five, _ = yahoo.charts([yahoo.to_yahoo(s) for s in intra_syms], "60d", "5m") if intra_syms else ({}, {})
    items = []
    for c in calls:
        ys = yahoo.to_yahoo(c["symbol"])
        items.append(judge(c, (daily.get(ys) or {}).get("bars"), (five.get(ys) or {}).get("bars")))
    items.sort(key=lambda x: x["ts"], reverse=True)
    real = [i for i in items if i["outcome"] != "none"]
    return {"summary": summarize(items), "items": real[:500], "total": len(real),
            "no_prediction": len(items) - len(real), "swing_window": SWING_WINDOW, "direction_days": DIRECTION_DAYS,
            "computed_at": time.time(), "computed_ist": cal.now_ist().strftime("%d %b %Y %H:%M IST")}


_pred = {"status": "idle", "result": None, "started": None, "finished": None, "error": None}


def _run_pred():
    try:
        res = _compute_predictions()
        with _acc_lock:
            _pred.update(status="done", result=res, finished=time.time(), error=None)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with _acc_lock:
            _pred.update(status="error", error="%s: %s" % (type(e).__name__, str(e)[:200]), finished=time.time())


def predictions(force=False):
    with _acc_lock:
        stale = not _pred["result"] or time.time() - (_pred["finished"] or 0) > 30 * 60
        if _pred["status"] != "running" and (force or stale):
            _pred.update(status="running", started=time.time(), error=None)
            threading.Thread(target=_run_pred, daemon=True, name="predictions").start()
        return {k: _pred[k] for k in ("status", "result", "started", "finished", "error")}


# ---------------------------------------------------------------- background job

def _compute():
    symbols = [s["symbol"] for s in universe.constituents("nifty50")["stocks"]]
    live_syms = sorted({r["symbol"] for r in db.q("SELECT DISTINCT symbol FROM signals")})
    ysym = {s: yahoo.to_yahoo(s) for s in set(symbols) | set(live_syms)}
    fetched, _ = yahoo.charts(sorted(set(ysym.values())), "2y", "1d")
    charts = {s: fetched.get(y) for s, y in ysym.items()}
    bench = yahoo.chart("^NSEI", "2y", "1d")
    t0 = time.time()
    rep = replay(symbols, bench, charts)
    rep["seconds"] = round(time.time() - t0, 1)
    return {"replay": rep, "live": live(charts, bench), "min_sample": MIN_SAMPLE,
            "computed_at": time.time(), "computed_ist": cal.now_ist().strftime("%d %b %Y %H:%M IST")}


def _run():
    try:
        res = _compute()
        with _acc_lock:
            _acc.update(status="done", result=res, finished=time.time(), error=None)
    except Exception as e:  # noqa: BLE001 - report it on the admin page instead of crashing
        traceback.print_exc()
        with _acc_lock:
            _acc.update(status="error", error="%s: %s" % (type(e).__name__, str(e)[:200]), finished=time.time())


def accuracy(force=False):
    """The latest accuracy report; starts a background recompute when missing, stale or forced."""
    with _acc_lock:
        stale = not _acc["result"] or time.time() - (_acc["finished"] or 0) > REFRESH_AFTER
        if _acc["status"] != "running" and (force or stale):
            _acc.update(status="running", started=time.time(), error=None)
            threading.Thread(target=_run, daemon=True, name="accuracy").start()
        return {k: _acc[k] for k in ("status", "result", "started", "finished", "error")}


# ---------------------------------------------------------------- people, activity, health

def _paper_stats():
    """Closed paper trades per owner and per segment (mode), with R multiples when the plan's stop is known."""
    rows = db.q("SELECT p.owner, p.mode, p.qty, p.entry_price, p.exit_price, p.pnl, p.exit_reason, o.stop AS plan_stop "
                "FROM paper_positions p LEFT JOIN paper_orders o ON o.id = p.order_id WHERE p.status='closed'")
    by_owner, by_mode = {}, {}
    for r in rows:
        r_mult = None
        if r["plan_stop"] and r["entry_price"] > r["plan_stop"] and r["exit_price"]:
            r_mult = (r["exit_price"] - r["entry_price"]) / (r["entry_price"] - r["plan_stop"])
        for key, store in ((r["owner"], by_owner), (r["mode"] or "swing", by_mode)):
            s = store.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "r_n": 0, "reasons": {}})
            s["trades"] += 1
            s["wins"] += (r["pnl"] or 0) > 0
            s["pnl"] += r["pnl"] or 0
            if r_mult is not None:
                s["r_sum"] += r_mult
                s["r_n"] += 1
            reason = r["exit_reason"] or "other"
            s["reasons"][reason] = s["reasons"].get(reason, 0) + 1

    def fin(s):
        return {"trades": s["trades"], "win_rate": round(100 * s["wins"] / s["trades"], 1) if s["trades"] else None,
                "pnl": round(s["pnl"], 2), "avg_pnl": round(s["pnl"] / s["trades"], 2) if s["trades"] else None,
                "avg_r": round(s["r_sum"] / s["r_n"], 2) if s["r_n"] else None, "exit_reasons": s["reasons"]}
    return {k: fin(v) for k, v in by_owner.items()}, {k: fin(v) for k, v in by_mode.items()}


def _count_by(sql, args=()):
    return {r["owner"]: r["n"] for r in db.q(sql, args)}


def overview(bg=None):
    now = time.time()
    accounts = db.q("SELECT a.email AS id, a.name, a.status, a.contact_email, a.created_at, a.approved_at, "
                    "a.last_login, a.last_seen, i.note AS invite_note FROM accounts a "
                    "LEFT JOIN invites i ON i.id = a.invite_id ORDER BY a.created_at DESC")
    owners = sorted(set(auth.USERS) | set(auth.FILE_USERS))
    per_owner, per_mode = _paper_stats()
    open_pos = _count_by("SELECT owner, COUNT(*) AS n FROM paper_positions WHERE status='open' GROUP BY owner")
    watch = _count_by("SELECT owner, COUNT(*) AS n FROM user_watchlist GROUP BY owner")
    orders = _count_by("SELECT owner, COUNT(*) AS n FROM paper_orders GROUP BY owner")
    people = []
    for o in owners:
        people.append({"id": o, "name": o, "status": "owner", "created_at": None, "last_login": None, "last_seen": None,
                       "invite_note": None, "contact_email": None})
    people += accounts
    for p in people:
        st = per_owner.get(p["id"], {})
        p.update(open_positions=open_pos.get(p["id"], 0), watchlist=watch.get(p["id"], 0),
                 orders=orders.get(p["id"], 0), closed_trades=st.get("trades", 0), win_rate=st.get("win_rate"),
                 realised_pnl=st.get("pnl", 0.0))
    day = 86400
    active = [a for a in accounts if a["status"] != "pending"]
    seen = lambda a: max(a.get("last_seen") or 0, a.get("last_login") or 0)  # noqa: E731
    invites = db.q("SELECT used_by, revoked FROM invites")
    signups = {}
    for a in accounts:
        d = cal.to_ist(a["created_at"]).date().isoformat()
        signups[d] = signups.get(d, 0) + 1
    kpis = {
        "members": len(active), "pending": len(accounts) - len(active),
        "joined_7d": sum(1 for a in accounts if now - a["created_at"] < 7 * day),
        "active_24h": sum(1 for a in active if now - seen(a) < day),
        "active_7d": sum(1 for a in active if now - seen(a) < 7 * day),
        "invites_unused": sum(1 for i in invites if not i["used_by"] and not i["revoked"]),
        "invites_used": sum(1 for i in invites if i["used_by"]),
        "open_positions": sum(open_pos.values()), "closed_trades": sum(s["trades"] for s in per_owner.values()),
        "paper_orders": sum(orders.values()),
    }
    health = {
        "storage": db.backend_name(), "email": mailer.describe(),
        "signals_stored": (db.q("SELECT COUNT(*) AS n FROM signals", one=True) or {}).get("n"),
        "market": cal.market_status(),
        "background": {k: v for k, v in (bg or {}).items() if k != "errors"},
        "background_errors": (bg or {}).get("errors", [])[-5:],
        "scans": [{"universe": k, "mode": m, "finished_ist": v.get("finished_ist"), "analysed": v.get("analysed"),
                   "errors": len(v.get("errors", {}))} for (k, m), v in services.SCANS.items()],
    }
    return {"kpis": kpis, "people": people, "signups_by_day": sorted(signups.items())[-30:],
            "paper_by_segment": per_mode, "health": health}


# ---------------------------------------------------------------- "last seen" (cheap, at most every 5 minutes)

_seen = {}


def touch(user):
    if not user or not auth.is_account_id(user):
        return
    now = time.time()
    if now - _seen.get(user, 0) < 300:
        return
    _seen[user] = now
    try:
        db.x("UPDATE accounts SET last_seen=? WHERE email=?", (now, user))
    except Exception:  # noqa: BLE001 - never block a request for this
        pass
