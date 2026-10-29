"""
Will the fight go the distance, with the betting market? (goes-the-distance model v2)

v1 (model_distance.py) predicts the chance of a decision from how long the two fighters' earlier fights lasted. v2 keeps that prediction
and adds exactly one thing: how strong the favourite is on the betting line (a 50/50 fight and an 85% favourite finish very differently).
It is a small second stage on top of v1: the v1 prediction, the favourite's strength, and its square, fitted on EARLIER fights only.

Why it exists, and how sure we are. pipeline/experiment_distance_market.py tested this against a rule fixed in advance. It passed, but
narrowly: a log-loss gain of about 0.003, with a 95% interval whose low end was barely above zero. So v2 is added ALONGSIDE v1, never instead
of it: both are locked before each event and both are scored on the same fights, so the live record can settle whether the line really
helps. It is a market-informed prediction in the same sense as the winner model v3, and it is not betting advice; no odds are shown.

    python pipeline/model_distance_v2.py setup                          # create the two tables
    python pipeline/model_distance_v2.py backtest [--rebuild]           # a v2 prediction for every past fight that has a line (earlier fights only)
    python pipeline/model_distance_v2.py predict [--today D] [--within-days N] [--dry-run]   # lock v2 for bouts that have a v1 prediction and a line
    python pipeline/model_distance_v2.py grade                          # after the results are in the dataset
    python pipeline/model_distance_v2.py scoring                        # the report, against v1 on the same fights

A v2 prediction is locked once, with the latest line at that moment, and never changes. If v1 is rebuilt from scratch, rebuild v2 too.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import distance_scores
import model_distance as v1
import model_v2 as v2

log = logging.getLogger("model_distance_v2")
MIN_TRAIN = 800                     # fights with lines before the second stage trains (the experiment's own threshold)
C_REG = 1.0
EPS = 1e-6
TABLE_PAST, TABLE_UP = "fight_distance_v2", "upcoming_distance_v2"

DDL = [
    f"""CREATE TABLE IF NOT EXISTS {TABLE_PAST} (
        fight_url TEXT PRIMARY KEY, event_date DATE, event_name TEXT, weight_class TEXT, scheduled_rounds INTEGER,
        fighter_a TEXT, fighter_b TEXT, tier TEXT, p_v1 DOUBLE PRECISION, fav_prob DOUBLE PRECISION, p_distance DOUBLE PRECISION,
        p_base DOUBLE PRECISION, went_distance INTEGER, trained_on_fights INTEGER, scored_at TIMESTAMPTZ)""",
    f"""CREATE TABLE IF NOT EXISTS {TABLE_UP} (
        bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, bout_order INTEGER, weight_class TEXT, scheduled_rounds INTEGER,
        fighter_a TEXT, fighter_b TEXT, tier TEXT, p_v1 DOUBLE PRECISION, fav_prob DOUBLE PRECISION, p_distance DOUBLE PRECISION,
        p_base DOUBLE PRECISION, trained_on_fights INTEGER, status TEXT, predicted_at TIMESTAMPTZ, went_distance INTEGER, graded_at TIMESTAMPTZ)""",
]


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.exec_driver_sql(stmt)


# ----------------------------------------------------------------------------------------------
# the second stage: v1's prediction + how strong the favourite is
# ----------------------------------------------------------------------------------------------
def logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def stack_design(p_v1, fav_prob) -> np.ndarray:
    """[v1's prediction on the log-odds scale, the favourite's strength, its square]. The strength is how far the favourite's chance (margin
    removed) is above 50%, so it is the same whichever fighter is listed first."""
    s = np.asarray(fav_prob, dtype=float) - 0.5
    return np.column_stack([logit(p_v1), s, s ** 2])


def fit_stacker(T: pd.DataFrame):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C_REG, max_iter=3000)).fit(stack_design(T.p_v1.values, T.fav_prob.values), T.went_distance.astype(int))


def stack_predict(model, p_v1, fav_prob) -> np.ndarray:
    return model.predict_proba(stack_design(p_v1, fav_prob))[:, 1]


def training_table(engine) -> pd.DataFrame:
    """Every past fight with both a stored v1 prediction and an opening line, oldest first."""
    T = pd.read_sql(sa.text("""
        SELECT d.fight_url, d.event_date, d.event_name, d.weight_class, d.scheduled_rounds, d.fighter_a, d.fighter_b, d.tier,
               d.p_distance AS p_v1, d.p_base, d.went_distance, o.open_prob_f1
        FROM fight_distance_v1 d JOIN fight_odds o ON o.fight_id = split_part(d.fight_url, '/', 5)
        WHERE o.open_prob_f1 > 0 AND o.open_prob_f1 < 1 AND d.p_distance > 0 AND d.p_distance < 1
        ORDER BY d.event_date, d.fight_url"""), engine)
    T["event_date"] = pd.to_datetime(T.event_date)
    T["fav_prob"] = np.maximum(T.open_prob_f1, 1 - T.open_prob_f1)
    return T.drop(columns="open_prob_f1")


# ----------------------------------------------------------------------------------------------
# backtest
# ----------------------------------------------------------------------------------------------
def run_backtest(engine, rebuild: bool = False) -> int:
    ensure_tables(engine)
    v1.ensure_tables(engine)
    with engine.begin() as conn:
        if rebuild:
            conn.execute(sa.text(f"DELETE FROM {TABLE_PAST}"))
        done = {r[0] for r in conn.execute(sa.text(f"SELECT fight_url FROM {TABLE_PAST}"))}
    T = training_table(engine)
    if T.empty:
        log.warning("No fights have both a goes-the-distance prediction and a line yet (run model_distance.py backtest and model_v3.py load-odds); nothing to score.")
        return 0                                  # not an error: the daily job calls this, and until lines are loaded there is simply nothing to do
    todo = T[~T.fight_url.isin(done)]
    out, skipped = [], 0
    for when, te in todo.groupby("event_date"):
        tr = T[T.event_date < when]
        if len(tr) < MIN_TRAIN:
            skipped += len(te)
            continue
        r = te.copy()
        r["p_distance"] = stack_predict(fit_stacker(tr), te.p_v1.values, te.fav_prob.values)
        r["trained_on_fights"] = len(tr)
        out.append(r)
    if not out:
        log.info("Nothing new to score (%d fights are before the second stage has enough history).", skipped)
        return 0
    R = pd.concat(out, ignore_index=True)
    R["event_date"] = R.event_date.dt.date
    R["scored_at"] = datetime.now(timezone.utc)
    cols = ["fight_url", "event_date", "event_name", "weight_class", "scheduled_rounds", "fighter_a", "fighter_b", "tier", "p_v1", "fav_prob", "p_distance",
            "p_base", "went_distance", "trained_on_fights", "scored_at"]
    with engine.begin() as conn:
        R[cols].to_sql(TABLE_PAST, conn, if_exists="append", index=False, method="multi", chunksize=200)
    y = R.went_distance.astype(float).to_numpy()
    ll = lambda p: float(-np.mean(y * np.log(np.clip(p, 1e-4, 1 - 1e-4)) + (1 - y) * np.log(1 - np.clip(p, 1e-4, 1 - 1e-4))))
    log.info("Scored %d past fights. Log loss: market-informed %.4f vs v1 %.4f vs the recent decision rate %.4f", len(R), ll(R.p_distance.values), ll(R.p_v1.values), ll(R.p_base.values))
    return 0


# ----------------------------------------------------------------------------------------------
# predictions for announced bouts
# ----------------------------------------------------------------------------------------------
def run_predict(engine, today: date, within_days: int | None = None, dry_run: bool = False) -> int:
    ensure_tables(engine)
    with engine.begin() as conn:      # keep cancelled and reinstated bouts in step with the card
        conn.execute(sa.text(f"""UPDATE {TABLE_UP} SET status = CASE WHEN bout_id IN (SELECT bout_id FROM upcoming_fights WHERE status = 'cancelled')
                                 THEN 'cancelled' ELSE 'locked' END WHERE graded_at IS NULL AND status IN ('locked', 'cancelled')"""))
    window = "AND u.event_date <= :lim" if within_days is not None else ""
    params = {"t": today}
    if within_days is not None:
        params["lim"] = today + timedelta(days=int(within_days))
    todo = pd.read_sql(sa.text(f"""
        SELECT o.bout_id, o.market_prob_a, u.event_name, u.event_date, u.bout_order, u.weight_class,
               v.fighter_a, v.fighter_b, v.scheduled_rounds, v.tier, v.p_distance AS p_v1, v.p_base
        FROM upcoming_odds o
        JOIN upcoming_fights u USING (bout_id)
        LEFT JOIN upcoming_distance_v1 v ON v.bout_id = o.bout_id AND v.status = 'locked'
        LEFT JOIN {TABLE_UP} p ON p.bout_id = o.bout_id
        WHERE p.bout_id IS NULL AND u.status = 'scheduled' AND u.event_date > :t {window}
              AND o.market_prob_a > 0 AND o.market_prob_a < 1
        ORDER BY u.event_date, u.bout_order"""), engine, params=params)
    if todo.empty:
        log.info("No new bouts to predict (no line loaded, event not in the future or not yet inside the lock window, or already locked).")
        return 0
    missing = todo[todo.p_v1.isna()]
    if len(missing):
        log.warning("%d bout(s) have a line but no goes-the-distance prediction yet; they are skipped for now (%s).", len(missing),
                    ", ".join(str(b) for b in missing.bout_id))
        todo = todo[todo.p_v1.notna()].copy()
        if todo.empty:
            return 0
    T = training_table(engine)
    out = []
    for when in sorted(todo.event_date.unique()):
        te = todo[todo.event_date == when].copy()
        tr = T[T.event_date < pd.Timestamp(when)]
        if len(tr) < MIN_TRAIN:
            log.error("Only %d fights with lines to learn from; need %d. Run model_v3.py load-odds and model_distance_v2.py backtest.", len(tr), MIN_TRAIN)
            return 2
        te["fav_prob"] = np.maximum(te.market_prob_a, 1 - te.market_prob_a)
        te["p_distance"] = stack_predict(fit_stacker(tr), te.p_v1.values, te.fav_prob.values)
        te["trained_on_fights"] = len(tr)
        out.append(te)
    R = pd.concat(out, ignore_index=True)
    R["status"], R["predicted_at"] = "locked", datetime.now(timezone.utc)
    R["scheduled_rounds"] = R.scheduled_rounds.astype(int)
    cols = ["bout_id", "event_name", "event_date", "bout_order", "weight_class", "scheduled_rounds", "fighter_a", "fighter_b", "tier", "p_v1", "fav_prob",
            "p_distance", "p_base", "trained_on_fights", "status", "predicted_at"]
    if dry_run:
        log.info("[dry run] %d market-informed goes-the-distance predictions would be locked; nothing written", len(R))
    else:
        with engine.begin() as conn:
            R[cols].to_sql(TABLE_UP, conn, if_exists="append", index=False, method="multi", chunksize=200)
        log.info("Locked %d market-informed goes-the-distance predictions (the second stage learned from %d earlier fights)", len(R), int(R.trained_on_fights.iloc[0]))
    for r in R.itertuples():
        print(f"  {r.event_date}  {r.fighter_a:24s} vs {r.fighter_b:24s} -> {r.p_distance:.0%} goes the distance (model-only said {r.p_v1:.0%}, favourite {r.fav_prob:.0%})")
    return 0


# ----------------------------------------------------------------------------------------------
# grading
# ----------------------------------------------------------------------------------------------
def run_grade(engine) -> int:
    ensure_tables(engine)
    return v1.run_grade(engine, TABLE_UP)


# ----------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------
def cmd_scoring(engine) -> int:
    d = pd.read_sql(sa.text(f"SELECT event_date, p_distance, p_v1, p_base, went_distance, tier FROM {TABLE_PAST}"), engine)
    if d.empty:
        log.error("No market-informed backtest yet. Run: python pipeline/model_distance_v2.py backtest")
        return 2
    s = distance_scores.distance_scores(d)
    print(f"\n{s['n']:,} fights with a line ({s['first_year']} to {s['last_year']}); {s['rate']:.1%} went the distance. Lower is better, except AUC.\n")
    print(f"{'Predictor':40s} {'Log loss':>9s} {'Brier':>8s} {'AUC':>6s}")
    v1_s = distance_scores.distance_scores(d.assign(p_distance=d.p_v1))["scores"]["model"]
    rows = (("Market-informed (v2)", s["scores"]["model"]), ("Model-only (v1), same fights", v1_s), ("Last-3-years decision rate", s["scores"]["recent"]), ("Coin flip", s["scores"]["coin"]))
    for label, x in rows:
        print(f"{label:40s} {x['log_loss']:9.4f} {x['brier']:8.4f} {x['auc']:6.3f}")
    cmp = distance_scores.paired_vs(d, "p_distance", "p_v1")
    print(f"\nPaired log-loss difference, market-informed minus model-only: {cmp['mean']:+.4f}  [{cmp['lo']:+.4f}, {cmp['hi']:+.4f}]  {cmp['verdict']}")
    yb, yt = distance_scores.years_better_vs(d, "p_distance", "p_v1")
    print(f"Better than model-only in {yb} of {yt} years.")
    print("\nCalibration of the market-informed prediction: predicted chance of going the distance vs what happened")
    for c in s["calibration"]:
        print(f"  {c['label']:12s} {c['n']:5d} fights  predicted {c['predicted']:.1%}  actual {c['actual']:.1%}")
    live = pd.read_sql(sa.text(f"""SELECT m.p_distance AS p_v2, v.p_distance AS p_v1, m.p_base, m.went_distance
                                   FROM {TABLE_UP} m JOIN upcoming_distance_v1 v ON v.bout_id = m.bout_id
                                   WHERE m.status = 'locked' AND m.graded_at IS NOT NULL AND m.went_distance IS NOT NULL"""), engine)
    print(f"\nThe locked record (predictions made before their events, now graded): {len(live)}")
    if len(live):
        y = live.went_distance.astype(float).to_numpy()
        ll = lambda p: float(-np.mean(y * np.log(np.clip(p, 1e-4, 1 - 1e-4)) + (1 - y) * np.log(1 - np.clip(p, 1e-4, 1 - 1e-4))))
        print(f"  log loss: market-informed {ll(live.p_v2.values):.4f}   model-only {ll(live.p_v1.values):.4f}   (a few cards are too few fights to conclude anything)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Goes-the-distance model v2 (with the betting market)")
    sub = ap.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("backtest")
    bt.add_argument("--rebuild", action="store_true", help="score every fight again from scratch")
    pr = sub.add_parser("predict")
    pr.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    pr.add_argument("--within-days", type=int, default=None, help="only lock bouts whose event is at most this many days away")
    pr.add_argument("--dry-run", action="store_true", help="show what would be locked; write nothing")
    for name in ("setup", "grade", "scoring"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = v2.make_engine()
    if args.command == "setup":
        ensure_tables(engine)
        log.info("Tables ready.")
        return 0
    if args.command == "backtest":
        return run_backtest(engine, args.rebuild)
    if args.command == "predict":
        return run_predict(engine, date.fromisoformat(args.today) if args.today else date.today(), args.within_days, args.dry_run)
    if args.command == "grade":
        return run_grade(engine)
    return cmd_scoring(engine)


if __name__ == "__main__":
    sys.exit(main())
