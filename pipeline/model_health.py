"""
Model health: is each model's live record consistent with what the model said, and does a rule fixed in advance say to retire it?

    python pipeline/model_health.py          # print the report (reads the database; a read-only connection string is enough)

The daily job runs the same check (the `health` step) and lists anything that needs a person under "Needs your attention". The rules are written down in
docs/MODEL_CARDS.md BEFORE the data can tempt anyone to bend them, and a test keeps that document and this file saying the same thing.

Two kinds of check, both on LOCKED predictions that have been graded (made before the event, never changed):

  1. CALIBRATION. Each model states a probability for what it predicts. Adding them up says how many it expects to get right (or how many fights it expects to
     go the distance); the spread around that is known. If the real count is far below what the model itself expected, something is wrong, and that is worth a
     look long before there is enough data to compare models. It needs at least CHECK_AFTER_FIGHTS graded fights; a record more than Z_WORSE standard deviations
     below what the model said is reported as worse than it said, and more than Z_ALERT is flagged for a person (a stricter bar, because this is checked every
     day, and a daily check that cried wolf at 2 standard deviations would be ignored).

  2. THE RETIREMENT RULES. Each model must beat, or at least not clearly lose to, a simple baseline, over at least RETIRE_AFTER_FIGHTS graded fights, judged on log
     loss with the 95% range of the paired per-fight difference:
        winner model v2 (model-only)      retire if clearly worse than a coin flip
        winner model v3 (market-informed) retire if clearly worse than the bookmakers' opening line alone
        goes the distance v1              retire if clearly worse than the plain decision rate of the previous three years
        goes the distance v2              retire if clearly worse than v1 (the rule that lives in model_distance_v2.py)
     "Clearly worse" means the whole 95% range lies on the wrong side of zero. With fewer fights than the rule needs, the answer is "keep", and it says how many
     more are needed. These are rules to retire a model that is doing harm, not tests of whether it is good: for the small gains in this project, the live record
     cannot show that for years (the README says how many fights it would take).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent))
import distance_scores as ds  # noqa: E402

CHECK_AFTER_FIGHTS = 100
RETIRE_AFTER_FIGHTS = 300
Z_WORSE = 2.0
Z_ALERT = 3.0
COIN = 0.5

MODELS = ("Winner model v2 (model-only)", "Winner model v3 (market-informed)", "Goes the distance v1 (model-only)", "Goes the distance v2 (market-informed)")


# ---------------------------------------------------------------------------------------------- the two checks
def calibration_check(p, outcome) -> dict:
    """p: the probability the model gave to what it predicted; outcome: 1 if that happened. Expected count, actual count, and how far apart they are in standard deviations."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(outcome, dtype=float)
    n = len(p)
    out = {"n": n, "right": int(y.sum()) if n else 0, "expected": float(p.sum()) if n else 0.0, "sd": float(np.sqrt((p * (1 - p)).sum())) if n else 0.0}
    out["z"] = (out["right"] - out["expected"]) / out["sd"] if n and out["sd"] > 0 else 0.0
    if n < CHECK_AFTER_FIGHTS:
        out.update(status=f"too few graded fights to check yet ({n} so far; it needs at least {CHECK_AFTER_FIGHTS})", alert=False)
    elif out["z"] < -Z_WORSE:
        out.update(status="worse than it said", alert=out["z"] < -Z_ALERT)
    elif out["z"] > Z_WORSE:
        out.update(status="better than it said", alert=False)
    else:
        out.update(status="consistent with what it said", alert=False)
    return out


def rule_status(n: int, diff: dict, baseline: str, subject: str) -> dict:
    """The retirement rule against one baseline. `diff` is distance_scores.paired_diff(...) of the model against the baseline (negative = the model is better)."""
    if n < RETIRE_AFTER_FIGHTS:
        return {"retire": False, "text": f"keep ({n} graded fights so far; the rule needs at least {RETIRE_AFTER_FIGHTS} before it can say anything)"}
    if diff["verdict"] == "clearly worse":
        return {"retire": True, "text": f"RETIRE {subject}: over {n} graded fights it is clearly worse than {baseline}"}
    phrase = {"clearly better": f"clearly better than {baseline}", "cannot be told apart": f"not clearly different from {baseline}"}[diff["verdict"]]
    return {"retire": False, "text": f"keep (over {n} graded fights it is {phrase})"}


# ---------------------------------------------------------------------------------------------- one entry per model
def _entry(model: str, n: int, cal: dict | None, rule: dict | None, extra: str = "") -> dict:
    needs = []
    if cal and cal["alert"]:
        needs.append(f"{model}: its live record is {abs(cal['z']):.1f} standard deviations worse than its own stated probabilities ({cal['right']} against {cal['expected']:.1f} expected, "
                     f"over {cal['n']} graded fights). Look at it before the next card.")
    if rule and rule["retire"]:
        needs.append(f"{model}: {rule['text']} (a rule fixed in advance, see docs/MODEL_CARDS.md).")
    return {"model": model, "n": n, "calibration": cal, "rule": rule, "extra": extra, "needs": needs}


def _winner_outcome(f: pd.DataFrame) -> np.ndarray:
    return (f.actual_winner == f.fighter_a).astype(int).to_numpy()


def winner_v2(f: pd.DataFrame | None) -> dict:
    name = MODELS[0]
    if f is None or not len(f):
        return _entry(name, 0, None, None)
    cal = calibration_check(f.confidence, f.correct)
    rule = rule_status(len(f), ds.paired_diff(_winner_outcome(f), f.p_a_wins, np.full(len(f), COIN)), "a coin flip", "the model-only winner pick")
    return _entry(name, len(f), cal, rule)


