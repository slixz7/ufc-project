#!/usr/bin/env python3
"""
predict.py - score fights with a model trained ONLY on earlier fights, and log the results.

For every eligible fight that has no prediction yet, this script
  1. trains the notebook-3 logistic regression on all eligible fights from BEFORE that event
  2. predicts the fight, records who the model picked and whether it was right
  3. appends the row to the fight_predictions table (the Power BI dashboard reads it)

Every prediction therefore uses only information that existed before the event. It is a fair
out-of-sample scoreboard, but note it runs AFTER the results are scraped, so the predictions are
"as if made before the card" rather than published in advance. (Predicting upcoming cards needs the
upcoming matchups scraped first - a later step.)

Only fights where both fighters have at least --min-prior earlier UFC fights are scored (default 3),
because the features are unreliable for fighters with little history. That is roughly 45% of fights.

Usage
    python pipeline/predict.py                    # score anything new since 2025-01-01
    python pipeline/predict.py --since 2024-01-01 # backfill further
    python pipeline/predict.py --dry-run          # show what would be scored, write nothing
    python pipeline/predict.py --rescore          # delete + redo predictions since --since

Environment: DATABASE_URL (default postgresql+psycopg2://postgres:password@localhost:5432/ufc)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("predict")

DEFAULT_DB_URL = "postgresql+psycopg2://postgres:password@localhost:5432/ufc"
SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "predictions.sql"
MIN_TRAIN_FIGHTS = 500


def make_engine():
    url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    # hosted providers (Neon, Supabase, Heroku) hand out postgres:// or postgresql:// URLs
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    return sa.create_engine(url)


def make_model(C: float):
    # No intercept + scale-only standardising: swapping fighters A and B flips the sign of every
    # difference and the outcome, so the model must treat both orderings the same way.
    return make_pipeline(StandardScaler(with_mean=False),
                         LogisticRegression(C=C, fit_intercept=False, max_iter=2000))


def load_eligible(engine, min_prior: int) -> pd.DataFrame:
    sql = """SELECT m.*, f.event_name
             FROM model_rows m JOIN fights f USING (fight_url)
             WHERE m.min_prev_fights >= :k
             ORDER BY m.event_date, m.fight_url"""
    df = pd.read_sql(sa.text(sql), engine, params={"k": min_prior})
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df


def score(df: pd.DataFrame, todo: pd.DataFrame, C: float) -> pd.DataFrame:
    diffs = [c for c in df.columns if c.startswith("diff_")]
    df = df.copy()
    todo = todo.copy()
    df[diffs] = df[diffs].astype(float).fillna(0)
    todo[diffs] = todo[diffs].astype(float).fillna(0)

    out = []
    for event_date in sorted(todo.event_date.unique()):
        train = df[df.event_date < event_date]                  # strictly earlier events only
        batch = todo[todo.event_date == event_date]
        if len(train) < MIN_TRAIN_FIGHTS:
            log.warning("Skipping %s: only %d earlier fights to train on", str(event_date)[:10], len(train))
            continue
        model = make_model(C).fit(train[diffs], train.a_wins)
        p_a = model.predict_proba(batch[diffs])[:, 1]
        res = pd.DataFrame({
            "fight_url": batch.fight_url.values,
            "event_date": batch.event_date.dt.date.values,
            "event_name": batch.event_name.values,
            "weight_class": batch.weight_class.values,
            "fighter_a": batch.fighter_a.values,
            "fighter_b": batch.fighter_b.values,
            "p_a_wins": p_a,
            "confidence": np.maximum(p_a, 1 - p_a),
            "predicted_winner": np.where(p_a >= 0.5, batch.fighter_a.values, batch.fighter_b.values),
            "actual_winner": np.where(batch.a_wins.values == 1, batch.fighter_a.values, batch.fighter_b.values),
            "trained_on_fights": len(train),
        })
        res["correct"] = (res.predicted_winner == res.actual_winner).astype(int)
        out.append(res)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score fights and log predictions to Postgres")
    ap.add_argument("--since", default="2025-01-01", help="only score events on/after this date")
    ap.add_argument("--min-prior", type=int, default=3, help="min earlier UFC fights for BOTH fighters")
    ap.add_argument("--C", type=float, default=1.0, help="regularization strength (from notebook 3)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", action="store_true", help="delete and redo predictions since --since")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = make_engine()
    with engine.begin() as conn:                       # create table + views if they don't exist yet
        conn.exec_driver_sql(SQL_FILE.read_text())
        if args.rescore and not args.dry_run:
            n = conn.execute(sa.text("DELETE FROM fight_predictions WHERE event_date >= :d"),
                             {"d": args.since}).rowcount
            log.info("Deleted %d existing predictions since %s", n, args.since)

    df = load_eligible(engine, args.min_prior)
    with engine.connect() as conn:
        done = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fight_predictions"))}
    todo = df[(df.event_date >= pd.Timestamp(args.since)) & (~df.fight_url.isin(done))]
    log.info("%d eligible fights in total; %d new fights to score since %s", len(df), len(todo), args.since)
    if todo.empty:
        log.info("Nothing to score.")
        return 0

    results = score(df, todo, args.C)
    if results.empty:
        return 0
    acc = results.correct.mean()
    log.info("Scored %d fights across %d events. Accuracy on these: %.1f%%",
             len(results), results.event_date.nunique(), 100 * acc)

    if args.dry_run:
        print(results.tail(10)[["event_date", "fighter_a", "fighter_b", "predicted_winner",
                                "confidence", "correct"]].to_string(index=False))
        log.info("[dry run] nothing written")
        return 0

    with engine.begin() as conn:
        results.to_sql("fight_predictions", conn, if_exists="append", index=False, method="multi", chunksize=500)
    log.info("Wrote %d rows to fight_predictions", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
