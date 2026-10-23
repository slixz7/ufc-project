#!/usr/bin/env python3
"""
model_v3.py - model v3: model v2 plus the betting market.

Why: in walk-forward tests (2016-2026, 5,279 fights, each year predicted only from earlier years) model v2 alone was right
61.7% of the time, the bookmakers' OPENING line alone 64.9%, and v2 + the opening line 65.4% (+3.8 points, clearly more than
noise). The gain is biggest for debutants (58.5% -> 67.4%). The betting line is used ONLY as a forecasting input; this is not
betting advice and the app never shows odds or suggests bets.

How: a tiny "stacker" (logistic regression, no intercept) combines the logit of v2's probability with the logit of the market's
probability (bookmaker margin removed). It is trained only on earlier fights, so every backtest pick is a fair test.

Commands (use the OWNER database string, these write to the database)
    python pipeline/model_v3.py setup                          # create the v3 tables and views (safe to re-run)
    python pipeline/model_v3.py load-odds --csv data/nlbair/data/ufc_all_fights_wide.csv
                                                               # historical lines, 2013-2026 (CC BY 4.0, see README for credits)
    python pipeline/model_v3.py backtest [--rebuild]           # v3 pick for every past fight that has a line (2016 onward)
    python pipeline/model_v3.py odds --csv upcoming/odds-ufc-334.csv [--dry-run]
                                                               # enter the current line for announced bouts
    python pipeline/model_v3.py predict [--within-days 2]      # lock v3 picks for bouts that have a line (before the event)
    python pipeline/model_v3.py grade                          # copy results over once `model_v2.py grade` has run
    python pipeline/model_v3.py explain                        # store the "Why this pick?" reasons for locked picks that lack them

Two-stage picks: the v2 pick is locked as soon as a card is announced (model_v2.py predict). Once a line is loaded, the v3 pick
is locked too. Neither is ever changed. The app shows v3 when it exists and v2 otherwise.

odds CSV (names as on the card file, American odds):   fighter_a,fighter_b,odds_a,odds_b
"""
from __future__ import annotations

import argparse
import difflib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import model_v2 as v2

log = logging.getLogger("model_v3")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "nlbair/ufc-data v0.1.1 (CC BY 4.0): ufcstats, BestFightOdds"
FIRST_BACKTEST_YEAR = 2016      # earlier fights are the stacker's training data
MIN_STACK_TRAIN = 1000


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def american_to_implied(o: float) -> float:
    o = float(o)
    return -o / (-o + 100.0) if o < 0 else 100.0 / (o + 100.0)


def devig(odds_a: float, odds_b: float) -> float:
    """P(A wins) from the two American prices, with the bookmaker's margin divided out."""
    a, b = american_to_implied(odds_a), american_to_implied(odds_b)
    return a / (a + b)


def fit_stacker(T: pd.DataFrame):
    """T needs: fight_id, p_v2_a, market_a, a_wins. Antisymmetric, so A/B orientation does not matter."""
    X = pd.DataFrame({"x_model": logit(T.p_v2_a), "x_market": logit(T.market_a)}, index=T.index)
    flip = (pd.util.hash_array(T.fight_id.values.astype(object)) % 2) == 1
    X.loc[flip] = -X.loc[flip]
    y = np.where(flip, 1 - T.a_wins.values.astype(int), T.a_wins.values.astype(int))
    return make_pipeline(StandardScaler(with_mean=False), LogisticRegression(C=1.0, fit_intercept=False, max_iter=3000)).fit(X, y)


def stack_predict(model, p_v2_a, market_a) -> np.ndarray:
    X = pd.DataFrame({"x_model": logit(p_v2_a), "x_market": logit(market_a)})
    return model.predict_proba(X)[:, 1]


def stack_weights(model) -> tuple[float, float]:
    """The blend's weights on the log-odds scale: (statistical model, betting market). The pick is exactly
    w_model * logit(model probability) + w_market * logit(market probability)."""
    w = model[1].coef_[0] / model[0].scale_
    return float(w[0]), float(w[1])


