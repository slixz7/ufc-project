"""
What the data says: plain findings from the fights table, each with its sample size and a 95% range. Pure functions (numpy and pandas only): no database,
no Streamlit, so every number can be checked against a hand-worked answer. The website's Insights page (app/insights_ui.py) draws them.

The definitions are the goes-the-distance model's own, so this page can never disagree with the Track record: fights since 2002 with 3 or 5 scheduled
rounds, disqualifications left out, and a fight "went the distance" when its result starts with "Decision". Nothing here is hard-coded prose about
the results: every sentence is built from the numbers, and says nothing when there are too few fights to say it.

These are patterns in past fights, not causes. A division, an age gap or a reach advantage also travels with other things (who gets booked against whom,
who is the favourite), so none of it says what would happen if you changed one thing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from small_sample import wilson

ERA_START = pd.Timestamp("2002-01-01")
EXPERIENCE_START = pd.Timestamp("2005-01-01")      # before this, a fighter's "earlier UFC fights" are cut short by the start of the data
MIN_FIGHTS_SENTENCE = 30                           # a group smaller than this is shown, but never quoted in a sentence
INCH_CM = 2.54                                     # reach is stored in whole inches converted to centimetres (every value in the real column is a multiple of 2.54), so gaps come in inches
REACH_EDGES = [0.5, 1.5, 3.5, 5.5, np.inf]         # gaps in inches; a gap under half an inch is not a gap (it is left out like a tie)
REACH_LABELS = ["1 inch longer", "2 to 3 inches longer", "4 to 5 inches longer", "6 or more inches longer"]
FINISH_RESULTS = ("KO/TKO", "Submission", "TKO - Doctor's Stoppage")


def pct(x: float) -> str:
    return f"{round(100 * x)}%"


def rate(k: int, n: int) -> dict:
    """k out of n, as a share, with its 95% range."""
    n, k = int(n), int(k)
    if n == 0:
        return {"n": 0, "k": 0, "rate": float("nan"), "lo": float("nan"), "hi": float("nan")}
    lo, hi = wilson(k, n)
    return {"n": n, "k": k, "rate": k / n, "lo": lo, "hi": hi}


def vs_even(r: dict) -> str:
    """How a win rate compares with a coin flip, in words that follow the range, not just the point estimate."""
    if r["lo"] <= 0.5 <= r["hi"]:
        return "about even: the range includes 50%"
    return "more often than not" if r["rate"] > 0.5 else "less often than not"


# ---------------------------------------------------------------------------------------------- the fights
def prepare_fights(fights: pd.DataFrame) -> pd.DataFrame:
    """Columns needed: fight_url, event_date, weight_class, num_rounds, result, finish_round. Keeps the fights the distance model keeps, and adds went_distance,
    method (Decision, KO/TKO, Submission, Other) and year."""
    f = fights.copy()
    f["event_date"] = pd.to_datetime(f["event_date"])
    res = f["result"].fillna("")
    f = f[(f.event_date >= ERA_START) & f.num_rounds.isin([3, 5]) & (res != "") & (res != "DQ")].copy()
    res = f["result"]
    f["went_distance"] = res.str.startswith("Decision").astype(int)
    f["method"] = np.select([res.str.startswith("Decision"), res.isin(["KO/TKO", "TKO - Doctor's Stoppage"]), res == "Submission"], ["Decision", "KO/TKO", "Submission"], default="Other")
    f["year"] = f.event_date.dt.year
    return f


def method_shares(f: pd.DataFrame) -> dict:
    """How fights end: the share of all eligible fights that ended each way."""
    n = len(f)
    rows = [{"method": m, **rate((f.method == m).sum(), n)} for m in ("Decision", "KO/TKO", "Submission", "Other")]
    return {"n": n, "rows": rows}


def finish_round_shares(f: pd.DataFrame) -> list[dict]:
    """For fights that ended early (a knockout, TKO or submission with a known round), the share ending in each round, for 3-round and 5-round fights."""
    out = []
    for sched in (3, 5):
        e = f[(f.num_rounds == sched) & f.method.isin(["KO/TKO", "Submission"]) & f.finish_round.notna()]
        n = len(e)
        out.append({"scheduled": sched, "n": n, "rounds": [{"round": r, **rate((e.finish_round == r).sum(), n)} for r in range(1, sched + 1)]})
    return out


def division_rates(f: pd.DataFrame, min_fights: int = 150) -> list[dict]:
    """Share of fights that went the distance, by weight class, for divisions with at least `min_fights` fights, highest first."""
    rows = []
    for name, g in f.groupby("weight_class"):
        if len(g) >= min_fights and isinstance(name, str) and name.strip():
            rows.append({"division": name, **rate(g.went_distance.sum(), len(g))})
    return sorted(rows, key=lambda r: (-r["rate"], r["division"]))


def yearly_rates(f: pd.DataFrame, min_fights: int = 100) -> list[dict]:
    """Share of fights that went the distance, by year, for years with at least `min_fights` fights, oldest first."""
    return [{"year": int(y), **rate(g.went_distance.sum(), len(g))} for y, g in f.groupby("year") if len(g) >= min_fights]


def _compare(rows: list[dict], span: int) -> dict:
    """The first `span` years against the last `span` years, pooled. 'rose' or 'fell' only if the difference is bigger than chance alone would give."""
    early, late = rows[:span], rows[-span:]
    k1, n1, k2, n2 = sum(r["k"] for r in early), sum(r["n"] for r in early), sum(r["k"] for r in late), sum(r["n"] for r in late)
    p1, p2 = k1 / n1, k2 / n2
    se = np.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    diff = p2 - p1
    verdict = "rose" if diff > 1.96 * se else "fell" if diff < -1.96 * se else "no clear change"
    return {"early_years": (early[0]["year"], early[-1]["year"]), "late_years": (late[0]["year"], late[-1]["year"]), "early": p1, "late": p2, "early_n": n1, "late_n": n2, "verdict": verdict}


def trend_summary(rows: list[dict], span: int = 3, recent_years: int = 10) -> dict | None:
    """The whole span (first `span` years against last `span`), and, when there are more than `recent_years` years, the same for the last `recent_years` alone with the
    lowest and highest year in it: a change that happened long ago says nothing about now."""
    if len(rows) < 2 * span:
        return None
    out = _compare(rows, span)
    if len(rows) > recent_years and recent_years >= 2 * span:
        recent = rows[-recent_years:]
        lo, hi = min(recent, key=lambda r: r["rate"]), max(recent, key=lambda r: r["rate"])
        out["recent"] = {**_compare(recent, span), "n_years": len(recent), "lowest": lo["rate"], "highest": hi["rate"]}
    return out


# ---------------------------------------------------------------------------------------------- favourites and upsets
FAVOURITE_EDGES = (0.5, 0.6, 0.7, 0.8, 1.0001)
FAVOURITE_LABELS = ("Under 60%", "60% to 70%", "70% to 80%", "80% or more")


def favourite_buckets(f: pd.DataFrame, picks: pd.DataFrame) -> list[dict]:
    """Fights grouped by how strong a favourite the betting market made one fighter (de-vigged, so the two sides add to 100%). `picks` columns: fight_url,
    fighter_a, fighter_b, market_prob_a, actual_winner. For each group: how often the favourite won against what the market implied, and how often the fight
    went the distance. Fights with no winner (a draw, a no contest) are left out."""
    p = picks.dropna(subset=["market_prob_a", "actual_winner"]).merge(f[["fight_url", "went_distance"]], on="fight_url", how="inner")
    p = p[(p.actual_winner == p.fighter_a) | (p.actual_winner == p.fighter_b)].copy()
    m = p.market_prob_a.astype(float)
    p = p[(m - 0.5).abs() > 1e-9].copy()                                                          # an exactly even fight has no favourite
    m = p.market_prob_a.astype(float)
    p["strength"] = np.maximum(m, 1 - m)
    p["fav_won"] = ((p.actual_winner == p.fighter_a) == (m >= 0.5)).astype(int)
    p["bucket"] = pd.cut(p.strength, bins=list(FAVOURITE_EDGES), right=False, labels=list(FAVOURITE_LABELS))
    rows = []
    for label in FAVOURITE_LABELS:
        g = p[p.bucket == label]
        if len(g):
            rows.append({"label": label, "n": len(g), "implied": float(g.strength.mean()), "fav_won": rate(g.fav_won.sum(), len(g)), "decision": rate(g.went_distance.sum(), len(g))})
    return rows


# ---------------------------------------------------------------------------------------------- experience, age and reach
def build_pairs(rows: pd.DataFrame, fights: pd.DataFrame) -> pd.DataFrame:
    """One row per fight with both fighters side by side. `rows` columns: fight_url, fighter_url, side, won, reach_cm, dob. `fights` columns: fight_url, event_date.
    prior = how many UFC fights the fighter had in this data before the fight. Fights without exactly one winner are left out."""
    r = rows.merge(fights[["fight_url", "event_date"]], on="fight_url", how="inner")
    r["event_date"] = pd.to_datetime(r["event_date"])
    r = r.sort_values(["fighter_url", "event_date", "fight_url"])
    r["prior"] = r.groupby("fighter_url").cumcount()
    r["age"] = (r.event_date - pd.to_datetime(r["dob"], errors="coerce")).dt.days / 365.25
    cols = ["fight_url", "event_date", "won", "prior", "age", "reach_cm"]
    a, b = r[r.side == 1][cols], r[r.side == 2][cols]
    pairs = a.merge(b, on=["fight_url", "event_date"], suffixes=("_a", "_b"))
    pairs = pairs[pairs.won_a + pairs.won_b == 1].rename(columns={"won_a": "a_won"})
    return pairs.drop(columns=["won_b"]).reset_index(drop=True)


def _bucketed(gap: pd.Series, edges: list, labels: list[str], won: pd.Series) -> list[dict]:
    b = pd.cut(gap, bins=edges, right=False, labels=labels)
    return [{"label": lab, **rate(won[b == lab].sum(), (b == lab).sum())} for lab in labels if (b == lab).sum()]


def experience_rows(pairs: pd.DataFrame) -> dict:
    """Does the fighter with more UFC fights behind them win? By how many more, since 2005 (earlier fighters' histories are cut short by the start of the data).
    For each group it also measures how often that more experienced fighter was ALSO the older one, because the two travel together and would otherwise be
    mistaken for one effect. Also: fighters making their UFC debut against someone with 3 or more UFC fights."""
    p = pairs[pairs.event_date >= EXPERIENCE_START]
    gap = p.prior_a - p.prior_b
    keep = gap != 0
    more_is_a = gap[keep] > 0
    more_won = pd.Series(np.where(gap > 0, p.a_won, 1 - p.a_won), index=p.index)[keep]
    known = (p.age_a.notna() & p.age_b.notna())[keep]
    a_older = (p.age_a > p.age_b)[keep]
    also_older = pd.Series(np.where(more_is_a, a_older, ~a_older), index=a_older.index)
    labels = ["1 to 2 more", "3 to 5 more", "6 to 10 more", "11 or more"]
    bucket = pd.cut(gap[keep].abs(), bins=[1, 3, 6, 11, np.inf], right=False, labels=labels)
    rows = []
    for lab in labels:
        sel = bucket == lab
        if sel.sum():
            rows.append({"label": lab, **rate(more_won[sel].sum(), sel.sum()), "also_older": rate(also_older[sel & known].sum(), (sel & known).sum())})
    debut = p[((p.prior_a == 0) & (p.prior_b >= 3)) | ((p.prior_b == 0) & (p.prior_a >= 3))]
    debut_won = np.where(debut.prior_a == 0, debut.a_won, 1 - debut.a_won)
    return {"gap": rows, "debut": rate(debut_won.sum(), len(debut))}


def edge_rows(pairs: pd.DataFrame) -> dict:
    """Does the older fighter win, and the one with the longer reach? Grouped by the size of the gap (reach in inches). Exact ties are left out, and so is a reach gap under half an inch."""
    out = {}
    age = pairs.dropna(subset=["age_a", "age_b"])
    gap = (age.age_a - age.age_b)
    older_won = pd.Series(np.where(gap > 0, age.a_won, 1 - age.a_won), index=age.index)
    keep = gap != 0
    out["age"] = _bucketed(gap[keep].abs(), [0, 2, 5, 8, np.inf], ["Under 2 years older", "2 to 5 years older", "5 to 8 years older", "8 or more years older"], older_won[keep])
    rc = pairs.dropna(subset=["reach_cm_a", "reach_cm_b"])
    rgap = (rc.reach_cm_a - rc.reach_cm_b) / INCH_CM                                                          # in inches
    longer_won = pd.Series(np.where(rgap > 0, rc.a_won, 1 - rc.a_won), index=rc.index)
    rkeep = rgap.abs() >= REACH_EDGES[0]
    out["reach"] = _bucketed(rgap[rkeep].abs(), REACH_EDGES, REACH_LABELS, longer_won[rkeep])
    return out


# ---------------------------------------------------------------------------------------------- sentences: built from the numbers, silent when there is too little to say
def sentence_how_fights_end(s: dict) -> str | None:
    if s["n"] < MIN_FIGHTS_SENTENCE:
        return None
    d = {r["method"]: r for r in s["rows"]}
    return (f"Of {s['n']:,} fights since 2002, {pct(d['Decision']['rate'])} went to a decision, {pct(d['KO/TKO']['rate'])} ended in a knockout or TKO "
            f"and {pct(d['Submission']['rate'])} in a submission.")


def sentence_finish_rounds(rounds: list[dict]) -> str | None:
    three = next((r for r in rounds if r["scheduled"] == 3), None)
    if not three or three["n"] < MIN_FIGHTS_SENTENCE:
        return None
    r1, r2 = three["rounds"][0], three["rounds"][1]
    return f"In three-round fights that ended early ({three['n']:,}), {pct(r1['rate'])} ended in round 1 and {pct(r2['rate'])} in round 2."


def sentence_divisions(rows: list[dict]) -> str | None:
    if len(rows) < 2:
        return None
    top, bottom = rows[0], rows[-1]
    return (f"{top['division']} fights went the distance most often ({pct(top['rate'])} of {top['n']:,}) and {bottom['division']} least often "
            f"({pct(bottom['rate'])} of {bottom['n']:,}): a spread of {round(100 * (top['rate'] - bottom['rate']))} points between divisions.")


def _span_text(t: dict) -> str:
    (e0, e1), (l0, l1) = t["early_years"], t["late_years"]
    return f"{pct(t['early'])} of fights went the distance in {e0} to {e1} ({t['early_n']:,} fights) and {pct(t['late'])} in {l0} to {l1} ({t['late_n']:,})"


def sentence_trend(t: dict | None) -> str | None:
    if not t:
        return None
    base = _span_text(t)
    out = (f"{base}: a difference small enough that chance alone could produce it." if t["verdict"] == "no clear change"
           else f"{base}: the share {t['verdict']}, by more than chance alone would give.")
    rec = t.get("recent")
    if rec:
        (a0, a1), (b0, b1) = rec["early_years"], rec["late_years"]
        tail = f" Over the last {rec['n_years']} years the yearly share stayed between {pct(rec['lowest'])} and {pct(rec['highest'])}, "
        tail += (f"with no clear change between {a0} to {a1} and {b0} to {b1}." if rec["verdict"] == "no clear change"
                 else f"and it {rec['verdict']} from {pct(rec['early'])} in {a0} to {a1} to {pct(rec['late'])} in {b0} to {b1}.")
        out += tail
    return out


def sentence_favourites(rows: list[dict]) -> str | None:
    usable = [r for r in rows if r["n"] >= MIN_FIGHTS_SENTENCE]
    if len(usable) < 2:
        return None
    top, bottom = usable[-1], usable[0]
    return (f"When the market had a favourite at {top['label']} ({top['n']:,} fights), that favourite won {pct(top['fav_won']['rate'])} "
            f"(the market implied {pct(top['implied'])}) and {pct(top['decision']['rate'])} of those fights went the distance. "
            f"Where the favourite was {bottom['label'].lower()} ({bottom['n']:,} fights), {pct(bottom['decision']['rate'])} did.")


def sentence_upsets(rows: list[dict]) -> str | None:
    usable = [r for r in rows if r["n"] >= MIN_FIGHTS_SENTENCE]
    if not usable:
        return None
    top = usable[-1]
    upset = 1 - top["fav_won"]["rate"]
    if upset <= 0:
        return f"In the {top['label'].lower()} group ({top['n']:,} fights) there was no upset."
    base = f"Even in the {top['label'].lower()} group, the underdog won {pct(upset)} of the time, about one fight in {round(1 / upset)}."
    if top["fav_won"]["lo"] <= top["implied"] <= top["fav_won"]["hi"]:
        return f"{base} The market's price (the favourite at {pct(top['implied'])}) is consistent with what happened."
    return (f"{base} The market implied {pct(top['implied'])} for the favourite; the real figure was {pct(top['fav_won']['rate'])}. "
            f"The note below explains why a gap like this does not mean the betting line was wrong.")


def sentence_experience(e: dict) -> str | None:
    d = e["debut"]
    parts = []
    if d["n"] >= MIN_FIGHTS_SENTENCE:
        parts.append(f"Fighters making their UFC debut against someone with 3 or more UFC fights won {pct(d['rate'])} of {d['n']:,} ({vs_even(d)}).")
    big = next((r for r in reversed(e["gap"]) if r["n"] >= MIN_FIGHTS_SENTENCE), None)
    if big:
        parts.append(f"The fighter with an edge of {big['label']} UFC fights won {pct(big['rate'])} of {big['n']:,} ({vs_even(big)}).")
        older = big.get("also_older")
        if older and older["n"] >= MIN_FIGHTS_SENTENCE:
            parts.append(f"In {pct(older['rate'])} of those fights the more experienced fighter was also the older one" + (", so this overlaps with the age pattern below." if older["rate"] >= 0.6 else "."))
    return " ".join(parts) or None


def sentence_edges(e: dict) -> str | None:
    parts = []
    for key, who in (("age", "the older fighter"), ("reach", "the fighter with the longer reach")):
        row = next((r for r in reversed(e[key]) if r["n"] >= MIN_FIGHTS_SENTENCE), None)
        if row:
            parts.append(f"{who[0].upper() + who[1:]} won {pct(row['rate'])} of {row['n']:,} fights where the gap was {row['label'].lower()} ({vs_even(row)}).")
    return " ".join(parts) or None


# ---------------------------------------------------------------------------------------------- everything at once
def compute_all(fights: pd.DataFrame, picks: pd.DataFrame, rows: pd.DataFrame) -> dict:
    """Every finding and its sentence, from the three tables the page reads. `fights`: ALL fights (fight_url, event_date, weight_class, num_rounds, result,
    finish_round; pre-2002 fights are needed to count a fighter's earlier fights). `picks`: fight_url, fighter_a, fighter_b, market_prob_a, actual_winner.
    `rows`: fight_url, fighter_url, side, won, reach_cm, dob."""
    f = prepare_fights(fights)
    pairs = build_pairs(rows, fights)
    out = {"n_fights": len(f), "n_with_market": 0, "method": method_shares(f), "rounds": finish_round_shares(f), "divisions": division_rates(f, 100), "years": yearly_rates(f, 100)}
    out["trend"] = trend_summary(out["years"])
    out["favourites"] = favourite_buckets(f, picks) if len(picks) else []
    out["n_with_market"] = int(sum(r["n"] for r in out["favourites"]))
    out["experience"], out["edges"] = experience_rows(pairs), edge_rows(pairs)
    out["sentences"] = {"method": sentence_how_fights_end(out["method"]), "rounds": sentence_finish_rounds(out["rounds"]), "divisions": sentence_divisions(out["divisions"]),
                        "trend": sentence_trend(out["trend"]), "favourites": sentence_favourites(out["favourites"]), "upsets": sentence_upsets(out["favourites"]),
                        "experience": sentence_experience(out["experience"]), "edges": sentence_edges(out["edges"])}
    return out
