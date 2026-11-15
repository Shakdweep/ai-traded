"""Append-only prediction storage.

A weekly cohort = 10 predictions made at one time from one data date with one model version.
Predictions are only ever INSERTed (there is no code path that updates or deletes them).
Each one carries a SHA-256 hash of its content, so any later change is detectable
(verify()). Outcomes live in a separate table and are also insert-once.
kind = "live" (made in real time) or "simulated" (replayed on a past date using only data
available then); the two are never mixed in the dashboard.
"""

import hashlib
import json
import time

from .. import db

HASHED = ("cohort_id", "symbol", "as_of", "model_version", "kind", "created_at", "payload")


def _canonical(row):
    return json.dumps({k: row[k] for k in HASHED}, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(row):
    return hashlib.sha256(_canonical(row).encode()).hexdigest()


def cohort_for(week_of, kind, model_version):
    return db.q("SELECT * FROM cohorts WHERE week_of=? AND kind=? AND model_version=?",
                (str(week_of), kind, model_version), one=True)


def save_cohort(week_of, as_of, kind, model_version, universe, scored, notes, forecasts, created_at=None):
    """Create a cohort and its predictions once. Returns the cohort id (existing one if already made)."""
    existing = cohort_for(week_of, kind, model_version)
    if existing:
        return existing["id"], False
    created = created_at or time.time()
    try:
        cid = db.x("INSERT INTO cohorts(week_of, as_of, kind, model_version, universe, scored, created_at, notes) "
                   "VALUES(?,?,?,?,?,?,?,?)", (str(week_of), str(as_of), kind, model_version, universe, scored,
                                               created, json.dumps(notes)))
    except db.IntegrityError:  # made by a parallel run
        return cohort_for(week_of, kind, model_version)["id"], False
    rows = []
    for fc in forecasts:
        row = {"cohort_id": cid, "symbol": fc["symbol"], "as_of": str(as_of), "model_version": model_version,
               "kind": kind, "created_at": created, "payload": json.dumps(fc, sort_keys=True, default=str)}
        row["hash"] = content_hash(row)
        rows.append(row)
    db.xmany("INSERT INTO predictions(cohort_id, symbol, as_of, model_version, kind, created_at, payload, hash) "
             "VALUES(?,?,?,?,?,?,?,?)",
             [(r["cohort_id"], r["symbol"], r["as_of"], r["model_version"], r["kind"], r["created_at"], r["payload"],
               r["hash"]) for r in rows])
    return cid, True


def predictions(cohort_id=None, kind=None):
    sql, args = "SELECT * FROM predictions WHERE 1=1", []
    if cohort_id is not None:
        sql += " AND cohort_id=?"
        args.append(cohort_id)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    out = []
    for r in db.q(sql + " ORDER BY id", tuple(args)):
        r["forecast"] = json.loads(r["payload"])
        out.append(r)
    return out


def verify(rows=None):
    """Recompute every hash. -> (checked, list of ids whose content no longer matches)."""
    rows = rows if rows is not None else db.q("SELECT * FROM predictions")
    bad = [r["id"] for r in rows if content_hash(r) != r["hash"]]
    return len(rows), bad


def save_outcomes(rows):
    """Insert-once outcomes (prediction_id, horizon) - re-running never changes a stored result."""
    if not rows:
        return 0
    cols = ("prediction_id", "horizon", "eval_date", "actual_close", "actual_ret_pct", "window_high", "window_low",
            "predicted_dir", "actual_dir", "dir_correct", "in_range", "abs_err", "pct_err", "support_held",
            "resistance_broken", "stop_hit", "nifty_ret_pct", "evaluated_at")
    db.xmany("INSERT INTO outcomes(%s) VALUES(%s) ON CONFLICT(prediction_id, horizon) DO NOTHING"
             % (", ".join(cols), ", ".join("?" * len(cols))), [tuple(r[c] for c in cols) for r in rows])
    return len(rows)