def v3_reasons(name_a: str, name_b: str, p_v2_a: float, market_a: float, final_p: float, w_model: float, w_market: float) -> dict:
    return {"a": name_a, "b": name_b, "model_p_a": round(float(p_v2_a), 4), "market_p_a": round(float(market_a), 4), "final_p_a": round(float(final_p), 4),
            "w_model": round(w_model, 3), "w_market": round(w_market, 3), "market_share": round(w_market / (w_model + w_market), 3),
            "model_c": round(w_model * float(logit(p_v2_a)), 4), "market_c": round(w_market * float(logit(market_a)), 4)}


def training_table(engine) -> pd.DataFrame:
    """Every past fight that has BOTH a v2 backtest probability and a historical line, oriented to fighter A (= side 1)."""
    history = v2.load_history(engine)
    P = v2.make_pairs(history)
    P = P[~P.is_up].copy()
    P["fight_id"] = P.fight_url.str.split("/").str[-1]
    P["a_id"] = P.url_a.str.split("/").str[-1]
    picks = pd.read_sql(sa.text("SELECT fight_url, p_a_wins AS p_v2_a, method FROM fight_picks_v2"), engine)
    odds = pd.read_sql(sa.text("SELECT fight_id, f1_id, open_prob_f1, close_prob_f1 FROM fight_odds"), engine)
    T = P.merge(picks, on="fight_url", how="inner").merge(odds, on="fight_id", how="inner")
    T = T[T.method == "model"].copy()
    same = (T.a_id == T.f1_id).values
    T["market_a"] = np.where(same, T.open_prob_f1, 1 - T.open_prob_f1)
    T["close_a"] = np.where(same, T.close_prob_f1, 1 - T.close_prob_f1)
    return T.sort_values(["event_date", "fight_url"]).reset_index(drop=True)


# ----------------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    with v2.make_engine().begin() as conn:
        conn.exec_driver_sql((ROOT / "sql" / "model_v3.sql").read_text())
    log.info("Applied model_v3.sql")
    return 0


def cmd_load_odds(args) -> int:
    path = Path(args.csv)
    if not path.exists():
        log.error("File not found: %s (see the README for how to download the odds dataset)", path)
        return 2
    cols = ["fight_id", "event_date", "f1_id", "f2_id", "f1_name", "f2_name", "has_odds", "f1_open_prob_novig", "f1_close_prob_novig"]
    d = pd.read_csv(path, usecols=cols, low_memory=False)
    d = d[d.has_odds.astype(str).str.lower().eq("true") & d.f1_open_prob_novig.notna()].copy()
    d["close"] = d.f1_close_prob_novig.fillna(d.f1_open_prob_novig)
    bad = d[(d.f1_open_prob_novig <= 0) | (d.f1_open_prob_novig >= 1)]
    if len(bad):
        log.error("%d rows have impossible probabilities, refusing to load", len(bad))
        return 2
    out = pd.DataFrame({"fight_id": d.fight_id, "event_date": pd.to_datetime(d.event_date).dt.date, "f1_id": d.f1_id, "f2_id": d.f2_id,
                        "f1_name": d.f1_name, "f2_name": d.f2_name, "open_prob_f1": d.f1_open_prob_novig, "close_prob_f1": d.close,
                        "source": SOURCE}).drop_duplicates("fight_id")
    engine = v2.make_engine()
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM fight_odds"))
        out.to_sql("fight_odds", conn, if_exists="append", index=False, method="multi", chunksize=500)
        n_match = conn.execute(sa.text("SELECT COUNT(*) FROM fights f JOIN fight_odds o ON o.fight_id = split_part(f.fight_url, '/', 5)")).scalar()
    log.info("Loaded %d lines (%s to %s); %d of them match fights in your database", len(out), out.event_date.min(), out.event_date.max(), n_match)
    return 0


