"""
Experiment: does the betting line say anything about whether a fight goes the distance, beyond what the distance model already knows?

READ-ONLY. It changes nothing in the database and nothing on the site. It prints a report and a verdict.

    python pipeline/experiment_distance_market.py

Why this is worth testing. In 6,705 fights with lines, a heavy favourite finishes more often (3-round fights: about 52% go to a decision
when the favourite is at 55-65%, about 36% when the favourite is at 80% or more). The distance model already uses how long each fighter's
earlier fights lasted, and a strong favourite is often a finisher, so the line may add little on top. This measures it.

How it is tested (the same way the winner model's market blend was). Every past fight already has a stored distance prediction made by a
model trained on EARLIER fights only (fight_distance_v1). For each test year Y, a small second-stage model is fitted on the fights of years
before Y (their stored prediction, their opening line, whether they went the distance) and scored on year Y. Nothing from year Y, or
later, is ever used to score year Y.

The comparison that matters is against the same stored prediction RECALIBRATED the same way (one-variable stack). A gain over the raw
prediction alone could come from merely rescaling its probabilities; a gain over the recalibrated one can only come from the line.

THE DECISION RULE, fixed before any real result was seen. The line is worth adding only if ALL of these hold, for the primary
specification (stored prediction + the opening line's favourite strength and its square), measured against the recalibrated prediction:
  1. the log-loss gain, over all test fights, is at least 0.002;
  2. the 95% interval of that gain (resampling whole events) lies above zero;
  3. the gain is positive in at least 60% of the test years;
  4. calibration is not worse: the stacked prediction's calibration error is at most 0.005 above the raw prediction's.
Otherwise the verdict is DROP, the idea is retired, and the README says so. The opening line is the primary input because that is the line
that exists when picks are locked; the closing line is reported for information only.
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import model_v2 as v2

log = logging.getLogger("experiment_distance_market")

MIN_TRAIN = 800                     # fights (with lines) before the second stage is fitted
MIN_TEST = 100                      # fights in a year before it is scored
EPS = 1e-6
BOOTSTRAP = 2000
SEED = 20261029
C_REG = 1.0
RULE = {"min_gain": 0.002, "min_year_share": 0.60, "max_ece_worse": 0.005}
PRIMARY = "quadratic"
# name -> (which line, how many powers of the favourite's strength). "recal" uses no line at all.
SPECS = {"recal": (None, 0), "linear": ("open", 1), "quadratic": ("open", 2), "quadratic_close": ("close", 2)}

QUERY = """SELECT d.fight_url, d.event_date, d.p_distance, d.went_distance, d.scheduled_rounds, o.open_prob_f1, o.close_prob_f1
           FROM fight_distance_v1 d JOIN fight_odds o ON o.fight_id = split_part(d.fight_url, '/', 5)
           WHERE o.open_prob_f1 > 0 AND o.open_prob_f1 < 1 AND d.p_distance > 0 AND d.p_distance < 1
           ORDER BY d.event_date, d.fight_url"""


# ----------------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------------
@contextmanager
def read_only(engine):
    """A connection that cannot write: the database itself refuses any change."""
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        yield conn


def load(engine) -> pd.DataFrame:
    with read_only(engine) as conn:
        df = pd.read_sql(sa.text(QUERY), conn)
    df["event_date"] = pd.to_datetime(df.event_date)
    df["year"] = df.event_date.dt.year
    df["close_prob_f1"] = df.close_prob_f1.fillna(df.open_prob_f1)
    return df


# ----------------------------------------------------------------------------------------------
# the second stage, walk-forward
# ----------------------------------------------------------------------------------------------
def logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def design(df: pd.DataFrame, spec: str) -> np.ndarray:
    line, powers = SPECS[spec]
    cols = [logit(df.p_distance.values)]
    if line is not None:
        s = np.abs(df["open_prob_f1" if line == "open" else "close_prob_f1"].values.astype(float) - 0.5)      # how strong the favourite is, whoever it is
        cols += [s ** k for k in range(1, powers + 1)]
    return np.column_stack(cols)


def fit(train: pd.DataFrame, spec: str):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C_REG, max_iter=3000)).fit(design(train, spec), train.went_distance.astype(int))


def walk_forward(df: pd.DataFrame) -> pd.DataFrame:
    """For each year, a second stage fitted on EARLIER years only, scored on that year."""
    parts = []
    for year in sorted(df.year.unique()):
        train, test = df[df.year < year], df[df.year == year]
        if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
            continue
        out = test[["fight_url", "event_date", "year", "open_prob_f1"]].copy()
        out["y"] = test.went_distance.astype(int).values
        out["p_raw"] = np.clip(test.p_distance.values, EPS, 1 - EPS)
        for name in SPECS:
            out[f"p_{name}"] = fit(train, name).predict_proba(design(test, name))[:, 1]
        parts.append(out)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ----------------------------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------------------------
def losses(y, p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    y = np.asarray(y, dtype=float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def ece(y, p, bins: int = 10) -> float:
    """Calibration error: how far, on average, the stated probabilities are from how often things happened."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).sum() for b in range(bins) if (idx == b).any()) / len(y))


