"""
Will the fight go the distance? (goes-the-distance model, v1)

For every fight it estimates the chance that the fight ends in a decision. The inputs are only how long these two fighters' earlier
fights lasted (the share of scheduled time used, how often they went to a decision, how often they won or lost by a finish) and
whether the fight is a title fight or five rounds. Everything is computed from EARLIER fights only. Picks for upcoming bouts are
locked before the event and never change, exactly like the winner picks.

Honest test (see `scoring`): trained and scored year by year, it beat the plain decision rate of the previous three years in 14 of
15 years, and its probabilities were calibrated to within about 3 points. Fights since 2002, 3 or 5 scheduled rounds, no DQs.

    python pipeline/model_distance.py setup               # create the two tables
    python pipeline/model_distance.py backtest [--rebuild]  # score every past fight, each with a model trained on earlier fights only
    python pipeline/model_distance.py predict [--today D]   # lock a prediction for every newly announced upcoming bout
    python pipeline/model_distance.py grade                 # after the results are in the dataset
    python pipeline/model_distance.py scoring               # the report: log loss, calibration, year by year

Scheduled rounds of upcoming bouts are not on the cards, so a title fight or main event is taken to be five rounds and anything else
three (historically 84% of title fights and 5% of other fights had five).
"""
from __future__ import annotations

import argparse
import difflib
import logging
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import distance_scores
import model_v2 as v2
import upcoming

log = logging.getLogger("model_distance")
FINISH = ("KO/TKO", "Submission", "TKO - Doctor's Stoppage")
K_SHRINK = 3.0                      # pseudo-fights pulling thin histories toward the average
C_REG = 0.1
MIN_TRAIN = 300                     # fights needed before the model trains
ERA_START = pd.Timestamp("2002-01-01")
RECENT_DAYS = 3 * 365
MIN_RECENT = 100                    # fewer than this many recent fights: use the whole training period for the baseline
FEATURES = ["dec_mean", "dec_min", "dec_max", "finwin_mean", "finwin_max", "finloss_mean", "finloss_max", "dur_mean", "five", "title"]
COUNT_COLS = ["c_elig", "c_dec", "c_fin_win", "c_fin_loss", "c_dur_frac"]

DDL = [
    """CREATE TABLE IF NOT EXISTS fight_distance_v1 (
        fight_url TEXT PRIMARY KEY, event_date DATE, event_name TEXT, weight_class TEXT, scheduled_rounds INTEGER,
        fighter_a TEXT, fighter_b TEXT, tier TEXT, p_distance DOUBLE PRECISION, p_base DOUBLE PRECISION, went_distance INTEGER,
        trained_on_fights INTEGER, scored_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS upcoming_distance_v1 (
        bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, bout_order INTEGER, weight_class TEXT, scheduled_rounds INTEGER,
        fighter_a TEXT, fighter_b TEXT, tier TEXT, p_distance DOUBLE PRECISION, p_base DOUBLE PRECISION, trained_on_fights INTEGER,
        status TEXT, predicted_at TIMESTAMPTZ, went_distance INTEGER, graded_at TIMESTAMPTZ)""",
]


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.exec_driver_sql(stmt)


# ----------------------------------------------------------------------------------------------
# features: every count comes from EARLIER fights only
# ----------------------------------------------------------------------------------------------
def load_history(engine) -> pd.DataFrame:
    """The winner model's fighter-fight history plus each fight's scheduled rounds."""
    h = v2.load_history(engine)
    rounds = pd.read_sql(sa.text("SELECT fight_url, num_rounds FROM fights"), engine)
    return h.merge(rounds, on="fight_url", how="left")


def _flags(h: pd.DataFrame) -> pd.DataFrame:
    h = h.copy()
    res = h.result.fillna("")
    fin = res.isin(FINISH).astype(int)
    h["dec"] = res.str.startswith("Decision").astype(int)
    h["elig"] = 1 - (res == "DQ").astype(int)                        # a disqualification counts for nothing
    h["fin_win"] = ((h.won == 1) & (fin == 1)).astype(int)
    h["fin_loss"] = ((h.won == 0) & (fin == 1)).astype(int)
    sched = h.num_rounds.where(h.num_rounds.isin([3, 5]), 3) * 300.0
    h["dur_frac"] = (h.dur_sec / sched).clip(0, 1)                    # the share of the scheduled time the fight used
    return h