def cmd_backtest(args) -> int:
    engine = v2.make_engine()
    if args.rebuild:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM fight_picks_v3"))
    with engine.connect() as conn:
        done = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fight_picks_v3"))}
    T = training_table(engine)
    if T.empty:
        log.error("No fights have both a v2 pick and a line. Run model_v2.py backtest and model_v3.py load-odds first.")
        return 2
    todo = T[(T.event_date >= f"{FIRST_BACKTEST_YEAR}-01-01") & ~T.fight_url.isin(done)]
    log.info("%d fights have a line; %d need a v3 pick (%s onward)", len(T), len(todo), FIRST_BACKTEST_YEAR)
    out = []
    for D in sorted(todo.event_date.unique()):
        te = todo[todo.event_date == D]
        tr = T[T.event_date < D]
        if len(tr) < MIN_STACK_TRAIN:
            continue
        p = stack_predict(fit_stacker(tr), te.p_v2_a.values, te.market_a.values)
        r = te.copy()
        r["p_a_wins"], r["trained_on_fights"] = p, len(tr)
        out.append(r)
    if not out:
        log.info("Nothing to score.")
        return 0
    R = pd.concat(out)
    R["fighter_a"], R["fighter_b"] = R.name_a, R.name_b
    R["predicted_winner"] = np.where(R.p_a_wins >= 0.5, R.fighter_a, R.fighter_b)
    R["confidence"] = np.maximum(R.p_a_wins, 1 - R.p_a_wins)
    R["actual_winner"] = np.where(R.a_wins == 1, R.fighter_a, R.fighter_b)
    R["correct"] = ((R.p_a_wins >= 0.5).astype(int) == R.a_wins.astype(int)).astype(int)
    R["v2_correct"] = ((R.p_v2_a >= 0.5).astype(int) == R.a_wins.astype(int)).astype(int)
    R["market_correct"] = ((R.market_a >= 0.5).astype(int) == R.a_wins.astype(int)).astype(int)
    R["tier"] = [v2.tier_of(m) for m in R.min_prev]
    R["market_prob_a"] = R.market_a
    R["event_date"] = R.event_date.dt.date
    cols = ["fight_url", "event_date", "event_name", "weight_class", "title_fight", "fighter_a", "fighter_b", "tier", "p_v2_a", "market_prob_a",
            "p_a_wins", "predicted_winner", "confidence", "actual_winner", "correct", "v2_correct", "market_correct", "trained_on_fights"]
    with engine.begin() as conn:
        R[cols].to_sql("fight_picks_v3", conn, if_exists="append", index=False, method="multi", chunksize=500)
    log.info("Wrote %d v3 picks. Accuracy on them: v3 %.1f%%  |  v2 alone %.1f%%  |  the opening line alone %.1f%%",
             len(R), 100 * R.correct.mean(), 100 * R.v2_correct.mean(), 100 * R.market_correct.mean())
    for tier, g in R.groupby("tier"):
        log.info("   %-8s %5d fights   v3 %.1f%%   v2 %.1f%%", tier, len(g), 100 * g.correct.mean(), 100 * g.v2_correct.mean())
    return 0


def _parse_american(x) -> float:
    v = float(str(x).replace("+", "").strip())
    if abs(v) < 100:
        raise ValueError(f"{x!r} is not an American price (it must be at least 100 or at most -100, like -150 or +130)")
    return v


