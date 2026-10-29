"""
Scoring for the goes-the-distance model, shared by the pipeline, the README generator and the website (numpy and pandas only).

`d` has one row per scored fight with: p_distance (the model's chance the fight goes to a decision), p_base (the plain decision rate
over the previous three years, the honest baseline), went_distance (1 if it did), event_date, and optionally tier.
All numbers are "lower is better" except AUC.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BINS = [0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 1.01]
BIN_LABELS = ["under 40%", "40 to 45%", "45 to 50%", "50 to 55%", "55 to 60%", "60% and up"]
TIER_NAMES = {"full": "both fighters have 3+ UFC fights", "limited": "a fighter has 1 to 2", "debut": "a fighter is a debutant"}


def _clip(p) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)


def auc(y, p) -> float:
    """Probability that a fight that went the distance was given a higher chance than one that did not (0.5 = no skill)."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    pos, neg = int((y == 1).sum()), int((y == 0).sum())
    if pos == 0 or neg == 0 or np.ptp(p) < 1e-12:
        return 0.5
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _loss(y, p) -> np.ndarray:
    p = _clip(p)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _paired(diff: np.ndarray) -> dict:
    n = len(diff)
    m = float(diff.mean())
    se = float(diff.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    lo, hi = m - 1.96 * se, m + 1.96 * se
    return {"n": n, "mean": m, "lo": lo, "hi": hi, "sd": se * np.sqrt(n) if n > 1 else float("nan"),
            "verdict": "clearly better" if hi < 0 else ("clearly worse" if lo > 0 else "cannot be told apart")}


def distance_scores(d: pd.DataFrame, min_year_fights: int = 50) -> dict:
    y = d.went_distance.astype(float).to_numpy()
    P = {"model": _clip(d.p_distance), "recent": _clip(d.p_base), "coin": np.full(len(d), 0.5)}
    ll = {k: _loss(y, p) for k, p in P.items()}
    scores = {k: {"log_loss": float(ll[k].mean()), "brier": float(((P[k] - y) ** 2).mean()), "auc": auc(y, P[k]) if k != "coin" else 0.5} for k in P}
    d = d.assign(_year=pd.to_datetime(d.event_date).dt.year.to_numpy(), _gain=ll["recent"] - ll["model"], _p=P["model"], _y=y)
    gain = d.groupby("_year").agg(fights=("_gain", "size"), gain=("_gain", "mean"))          # positive = the model beat the plain decision rate that year
    gain = gain[gain.fights >= min_year_fights]
    cal = []
    binned = pd.cut(d._p, BINS, labels=BIN_LABELS, right=False)
    for label in BIN_LABELS:
        g = d[binned == label]
        if len(g):
            cal.append({"label": label, "n": len(g), "predicted": float(g._p.mean()), "actual": float(g._y.mean())})
    tiers = {}
    if "tier" in d:
        for t in ("full", "limited", "debut"):
            m = (d.tier == t).to_numpy()
            if m.sum() > 1:
                tiers[t] = _paired((ll["model"] - ll["recent"])[m])
    return {"n": len(d), "rate": float(y.mean()), "scores": scores, "vs_recent": _paired(ll["model"] - ll["recent"]), "vs_coin": _paired(ll["model"] - ll["coin"]),
            "calibration": cal, "max_gap": max([abs(c["predicted"] - c["actual"]) for c in cal if c["n"] >= 150] or [0.0]),
            "gain_by_year": {int(k): float(v) for k, v in gain.gain.items()}, "years_better": int((gain.gain > 0).sum()), "years_total": int(len(gain)),
            "tiers": tiers, "first_year": int(d._year.min()), "last_year": int(d._year.max())}


def pair_summary(d: pd.DataFrame, new: str, ref: str) -> dict:
    """Fights, how often they went the distance, and each prediction's average log loss on those same fights."""
    y = d.went_distance.astype(float).to_numpy()
    return {"n": len(d), "rate": float(y.mean()), "new": float(_loss(y, d[new].to_numpy()).mean()), "ref": float(_loss(y, d[ref].to_numpy()).mean())}


def paired_vs(d: pd.DataFrame, new: str, ref: str) -> dict:
    """The per-fight log-loss difference between two predictions on the SAME fights (new minus ref; negative means `new` is better), with a 95% range."""
    y = d.went_distance.astype(float).to_numpy()
    return _paired(_loss(y, d[new].to_numpy()) - _loss(y, d[ref].to_numpy()))


def years_better_vs(d: pd.DataFrame, new: str, ref: str, min_year_fights: int = 50) -> tuple[int, int]:
    """(years in which `new` had the lower log loss, years counted), over years with at least `min_year_fights` fights."""
    y = d.went_distance.astype(float).to_numpy()
    g = pd.DataFrame({"year": pd.to_datetime(d.event_date).dt.year.to_numpy(), "gain": _loss(y, d[ref].to_numpy()) - _loss(y, d[new].to_numpy())}).groupby("year").gain.agg(["size", "mean"])
    g = g[g["size"] >= min_year_fights]
    return int((g["mean"] > 0).sum()), int(len(g))
