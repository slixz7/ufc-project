"""
"Why this prediction?" for the goes-the-distance models: what moved each prediction away from an average fight, in plain language.

Pure functions (numpy and pandas only): no database, no Streamlit. model_distance.py and model_distance_v2.py store the result in `pick_reasons` under the versions
'd1' (model-only) and 'd2' (with the betting line), and app/distance_why_ui.py draws it. The winner picks already work this way (model_v2.build_reasons).

THE MODEL-ONLY PREDICTION (d1) is a logistic regression on ten standardised inputs, so it decomposes EXACTLY. Each input adds `coefficient x standardised value` to the
log-odds, the intercept is the log-odds of an average fight (every standardised input at zero), and

        intercept + the sum of the contributions = the model's log-odds, so   sigmoid(that) = the prediction.

The ten inputs are grouped into six readable factors (how often these fighters go to a decision, how often they win or lose by a finish, how long their fights last, the
scheduled rounds, a title fight). A group can mix inputs that pull in different directions, so a bar might oppose the headline numbers beside it: the same trap the winner
panel fell into once (the README's lessons list it). Every factor therefore carries a `net` flag, set when its bar points against what its headline number suggests, and the
page says "net effect" beside it instead of letting a bar contradict the words.

THE MARKET-INFORMED PREDICTION (d2) is a second stage on top of d1: d1's log-odds, the favourite's strength and its square. That does not split into tidy factors, so it is
told as one comparison: what d1 said, what the line makes of it, and how much of the change comes from the favourite's strength (the same fight with a typical favourite
gives `p_typical`; the rest of the change is the second stage re-weighting d1's number).

An explanation is stored only when it reproduces the locked prediction (the callers check), so it can never contradict the number beside it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import model_v2 as v2

SCHEMA = 1
MIN_SHOWN = 0.02                 # a factor smaller than this (on the log-odds scale) is folded into "the rest"
TOLERANCE = 1e-6                 # how closely a rebuilt model must reproduce the locked prediction

GROUPS = {
    "decisions": ["dec_mean", "dec_min", "dec_max"],
    "finishing": ["finwin_mean", "finwin_max"],
    "durability": ["finloss_mean", "finloss_max"],
    "pace": ["dur_mean"],
    "rounds": ["five"],
    "title": ["title"],
}
LABELS = {"decisions": "Going to a decision", "finishing": "Winning by a finish", "durability": "Being finished", "pace": "How long fights last",
          "rounds": "Scheduled rounds", "title": "Title fight"}
# For the factors a headline number speaks for: the input that is the headline, and which way a HIGH value should push (+1 toward a decision, -1 toward a finish).
HEADLINE = {"decisions": ("dec_mean", 1), "finishing": ("finwin_mean", -1), "durability": ("finloss_mean", -1), "pace": ("dur_mean", 1)}


def sigmoid(x) -> float:
    return float(1.0 / (1.0 + np.exp(-x)))


def contributions(model, X: pd.DataFrame):
    """(C, intercept, Z): C holds what each input adds to the log-odds, Z the standardised inputs. intercept + C.sum(axis=1) is the model's log-odds."""
    Z = pd.DataFrame(model[0].transform(X), index=X.index, columns=X.columns)
    return Z * model[1].coef_[0], float(model[1].intercept_[0]), Z


# ----------------------------------------------------------------------------------------------
# the words
# ----------------------------------------------------------------------------------------------
def _n(r, s: str) -> int:
    return int(round(float(r[f"c_elig_{s}"])))


def _each(la: str, lb: str, r, text) -> str:
    """text(who, side) for each fighter, with a plain line for a fighter who has no UFC fights to count."""
    return ", ".join(f"{who} has no UFC fights yet" if _n(r, s) == 0 else text(who, s) for who, s in ((la, "a"), (lb, "b")))


def _detail(g: str, r, la: str, lb: str) -> str:
    count = lambda key: (lambda who, s: f"{who} {int(round(float(r[f'{key}_{s}'])))} of {_n(r, s)}")
    if g == "decisions":
        return "Earlier fights that went to a decision: " + _each(la, lb, r, count("c_dec"))
    if g == "finishing":
        return "Earlier fights won by a finish: " + _each(la, lb, r, count("c_fin_win"))
    if g == "durability":
        return "Earlier fights lost by a finish: " + _each(la, lb, r, count("c_fin_loss"))
    if g == "pace":
        return "Average share of the scheduled time their earlier fights used: " + _each(la, lb, r, lambda who, s: f"{who} {float(r[f'c_dur_frac_{s}']) / _n(r, s):.0%}")
    if g == "rounds":
        return ("Scheduled for five rounds (assumed: a title fight or main event)" if int(r["five"]) else
                "Scheduled for three rounds (assumed: only a title fight or main event has five)")
    if g == "title":
        return "A title fight" if int(r["title"]) else "Not a title fight"
    return ""


# ----------------------------------------------------------------------------------------------
# the model-only prediction
# ----------------------------------------------------------------------------------------------
def build_v1(r, c: pd.Series, z: pd.Series, intercept: float, name_a: str, name_b: str) -> dict:
    """The breakdown of one model-only prediction. r: the bout's feature row (the c_* counts, five, title); c, z: its contributions and standardised inputs."""
    la, lb = v2.last_name(name_a), v2.last_name(name_b)
    factors = []
    for g, feats in GROUPS.items():
        val = float(sum(c[f] for f in feats))
        net = False
        if g in HEADLINE:                                                               # does the bar point against what the headline number suggests?
            head, orient = HEADLINE[g]
            net = abs(float(z[head])) >= 0.05 and (val > 0) != (orient * float(z[head]) > 0)
        factors.append({"key": g, "label": LABELS[g], "c": round(val, 4), "toward": "decision" if val > 0 else "finish", "detail": _detail(g, r, la, lb), "net": bool(net)})
    factors.sort(key=lambda f: -abs(f["c"]))
    shown = [f for f in factors if abs(f["c"]) >= MIN_SHOWN]
    rest = float(sum(f["c"] for f in factors if f not in shown))
    total = intercept + float(c.sum())
    return {"schema": SCHEMA, "kind": "d1", "a": name_a, "b": name_b, "rounds": 5 if int(r["five"]) else 3, "intercept": round(intercept, 4), "base": round(sigmoid(intercept), 4),
            "total": round(total, 4), "p": round(sigmoid(total), 4), "factors": shown, "rest": round(rest, 4)}


# ----------------------------------------------------------------------------------------------
# the market-informed prediction
# ----------------------------------------------------------------------------------------------
def build_v2(name_a: str, name_b: str, p_v1: float, fav_prob: float, p_v2: float, typical: float, p_typical: float) -> dict:
    """One comparison. typical: the favourite strength of a typical fight in the second stage's training data; p_typical: what the second stage says for THIS fight if its line had
    shown a typical favourite. line_effect is therefore the part of the change that comes from how strong the favourite is; rest_effect is the second stage re-weighting d1."""
    r4 = lambda x: round(float(x), 4)
    return {"schema": SCHEMA, "kind": "d2", "a": name_a, "b": name_b, "p_v1": r4(p_v1), "fav_prob": r4(fav_prob), "typical_fav": r4(typical), "p_typical": r4(p_typical),
            "p_v2": r4(p_v2), "line_effect": r4(p_v2 - p_typical), "rest_effect": r4(p_typical - p_v1)}
