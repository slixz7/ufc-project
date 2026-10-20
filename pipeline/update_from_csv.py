#!/usr/bin/env python3
"""
update_from_csv.py - add new fights from the maintained Kaggle dataset (scarekrow/ufc-data) to Postgres.

Why this exists: ufcstats.com blocks plain HTTP scraping (it serves a JavaScript "Checking your browser"
page), but the dataset author publishes an updated copy after every UFC card. This script reads a
downloaded copy of that CSV and appends any fights your database doesn't have yet. The SQL views
(fighter_features, model_rows) update themselves, and pipeline/predict.py then scores the new fights.

Usage
    python pipeline/update_from_csv.py --csv data/UFC_full_data_silver_v2.csv --dry-run
    python pipeline/update_from_csv.py --csv data/UFC_full_data_silver_v2.csv
    python pipeline/update_from_csv.py --csv data/                       # a folder: finds the right CSV
    python pipeline/update_from_csv.py --csv data/new.csv --replace-since 2026-09-01
        (deletes your rows from that date on and reloads them, to pick up corrections)

Fights without a winner (draws, no contests) are skipped, as in notebook 1.
Existing fights are never changed unless you use --replace-since.

Environment: DATABASE_URL (default postgresql+psycopg2://postgres:password@localhost:5432/ufc)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

log = logging.getLogger("update_from_csv")

DEFAULT_DB_URL = "postgresql+psycopg2://postgres:password@localhost:5432/ufc"
FIGHT_COLS = ["fight_url", "event_date", "event_name", "weight_class", "title_fight", "num_rounds",
              "result", "finish_round", "finish_time"]
REQUIRED = FIGHT_COLS + ["winner", "f_1_url", "f_2_url", "f_1_name", "f_2_name"]


def make_engine():
    url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    for prefix in ("postgres://", "postgresql://"):          # hosted providers (Neon, Supabase)
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    return sa.create_engine(url)


GENERATED = {"ufc_clean.csv", "ufc_features.csv", "ufc_test_predictions.csv", "ufc_model_coefficients.csv"}


def find_csv(path: str) -> Path:
    """A file path is used as given. For a folder, pick the newest CSV that has the fights layout,
    skipping files your own notebooks generate (ufc_clean.csv etc.), which would be stale."""
    p = Path(path)
    if p.is_file():
        return p
    if p.is_dir():
        candidates = [c for c in p.rglob("*.csv") if c.name.lower() not in GENERATED]
        for c in sorted(candidates, key=lambda f: f.stat().st_mtime, reverse=True):
            cols = pd.read_csv(c, nrows=0).columns
            if "f_1_url" in cols and "fight_url" in cols:
                return c
    raise SystemExit(f"No fights CSV found at {path}")


def load_csv(csv: Path) -> pd.DataFrame:
    df = pd.read_csv(csv, parse_dates=["event_date"])
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise SystemExit(f"{csv.name} is missing expected columns: {missing[:6]} ... "
                         "The dataset layout may have changed.")
    dupes = df.fight_url.duplicated().sum()
    if dupes:
        log.warning("%d duplicate fight_url rows in the CSV; keeping the first of each", dupes)
        df = df.drop_duplicates("fight_url")
    has_winner = df.winner.notna() & ((df.winner == df.f_1_name) | (df.winner == df.f_2_name))
    log.info("CSV: %d fights, %d with a usable winner (draws/no-contests skipped)", len(df), has_winner.sum())
    return df[has_winner].copy()


def side_rows(df: pd.DataFrame, me: int, opp: int) -> pd.DataFrame:
    rd = lambda who, stat: df[[f"f_{who}_r{i}_{stat}" for i in range(1, 6)]].sum(axis=1)
    return pd.DataFrame({
        "fight_url": df.fight_url, "fighter_url": df[f"f_{me}_url"], "fighter_name": df[f"f_{me}_name"],
        "side": me, "won": (df.winner == df[f"f_{me}_name"]).astype(int),
        "sig_land": df[f"f_{me}_sig_strikes_succ"], "sig_att": df[f"f_{me}_sig_strikes_att"],
        "td_land": df[f"f_{me}_takedown_succ"], "td_att": df[f"f_{me}_takedown_att"],
        "sub_att": df[f"f_{me}_submission_att"], "kd": df[f"f_{me}_knockdowns"],
        "ctrl_sec": df[f"f_{me}_ctrl_time_sec"],
        "dist_att": rd(me, "distance_att"), "clinch_att": rd(me, "clinch_att"), "ground_att": rd(me, "ground_att"),
        "sig_absorbed": df[f"f_{opp}_sig_strikes_succ"],
        "opp_td_land": df[f"f_{opp}_takedown_succ"], "opp_td_att": df[f"f_{opp}_takedown_att"],
        "height_cm": df[f"f_{me}_fighter_height_cm"], "reach_cm": df[f"f_{me}_fighter_reach_cm"],
        "stance": df[f"f_{me}_fighter_stance"],
        "dob": pd.to_datetime(df[f"f_{me}_fighter_dob"], errors="coerce").dt.date,
    })


def build_tables(df: pd.DataFrame):
    fights = df[FIGHT_COLS].copy()
    fights["event_date"] = fights.event_date.dt.date
    ff = pd.concat([side_rows(df, 1, 2), side_rows(df, 2, 1)], ignore_index=True)
    return fights, ff


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Append new fights from the Kaggle CSV to Postgres")
    ap.add_argument("--csv", default="data/UFC_full_data_silver_v2.csv", help="CSV file, or a folder containing it")
    ap.add_argument("--dry-run", action="store_true", help="show what would be added; write nothing")
    ap.add_argument("--replace-since", metavar="YYYY-MM-DD",
                    help="delete your rows from this date on and reload them from the CSV (picks up corrections)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    csv = find_csv(args.csv)
    log.info("Reading %s", csv)
    df = load_csv(csv)
    engine = make_engine()

    with engine.connect() as conn:
        db_latest = conn.execute(sa.text("SELECT MAX(event_date) FROM fights")).scalar()
        if args.replace_since:
            since = date.fromisoformat(args.replace_since)
            keep = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fights WHERE event_date < :d"),
                                               {"d": since})}
            n_replace = conn.execute(sa.text("SELECT COUNT(*) FROM fights WHERE event_date >= :d"),
                                     {"d": since}).scalar()
        else:
            keep = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fights"))}
            n_replace = 0

    csv_latest = df.event_date.max().date()
    log.info("Latest event in database: %s | in CSV: %s", db_latest, csv_latest)
    if db_latest and csv_latest < db_latest:
        log.warning("The CSV is OLDER than your database. Download the latest copy from Kaggle.")

    new = df[~df.fight_url.isin(keep)].copy()
    if args.replace_since:
        new = new[new.event_date.dt.date >= since]
    if new.empty:
        log.info("Nothing new to add.")
        return 0

    by_event = new.groupby(["event_date", "event_name"]).size().reset_index(name="fights").sort_values("event_date")
    for _, r in by_event.iterrows():
        log.info("  %s  %-45s %d fights", r.event_date.date(), r.event_name, r.fights)
    log.info("%d new fights across %d events%s", len(new), len(by_event),
             f" (replacing {n_replace} existing rows since {args.replace_since})" if args.replace_since else "")
    if args.dry_run:
        log.info("[dry run] nothing written")
        return 0

    fights, ff = build_tables(new)
    with engine.begin() as conn:                         # all-or-nothing
        conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ux_ff_fight_fighter "
                             "ON fighter_fights (fight_url, fighter_url)"))
        if args.replace_since:
            for table in ("fighter_fights", "fights"):
                if table == "fighter_fights":
                    conn.execute(sa.text("DELETE FROM fighter_fights WHERE fight_url IN "
                                         "(SELECT fight_url FROM fights WHERE event_date >= :d)"), {"d": since})
                else:
                    conn.execute(sa.text("DELETE FROM fights WHERE event_date >= :d"), {"d": since})
        fights.to_sql("fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
        ff.to_sql("fighter_fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
    log.info("Added %d fights (%d fighter rows). Next: python pipeline/predict.py", len(fights), len(ff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