def cmd_odds(args) -> int:
    engine = v2.make_engine()
    today = date.fromisoformat(args.today) if args.today else date.today()
    csv = pd.read_csv(args.csv)
    need = {"fighter_a", "fighter_b", "odds_a", "odds_b"}
    if not need <= set(csv.columns):
        log.error("The CSV needs the columns: fighter_a,fighter_b,odds_a,odds_b (found: %s)", ", ".join(csv.columns))
        return 2
    bouts = pd.read_sql(sa.text("""SELECT bout_id, event_name, event_date, fighter_a_name AS a, fighter_b_name AS b FROM upcoming_fights
                                   WHERE status = 'scheduled' AND event_date > :t"""), engine, params={"t": today})
    locked = {r[0] for r in engine.connect().execute(sa.text("SELECT bout_id FROM upcoming_picks_v3"))}
    by_pair = {frozenset([v2.norm(r.a), v2.norm(r.b)]): r for r in bouts.itertuples()}
    rows, problems = [], 0
    print(f"{'bout':54s} {'odds':>13s}  {'P(A) no margin':>14s}")
    for r in csv.itertuples():
        try:
            oa, ob = _parse_american(r.odds_a), _parse_american(r.odds_b)
        except ValueError as e:
            print(f"  PROBLEM  {r.fighter_a} vs {r.fighter_b}: {e}")
            problems += 1
            continue
        na, nb = v2.norm(r.fighter_a), v2.norm(r.fighter_b)
        hit = by_pair.get(frozenset([na, nb]))
        if hit is None:                      # tolerate small spelling differences
            keys = list(by_pair)
            best = max(keys, key=lambda k: difflib.SequenceMatcher(None, " | ".join(sorted(k)), " | ".join(sorted([na, nb]))).ratio())
            if difflib.SequenceMatcher(None, " | ".join(sorted(best)), " | ".join(sorted([na, nb]))).ratio() >= 0.9:
                hit = by_pair[best]
        if hit is None:
            print(f"  PROBLEM  no announced bout matches {r.fighter_a} vs {r.fighter_b}. Is the card loaded (upcoming.py load), and are the names the same?")
            problems += 1
            continue
        if hit.bout_id in locked:
            print(f"  SKIPPED  {hit.a} vs {hit.b}: its v3 pick is already locked, so its line can no longer change")
            continue
        if v2.norm(r.fighter_a) != v2.norm(hit.a) and difflib.SequenceMatcher(None, v2.norm(r.fighter_a), v2.norm(hit.a)).ratio() < difflib.SequenceMatcher(None, v2.norm(r.fighter_a), v2.norm(hit.b)).ratio():
            oa, ob = ob, oa                    # the CSV lists the fighters in the other order: flip so odds_a belongs to the card's fighter A
            order = "swapped"
        else:
            order = ""
        total = american_to_implied(oa) + american_to_implied(ob)
        if not 1.0 <= total <= 1.25:
            print(f"  PROBLEM  {hit.a} vs {hit.b}: the two prices imply {total:.2f} in total, which is not a normal betting line. Check for a typo.")
            problems += 1
            continue
        pa = devig(oa, ob)
        print(f"  {hit.a + ' vs ' + hit.b:52s} {oa:+6.0f}/{ob:+6.0f}  {pa:13.1%}  {order}")
        rows.append({"bout_id": hit.bout_id, "odds_a": oa, "odds_b": ob, "market_prob_a": pa, "source": f"entered by hand from {Path(args.csv).name}"})
    if problems:
        print(f"\n{problems} row(s) had problems (above). Nothing was saved. Fix the CSV and run this again.")
        return 1
    if args.dry_run:
        print(f"\nDry run: {len(rows)} lines are valid and would be saved. Run again without --dry-run to save them.")
        return 0
    with engine.begin() as conn:
        for row in rows:
            conn.execute(sa.text("""INSERT INTO upcoming_odds (bout_id, odds_a, odds_b, market_prob_a, source) VALUES (:bout_id, :odds_a, :odds_b, :market_prob_a, :source)
                                    ON CONFLICT (bout_id) DO UPDATE SET odds_a = EXCLUDED.odds_a, odds_b = EXCLUDED.odds_b,
                                    market_prob_a = EXCLUDED.market_prob_a, source = EXCLUDED.source, entered_at = now()"""), row)
    log.info("Saved %d lines. Next: python pipeline/model_v3.py predict", len(rows))
    return 0