def _weighted(h: pd.DataFrame, c: str) -> pd.Series:
    return h[c] * (h.elig if c != "elig" else 1)


def counts(h: pd.DataFrame) -> pd.DataFrame:
    """Adds c_* columns: for each row, the fighter's totals over their earlier fights (this fight excluded)."""
    h = h.sort_values(["fighter_url", "event_date", "fight_url"]).copy()
    for c in ("elig", "dec", "fin_win", "fin_loss", "dur_frac"):
        w = _weighted(h, c)
        # everything up to this row, minus everything on this fighter's own event date up to this row: strictly EARLIER dates only
        h[f"c_{c}"] = w.groupby(h.fighter_url).cumsum() - w.groupby([h.fighter_url, h.event_date]).cumsum()
    return h


def frame(history: pd.DataFrame, extra: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per fight with the winner model's as-of features plus the distance counts. Upcoming bouts (the `extra` placeholder rows)
    use each fighter's totals over all their real fights; a fighter with no UFC history starts at zero."""
    h = counts(_flags(history))
    a = h[h.side == 1].set_index("fight_url")[COUNT_COLS].add_suffix("_a")
    b = h[h.side == 2].set_index("fight_url")[COUNT_COLS].add_suffix("_b")
    fl = h.drop_duplicates("fight_url").set_index("fight_url")[["result", "num_rounds", "dec", "elig"]]
    D = v2.make_pairs(history, extra).set_index("fight_url").join(a).join(b).join(fl)
    if extra is not None and not extra.empty:
        tot = {c: _weighted(h, c[2:]).groupby(h.fighter_url).sum() for c in COUNT_COLS}
        up = D.is_up.to_numpy()
        for s, col in (("a", "url_a"), ("b", "url_b")):
            for c in COUNT_COLS:
                D.loc[up, f"{c}_{s}"] = D.loc[up, col].map(tot[c]).fillna(0.0)
    D["year"] = D.event_date.dt.year
    D["title"] = D.title_fight.fillna(False).astype(int)
    D["five"] = (D.num_rounds == 5).astype(int)
    return D.reset_index()


def eligible(D: pd.DataFrame) -> pd.DataFrame:
    """Real fights the model learns from and is scored on: since 2002, three or five scheduled rounds, not a disqualification."""
    return D[(~D.is_up) & (D.event_date >= ERA_START) & D.num_rounds.isin([3, 5]) & (D.elig == 1)].copy()


def priors(Tr: pd.DataFrame) -> dict:
    n = pd.concat([Tr.c_elig_a, Tr.c_elig_b]).sum()
    tot = lambda c: pd.concat([Tr[f"{c}_a"], Tr[f"{c}_b"]]).sum() / max(n, 1)
    return {"dec": tot("c_dec"), "finwin": tot("c_fin_win"), "finloss": tot("c_fin_loss"), "dur": tot("c_dur_frac")}


def design(D: pd.DataFrame, mu: dict) -> pd.DataFrame:
    def side(s: str) -> dict:
        n = D[f"c_elig_{s}"]
        return {k: (D[f"{c}_{s}"] + K_SHRINK * mu[k]) / (n + K_SHRINK) for k, c in (("dec", "c_dec"), ("finwin", "c_fin_win"), ("finloss", "c_fin_loss"), ("dur", "c_dur_frac"))}
    A, B = side("a"), side("b")
    X = pd.DataFrame(index=D.index)
    for k in ("dec", "finwin", "finloss", "dur"):
        X[f"{k}_mean"] = (A[k] + B[k]) / 2
    X["dec_min"], X["dec_max"] = np.minimum(A["dec"], B["dec"]), np.maximum(A["dec"], B["dec"])
    X["finwin_max"], X["finloss_max"] = np.maximum(A["finwin"], B["finwin"]), np.maximum(A["finloss"], B["finloss"])
    X["five"], X["title"] = D["five"], D["title"]
    return X[FEATURES]


def fit(Tr: pd.DataFrame):
    mu = priors(Tr)
    model = make_pipeline(StandardScaler(), LogisticRegression(C=C_REG, max_iter=3000)).fit(design(Tr, mu), Tr.dec)
    return model, mu


def recent_rate(Tr: pd.DataFrame, when: pd.Timestamp) -> float:
    """The baseline: the plain decision rate over the three years before `when` (the whole training period if that is too little)."""
    r = Tr[Tr.event_date >= when - pd.Timedelta(days=RECENT_DAYS)]
    return float((r if len(r) >= MIN_RECENT else Tr).dec.mean())


# ----------------------------------------------------------------------------------------------
# backtest
# ----------------------------------------------------------------------------------------------
def run_backtest(engine, rebuild: bool = False) -> int:
    ensure_tables(engine)
    E = eligible(frame(load_history(engine)))
    with engine.begin() as conn:
        if rebuild:
            conn.execute(sa.text("DELETE FROM fight_distance_v1"))
        done = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fight_distance_v1"))}
    todo = E[~E.fight_url.isin(done)]
    out, skipped = [], 0
    for when, te in todo.groupby("event_date"):
        Tr = E[E.event_date < when]
        if len(Tr) < MIN_TRAIN:
            skipped += len(te)
            continue
        model, mu = fit(Tr)
        base = recent_rate(Tr, when)
        out.append(pd.DataFrame({
            "fight_url": te.fight_url.values, "event_date": [d.date() for d in te.event_date], "event_name": te.event_name.values,
            "weight_class": te.weight_class.values, "scheduled_rounds": np.where(te.five == 1, 5, 3), "fighter_a": te.name_a.values,
            "fighter_b": te.name_b.values, "tier": [v2.tier_of(m) for m in te.min_prev], "p_distance": model.predict_proba(design(te, mu))[:, 1],
            "p_base": base, "went_distance": te.dec.astype(int).values, "trained_on_fights": len(Tr)}))
    if not out:
        log.info("Nothing new to score (%d fights are before the model has enough history).", skipped)
        return 0
    R = pd.concat(out, ignore_index=True)
    R["scored_at"] = datetime.now(timezone.utc)
    with engine.begin() as conn:
        R.to_sql("fight_distance_v1", conn, if_exists="append", index=False, method="multi", chunksize=200)
    s = distance_scores.distance_scores(R)
    log.info("Scored %d past fights. Log loss: model %.4f vs the recent decision rate %.4f; AUC %.3f", len(R),
             s["scores"]["model"]["log_loss"], s["scores"]["recent"]["log_loss"], s["scores"]["model"]["auc"])
    return 0


# ----------------------------------------------------------------------------------------------
# predictions for announced bouts
# ----------------------------------------------------------------------------------------------
def scheduled_rounds(title_fight, bout_order) -> int:
    return 5 if (bool(title_fight) or (pd.notna(bout_order) and int(bout_order) == 1)) else 3


def run_predict(engine, today: date) -> int:
    ensure_tables(engine)
    with engine.begin() as conn:      # keep cancelled and reinstated bouts in step with the card
        conn.execute(sa.text("""UPDATE upcoming_distance_v1 SET status = CASE WHEN bout_id IN (SELECT bout_id FROM upcoming_fights WHERE status = 'cancelled')
                                THEN 'cancelled' ELSE 'locked' END WHERE graded_at IS NULL AND status IN ('locked', 'cancelled')"""))
        have = {r[0] for r in conn.execute(sa.text("SELECT bout_id FROM upcoming_distance_v1"))}
        todo = pd.read_sql(sa.text("SELECT bout_id, bout_order, title_fight FROM upcoming_fights WHERE status = 'scheduled' AND event_date > :t"), conn, params={"t": today})
    todo = todo[~todo.bout_id.isin(have)]
    if todo.empty:
        log.info("No new bouts to predict (already done, event not in the future, or no card loaded).")
        return 0
    history = load_history(engine)
    D = frame(history, v2.placeholders(engine, history, only_ids=set(todo.bout_id)))
    up = D[D.is_up].copy()
    up["bout_id"] = up.fight_url.str.replace("upcoming:", "", regex=False)
    meta = todo.set_index("bout_id")
    up["five"] = [int(scheduled_rounds(meta.title_fight[b], meta.bout_order[b]) == 5) for b in up.bout_id]
    train = eligible(D)
    if len(train) < MIN_TRAIN:
        log.error("Only %d fights to learn from; need %d.", len(train), MIN_TRAIN)
        return 2
    model, mu = fit(train)
    now = datetime.now(timezone.utc)
    rows = pd.DataFrame({
        "bout_id": up.bout_id.values, "event_name": up.event_name.values, "event_date": [d.date() for d in up.event_date], "bout_order": up.bout_id.map(meta.bout_order).values,
        "weight_class": up.weight_class.values, "scheduled_rounds": np.where(up.five == 1, 5, 3), "fighter_a": up.name_a.values, "fighter_b": up.name_b.values,
        "tier": [v2.tier_of(m) for m in up.min_prev], "p_distance": model.predict_proba(design(up, mu))[:, 1],
        "p_base": [recent_rate(train, pd.Timestamp(d)) for d in up.event_date], "trained_on_fights": len(train), "status": "locked", "predicted_at": now})
    rows = rows.sort_values(["event_date", "bout_order"])
    with engine.begin() as conn:
        rows.to_sql("upcoming_distance_v1", conn, if_exists="append", index=False, method="multi", chunksize=200)
    log.info("Locked %d goes-the-distance predictions (model trained on %d fights)", len(rows), len(train))
    for r in rows.itertuples():
        print(f"  {r.event_date}  {r.fighter_a:24s} vs {r.fighter_b:24s} -> {r.p_distance:.0%} goes the distance ({r.scheduled_rounds} rounds, base rate {r.p_base:.0%})")
    return 0


# ----------------------------------------------------------------------------------------------
# grading
# ----------------------------------------------------------------------------------------------
def match_fight(win: pd.DataFrame, url_a: str, url_b: str, name_a: str, name_b: str):
    """The real fight (fight_url) that a scheduled bout turned into, among the fights near the event date; None if there is none."""
    for url in (url_a, url_b):
        if not str(url).startswith("new:"):
            hit = win[win.fighter_url == url]
            if not hit.empty:                                           # the known fighter fought that night: that is the bout
                return hit.iloc[0].fight_url
    if str(url_a).startswith("new:") and str(url_b).startswith("new:"):      # two debuts: match on names
        na, nb = upcoming.norm(name_a), upcoming.norm(name_b)
        for fu, g in win.groupby("fight_url"):
            names = [upcoming.norm(x) for x in g.fighter_name]
            ma, mb = difflib.get_close_matches(na, names, n=1, cutoff=0.85), difflib.get_close_matches(nb, names, n=1, cutoff=0.85)
            if ma and mb and ma[0] != mb[0]:
                return fu
    return None


def run_grade(engine) -> int:
    ensure_tables(engine)
    pend = pd.read_sql(sa.text("""SELECT p.bout_id, p.fighter_a, p.fighter_b, p.event_date, u.fighter_a_url, u.fighter_b_url
                                  FROM upcoming_distance_v1 p JOIN upcoming_fights u ON u.bout_id = p.bout_id
                                  WHERE p.graded_at IS NULL AND p.status = 'locked'"""), engine)
    if pend.empty:
        log.info("Nothing waiting to be graded.")
        return 0
    fights = pd.read_sql(sa.text("""SELECT f.fight_url, f.event_date, f.result, ff.fighter_url, ff.fighter_name
                                    FROM fights f JOIN fighter_fights ff ON ff.fight_url = f.fight_url"""), engine)
    fights["event_date"] = pd.to_datetime(fights.event_date)
    now = datetime.now(timezone.utc)
    graded = missing = waiting = 0
    for r in pend.itertuples():
        d = pd.Timestamp(r.event_date)
        win = fights[(fights.event_date >= d - pd.Timedelta(days=1)) & (fights.event_date <= d + pd.Timedelta(days=1))]
        fu = match_fight(win, r.fighter_a_url, r.fighter_b_url, r.fighter_a, r.fighter_b)
        with engine.begin() as conn:
            if fu is None:
                if len(win):                    # the dataset has that night but this bout is not in it: it did not happen
                    conn.execute(sa.text("UPDATE upcoming_distance_v1 SET status = 'no_result', graded_at = :t WHERE bout_id = :b"), {"t": now, "b": r.bout_id})
                    missing += 1
                else:
                    waiting += 1                # the dataset does not have that night yet
                continue
            result = str(win[win.fight_url == fu].iloc[0].result)
            if result == "DQ":                  # neither a decision nor a real finish: leave it out, as in the backtest
                conn.execute(sa.text("UPDATE upcoming_distance_v1 SET status = 'no_result', graded_at = :t WHERE bout_id = :b"), {"t": now, "b": r.bout_id})
                missing += 1
                continue
            conn.execute(sa.text("UPDATE upcoming_distance_v1 SET went_distance = :w, graded_at = :t WHERE bout_id = :b"),
                         {"w": int(result.startswith("Decision")), "t": now, "b": r.bout_id})
            graded += 1
    log.info("Graded %d; %d had no result; %d are waiting for the dataset to include their event", graded, missing, waiting)
    return 0


# ----------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------
def cmd_scoring(engine) -> int:
    d = pd.read_sql(sa.text("SELECT event_date, p_distance, p_base, went_distance, tier FROM fight_distance_v1"), engine)
    if d.empty:
        log.error("No backtest yet. Run: python pipeline/model_distance.py backtest")
        return 2
    s = distance_scores.distance_scores(d)
    print(f"\n{s['n']:,} fights ({s['first_year']} to {s['last_year']}); {s['rate']:.1%} went the distance. Lower is better, except AUC.\n")
    print(f"{'Predictor':34s} {'Log loss':>9s} {'Brier':>8s} {'AUC':>6s}")
    for k, label in (("model", "Goes-the-distance model"), ("recent", "Last-3-years decision rate"), ("coin", "Coin flip")):
        x = s["scores"][k]
        print(f"{label:34s} {x['log_loss']:9.4f} {x['brier']:8.4f} {x['auc']:6.3f}")
    for label, key in (("vs. the last-3-years decision rate", "vs_recent"), ("vs. a coin flip", "vs_coin")):
        x = s[key]
        print(f"\nPaired log-loss difference {label}: {x['mean']:+.4f}  [{x['lo']:+.4f}, {x['hi']:+.4f}]  {x['verdict']}")
    print("\nCalibration: predicted chance of going the distance vs what happened")
    for c in s["calibration"]:
        print(f"  {c['label']:12s} {c['n']:5d} fights  predicted {c['predicted']:.1%}  actual {c['actual']:.1%}")
    print(f"\nBetter than the recent decision rate in {s['years_better']} of {s['years_total']} years.")
    for t, x in s["tiers"].items():
        print(f"  {distance_scores.TIER_NAMES[t]:36s} {x['n']:5d} fights  {x['mean']:+.4f}  [{x['lo']:+.4f}, {x['hi']:+.4f}]")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Goes-the-distance model")
    sub = ap.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("backtest")
    bt.add_argument("--rebuild", action="store_true", help="score every fight again from scratch")
    pr = sub.add_parser("predict")
    pr.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
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
        return run_predict(engine, date.fromisoformat(args.today) if args.today else date.today())
    if args.command == "grade":
        return run_grade(engine)
    return cmd_scoring(engine)


if __name__ == "__main__":
    sys.exit(main())
