"""
The evidence meter: for each claim the project makes about a model, how much does the live record say so far, and how much more would it take to be reasonably sure?

Pure functions (numpy and pandas only): no database, no Streamlit. The Track record page draws them (app/evidence_ui.py).

Four claims, each a comparison of two predictions of the same yes/no outcome, scored by log loss on the same fights (negative means the model is better):

    the model-only winner pick      against  a coin flip
    the market-informed winner pick against  the bookmakers' opening line alone
    the goes-the-distance prediction against  the plain decision rate of the previous three years
    the market-informed distance prediction against  the model-only one

For each: the LIVE estimate (graded locked predictions only) with its 95% range, what the BACKTEST found for the same comparison, and how many graded live fights would give an
80% chance of seeing an effect that size at the usual 5% level: ((1.96 + 0.84) x sd / effect) squared, where sd is how much the per-fight difference varied in the backtest. That
is the same convention as the README's "live fights needed", and a test compares the two. The point is honesty about scale: a few dozen fights cannot show a gain of a few
thousandths of a log loss, however good the first results look.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import distance_scores as ds

POWER_Z = 1.96 + 0.84                # 80% power at the 5% level
MIN_FIGHTS_TO_SAY = 10               # below this, no sentence about the estimate
RETIRE_AFTER_FIGHTS = 300            # the retirement rule's number (a test checks it against model_health)

CLAIMS = (
    {"key": "v2", "question": "Does the model-only winner pick beat a coin flip?", "model": "the model-only winner pick", "baseline": "a coin flip", "unit": "winner picks",
     "outcome": "winner", "cols": {"backtest": ("p_v2_a", None), "live": ("p_a_wins", None)}},
    {"key": "v3", "question": "Does the market-informed winner pick beat the bookmakers' line alone?", "model": "the market-informed winner pick", "baseline": "the bookmakers' line alone", "unit": "winner picks",
     "outcome": "winner", "cols": {"backtest": ("p_a_wins", "market_prob_a"), "live": ("p_a_wins", "market_prob_a")}},
    {"key": "d1", "question": "Does the goes-the-distance prediction beat the plain decision rate?", "model": "the goes-the-distance prediction", "baseline": "the plain decision rate of the previous three years",
     "unit": "distance predictions", "outcome": "distance", "cols": {"backtest": ("p_distance", "p_base"), "live": ("p_distance", "p_base")}},
    {"key": "d2", "question": "Does the market-informed distance prediction beat the model-only one?", "model": "the market-informed distance prediction", "baseline": "the model-only one",
     "unit": "distance predictions", "outcome": "distance", "cols": {"backtest": ("p_distance", "p_v1"), "live": ("p_v2", "p_v1")}},
)


def _triple(spec: dict, frame: pd.DataFrame | None, source: str):
    """(outcome, the model's probability, the baseline's probability) as arrays, from a backtest or live frame; None when there is nothing to use."""
    if frame is None or not len(frame):
        return None
    new_col, ref_col = spec["cols"][source]
    need = ["fighter_a", "actual_winner"] if spec["outcome"] == "winner" else ["went_distance"]
    if any(c not in frame.columns for c in need + [new_col] + ([ref_col] if ref_col else [])):
        return None
    f = frame.dropna(subset=need + [new_col] + ([ref_col] if ref_col else []))
    if not len(f):
        return None
    y = (f.actual_winner == f.fighter_a).astype(int).to_numpy() if spec["outcome"] == "winner" else f.went_distance.astype(int).to_numpy()
    new = f[new_col].astype(float).to_numpy()
    ref = f[ref_col].astype(float).to_numpy() if ref_col else np.full(len(f), 0.5)
    return y, new, ref


def paired(spec: dict, frame: pd.DataFrame | None, source: str) -> dict | None:
    t = _triple(spec, frame, source)
    return None if t is None else ds.paired_diff(*t)


def needed_fights(effect: dict | None) -> int | None:
    """Graded live fights for an 80% chance of seeing an effect of the backtest's size; None when the backtest found no gain to confirm."""
    if not effect or not effect["mean"] < 0 or not effect["sd"] > 0:
        return None
    return int(math.ceil((POWER_Z * effect["sd"] / abs(effect["mean"])) ** 2))


def fights_per_year(frame: pd.DataFrame | None) -> float | None:
    if frame is None or not len(frame) or "event_date" not in frame.columns:
        return None
    years = pd.to_datetime(frame["event_date"]).dt.year
    return len(frame) / max(1, int(years.max() - years.min() + 1))


def years_text(years: float) -> str:
    if years < 0.9:
        months = max(1, round(years * 12))
        return f"about {months} month{'s' if months != 1 else ''}"
    if years < 1.5:
        return "about a year"
    return f"about {round(years)} years"


def sentence(e: dict) -> str:
    """What the live record says so far, in one or two sentences that claim nothing the numbers do not."""
    n, live, bt = e["n_live"], e["live"], e["backtest"]
    if not n or live is None:
        return "No graded fights yet, so there is nothing to show. The bar fills as cards are graded."
    if n < MIN_FIGHTS_TO_SAY:
        return f"Only {n} graded {e['unit']} so far: too few to say anything."
    m, lo, hi = live["mean"], live["lo"], live["hi"]
    if live["verdict"] == "clearly better":
        core = f"So far {e['model']} is clearly better than {e['baseline']}: {abs(m):.4f} less log loss per fight (95% range {abs(hi):.4f} to {abs(lo):.4f} less)."
    elif live["verdict"] == "clearly worse":
        rule = f"The retirement rule acts only after {RETIRE_AFTER_FIGHTS} graded fights, and there are {n}." if n < RETIRE_AFTER_FIGHTS else "That is what the retirement rule is for."
        core = f"So far {e['model']} is clearly worse than {e['baseline']}: {m:.4f} more log loss per fight (95% range {lo:.4f} to {hi:.4f} more). {rule}"
    else:
        core = (f"After {n} graded fights, {e['model']} cannot be told apart from {e['baseline']}: the difference is {m:+.4f} per fight, "
                f"with a 95% range from {lo:+.4f} to {hi:+.4f}.")
    if bt is not None:
        if lo <= bt["mean"] <= hi:
            core += f" That range includes what the backtest found ({bt['mean']:+.4f}), so nothing here contradicts it."
        else:
            core += f" That range does not include what the backtest found ({bt['mean']:+.4f}). Worth watching, though at this size chance does that about one time in twenty."
    return core


def entry(spec: dict, backtest: dict, live: dict) -> dict:
    bt_frame, live_frame = backtest.get(spec["key"]), live.get(spec["key"])
    bt, lv = paired(spec, bt_frame, "backtest"), paired(spec, live_frame, "live")
    needed = needed_fights(bt)
    per_year = fights_per_year(bt_frame)
    n = lv["n"] if lv else 0
    e = {"key": spec["key"], "question": spec["question"], "model": spec["model"], "baseline": spec["baseline"], "unit": spec["unit"], "n_live": n, "live": lv, "backtest": bt,
         "needed": needed, "per_year": per_year, "progress": (min(1.0, n / needed) if needed else None),
         "years_needed": (needed / per_year if needed and per_year else None)}
    e["sentence"] = sentence(e)
    return e


def compute_all(backtest: dict, live: dict) -> list[dict]:
    """backtest and live map the claim keys (v2, v3, d1, d2) to frames (or None). Always one entry per claim, in the same order."""
    return [entry(spec, backtest, live) for spec in CLAIMS]