def cmd_predict(args) -> int:
    engine = v2.make_engine()
    today = date.fromisoformat(args.today) if args.today else date.today()
    with engine.begin() as conn:
        conn.execute(sa.text("""UPDATE upcoming_picks_v3 p SET status = CASE WHEN u.status = 'cancelled' THEN 'cancelled' ELSE 'locked' END
                                FROM upcoming_fights u WHERE u.bout_id = p.bout_id AND p.graded_at IS NULL AND p.status IN ('locked', 'cancelled')"""))
    window = "AND u.event_date <= :lim" if args.within_days is not None else ""
    params = {"t": today}
    if args.within_days is not None:
        params["lim"] = today + timedelta(days=int(args.within_days))
    todo = pd.read_sql(sa.text(f"""
        SELECT o.bout_id, o.market_prob_a, o.odds_a, o.odds_b, u.event_name, u.event_date, u.bout_order, u.weight_class, u.title_fight,
               v.fighter_a, v.fighter_b, v.p_a_wins AS p_v2_a, v.tier
        FROM upcoming_odds o
        JOIN upcoming_fights u USING (bout_id)
        LEFT JOIN upcoming_picks_v2 v USING (bout_id)
        LEFT JOIN upcoming_picks_v3 p ON p.bout_id = o.bout_id
        WHERE p.bout_id IS NULL AND u.status = 'scheduled' AND u.event_date > :t {window}
        ORDER BY u.event_date, u.bout_order"""), engine, params=params)
    if todo.empty:
        log.info("No new bouts to pick (no line loaded, event not in the future or not yet inside the lock window, or already locked).")
        return 0
    missing = todo[todo.p_v2_a.isna()]
    if len(missing):
        log.error("%d bout(s) have a line but no v2 pick yet. Run: python pipeline/model_v2.py predict   first. (%s)", len(missing),
                  ", ".join(f"{r.bout_id}" for r in missing.itertuples()))
        todo = todo[todo.p_v2_a.notna()]
        if todo.empty:
            return 2
    T = training_table(engine)
    out = []
    for D in sorted(todo.event_date.unique()):
        te = todo[todo.event_date == D].copy()
        tr = T[T.event_date < pd.Timestamp(D)]
        if len(tr) < MIN_STACK_TRAIN:
            log.error("Only %d fights with lines to learn from; need %d. Run load-odds and backtest.", len(tr), MIN_STACK_TRAIN)
            return 2
        mdl = fit_stacker(tr)
        te["p_a_wins"] = stack_predict(mdl, te.p_v2_a.values, te.market_prob_a.values)
        te["w_model"], te["w_market"] = stack_weights(mdl)
        te["trained_on_fights"] = len(tr)
        out.append(te)
    R = pd.concat(out)
    R["predicted_winner"] = np.where(R.p_a_wins >= 0.5, R.fighter_a, R.fighter_b)
    R["confidence"] = np.maximum(R.p_a_wins, 1 - R.p_a_wins)
    cols = ["bout_id", "event_name", "event_date", "bout_order", "weight_class", "title_fight", "fighter_a", "fighter_b", "tier", "p_v2_a",
            "market_prob_a", "p_a_wins", "predicted_winner", "confidence", "trained_on_fights"]
    if args.dry_run:
        log.info("[dry run] %d market-informed picks would be locked; nothing written", len(R))
    else:
        with engine.begin() as conn:
            R[cols].to_sql("upcoming_picks_v3", conn, if_exists="append", index=False, method="multi")
        log.info("Locked %d market-informed picks (the stacker learned from %d earlier fights)", len(R), int(R.trained_on_fights.iloc[0]))
        try:                               # the reasons are a nicety: they must never be able to block a pick
            v2.save_reasons(engine, "v3", [(r.bout_id, v3_reasons(r.fighter_a, r.fighter_b, r.p_v2_a, r.market_prob_a, r.p_a_wins, r.w_model, r.w_market))
                                           for r in R.itertuples()])
        except Exception as exc:
            log.warning("could not store the reasons for the new picks (%s); run: python pipeline/model_v3.py explain", exc)
    for r in R.itertuples():
        v2w = r.fighter_a if r.p_v2_a >= 0.5 else r.fighter_b
        print(f"  {r.event_date}  {r.fighter_a:22s} vs {r.fighter_b:22s} -> {r.predicted_winner} {r.confidence:.0%}   (model-only said {v2w} {max(r.p_v2_a, 1 - r.p_v2_a):.0%}, market {max(r.market_prob_a, 1 - r.market_prob_a):.0%} on {r.fighter_a if r.market_prob_a >= .5 else r.fighter_b})")
    return 0


