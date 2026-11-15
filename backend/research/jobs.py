"""Scheduled research jobs. Run by GitHub Actions (see .github/workflows/research.yml) or
from the Research tab's "Run now" buttons.

  python -m backend.research.jobs daily      after the close: official data, indices, bulk deals, score outcomes
  python -m backend.research.jobs weekly     Monday before the open: this week's 10 stocks + forecasts (live)
  python -m backend.research.jobs backfill [days]   load past official data (default ~2 years)
  python -m backend.research.jobs simulate [weeks]  replay past Mondays as SIMULATED cohorts (default 52)
  python -m backend.research.jobs all        backfill + simulate + daily + weekly

Every job writes a row to data_runs (shown on the Research tab). Jobs never invent data:
if NSE's file for a day isn't available the day is reported as missing.
"""

import datetime
import sys
import threading
import time
import traceback

from .. import db
from ..analysis import indicators as ind
from ..market import calendar as cal
from . import data, evaluate, features, model, store

_status = {}
_lock = threading.Lock()


def _today():
    return cal.now_ist().date()


def daily(lookback_days=14):
    t0 = time.time()
    meta = data.research_universe()
    last = data.latest_equity_date()
    start = datetime.date.fromisoformat(last) + datetime.timedelta(days=1) if last else \
        _today() - datetime.timedelta(days=lookback_days)
    start = max(start, _today() - datetime.timedelta(days=lookback_days * 3))
    end = _today()
    if cal.market_status()["open"] or (cal.is_trading_day(end) and cal.now_ist().time() < datetime.time(18, 0)):
        end -= datetime.timedelta(days=1)  # today's official file appears in the evening
    loaded, missing = data.load_bhavcopy_range(start, end, keep=set(meta))
    n_ix, ix_err = data.load_indices("3mo")
    try:
        n_bulk = data.load_bulk_deals()
    except data.DataUnavailable as e:
        n_bulk, ix_err = 0, ix_err + [str(e)]
    pending = {r["symbol"] for r in db.q(  # only stocks whose predictions still have horizons to score
        "SELECT DISTINCT p.symbol FROM predictions p WHERE (SELECT COUNT(*) FROM outcomes o "
        "WHERE o.prediction_id = p.id) < ?", (len(model.HORIZONS),))}
    st = features.Store(pending if len(pending) <= 60 else None)
    scored = evaluate.evaluate_pending(st)
    msg = "official days loaded %d, unavailable %d, index bars %d, bulk deals %d, outcomes scored %d" % (
        loaded, len(missing), n_ix, n_bulk, scored)
    if missing or ix_err:
        msg += " | " + "; ".join((missing + ix_err)[:4])
    data.log_run("daily", "ok", loaded + scored, msg, t0)
    return msg


def make_cohort(st, meta, week, as_of, kind, created_at=None):
    picks, scored, _ = model.select(st, as_of, meta)
    if not picks:
        raise RuntimeError("no stock had enough clean data on %s" % as_of)
    nb = st.upto("^NSEI", as_of, 40)
    atr = ind.last(ind.atr([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in nb])) if len(nb) > 15 else None
    nifty_atr_pct = atr / nb[-1]["c"] * 100 if atr else None
    fcs = []
    for p in picks:
        fc = model.forecast(p, nifty_atr_pct)
        fc["symbol"] = p["features"]["symbol"]
        fc["name"] = (meta.get(fc["symbol"]) or {}).get("name")
        fcs.append(fc)
    notes = {"data": "Official NSE end-of-day close of %s" % as_of,
             "universe_note": "Today's NIFTY 500 list (survivorship bias for past dates)",
             "simulated": kind == "simulated"}
    return store.save_cohort(week, as_of, kind, model.CHAMPION, "NIFTY 500", scored, notes, fcs, created_at)


