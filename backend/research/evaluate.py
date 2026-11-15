"""Predicted vs actual, per prediction and horizon, plus the accuracy dashboard numbers.

Judged on official NSE closes, h trading days after the prediction's data date:
  direction    Up / Down / Sideways (a move smaller than the prediction's own flat band)
  in range     did the actual close land inside the predicted 80% range? ("coverage")
  error        |actual close - middle of the range| in rupees (MAE) and % (MAPE)
  support      did the lowest price in the window stay above the first support?
  resistance   did the highest price break the first resistance?
  stop         was the stop touched inside the window?
Only predictions whose horizon has fully passed are counted; the rest show as pending.
Accuracy is not profitability: the dashboard also shows the average result of acting on
the 7-day direction after 0.30% round-trip costs.
"""

import datetime
import time

from . import model, store

COSTS_PCT = 0.30         # delivery round trip incl. STT, charges and slippage (approx.)
MIN_GROUP = 10           # smallest group an indicator comparison is shown for


def _dir_ok(pred, actual):
    return {"Bullish": "Up", "Bearish": "Down", "Sideways": "Sideways"}[pred] == actual


def evaluate_pending(st, now=None):
    """Score every horizon that has fully passed and isn't stored yet. -> rows saved."""
    from .. import db
    done = {(r["prediction_id"], r["horizon"]) for r in db.q("SELECT prediction_id, horizon FROM outcomes")}
    rows = []
    for p in store.predictions():
        if all((p["id"], h) in done for h in model.HORIZONS):
            continue
        fc = p["forecast"]
        base = st.close_on(p["symbol"], p["as_of"])
        if not base:
            continue
        factor = base["c"] / fc["price"] if fc["price"] else 1.0  # later split/bonus adjustment
        after = st.after(p["symbol"], p["as_of"], max(model.HORIZONS))
        nifty0 = st.close_on("^NSEI", p["as_of"])
        for h in model.HORIZONS:
            if (p["id"], h) in done or len(after) < h:
                continue
            win = after[:h]
            close = win[-1]["c"]
            rng = fc["ranges"][str(h)]
            ret = (close / base["c"] - 1) * 100
            band = rng["flat_band_pct"]
            actual = "Up" if ret > band else "Down" if ret < -band else "Sideways"
            mid = rng["mid"] * factor
            hi, lo = max(b["h"] for b in win), min(b["l"] for b in win)
            sup = (fc.get("supports") or [None])[0]
            res = (fc.get("resistances") or [None])[0]
            stop = fc["stop"] * factor
            n1 = st.close_on("^NSEI", win[-1]["date"])
            rows.append({
                "prediction_id": p["id"], "horizon": h, "eval_date": win[-1]["date"], "actual_close": round(close, 2),
                "actual_ret_pct": round(ret, 3), "window_high": round(hi, 2), "window_low": round(lo, 2),
                "predicted_dir": fc["direction"], "actual_dir": actual,
                "dir_correct": int(_dir_ok(fc["direction"], actual)),
                "in_range": int(rng["low"] * factor <= close <= rng["high"] * factor),
                "abs_err": round(abs(close - mid), 2), "pct_err": round(abs(close - mid) / close * 100, 3),
                "support_held": None if not sup else int(lo >= sup * factor * 0.995),
                "resistance_broken": None if not res else int(hi > res * factor),
                "stop_hit": int(hi >= stop if fc["direction"] == "Bearish" else lo <= stop),
                "nifty_ret_pct": round((n1["c"] / nifty0["c"] - 1) * 100, 3) if nifty0 and n1 else None,
                "evaluated_at": now or time.time(),
            })
    return store.save_outcomes(rows)


# ---------------------------------------------------------------- dashboard numbers

def _rows(kind):
    from .. import db
    preds = {p["id"]: p for p in store.predictions(kind=kind)}
    outs = db.q("SELECT o.* FROM outcomes o JOIN predictions p ON p.id = o.prediction_id WHERE p.kind=?", (kind,))
    weeks = {c["id"]: c["week_of"] for c in db.q("SELECT id, week_of FROM cohorts WHERE kind=?", (kind,))}
    for o in outs:
        p = preds.get(o["prediction_id"])
        if p:
            o["fc"] = p["forecast"]
            o["week_of"] = weeks.get(p["cohort_id"])
    return preds, [o for o in outs if o.get("fc")]