def winner_v3(f: pd.DataFrame | None) -> dict:
    name = MODELS[1]
    if f is None or not len(f):
        return _entry(name, 0, None, None)
    y = _winner_outcome(f)
    cal = calibration_check(f.confidence, f.correct)
    rule = rule_status(len(f), ds.paired_diff(y, f.p_a_wins, f.market_prob_a), "the bookmakers' opening line alone", "the market-informed winner pick")
    ll = lambda p: float(ds._loss(y, np.asarray(p, dtype=float)).mean())
    extra = f"log loss on these {len(f)} fights: blend {ll(f.p_a_wins):.4f}, the line alone {ll(f.market_prob_a):.4f}, model-only {ll(f.p_v2_a):.4f} (lower is better; a coin flip is 0.6931)"
    return _entry(name, len(f), cal, rule, extra)


def distance_v1(f: pd.DataFrame | None) -> dict:
    name = MODELS[2]
    if f is None or not len(f):
        return _entry(name, 0, None, None)
    cal = calibration_check(f.p_distance, f.went_distance)                                  # for these, "right" is "went the distance", and p is the chance it would
    rule = rule_status(len(f), ds.paired_diff(f.went_distance, f.p_distance, f.p_base), "the plain decision rate of the previous three years", "the goes-the-distance prediction")
    return _entry(name, len(f), cal, rule)


def distance_v2(f: pd.DataFrame | None) -> dict:
    name = MODELS[3]
    if f is None or not len(f):
        return _entry(name, 0, None, None)
    import model_distance_v2 as mdv2                                                       # the rule lives with the model it governs
    cal = calibration_check(f.p_v2, f.went_distance)
    r = mdv2.retirement_status(f)
    return _entry(name, len(f), cal, {"retire": r["retire"], "text": r["text"]})


def health_report(frames: dict) -> list[dict]:
    """frames: v2, v3, d1, d2 (graded locked predictions, or None when there are none yet). One entry per model, always in the same order."""
    return [winner_v2(frames.get("v2")), winner_v3(frames.get("v3")), distance_v1(frames.get("d1")), distance_v2(frames.get("d2"))]


def lines(report: list[dict]) -> list[str]:
    """The report as plain lines, one per model (plus the log-loss line for the winner blend)."""
    out = []
    for e in report:
        if not e["n"]:
            out.append(f"{e['model']}: no graded locked predictions yet.")
            continue
        c, r = e["calibration"], e["rule"]
        what = "went the distance" if "distance" in e["model"] else "right"
        out.append(f"{e['model']}: {c['right']} of {e['n']} {what}, against {c['expected']:.1f} expected from its own probabilities ({c['z']:+.1f} standard deviations): {c['status']}. "
                   f"Retirement rule: {r['text']}.")
        if e["extra"]:
            out.append(f"  {e['extra']}")
    return out


# ---------------------------------------------------------------------------------------------- the database and the daily job's step
QUERIES = {
    "v2": "SELECT fighter_a, p_a_wins, confidence, actual_winner, correct FROM upcoming_picks_v2 WHERE status = 'locked' AND graded_at IS NOT NULL AND correct IS NOT NULL",
    "v3": "SELECT fighter_a, p_v2_a, market_prob_a, p_a_wins, confidence, actual_winner, correct FROM upcoming_picks_v3 WHERE status = 'locked' AND graded_at IS NOT NULL AND correct IS NOT NULL",
    "d1": "SELECT p_distance, p_base, went_distance FROM upcoming_distance_v1 WHERE status = 'locked' AND graded_at IS NOT NULL AND went_distance IS NOT NULL",
    "d2": ("SELECT m.p_distance AS p_v2, v.p_distance AS p_v1, m.p_base, m.went_distance FROM upcoming_distance_v2 m JOIN upcoming_distance_v1 v ON v.bout_id = m.bout_id "
           "WHERE m.status = 'locked' AND m.graded_at IS NOT NULL AND m.went_distance IS NOT NULL"),
}


def load_frames(engine) -> dict:
    """Each model's graded locked predictions. A table that does not exist yet (a fresh database) gives None for that model, never an error."""
    out = {}
    for key, sql in QUERIES.items():
        try:
            out[key] = pd.read_sql(sa.text(sql), engine)
        except (sa.exc.SQLAlchemyError, pd.errors.DatabaseError):                          # pandas wraps the database's own "no such table" in its own error type
            out[key] = None
    return out


def step_health(engine, today, rep, loader=load_frames) -> None:
    """The daily job's step: add the health report, and list anything a rule says needs a person."""
    report = health_report(loader(engine))
    rep.add("Model health", *lines(report))
    for e in report:
        for text in e["needs"]:
            rep.need(text)


def main() -> int:
    import os
    import model_v2                                                                         # its make_engine adds the driver a postgresql:// address needs, as every other command does
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set.")
        return 2
    report = health_report(load_frames(model_v2.make_engine()))
    print("Model health (locked predictions that have been graded)\n")
    for line in lines(report):
        print(line)
    needs = [t for e in report for t in e["needs"]]
    print("\n" + ("Needs a person:\n  " + "\n  ".join(needs) if needs else "Nothing needs a person."))
    return 1 if needs else 0


if __name__ == "__main__":
    sys.exit(main())