def bootstrap_gain(loss_ref: np.ndarray, loss_new: np.ndarray, clusters: np.ndarray, draws: int = BOOTSTRAP, seed: int = SEED) -> tuple[float, float, float]:
    """The mean log-loss gain (reference minus new) and its 95% interval, resampling whole events (fights on one card are not independent)."""
    diff = np.asarray(loss_ref) - np.asarray(loss_new)
    frame = pd.DataFrame({"d": diff, "c": np.asarray(clusters)}).groupby("c").d.agg(["sum", "count"])
    sums, cnts = frame["sum"].to_numpy(), frame["count"].to_numpy()
    rng = np.random.default_rng(seed)
    k = len(sums)
    means = np.empty(draws)
    for i in range(draws):
        pick = rng.integers(0, k, k)
        means[i] = sums[pick].sum() / cnts[pick].sum()
    return float(diff.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def verdict(res: pd.DataFrame, rule: dict = RULE) -> dict:
    ref, new = losses(res.y, res.p_recal), losses(res.y, res[f"p_{PRIMARY}"])
    gain, lo, hi = bootstrap_gain(ref, new, res.event_date.values)
    per_year = pd.DataFrame({"year": res.year.values, "d": ref - new}).groupby("year").d.mean()
    share = float((per_year > 0).mean())
    ece_raw, ece_new = ece(res.y, res.p_raw), ece(res.y, res[f"p_{PRIMARY}"])
    checks = {
        "gain_big_enough": gain >= rule["min_gain"],
        "interval_above_zero": lo > 0,
        "enough_years_better": share >= rule["min_year_share"],
        "calibration_not_worse": ece_new <= ece_raw + rule["max_ece_worse"],
    }
    return {"gain": gain, "lo": lo, "hi": hi, "year_share": share, "years_better": int((per_year > 0).sum()), "years": int(len(per_year)),
            "ece_raw": ece_raw, "ece_new": ece_new, "checks": checks, "adopt": all(checks.values())}


# ----------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------
def report(res: pd.DataFrame, v: dict) -> str:
    L = []
    w = lambda s="": L.append(s)
    w("DOES THE BETTING LINE ADD TO THE GOES-THE-DISTANCE MODEL?   (read-only experiment)")
    w(f"{len(res):,} fights, {int(res.year.min())}-{int(res.year.max())}, each scored by a second stage fitted on earlier years only.")
    w()
    w("Log loss on the distance question (lower is better; a coin flip is 0.6931):")
    t = pd.DataFrame({"year": res.year.values})
    for name, label in (("raw", "stored"), ("recal", "recalibrated"), ("linear", "+line"), ("quadratic", "+line,sq"), ("quadratic_close", "+close")):
        t[label] = losses(res.y, res[f"p_{name}"])
    by = t.groupby("year").mean().round(4)
    by.insert(0, "fights", t.groupby("year").size())
    w(by.to_string())
    pooled = t.drop(columns="year").mean().round(4)
    w("all years (fight-weighted):  " + "   ".join(f"{k} {v_:.4f}" for k, v_ in pooled.items()))
    w("  stored = the distance model's own prediction.  recalibrated = the same, rescaled by a one-variable stack.  +line = adds how strong the")
    w("  favourite is (opening line).  +line,sq = also its square (the PRIMARY version).  +close = the same with the closing line (information only).")
    w()
    b = pd.cut(np.abs(res.open_prob_f1 - 0.5), [-0.001, 0.10, 0.20, 0.30, 0.5], labels=["favourite 50-60%", "60-70%", "70-80%", "80%+"])
    g = pd.DataFrame({"b": b, "y": res.y, "raw": res.p_raw, "stack": res[f"p_{PRIMARY}"]}).groupby("b", observed=True).agg(fights=("y", "size"), went_distance=("y", "mean"), stored_says=("raw", "mean"), stack_says=("stack", "mean")).rename_axis(None)
    w("What happened, and what each version said (% going the distance), by how strong the favourite was (opening line):")
    w((g * [1, 100, 100, 100]).round(1).assign(fights=g.fights).to_string())
    w()
    w("THE TEST  (primary: stored prediction + opening-line favourite strength and its square, against the same prediction recalibrated)")
    c = v["checks"]
    row = lambda n, text, passed: w(f"  {n}. {text:<82}{'PASS' if passed else 'FAIL'}")
    row(1, f"gain {v['gain']:+.4f} log loss (needs at least {RULE['min_gain']})", c["gain_big_enough"])
    row(2, f"95% interval {v['lo']:+.4f} to {v['hi']:+.4f} (the low end must be above zero)", c["interval_above_zero"])
    row(3, f"better in {v['years_better']} of {v['years']} years (needs {RULE['min_year_share']:.0%} of years)", c["enough_years_better"])
    row(4, f"calibration error {v['ece_new']:.4f} stacked vs {v['ece_raw']:.4f} stored (at most {RULE['max_ece_worse']} worse allowed)", c["calibration_not_worse"])
    w()
    w("VERDICT: " + ("ADOPT. The line adds something real to the distance model; it is worth building properly, with tests." if v["adopt"] else
                     "DROP. The line does not add enough to the distance model to be worth the complexity. Retire the idea and say so in the README."))
    w("This is evidence about past fights, not a promise about future ones, and nothing here is betting advice.")
    return "\n".join(L)


def run(engine) -> tuple[str, dict | None]:
    df = load(engine)
    if len(df) < MIN_TRAIN + MIN_TEST:
        return (f"Only {len(df)} fights have both a stored distance prediction and an opening line; at least {MIN_TRAIN + MIN_TEST} are needed.\n"
                "Run `python pipeline/model_distance.py backtest` and `python pipeline/model_v3.py load-odds --csv ...` first."), None
    res = walk_forward(df)
    if res.empty:
        return "No year had enough earlier fights and enough fights of its own to be scored.", None
    v = verdict(res)
    return report(res, v), v


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n")[1]).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    text, v = run(v2.make_engine())
    print(text)
    return 0 if v is not None else 1


if __name__ == "__main__":
    sys.exit(main())