def cmd_explain(args) -> int:
    """Store the 'two opinions' reasons for locked market-informed picks that have none, only when they reproduce the pick."""
    engine = v2.make_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(v2.REASONS_DDL))
    todo = pd.read_sql(sa.text("""SELECT p.bout_id, p.event_date, p.fighter_a, p.fighter_b, p.p_v2_a, p.market_prob_a, p.p_a_wins FROM upcoming_picks_v3 p
                                  LEFT JOIN pick_reasons r ON r.bout_id = p.bout_id AND r.version = 'v3'
                                  WHERE r.bout_id IS NULL AND p.status = 'locked'"""), engine)
    if todo.empty:
        log.info("Every locked market-informed pick already has its reasons.")
        return 0
    T = training_table(engine)
    items, skipped = [], 0
    for D, grp in todo.groupby("event_date"):
        mdl = fit_stacker(T[T.event_date < pd.Timestamp(D)])
        wm, wk = stack_weights(mdl)
        for r in grp.itertuples():
            z = wm * float(logit(r.p_v2_a)) + wk * float(logit(r.market_prob_a))
            p = 1 / (1 + np.exp(-z))
            if abs(p - float(r.p_a_wins)) > 0.002:
                skipped += 1
                log.warning("not explained (the pick cannot be reproduced exactly): %s vs %s", r.fighter_a, r.fighter_b)
                continue
            items.append((r.bout_id, v3_reasons(r.fighter_a, r.fighter_b, r.p_v2_a, r.market_prob_a, r.p_a_wins, wm, wk)))
    if items:
        v2.save_reasons(engine, "v3", items)
    log.info("Stored reasons for %d market-informed picks; %d could not be reproduced", len(items), skipped)
    return 0


def cmd_grade(args) -> int:
    engine = v2.make_engine()
    with engine.begin() as conn:
        a = conn.execute(sa.text("""UPDATE upcoming_picks_v3 p SET actual_winner = v.actual_winner,
                                    correct = (p.predicted_winner = v.actual_winner)::int, graded_at = v.graded_at
                                    FROM upcoming_picks_v2 v WHERE v.bout_id = p.bout_id AND p.graded_at IS NULL
                                    AND v.graded_at IS NOT NULL AND v.actual_winner IS NOT NULL""")).rowcount
        b = conn.execute(sa.text("""UPDATE upcoming_picks_v3 p SET status = 'no_result', graded_at = v.graded_at
                                    FROM upcoming_picks_v2 v WHERE v.bout_id = p.bout_id AND p.graded_at IS NULL
                                    AND v.status = 'no_result'""")).rowcount
        waiting = conn.execute(sa.text("SELECT COUNT(*) FROM upcoming_picks_v3 WHERE graded_at IS NULL AND status = 'locked'")).scalar()
        right = conn.execute(sa.text("SELECT COUNT(*) FROM upcoming_picks_v3 WHERE correct = 1")).scalar()
    log.info("Graded %d, %d had no result; %d still waiting (run model_v2.py grade first so results are available); %d right in total", a, b, waiting, right)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Model v3: v2 plus the betting market")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    lo = sub.add_parser("load-odds")
    lo.add_argument("--csv", required=True, help="ufc_all_fights_wide.csv from github.com/nlbair/ufc-data")
    b = sub.add_parser("backtest")
    b.add_argument("--rebuild", action="store_true")
    o = sub.add_parser("odds")
    o.add_argument("--csv", required=True)
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    p = sub.add_parser("predict")
    p.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    p.add_argument("--within-days", type=int, default=None, help="only lock bouts whose event is at most this many days away")
    p.add_argument("--dry-run", action="store_true", help="show the picks that would be locked; write nothing")
    sub.add_parser("grade")
    sub.add_parser("explain")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return {"setup": cmd_setup, "load-odds": cmd_load_odds, "backtest": cmd_backtest, "odds": cmd_odds, "predict": cmd_predict,
            "grade": cmd_grade, "explain": cmd_explain}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
