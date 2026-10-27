#!/usr/bin/env python3
"""
upcoming.py - predict announced UFC cards BEFORE they happen, lock the predictions, and grade them afterwards.

Weekly routine
    python pipeline/upcoming.py load --csv upcoming/ufc-333.csv --dry-run   # check names match your dataset
    python pipeline/upcoming.py load --csv upcoming/ufc-333.csv             # save the card
    python pipeline/upcoming.py predict                                     # make + lock predictions
    ... the event happens, the dataset updates a day or two later ...
    python pipeline/update_from_csv.py --csv data/new                       # add the real results
    python pipeline/upcoming.py grade                                       # score the locked predictions

One-time setup (creates the tables and views; safe to re-run)
    python pipeline/upcoming.py setup

How it stays honest
  * Features for an upcoming bout come from the same SQL used for training, computed from each fighter's real
    fights before the event date.
  * A prediction is saved once with a timestamp and is never overwritten. Predictions are only made while the
    event date is still in the future.
  * If a card changes (opponent swap, fight cancelled), reload the CSV: removed bouts are marked cancelled,
    new bouts get their own prediction, existing predictions are left alone.
  * Only bouts where both fighters have at least 3 earlier UFC fights get a probability; the rest are saved as
    "not_enough_history".

Card CSV columns: event_name, event_date, bout_order, weight_class, title_fight, fighter_a, fighter_b
Name matching: names are normalised (case, accents, apostrophes, hyphens). If a card name differs from the
dataset's spelling, add a line to upcoming/aliases.csv:  card_name,dataset_name  (use NEW for a fighter
with no UFC history, or a ufcstats fighter URL if two fighters share a name).

Environment: DATABASE_URL (default postgresql+psycopg2://postgres:password@localhost:5432/ufc)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import logging
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("upcoming")

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql"
DEFAULT_DB_URL = "postgresql+psycopg2://postgres:password@localhost:5432/ufc"
MIN_PRIOR = 3
C_REG = 1.0                      # regularisation strength chosen in notebook 3
MIN_TRAIN_FIGHTS = 500


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def make_engine():
    url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    return sa.create_engine(url)


def norm(name: str) -> str:
    """'Lone’er Kavanagh' / "Lone'er Kavanagh" / 'LONEER KAVANAGH' -> 'loneer kavanagh'."""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"['’‘`´]", "", s)
    s = re.sub(r"[-–—.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def make_model():
    # no intercept + scale-only standardising: swapping fighters A and B must flip the prediction (see notebook 3)
    return make_pipeline(StandardScaler(with_mean=False),
                         LogisticRegression(C=C_REG, fit_intercept=False, max_iter=2000))


def parse_bool(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "t", "title"}


def bout_id(event_date: date, url_a: str, url_b: str) -> str:
    key = f"{event_date.isoformat()}|" + "|".join(sorted([url_a, url_b]))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def fighter_index(engine) -> pd.DataFrame:
    sql = """SELECT ff.fighter_url, ff.fighter_name, COUNT(*) AS n_fights, MAX(f.event_date) AS last_fight,
                    (array_agg(f.weight_class ORDER BY f.event_date DESC))[1] AS weight_class
             FROM fighter_fights ff JOIN fights f USING (fight_url)
             GROUP BY ff.fighter_url, ff.fighter_name"""
    idx = pd.read_sql(sa.text(sql), engine)
    idx["norm"] = idx.fighter_name.map(norm)
    return idx


def load_aliases(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str).dropna()
    return {norm(r.card_name): r.dataset_name.strip() for r in df.itertuples()}


class Unresolved(Exception):
    pass


def resolve(name: str, idx: pd.DataFrame, aliases: dict[str, str], allow_new: bool):
    """Return (fighter_url, dataset_name, n_prior_fights, how)."""
    key = norm(name)
    target = aliases.get(key)
    if target:
        if target.upper() == "NEW":
            return f"new:{key.replace(' ', '-')}", None, 0, "new (debut)"
        if target.lower().startswith("http"):
            row = idx[idx.fighter_url == target]
            if row.empty:
                raise Unresolved(f"alias URL for '{name}' is not in your data: {target}")
            r = row.iloc[0]
            return r.fighter_url, r.fighter_name, int(r.n_fights), "alias (url)"
        key = norm(target)
        how = "alias"
    else:
        how = "exact"
    hits = idx[idx.norm == key]
    if len(hits) == 1:
        r = hits.iloc[0]
        return r.fighter_url, r.fighter_name, int(r.n_fights), how
    if len(hits) > 1:
        urls = ", ".join(hits.fighter_url.tolist())
        raise Unresolved(f"'{name}' matches {len(hits)} different fighters. Add a line to aliases.csv with the right "
                         f"ufcstats URL as dataset_name. Candidates: {urls}")
    if allow_new:
        return f"new:{norm(name).replace(' ', '-')}", None, 0, "new (--allow-new)"
    # suggestions: close spellings, and fighters sharing the last name
    near = difflib.get_close_matches(key, idx.norm.tolist(), n=3, cutoff=0.72)
    last = key.split()[-1]
    near += [n for n in idx.norm[idx.norm.str.split().str[-1] == last].tolist() if n not in near][:3]
    sugg = []
    for n in near[:4]:
        r = idx[idx.norm == n].iloc[0]
        sugg.append(f"{r.fighter_name} ({int(r.n_fights)} UFC fights, last {r.last_fight}, {r.weight_class})")
    raise Unresolved(f"'{name}' is not in your data. " + ("Did you mean: " + "; ".join(sugg) + "?" if sugg else
                     "No similar names; if this is a UFC debut add 'NEW' for it in aliases.csv."))


# ----------------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    engine = make_engine()
    for f in ("features.sql", "predictions.sql"):
        with engine.begin() as conn:
            conn.exec_driver_sql((SQL_DIR / f).read_text())
        log.info("Applied %s", f)
    return 0


def cmd_load(args) -> int:
    engine = make_engine()
    today = date.fromisoformat(args.today) if args.today else date.today()
    card = pd.read_csv(args.csv, dtype=str)
    need = ["event_name", "event_date", "bout_order", "weight_class", "title_fight", "fighter_a", "fighter_b"]
    miss = [c for c in need if c not in card.columns]
    if miss:
        raise SystemExit(f"{args.csv} is missing columns: {miss}")
    card["event_date"] = pd.to_datetime(card.event_date).dt.date
    idx = fighter_index(engine)
    aliases = load_aliases(Path(args.aliases))

    bouts, problems = [], []
    for r in card.itertuples():
        if r.event_date <= today and not args.allow_past:
            raise SystemExit(f"Event date {r.event_date} is not in the future (today is {today}). "
                             "Predictions must be made before the event.")
        try:
            ua, na, nfa, ha = resolve(r.fighter_a, idx, aliases, args.allow_new)
            ub, nb, nfb, hb = resolve(r.fighter_b, idx, aliases, args.allow_new)
        except Unresolved as exc:
            problems.append(str(exc))
            continue
        bouts.append(dict(bout_id=bout_id(r.event_date, ua, ub), event_name=r.event_name, event_date=r.event_date,
                          bout_order=int(r.bout_order) if str(r.bout_order).isdigit() else None,
                          weight_class=r.weight_class, title_fight=parse_bool(r.title_fight),
                          fighter_a_name=r.fighter_a, fighter_a_url=ua, fighter_b_name=r.fighter_b, fighter_b_url=ub))
        log.info("%-24s [%s, %d fights]  vs  %-24s [%s, %d fights]", r.fighter_a, ha, nfa, r.fighter_b, hb, nfb)

    if problems:
        print("\nSome names could not be matched. Fix them in the CSV or add them to aliases.csv, then run again:")
        for p in problems:
            print("  -", p)
        return 2
    if args.dry_run:
        log.info("[dry run] %d bouts would be saved; nothing written", len(bouts))
        return 0

    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(sa.text("SELECT bout_id FROM upcoming_fights"))}
        for b in bouts:
            conn.execute(sa.text("""
                INSERT INTO upcoming_fights (bout_id, event_name, event_date, bout_order, weight_class, title_fight,
                                             fighter_a_name, fighter_a_url, fighter_b_name, fighter_b_url, status)
                VALUES (:bout_id, :event_name, :event_date, :bout_order, :weight_class, :title_fight,
                        :fighter_a_name, :fighter_a_url, :fighter_b_name, :fighter_b_url, 'scheduled')
                ON CONFLICT (bout_id) DO UPDATE SET event_name = EXCLUDED.event_name, bout_order = EXCLUDED.bout_order,
                    weight_class = EXCLUDED.weight_class, title_fight = EXCLUDED.title_fight, status = 'scheduled'
                """), b)           # fighter A/B orientation is kept as first saved, so locked predictions stay consistent
        # a bout that was removed earlier but is back on the card gets its saved prediction back
        conn.execute(sa.text("UPDATE upcoming_predictions SET status = CASE WHEN p_a_wins IS NULL "
                             "THEN 'not_enough_history' ELSE 'predicted' END "
                             "WHERE bout_id = ANY(:ids) AND status = 'cancelled' AND graded_at IS NULL"),
                     {"ids": [b["bout_id"] for b in bouts]})
        cancelled = 0
        for d, grp in pd.DataFrame(bouts).groupby("event_date"):
            ids = grp.bout_id.tolist()
            gone = [r[0] for r in conn.execute(sa.text(
                "SELECT bout_id FROM upcoming_fights WHERE event_date = :d AND status = 'scheduled' "
                "AND bout_id <> ALL(:ids)"), {"d": d, "ids": ids})]
            if gone:
                conn.execute(sa.text("UPDATE upcoming_fights SET status = 'cancelled' WHERE bout_id = ANY(:g)"), {"g": gone})
                conn.execute(sa.text("UPDATE upcoming_predictions SET status = 'cancelled' "
                                     "WHERE bout_id = ANY(:g) AND graded_at IS NULL"), {"g": gone})
                cancelled += len(gone)
    new = sum(b["bout_id"] not in existing for b in bouts)
    log.info("Saved %d bouts (%d new, %d already saved); %d removed from the card and marked cancelled",
             len(bouts), new, len(bouts) - new, cancelled)
    log.info("Next: python pipeline/upcoming.py predict")
    return 0


def cmd_predict(args) -> int:
    engine = make_engine()
    today = date.fromisoformat(args.today) if args.today else date.today()
    with engine.connect() as conn:
        todo = pd.read_sql(sa.text("""
            SELECT m.* FROM upcoming_model_rows m LEFT JOIN upcoming_predictions p USING (bout_id)
            WHERE p.bout_id IS NULL AND m.event_date > :today ORDER BY m.event_date, m.bout_order"""),
            conn, params={"today": today})
        hist = pd.read_sql(sa.text("SELECT * FROM model_rows WHERE min_prev_fights >= :k"), conn, params={"k": MIN_PRIOR})
    if todo.empty:
        log.info("No new bouts to predict (already predicted, event not in the future, or no card loaded).")
        return 0
    diffs = [c for c in hist.columns if c.startswith("diff_")]
    if len(hist) < MIN_TRAIN_FIGHTS:
        raise SystemExit(f"Only {len(hist)} training fights; something is wrong with model_rows.")
    X = hist[diffs].astype(float).fillna(0)
    model = make_model().fit(X, hist.a_wins)

    rows = []
    for r in todo.itertuples():
        base = dict(bout_id=r.bout_id, event_name=r.event_name, event_date=r.event_date, bout_order=r.bout_order,
                    weight_class=r.weight_class, title_fight=r.title_fight, fighter_a=r.fighter_a, fighter_b=r.fighter_b,
                    a_prior_fights=int(r.a_n_prev), b_prior_fights=int(r.b_n_prev), trained_on_fights=len(hist))
        if r.min_prev_fights >= MIN_PRIOR:
            x = pd.DataFrame([{c: getattr(r, c) for c in diffs}]).astype(float).fillna(0)
            p_a = float(model.predict_proba(x[diffs])[0, 1])
            rows.append({**base, "status": "predicted", "p_a_wins": p_a, "confidence": max(p_a, 1 - p_a),
                         "predicted_winner": r.fighter_a if p_a >= 0.5 else r.fighter_b})
        else:
            rows.append({**base, "status": "not_enough_history", "p_a_wins": None, "confidence": None,
                         "predicted_winner": None})
    out = pd.DataFrame(rows)
    with engine.begin() as conn:
        out.to_sql("upcoming_predictions", conn, if_exists="append", index=False, method="multi")
    n_pred = int((out.status == "predicted").sum())
    log.info("Locked %d predictions and %d 'not enough history' bouts (model trained on %d fights)",
             n_pred, len(out) - n_pred, len(hist))
    for r in out.itertuples():
        pick = f"{r.predicted_winner} {r.confidence:.0%}" if r.status == "predicted" else "not enough history"
        print(f"  {r.event_date}  {r.fighter_a:24s} vs {r.fighter_b:24s} -> {pick}")
    return 0


def cmd_grade(args) -> int:
    engine = make_engine()
    with engine.begin() as conn:
        latest = conn.execute(sa.text("SELECT MAX(event_date) FROM fights")).scalar()
        rows = conn.execute(sa.text("""
            SELECT p.bout_id, p.fighter_a, p.fighter_b, p.predicted_winner, p.event_date,
                   (SELECT CASE WHEN fa.won = 1 THEN u.fighter_a_name ELSE u.fighter_b_name END
                      FROM fighter_fights fa
                      JOIN fighter_fights fb ON fb.fight_url = fa.fight_url AND fb.fighter_url = u.fighter_b_url
                      JOIN fights f ON f.fight_url = fa.fight_url
                     WHERE fa.fighter_url = u.fighter_a_url
                       AND f.event_date BETWEEN u.event_date - 1 AND u.event_date + 1
                     LIMIT 1) AS winner,
                   EXISTS (SELECT 1 FROM fights f WHERE f.event_date BETWEEN u.event_date - 1 AND u.event_date + 1) AS event_loaded
            FROM upcoming_predictions p JOIN upcoming_fights u USING (bout_id)
            WHERE p.graded_at IS NULL AND p.status = 'predicted'""")).fetchall()
        graded = noresult = pending = right = 0
        for r in rows:
            if r.winner:
                ok = int(r.winner == r.predicted_winner)
                conn.execute(sa.text("UPDATE upcoming_predictions SET actual_winner = :w, correct = :c, graded_at = now() "
                                     "WHERE bout_id = :b"), {"w": r.winner, "c": ok, "b": r.bout_id})
                graded += 1
                right += ok
            elif r.event_loaded:        # the event is in the dataset but this bout isn't: cancelled, draw or no contest
                conn.execute(sa.text("UPDATE upcoming_predictions SET status = 'no_result', graded_at = now() "
                                     "WHERE bout_id = :b"), {"b": r.bout_id})
                noresult += 1
            else:
                pending += 1
    log.info("Graded %d (%d right); %d had no result (cancelled/draw/no contest); %d still waiting for the dataset "
             "to include the event (latest event loaded: %s)", graded, right, noresult, pending, latest)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Predict announced UFC cards before they happen and grade them later")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="create/refresh the tables and views")
    p = sub.add_parser("load", help="save an announced card from a CSV")
    p.add_argument("--csv", required=True)
    p.add_argument("--aliases", default=str(ROOT / "upcoming" / "aliases.csv"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-new", action="store_true", help="treat unmatched names as fighters with no UFC history")
    p.add_argument("--allow-past", action="store_true", help="(testing only) accept an event date that has passed")
    p.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    p = sub.add_parser("predict", help="predict and lock all not-yet-predicted bouts")
    p.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    sub.add_parser("grade", help="score locked predictions once results are in the dataset")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return {"setup": cmd_setup, "load": cmd_load, "predict": cmd_predict, "grade": cmd_grade}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