def _agg(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    out = {"n": n,
           "dir_acc": round(100 * sum(r["dir_correct"] for r in rows) / n, 1),
           "coverage": round(100 * sum(r["in_range"] for r in rows) / n, 1),
           "mae": round(sum(r["abs_err"] for r in rows) / n, 2),
           "mape": round(sum(r["pct_err"] for r in rows) / n, 2),
           "stop_hit": round(100 * sum(r["stop_hit"] for r in rows) / n, 1)}
    sup = [r["support_held"] for r in rows if r["support_held"] is not None]
    res = [r["resistance_broken"] for r in rows if r["resistance_broken"] is not None]
    out["support_held"] = round(100 * sum(sup) / len(sup), 1) if sup else None
    out["resistance_broken"] = round(100 * sum(res) / len(res), 1) if res else None
    return out


def _bucket(conf):
    return "under 40" if conf < 40 else "40-59" if conf < 60 else "60-79" if conf < 80 else "80+"


def summary(kind="live", horizon=7):
    preds, outs = _rows(kind)
    by_h = {h: [o for o in outs if o["horizon"] == h] for h in model.HORIZONS}
    rows = by_h.get(horizon, [])
    evaluated_ids = {o["prediction_id"] for o in rows}
    res = {"kind": kind, "horizon": horizon, "predictions": len(preds), "evaluated": len(evaluated_ids),
           "pending": len(preds) - len(evaluated_ids),
           "horizons": {str(h): _agg(v) for h, v in by_h.items()}}
    a = _agg(rows)
    res["overall"] = a
    if rows:
        up = sum(1 for r in rows if r["actual_dir"] == "Up")
        res["baseline_always_bullish"] = round(100 * up / len(rows), 1)  # accuracy of just saying "up" every time
        res["correct"] = sum(r["dir_correct"] for r in rows)
        res["incorrect"] = len(rows) - res["correct"]
        # the fair benchmark: did the calls beat simply holding the market (NIFTY 50) over the same days?
        vs = [r for r in rows if r["nifty_ret_pct"] is not None and r["predicted_dir"] != "Sideways"]
        if vs:
            sign = lambda r: 1 if r["predicted_dir"] == "Bullish" else -1  # noqa: E731
            res["vs_nifty"] = {
                "n": len(vs),
                "beat_pct": round(100 * sum(1 for r in vs if sign(r) * (r["actual_ret_pct"] - r["nifty_ret_pct"]) > 0)
                                  / len(vs), 1),
                "avg_excess_pct": round(sum(sign(r) * (r["actual_ret_pct"] - r["nifty_ret_pct"]) for r in vs)
                                        / len(vs), 3),
                "avg_stock_pct": round(sum(r["actual_ret_pct"] for r in vs) / len(vs), 3),
                "avg_nifty_pct": round(sum(r["nifty_ret_pct"] for r in vs) / len(vs), 3)}

    def group(keyf):
        g = {}
        for r in rows:
            g.setdefault(keyf(r), []).append(r)
        return {k: _agg(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}
    res["by_direction"] = group(lambda r: r["predicted_dir"])
    res["by_confidence"] = group(lambda r: _bucket(r["fc"]["confidence"]))
    res["by_sector"] = group(lambda r: r["fc"].get("sector") or "Other")
    res["weekly"] = [dict(week=k, **v) for k, v in group(lambda r: r["week_of"]).items()]
    res["monthly"] = [dict(month=k, **v) for k, v in group(lambda r: (r["week_of"] or "")[:7]).items()]
    # which yes/no signals went with correct calls (at this horizon)
    ind = []
    from .features import SIGNALS
    for name, label in SIGNALS.items():
        on = [r for r in rows if (r["fc"].get("signals") or {}).get(name)]
        off = [r for r in rows if not (r["fc"].get("signals") or {}).get(name)]
        if on:
            acc_on = 100 * sum(r["dir_correct"] for r in on) / len(on)
            # a comparison needs enough cases on BOTH sides, otherwise the "lift" is noise
            acc_off = 100 * sum(r["dir_correct"] for r in off) / len(off) if len(off) >= MIN_GROUP else None
            if len(on) < MIN_GROUP:
                acc_off = None
            ind.append({"signal": name, "label": label, "n": len(on), "acc_when_present": round(acc_on, 1),
                        "acc_when_absent": round(acc_off, 1) if acc_off is not None else None,
                        "lift": round(acc_on - acc_off, 1) if acc_off is not None else None})
    ind.sort(key=lambda x: (x["lift"] is None, -(x["lift"] or 0)))
    res["indicators"] = ind
    # accuracy is not profitability: act on the 7-day direction, minus costs
    trades = [r for r in rows if r["predicted_dir"] != "Sideways"]
    if trades:
        pnl = [(r["actual_ret_pct"] if r["predicted_dir"] == "Bullish" else -r["actual_ret_pct"]) - COSTS_PCT
               for r in trades]
        res["trading"] = {"n": len(trades), "avg_net_pct": round(sum(pnl) / len(pnl), 3),
                          "win_pct": round(100 * sum(1 for x in pnl if x > 0) / len(pnl), 1), "costs_pct": COSTS_PCT}
    return res


def cohort_detail(st, cohort_id):
    """This week's 10: forecasts, latest official close, progress through each horizon, stored outcomes."""
    from .. import db
    c = db.q("SELECT * FROM cohorts WHERE id=?", (cohort_id,), one=True)
    if not c:
        return None
    outs = {}
    for o in db.q("SELECT o.* FROM outcomes o JOIN predictions p ON p.id=o.prediction_id WHERE p.cohort_id=?",
                  (cohort_id,)):
        outs.setdefault(o["prediction_id"], {})[str(o["horizon"])] = o
    items = []
    for p in store.predictions(cohort_id=cohort_id):
        fc = p["forecast"]
        after = st.after(p["symbol"], p["as_of"], max(model.HORIZONS))
        base = st.close_on(p["symbol"], p["as_of"])
        factor = base["c"] / fc["price"] if base and fc["price"] else 1.0
        last = after[-1] if after else None
        items.append({"id": p["id"], "symbol": p["symbol"], "hash": p["hash"], "created_at": p["created_at"],
                      "forecast": fc, "days_elapsed": len(after),
                      "latest": {"date": last["date"], "close": round(last["c"] / factor, 2),
                                 "ret_pct": round((last["c"] / base["c"] - 1) * 100, 2)} if last and base else None,
                      "outcomes": outs.get(p["id"], {})})
    c["items"] = items
    return c


def cohorts(kind=None):
    from .. import db
    sql = "SELECT c.*, (SELECT COUNT(*) FROM predictions p WHERE p.cohort_id=c.id) AS n FROM cohorts c"
    args = ()
    if kind:
        sql += " WHERE c.kind=?"
        args = (kind,)
    return db.q(sql + " ORDER BY week_of DESC, kind", args)


def week_of(d):
    return d - datetime.timedelta(days=d.weekday())
