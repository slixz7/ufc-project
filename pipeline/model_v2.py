#!/usr/bin/env python3
"""
model_v2.py - model v2: a pick for EVERY fight, past and upcoming.

What changed from v1 (see the walk-forward tests in the README):
  * trained on ALL earlier fights, not just those where both fighters had 3+ UFC fights
  * small-sample "shrinkage": a fighter's stats are pulled toward the average in proportion to how few fights they are based on
  * Elo rating and the level of opponents faced
  * age and layoff curves
Accuracy in testing: about 61% over 2014-2026 (v1: 60%), with better-calibrated probabilities, and picks for debuts.

Commands (use the OWNER database string, these write to the database)
    python pipeline/model_v2.py setup                 # create the v2 tables and views (once, safe to re-run)
    python pipeline/model_v2.py backtest              # a pick for every past fight, each using only earlier fights
    python pipeline/model_v2.py backtest --rebuild    # throw the old backtest away and redo it
    python pipeline/model_v2.py predict               # picks for loaded upcoming cards (locked, never changed)
    python pipeline/model_v2.py grade                 # score locked picks once results are in the dataset
    python pipeline/model_v2.py explain               # store the plain-language "Why this pick?" reasons for locked picks that lack them

Every pick carries a tier: full (both fighters have 3+ UFC fights), limited (the less experienced has 1-2),
debut (a fighter has none). Fights before the model had enough history to learn from (the first ~300) use a
simple rule (higher Elo rating, then the younger fighter) and are marked method = early_rule.

Environment: DATABASE_URL (default postgresql+psycopg2://postgres:password@localhost:5432/ufc)
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("model_v2")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql+psycopg2://postgres:password@localhost:5432/ufc"
MIN_TRAIN = 300                      # fights needed before the model trains; earlier fights use the Elo/age rule
K_SHRINK = 3                         # pseudo-fights of "average" mixed into every fighter's stats
K_NEW, K_ELO, NEW_N = 64.0, 32.0, 3  # Elo update sizes (bigger for a fighter's first fights), tuned on 2010-2013 only
SH = ["prev_win_rate", "last3_win_rate", "slpm", "sapm", "sig_acc", "td_per15", "td_acc", "td_def", "sub_att_per15",
      "kd_per15", "ctrl_share", "grappling_share", "ko_win_rate", "sub_win_rate"]
ADD = ["won", "ko_win", "sub_win", "dur_sec", "sig_land", "sig_att", "sig_absorbed", "td_land", "td_att", "opp_td_land",
       "opp_td_att", "sub_att", "kd", "ctrl_sec", "dist_att", "clinch_att", "ground_att", "one"]
STATS = ["sig_land", "sig_att", "sig_absorbed", "td_land", "td_att", "opp_td_land", "opp_td_att", "sub_att", "kd",
         "ctrl_sec", "dist_att", "clinch_att", "ground_att"]


def make_engine():
    url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    return sa.create_engine(url)


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"['’‘`´]", "", s)
    s = re.sub(r"[-–—.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------------------------
def load_history(engine) -> pd.DataFrame:
    """One row per fighter per real fight, with fight info and raw stats."""
    ff = pd.read_sql(sa.text("""
        SELECT ff.fight_url, ff.fighter_url, ff.fighter_name, ff.side, ff.won, ff.sig_land, ff.sig_att, ff.td_land, ff.td_att,
               ff.sub_att, ff.kd, ff.ctrl_sec, ff.dist_att, ff.clinch_att, ff.ground_att, ff.sig_absorbed, ff.opp_td_land,
               ff.opp_td_att, ff.height_cm, ff.reach_cm, ff.stance, ff.dob, f.event_date, f.event_name, f.weight_class,
               f.title_fight, f.result, f.finish_round, f.finish_time
        FROM fighter_fights ff JOIN fights f USING (fight_url)"""), engine)
    ff["event_date"] = pd.to_datetime(ff.event_date)
    ff["dob"] = pd.to_datetime(ff.dob, errors="coerce")
    t = ff.finish_time.str.split(":", expand=True).astype(float)
    ff["dur_sec"] = ((ff.finish_round - 1) * 300 + t[0] * 60 + t[1]).fillna(0)
    res = ff.result.fillna("")
    ff["ko_win"] = ((ff.won == 1) & (res == "KO/TKO")).astype(int)          # same definitions as sql/features.sql
    ff["sub_win"] = ((ff.won == 1) & (res == "Submission")).astype(int)
    ff[STATS] = ff[STATS].fillna(0)
    ff["is_up"] = False
    return ff.sort_values(["event_date", "fight_url", "side"]).reset_index(drop=True)


def placeholders(engine, real: pd.DataFrame, only_ids=None) -> pd.DataFrame:
    """Two rows per scheduled upcoming bout (no result, no stats). Features are computed as of the event date."""
    up = pd.read_sql(sa.text("SELECT * FROM upcoming_fights WHERE status = 'scheduled'"), engine)
    if only_ids is not None:
        up = up[up.bout_id.isin(only_ids)]
    if up.empty:
        return pd.DataFrame()
    latest = real.sort_values("event_date").groupby("fighter_url").tail(1).set_index("fighter_url")
    rows = []
    for u in up.itertuples():
        for side, url, name in ((1, u.fighter_a_url, u.fighter_a_name), (2, u.fighter_b_url, u.fighter_b_name)):
            a = latest.loc[url] if url in latest.index else None
            rows.append({"fight_url": f"upcoming:{u.bout_id}", "fighter_url": url, "fighter_name": name, "side": side,
                         "won": np.nan, "event_date": pd.Timestamp(u.event_date), "event_name": u.event_name,
                         "weight_class": u.weight_class, "title_fight": u.title_fight, "result": None,
                         "height_cm": None if a is None else a.height_cm, "reach_cm": None if a is None else a.reach_cm,
                         "stance": None if a is None else a.stance, "dob": pd.NaT if a is None else a.dob, "is_up": True,
                         **{c: 0 for c in STATS + ["dur_sec", "ko_win", "sub_win"]}})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------
# features: everything is "as of the start of the event day" (so double fights on a tournament night don't leak)
# ----------------------------------------------------------------------------------------------
def add_state(real: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    r = real.copy()
    r["one"] = 1
    daily = r.groupby(["fighter_url", "event_date"], as_index=False)[ADD].sum().sort_values(["fighter_url", "event_date"])
    cum = daily.copy()
    cum[ADD] = daily.groupby("fighter_url")[ADD].cumsum()
    cum["prev_date"] = cum.event_date
    # streak and recent form after the last fight of each day
    seq = real.sort_values(["fighter_url", "event_date", "fight_url"])[["fighter_url", "event_date", "won"]].copy()
    streak = np.zeros(len(seq))
    last3 = np.zeros(len(seq))
    fu = seq.fighter_url.values
    won = seq.won.values
    cur, start = 0, 0
    for i in range(len(seq)):
        if i == 0 or fu[i] != fu[i - 1]:
            cur, start = 0, i
        cur = (cur + 1 if cur > 0 else 1) if won[i] == 1 else (cur - 1 if cur < 0 else -1)
        streak[i] = cur
        last3[i] = won[max(start, i - 2): i + 1].mean()
    seq["streak_after"], seq["last3_after"] = streak, last3
    seq = seq.groupby(["fighter_url", "event_date"], as_index=False).tail(1)[["fighter_url", "event_date", "streak_after", "last3_after"]]
    cum = cum.merge(seq, on=["fighter_url", "event_date"], how="left").sort_values("event_date")
    q = rows[["fighter_url", "event_date"]].copy()
    q["_i"] = rows.index
    m = pd.merge_asof(q.sort_values("event_date"), cum, on="event_date", by="fighter_url", allow_exact_matches=False)
    m = m.set_index("_i").reindex(rows.index)
    for c in ADD:
        m[c] = m[c].fillna(0)
    n, dur = m["one"], m["dur_sec"]
    out = rows.copy()
    out["n_prev"] = n
    out["prev_win_rate"] = (m.won / n).where(n > 0)
    out["last3_win_rate"] = m.last3_after.where(n > 0)
    out["streak"] = m.streak_after.fillna(0)
    out["days_since_last"] = (rows.event_date - m.prev_date).dt.days
    out["age"] = (rows.event_date - rows.dob).dt.days / 365.25
    out["height"], out["reach"] = rows.height_cm.astype(float), rows.reach_cm.astype(float)
    out["slpm"] = (m.sig_land / (dur / 60)).where(dur > 0)
    out["sapm"] = (m.sig_absorbed / (dur / 60)).where(dur > 0)
    out["sig_acc"] = (m.sig_land / m.sig_att).where(m.sig_att > 0)
    out["td_per15"] = (m.td_land / (dur / 900)).where(dur > 0)
    out["td_acc"] = (m.td_land / m.td_att).where(m.td_att > 0)
    out["td_def"] = (1 - m.opp_td_land / m.opp_td_att).where(m.opp_td_att > 0)
    out["sub_att_per15"] = (m.sub_att / (dur / 900)).where(dur > 0)
    out["kd_per15"] = (m.kd / (dur / 900)).where(dur > 0)
    out["ctrl_share"] = (m.ctrl_sec / dur).where(dur > 0)
    tot = m.dist_att + m.clinch_att + m.ground_att
    out["grappling_share"] = ((m.clinch_att + m.ground_att) / tot).where(tot > 0)
    out["ko_win_rate"] = (m.ko_win / n).where(n > 0)
    out["sub_win_rate"] = (m.sub_win / n).where(n > 0)
    return out


def run_elo(real_pairs: pd.DataFrame):
    """Elo before each fight (all fights on one night use that night's starting ratings). Also strength of schedule."""
    R, N, opp, rows = {}, {}, {}, []
    for _, g in real_pairs.sort_values(["event_date", "fight_url"]).groupby("event_date", sort=True):
        delta, cnt, newopp = {}, {}, {}
        for fu, a, b, aw in zip(g.fight_url, g.url_a, g.url_b, g.a_wins):
            ra, rb = R.get(a, 1500.0), R.get(b, 1500.0)
            sa_ = float(np.mean(opp[a])) if opp.get(a) else 1500.0
            sb_ = float(np.mean(opp[b])) if opp.get(b) else 1500.0
            rows.append((fu, ra, rb, sa_, sb_))
            ea = 1 / (1 + 10 ** ((rb - ra) / 400))
            ka = K_NEW if N.get(a, 0) < NEW_N else K_ELO
            kb = K_NEW if N.get(b, 0) < NEW_N else K_ELO
            delta[a] = delta.get(a, 0.0) + ka * (aw - ea)
            delta[b] = delta.get(b, 0.0) + kb * ((1 - aw) - (1 - ea))
            for k in (a, b):
                cnt[k] = cnt.get(k, 0) + 1
            newopp.setdefault(a, []).append(rb)
            newopp.setdefault(b, []).append(ra)
        for k, v in delta.items():
            R[k] = R.get(k, 1500.0) + v
        for k, v in cnt.items():
            N[k] = N.get(k, 0) + v
        for k, v in newopp.items():
            opp.setdefault(k, []).extend(v)
    return pd.DataFrame(rows, columns=["fight_url", "elo_a", "elo_b", "sos_a", "sos_b"]), R, opp


FEAT = ["n_prev", "prev_win_rate", "last3_win_rate", "streak", "days_since_last", "age", "height", "reach"] + SH[2:]


def make_pairs(history: pd.DataFrame, extra: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per fight with a_*/b_* features. `extra` = upcoming placeholder rows (features as of their event date)."""
    real = history
    allrows = real if extra is None or extra.empty else pd.concat([real, extra], ignore_index=True)
    st = add_state(real, allrows)
    keep = ["fight_url", "fighter_url", "fighter_name", "event_date", "event_name", "weight_class", "title_fight", "is_up",
            "won", "n_prev", "prev_win_rate", "last3_win_rate", "streak", "days_since_last", "age", "height", "reach",
            "stance"] + SH[2:]
    keep = list(dict.fromkeys(keep))
    a = st[st.side == 1][keep].set_index("fight_url")
    b = st[st.side == 2][keep].set_index("fight_url")
    P = a.join(b, lsuffix="_a", rsuffix="_b").rename(columns={"fighter_url_a": "url_a", "fighter_url_b": "url_b", "fighter_name_a": "name_a",
                                                              "fighter_name_b": "name_b", "event_date_a": "event_date", "event_name_a": "event_name",
                                                              "weight_class_a": "weight_class", "title_fight_a": "title_fight", "is_up_a": "is_up"})
    P = P.drop(columns=[c for c in P.columns if c in ("event_date_b", "event_name_b", "weight_class_b", "title_fight_b", "is_up_b", "won_b")])
    P["a_wins"] = P["won_a"]
    P = P.drop(columns=["won_a"]).reset_index()
    real_pairs = P[~P.is_up]
    elo, R, opp = run_elo(real_pairs)
    P = P.merge(elo, on="fight_url", how="left")
    up = P.is_up
    if up.any():                                   # upcoming bouts: the ratings as they stand now
        for side, col in (("a", "url_a"), ("b", "url_b")):
            P.loc[up, f"elo_{side}"] = P.loc[up, col].map(lambda u: R.get(u, 1500.0))
            P.loc[up, f"sos_{side}"] = P.loc[up, col].map(lambda u: float(np.mean(opp[u])) if opp.get(u) else 1500.0)
    P["min_prev"] = np.minimum(P.n_prev_a, P.n_prev_b)
    return P.sort_values(["event_date", "fight_url"]).reset_index(drop=True)


def priors(tr: pd.DataFrame) -> dict:
    mu = {k: pd.concat([tr[f"{k}_a"], tr[f"{k}_b"]]).mean() for k in SH}
    mu["age"] = pd.concat([tr.age_a, tr.age_b]).mean()
    return mu


def design(P: pd.DataFrame, mu: dict) -> pd.DataFrame:
    X = pd.DataFrame(index=P.index)
    for k in SH:
        sh = {}
        for s in "ab":
            n = P[f"n_prev_{s}"].fillna(0)
            n = n.clip(upper=3) if k == "last3_win_rate" else n
            sh[s] = (n * P[f"{k}_{s}"].fillna(mu[k]) + K_SHRINK * mu[k]) / (n + K_SHRINK)
        X[f"d_{k}"] = sh["a"] - sh["b"]
    aa, ab = P.age_a.fillna(mu["age"]), P.age_b.fillna(mu["age"])
    X["d_age"] = aa - ab
    X["d_logn"] = np.log1p(P.n_prev_a) - np.log1p(P.n_prev_b)
    X["d_streak"] = P.streak_a.fillna(0) - P.streak_b.fillna(0)
    X["d_reach"] = (P.reach_a - P.reach_b).fillna(0)
    X["d_height"] = (P.height_a - P.height_b).fillna(0)
    X["d_layoff"] = (P.days_since_last_a.fillna(0) >= 365).astype(int) - (P.days_since_last_b.fillna(0) >= 365).astype(int)
    X["d_elo"] = (P.elo_a - P.elo_b) / 100.0
    X["d_sos"] = (P.sos_a - P.sos_b) / 100.0
    X["d_age35"] = (aa >= 35).astype(int) - (ab >= 35).astype(int)
    X["d_age2"] = ((aa - 30) ** 2 - (ab - 30) ** 2) / 100.0
    X["d_elo_x_new"] = X["d_elo"] * (P.min_prev < 3).astype(int)
    return X


def orient(P: pd.DataFrame) -> np.ndarray:
    """Deterministic pseudo-random A/B swap, so training labels are balanced (every feature is antisymmetric)."""
    return (pd.util.hash_array(P.fight_url.values.astype(object)) % 2).astype(int)


def fit(Ptr: pd.DataFrame):
    mu = priors(Ptr)
    X = design(Ptr, mu)
    y = Ptr.a_wins.values.astype(int)
    flip = orient(Ptr) == 1
    X.loc[flip] = -X.loc[flip]
    y = np.where(flip, 1 - y, y)
    model = make_pipeline(StandardScaler(with_mean=False), LogisticRegression(C=1.0, fit_intercept=False, max_iter=3000)).fit(X, y)
    return model, mu


def early_rule(P: pd.DataFrame) -> pd.Series:
    """P(A wins) from the Elo rating; if tied, a small tilt toward the younger fighter, else exactly 0.5."""
    p = 1 / (1 + 10 ** (-(P.elo_a - P.elo_b) / 400))
    tilt = np.sign((P.age_b.fillna(30) - P.age_a.fillna(30)))
    return p.where((P.elo_a - P.elo_b).abs() > 1e-9, 0.5 + 0.001 * tilt)


def tier_of(min_prev) -> str:
    return "full" if min_prev >= 3 else ("limited" if min_prev >= 1 else "debut")


def picks_frame(P: pd.DataFrame, p_a: np.ndarray, method: str, trained: int) -> pd.DataFrame:
    out = pd.DataFrame({
        "fighter_a": P.name_a.values, "fighter_b": P.name_b.values,
        "a_prior_fights": P.n_prev_a.astype(int).values, "b_prior_fights": P.n_prev_b.astype(int).values,
        "tier": [tier_of(m) for m in P.min_prev], "method": method, "p_a_wins": np.asarray(p_a, dtype=float),
        "trained_on_fights": trained}, index=P.index)
    out["predicted_winner"] = np.where(out.p_a_wins >= 0.5, out.fighter_a, out.fighter_b)
    out["confidence"] = np.maximum(out.p_a_wins, 1 - out.p_a_wins)
    return out


# ----------------------------------------------------------------------------------------------
# "Why this pick?": what pushed the model, in plain words. Computed at the moment a pick is locked and stored with it.
# The model is a sum: every input adds or subtracts a little log-odds for fighter A, and the sum IS the pick. So the
# reasons are exact, not an approximation, and they are checked to reproduce the stored probability before being saved.
# ----------------------------------------------------------------------------------------------
GROUPS = {
    "age": ["d_age", "d_age35", "d_age2"],
    "form": ["d_streak", "d_last3_win_rate", "d_layoff"],
    "record": ["d_prev_win_rate", "d_logn"],
    "rating": ["d_elo", "d_elo_x_new"],
    "schedule": ["d_sos"],
    "striking": ["d_slpm", "d_sapm", "d_sig_acc", "d_kd_per15"],
    "wrestling": ["d_td_per15", "d_td_acc", "d_td_def", "d_ctrl_share", "d_grappling_share"],
    "submissions": ["d_sub_att_per15", "d_sub_win_rate"],
    "finishing": ["d_ko_win_rate"],
    "size": ["d_reach", "d_height"],
}
LABELS = {"age": "Age", "form": "Recent form", "record": "UFC record", "rating": "Rating (Elo)", "schedule": "Strength of opposition", "striking": "Striking",
          "wrestling": "Wrestling and control", "submissions": "Submissions", "finishing": "Finishing power", "size": "Size"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def last_name(name: str) -> str:
    parts = str(name).split()
    if len(parts) > 2 and parts[-1].lower() in SUFFIXES:
        return " ".join(parts[-2:])
    return parts[-1] if parts else str(name)


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NA


def _num(x, nd=1) -> str:
    return "–" if _nan(x) else f"{float(x):.{nd}f}"


def _pct(x) -> str:
    return "–" if _nan(x) else f"{float(x):.0%}"


def _streak(x) -> str:
    return "–" if _nan(x) or int(x) == 0 else (f"W{int(x)}" if int(x) > 0 else f"L{-int(x)}")


def _inch(cm) -> str:
    return "–" if _nan(cm) else f"{round(float(cm) / 2.54)}\""


def _detail(g: str, r, la: str, lb: str) -> str:
    a, b = (lambda k: r[f"{k}_a"]), (lambda k: r[f"{k}_b"])
    if g == "age":
        return f"{la} {_num(a('age'))}, {lb} {_num(b('age'))}"
    if g == "form":
        out = f"streak {la} {_streak(a('streak'))} / {lb} {_streak(b('streak'))}; last 3 fights {_pct(a('last3_win_rate'))} / {_pct(b('last3_win_rate'))}"
        for who, d in ((la, a("days_since_last")), (lb, b("days_since_last"))):
            if not _nan(d) and float(d) >= 365:
                out += f"; {who} was off {int(float(d) // 30)} months"
        return out
    if g == "record":
        side = lambda who, k: f"{who}: no UFC fights yet" if int(r[f"n_prev_{k}"]) == 0 else f"{who}: {int(r[f'n_prev_{k}'])} UFC fights, {_pct(r[f'prev_win_rate_{k}'])} wins"
        return f"{side(la, 'a')}; {side(lb, 'b')}"
    if g == "rating":
        return f"Elo rating {la} {_num(r['elo_a'], 0)}, {lb} {_num(r['elo_b'], 0)} (it rises for beating strong opponents)"
    if g == "schedule":
        return f"average rating of past opponents: {la} {_num(r['sos_a'], 0)}, {lb} {_num(r['sos_b'], 0)}"
    if g == "striking":
        return f"{la} lands {_num(a('slpm'), 2)}/min, absorbs {_num(a('sapm'), 2)}; {lb} lands {_num(b('slpm'), 2)}/min, absorbs {_num(b('sapm'), 2)}"
    if g == "wrestling":
        return (f"takedowns per 15 min: {la} {_num(a('td_per15'), 2)}, {lb} {_num(b('td_per15'), 2)}; "
                f"takedown defense: {la} {_pct(a('td_def'))}, {lb} {_pct(b('td_def'))}")
    if g == "submissions":
        return f"submission attempts per 15 min: {la} {_num(a('sub_att_per15'), 2)}, {lb} {_num(b('sub_att_per15'), 2)}"
    if g == "finishing":
        return f"KO/TKO wins as a share of fights: {la} {_pct(a('ko_win_rate'))}, {lb} {_pct(b('ko_win_rate'))}"
    if g == "size":
        return f"reach {la} {_inch(a('reach'))}, {lb} {_inch(b('reach'))}; height {la} {_inch(a('height'))}, {lb} {_inch(b('height'))}"
    return ""


def contributions(model, mu: dict, P: pd.DataFrame) -> pd.DataFrame:
    """Log-odds each input adds for fighter A (negative = for fighter B). They sum exactly to the model's log-odds."""
    X = design(P, mu)
    Z = model[0].transform(X)
    return pd.DataFrame(Z * model[1].coef_[0], index=P.index, columns=X.columns)


def build_reasons(r, c: pd.Series, name_a: str, name_b: str) -> dict:
    la, lb = last_name(name_a), last_name(name_b)
    mapped = {f for feats in GROUPS.values() for f in feats}
    factors = []
    for g, feats in GROUPS.items():
        val = float(sum(c.get(f, 0.0) for f in feats))
        factors.append({"key": g, "label": LABELS[g], "c": round(val, 4), "toward": "A" if val > 0 else "B", "detail": _detail(g, r, la, lb)})
    factors.sort(key=lambda f: -abs(f["c"]))
    top = [f for f in factors if abs(f["c"]) >= 0.05][:5]
    other = float(sum(v for k, v in c.items() if k not in mapped))
    rest = float(sum(f["c"] for f in factors if f not in top)) + other
    total = float(c.sum())
    return {"a": name_a, "b": name_b, "total": round(total, 4), "p_a": round(float(1 / (1 + np.exp(-total))), 4),
            "factors": top, "rest": round(rest, 4)}


REASONS_DDL = """CREATE TABLE IF NOT EXISTS pick_reasons (
    bout_id    TEXT NOT NULL,
    version    TEXT NOT NULL,                 -- 'v2' (model-only) or 'v3' (market-informed)
    reasons    JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (bout_id, version))"""


def save_reasons(engine, version: str, items) -> int:
    with engine.begin() as conn:
        conn.execute(sa.text(REASONS_DDL))
        for bout_id, d in items:
            conn.execute(sa.text("INSERT INTO pick_reasons (bout_id, version, reasons) VALUES (:b, :v, CAST(:j AS jsonb)) "
                                 "ON CONFLICT (bout_id, version) DO NOTHING"), {"b": bout_id, "v": version, "j": json.dumps(d)})
    return len(items)


# ----------------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    with make_engine().begin() as conn:
        conn.exec_driver_sql((ROOT / "sql" / "model_v2.sql").read_text())
    log.info("Applied model_v2.sql")
    return 0


def cmd_backtest(args) -> int:
    engine = make_engine()
    if args.rebuild:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM fight_picks_v2"))
    with engine.connect() as conn:
        done = {r[0] for r in conn.execute(sa.text("SELECT fight_url FROM fight_picks_v2"))}
    history = load_history(engine)
    P = make_pairs(history)
    todo_dates = sorted(P[~P.fight_url.isin(done)].event_date.unique())
    log.info("%d fights in the database, %d without a v2 pick yet (%d event nights)", len(P), int((~P.fight_url.isin(done)).sum()), len(todo_dates))
    out = []
    for D in todo_dates:
        te = P[(P.event_date == D) & ~P.fight_url.isin(done)]
        tr = P[P.event_date < D]
        if len(tr) >= MIN_TRAIN:
            model, mu = fit(tr)
            X = design(te, mu)
            p = model.predict_proba(X)[:, 1]
            out.append(picks_frame(te, p, "model", len(tr)).join(te[["fight_url", "event_date", "event_name", "weight_class", "title_fight", "a_wins"]]))
        else:
            out.append(picks_frame(te, early_rule(te).values, "early_rule", len(tr)).join(te[["fight_url", "event_date", "event_name", "weight_class", "title_fight", "a_wins"]]))
    if not out:
        log.info("Nothing to score.")
        return 0
    R = pd.concat(out)
    R["actual_winner"] = np.where(R.a_wins == 1, R.fighter_a, R.fighter_b)
    R["correct"] = (R.predicted_winner == R.actual_winner).astype(int)
    R["event_date"] = R.event_date.dt.date
    cols = ["fight_url", "event_date", "event_name", "weight_class", "title_fight", "fighter_a", "fighter_b", "a_prior_fights", "b_prior_fights",
            "tier", "method", "p_a_wins", "predicted_winner", "confidence", "actual_winner", "correct", "trained_on_fights"]
    with engine.begin() as conn:
        R[cols].to_sql("fight_picks_v2", conn, if_exists="append", index=False, method="multi", chunksize=500)
    m = R[R.method == "model"]
    log.info("Wrote %d picks (%d from the model, %d from the early-era rule). Model accuracy on those: %.1f%%",
             len(R), len(m), len(R) - len(m), 100 * m.correct.mean() if len(m) else float("nan"))
    for tier, g in m.groupby("tier"):
        log.info("   %-8s %5d fights  %.1f%% right", tier, len(g), 100 * g.correct.mean())
    return 0


def cmd_predict(args) -> int:
    engine = make_engine()
    today = date.fromisoformat(args.today) if args.today else date.today()
    with engine.begin() as conn:     # keep cancelled / reinstated bouts in step with the card
        conn.execute(sa.text("""UPDATE upcoming_picks_v2 p SET status = CASE WHEN u.status = 'cancelled' THEN 'cancelled' ELSE 'locked' END
                                FROM upcoming_fights u WHERE u.bout_id = p.bout_id AND p.graded_at IS NULL
                                AND p.status IN ('locked', 'cancelled')"""))
        have = {r[0] for r in conn.execute(sa.text("SELECT bout_id FROM upcoming_picks_v2"))}
        todo = pd.read_sql(sa.text("""SELECT bout_id, bout_order FROM upcoming_fights WHERE status = 'scheduled' AND event_date > :t"""),
                           conn, params={"t": today})
    todo = todo[~todo.bout_id.isin(have)]
    if todo.empty:
        log.info("No new bouts to pick (already picked, event not in the future, or no card loaded).")
        return 0
    history = load_history(engine)
    extra = placeholders(engine, history, only_ids=set(todo.bout_id))
    P = make_pairs(history, extra)
    up = P[P.is_up].copy()
    real = P[~P.is_up]
    model, mu = fit(real)
    up["p"] = model.predict_proba(design(up, mu))[:, 1]
    meta = pd.read_sql(sa.text("SELECT bout_id, bout_order FROM upcoming_fights"), engine).set_index("bout_id")
    up["bout_id"] = up.fight_url.str.replace("upcoming:", "", regex=False)
    pf = picks_frame(up, up.p.values, "model", len(real)).join(up[["bout_id", "event_name", "event_date", "weight_class", "title_fight"]])
    pf["bout_order"] = pf.bout_id.map(meta.bout_order)
    pf["event_date"] = pf.event_date.dt.date
    pf = pf.sort_values(["event_date", "bout_order"])
    cols = ["bout_id", "event_name", "event_date", "bout_order", "weight_class", "title_fight", "fighter_a", "fighter_b", "a_prior_fights",
            "b_prior_fights", "tier", "method", "p_a_wins", "predicted_winner", "confidence", "trained_on_fights"]
    with engine.begin() as conn:
        pf[cols].to_sql("upcoming_picks_v2", conn, if_exists="append", index=False, method="multi")
    log.info("Locked %d picks (model trained on %d fights)", len(pf), len(real))
    for r in pf.itertuples():
        print(f"  {r.event_date}  {r.fighter_a:24s} vs {r.fighter_b:24s} -> {r.predicted_winner} {r.confidence:.0%}  [{r.tier}]")
    try:                                   # the reasons are a nicety: they must never be able to block a pick
        C = contributions(model, mu, up)
        save_reasons(engine, "v2", [(row.bout_id, build_reasons(row, C.loc[i], row.name_a, row.name_b)) for i, row in up.iterrows()])
    except Exception as exc:
        log.warning("could not store the reasons for the new picks (%s); run: python pipeline/model_v2.py explain", exc)
    return 0


def cmd_explain(args) -> int:
    """Store 'Why this pick?' reasons for locked picks that have none. Each pick is re-created exactly as it was when it was
    locked (only fights dated before the lock day, the same training set), and the reasons are saved only if they reproduce
    the stored probability."""
    engine = make_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(REASONS_DDL))
    todo = pd.read_sql(sa.text("""SELECT p.bout_id, p.p_a_wins, p.predicted_at FROM upcoming_picks_v2 p
                                  LEFT JOIN pick_reasons r ON r.bout_id = p.bout_id AND r.version = 'v2'
                                  WHERE r.bout_id IS NULL AND p.status = 'locked'"""), engine)
    if todo.empty:
        log.info("Every locked pick already has its reasons.")
        return 0
    history = load_history(engine)
    done = skipped = 0
    todo["lock_day"] = pd.to_datetime(todo.predicted_at).dt.tz_localize(None).dt.normalize()
    for day, grp in todo.groupby("lock_day"):
        asof = history[history.event_date < day]
        extra = placeholders(engine, asof, only_ids=set(grp.bout_id))
        if extra.empty:
            skipped += len(grp)
            continue
        P = make_pairs(asof, extra)
        up, real = P[P.is_up].copy(), P[~P.is_up]
        model, mu = fit(real)
        p_new = pd.Series(model.predict_proba(design(up, mu))[:, 1], index=up.index)
        C = contributions(model, mu, up)
        stored = dict(zip(grp.bout_id, grp.p_a_wins))
        items = []
        for i, row in up.iterrows():
            bid = row.fight_url.replace("upcoming:", "", 1)
            if bid not in stored or abs(float(p_new[i]) - float(stored[bid])) > 0.002:
                skipped += 1
                log.warning("not explained (the pick cannot be reproduced exactly): %s vs %s", row.name_a, row.name_b)
                continue
            items.append((bid, build_reasons(row, C.loc[i], row.name_a, row.name_b)))
        done += save_reasons(engine, "v2", items) if items else 0
    log.info("Stored reasons for %d picks; %d could not be reproduced and were left without reasons", done, skipped)
    return 0


def cmd_grade(args) -> int:
    engine = make_engine()
    with engine.begin() as conn:
        pend = pd.read_sql(sa.text("""SELECT p.bout_id, p.fighter_a, p.fighter_b, p.predicted_winner, p.event_date,
                                      u.fighter_a_url, u.fighter_b_url
                                      FROM upcoming_picks_v2 p JOIN upcoming_fights u USING (bout_id)
                                      WHERE p.graded_at IS NULL AND p.status = 'locked'"""), conn)
        if pend.empty:
            log.info("Nothing waiting to be graded.")
            return 0
        fights = pd.read_sql(sa.text("""SELECT f.fight_url, f.event_date, ff.fighter_url, ff.fighter_name, ff.won
                                        FROM fights f JOIN fighter_fights ff USING (fight_url)"""), conn)
        fights["event_date"] = pd.to_datetime(fights.event_date)
        graded = noresult = pending = right = 0
        for r in pend.itertuples():
            d = pd.Timestamp(r.event_date)
            win = fights[(fights.event_date >= d - pd.Timedelta(days=1)) & (fights.event_date <= d + pd.Timedelta(days=1))]
            winner_is_a = None
            known_a, known_b = not str(r.fighter_a_url).startswith("new:"), not str(r.fighter_b_url).startswith("new:")
            for url, is_a in ((r.fighter_a_url, True), (r.fighter_b_url, False)):
                if winner_is_a is not None or str(url).startswith("new:"):
                    continue
                hit = win[win.fighter_url == url]
                if not hit.empty:       # the known fighter fought on that night; the other fighter in that fight is the opponent
                    winner_is_a = bool(hit.iloc[0].won == 1) if is_a else bool(hit.iloc[0].won == 0)
            if winner_is_a is None and not known_a and not known_b:      # two debuts: match on names
                na, nb = norm(r.fighter_a), norm(r.fighter_b)
                for fu, g in win.groupby("fight_url"):
                    names = {norm(x): w for x, w in zip(g.fighter_name, g.won)}
                    ma = difflib.get_close_matches(na, list(names), n=1, cutoff=0.85)
                    mb = difflib.get_close_matches(nb, list(names), n=1, cutoff=0.85)
                    if ma and mb and ma[0] != mb[0]:
                        winner_is_a = bool(names[ma[0]] == 1)
                        break
            if winner_is_a is None:
                if len(win):
                    conn.execute(sa.text("UPDATE upcoming_picks_v2 SET status = 'no_result', graded_at = now() WHERE bout_id = :b"), {"b": r.bout_id})
                    noresult += 1
                else:
                    pending += 1
                continue
            actual = r.fighter_a if winner_is_a else r.fighter_b
            ok = int(actual == r.predicted_winner)
            conn.execute(sa.text("UPDATE upcoming_picks_v2 SET actual_winner = :w, correct = :c, graded_at = now() WHERE bout_id = :b"),
                         {"w": actual, "c": ok, "b": r.bout_id})
            graded += 1
            right += ok
    log.info("Graded %d (%d right); %d had no result (cancelled/draw/no contest); %d still waiting for the dataset", graded, right, noresult, pending)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Model v2: a pick for every fight")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    b = sub.add_parser("backtest")
    b.add_argument("--rebuild", action="store_true")
    p = sub.add_parser("predict")
    p.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    sub.add_parser("grade")
    sub.add_parser("explain")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return {"setup": cmd_setup, "backtest": cmd_backtest, "predict": cmd_predict, "grade": cmd_grade, "explain": cmd_explain}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