def weekly(today=None):
    """This week's live cohort, from the latest official close before the week's first session."""
    t0 = time.time()
    model.ensure_models()
    today = today or _today()
    week = evaluate.week_of(today)
    if today.weekday() >= 5:  # at the weekend, prepare the coming week
        week += datetime.timedelta(days=7)
    if store.cohort_for(week, "live", model.CHAMPION):
        msg = "live cohort for week of %s already exists" % week
        data.log_run("weekly", "ok", 0, msg, t0)
        return msg
    last = data.latest_equity_date()
    if not last:
        raise RuntimeError("no official end-of-day data loaded yet - run the daily or backfill job first")
    as_of = datetime.date.fromisoformat(last)
    first_session = next(d for d in (week + datetime.timedelta(days=i) for i in range(7)) if cal.is_trading_day(d))
    # before the week's first session: Friday's close is needed; a manual run mid-week: at least yesterday's
    expected = data.prev_trading_day(first_session) if today <= first_session else data.prev_trading_day(today)
    if as_of < expected:
        raise RuntimeError("official data only goes to %s but %s is needed: NSE's file isn't available yet. "
                           "No forecasts made from stale data." % (as_of, expected))
    meta = data.research_universe()
    st = features.Store(set(meta))
    cid, made = make_cohort(st, meta, week, as_of, "live")
    msg = "live cohort %d for week of %s from the close of %s (%s)" % (cid, week, as_of, "new" if made else "existing")
    data.log_run("weekly", "ok", 10 if made else 0, msg, t0)
    return msg


def backfill(days=730):
    t0 = time.time()
    meta = data.research_universe()
    end = _today() - datetime.timedelta(days=1)
    loaded, missing = data.load_bhavcopy_range(end - datetime.timedelta(days=days), end, keep=set(meta),
                                               on_progress=lambda i, n: _progress("backfill", i, n))
    n_ix, ix_err = data.load_indices("2y" if days <= 730 else "5y")
    msg = "official days loaded %d, unavailable %d (holidays or not published), index bars %d" % (
        loaded, len(missing), n_ix)
    if ix_err:
        msg += " | " + "; ".join(ix_err[:3])
    data.log_run("backfill", "ok", loaded, msg, t0)
    return msg


def simulate(weeks=52):
    """Replay past Mondays as SIMULATED cohorts (data up to the previous session only), then score them."""
    t0 = time.time()
    model.ensure_models()
    meta = data.research_universe()
    st = features.Store(set(meta))
    this_week = evaluate.week_of(_today())
    made = skipped = 0
    for k in range(weeks, 0, -1):
        week = this_week - datetime.timedelta(weeks=k)
        if store.cohort_for(week, "simulated", model.CHAMPION):
            skipped += 1
            continue
        as_of = data.prev_trading_day(week)  # the last session before that Monday
        if not st.close_on("^NSEI", as_of):
            skipped += 1  # no market data for that date
            continue
        try:
            make_cohort(st, meta, week, as_of, "simulated")
            made += 1
        except RuntimeError:
            skipped += 1
        _progress("simulate", weeks - k + 1, weeks)
    scored = evaluate.evaluate_pending(st)
    msg = "simulated cohorts made %d, skipped %d (existing or no data), outcomes scored %d" % (made, skipped, scored)
    data.log_run("simulate", "ok", made, msg, t0)
    return msg


JOBS = {"daily": daily, "weekly": weekly, "backfill": backfill, "simulate": simulate}


def _progress(job, i, n):
    with _lock:
        if job in _status:
            _status[job]["progress"] = "%d of %d" % (i, n)


def start(job, *args):
    """Run a job in a background thread (the Research tab's 'Run now')."""
    with _lock:
        if _status.get(job, {}).get("state") == "running":
            return _status[job]
        _status[job] = {"state": "running", "started": time.time(), "progress": None, "message": None}

    def go():
        try:
            msg = JOBS[job](*args)
            state = "done"
        except Exception as e:  # noqa: BLE001 - reported on the page, never crashes the app
            traceback.print_exc()
            msg, state = "%s: %s" % (type(e).__name__, str(e)[:300]), "error"
            try:
                data.log_run(job, "error", 0, msg)
            except Exception:  # noqa: BLE001
                pass
        with _lock:
            _status[job].update(state=state, message=msg, finished=time.time())
    threading.Thread(target=go, daemon=True, name="research-" + job).start()
    return _status[job]


def status():
    with _lock:
        return {k: dict(v) for k, v in _status.items()}


def main(argv):
    db.db()
    model.ensure_models()
    if not argv or argv[0] not in list(JOBS) + ["all"]:
        print(__doc__)
        return 2
    names = ["backfill", "simulate", "daily", "weekly"] if argv[0] == "all" else [argv[0]]
    code = 0
    for name in names:
        args = [int(a) for a in argv[1:]] if argv[0] != "all" else []
        try:
            print("[%s] %s" % (name, JOBS[name](*args)), flush=True)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            data.log_run(name, "error", 0, "%s: %s" % (type(e).__name__, str(e)[:300]))
            print("[%s] FAILED: %s" % (name, e), flush=True)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
