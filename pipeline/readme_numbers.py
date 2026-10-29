"""
README numbers straight from the database, so they cannot drift.

Inside README.md, two kinds of marker hold generated content:

  <!--n:acc_v3-->65.4%<!--/n-->          a single number (invisible on GitHub; the text between the markers is replaced)
  <!--auto:results_accuracy-->           a whole table (everything between the two markers is replaced)
  ...
  <!--/auto:results_accuracy-->

Everything outside the markers is ordinary prose that you edit by hand. Numbers that come from one-off experiments rather than
the database (the leakage story, the model comparisons) are written as plain text and are not touched.

  python pipeline/readme_numbers.py show     # print every number and table, and which claims in the prose still hold
  python pipeline/readme_numbers.py check    # exit 1 if README.md is out of date or a claim no longer holds
  python pipeline/readme_numbers.py update   # rewrite the markers in README.md

It only reads the database (a read-only connection string is enough) and only writes README.md.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MIN_YEAR_FIGHTS = 200            # a year needs this many scored fights to count toward the yearly range
CAL_MIN_FIGHTS = 200             # a confidence bucket needs this many fights before its calibration gap is judged
CAL_MAX_GAP = 0.05               # claim guard: stated confidence and the hit rate agree within this
TOKEN_RE = re.compile(r"<!--n:([a-z0-9_]+)-->(.*?)<!--/n-->", re.S)
BLOCK_RE = re.compile(r"(<!--auto:([a-z0-9_]+)-->)(.*?)(<!--/auto:\2-->)", re.S)

TIER_NAMES = {"full": "Both have 3+ UFC fights", "limited": "The less experienced has 1 to 2", "debut": "A fighter is a debutant"}
TIER_SHORT = {"full": "both fighters have 3+ UFC fights", "limited": "a fighter has 1 to 2", "debut": "a fighter is a debutant"}
EDGES = [(0.5, 0.6, "50 to 60%"), (0.6, 0.7, "60 to 70%"), (0.7, 0.8, "70 to 80%"), (0.8, 1.01, "80% and up")]


def pct(x: float, nd: int = 1) -> str:
    return f"{x * 100:.{nd}f}%"


def num(n) -> str:
    return f"{int(n):,}"


@dataclass
class Numbers:
    values: dict = field(default_factory=dict)      # inline numbers: name -> text
    blocks: dict = field(default_factory=dict)      # tables: name -> markdown
    broken_claims: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# reading the database (plain SQL, so it runs on Postgres and, in the tests, on SQLite)
# ----------------------------------------------------------------------------------------------
def _read(engine, table: str, columns: list[str], where: str = "") -> pd.DataFrame | None:
    if not sa.inspect(engine).has_table(table):
        return None
    return pd.read_sql(sa.text(f"SELECT {', '.join(columns)} FROM {table} {where}"), engine)


def load_frames(engine) -> dict:
    frames = {
        "v3": _read(engine, "fight_picks_v3", ["event_date", "fighter_a", "actual_winner", "p_v2_a", "market_prob_a", "p_a_wins", "confidence",
                                                "correct", "v2_correct", "market_correct", "tier"]),
        "v2": _read(engine, "fight_picks_v2", ["event_date", "method", "correct"]),
        "live_v2": _read(engine, "upcoming_picks_v2", ["event_name", "event_date", "status", "confidence", "correct", "graded_at"]),
        "live_v3": _read(engine, "upcoming_picks_v3", ["event_name", "event_date", "status", "confidence", "correct", "graded_at"]),
        "dist": _read(engine, "fight_distance_v1", ["event_date", "p_distance", "p_base", "went_distance", "tier"]),
        "dist2": _read(engine, "fight_distance_v2", ["event_date", "p_distance", "p_v1", "p_base", "went_distance", "tier"]),
        "market_share": None,
    }
    if sa.inspect(engine).has_table("fight_reasons") and frames["v3"] is not None:
        with engine.connect() as conn:                      # the blend's weights as of the most recent scored fight
            row = conn.execute(sa.text("SELECT r.reasons FROM fight_reasons r JOIN fight_picks_v3 p ON p.fight_url = r.fight_url "
                                       "WHERE r.version = 'v3' ORDER BY p.event_date DESC, r.fight_url LIMIT 1")).fetchone()
        if row is not None:
            doc = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            frames["market_share"] = float(doc["market_share"])
    return frames


def count_tests(root: Path = ROOT) -> int | None:
    """How many tests pytest collects (None if pytest is not available)."""
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"], cwd=str(root), capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+) tests? collected", out)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------------------------
# the numbers
# ----------------------------------------------------------------------------------------------
def compute(frames: dict, n_tests: int | None = None) -> Numbers:
    import model_v3                                         # the scoring maths lives next to the model so the app and the README agree

    out = Numbers()
    v, b = out.values, out.blocks
    d3 = frames.get("v3")
    if d3 is not None and len(d3) >= 30:
        n = len(d3)
        years = pd.to_datetime(d3.event_date).dt.year
        v.update(n_fights=num(n), first_year=str(int(years.min())), last_year=str(int(years.max())),
                 acc_v2=pct(d3.v2_correct.mean()), acc_line=pct(d3.market_correct.mean()), acc_v3=pct(d3.correct.mean()))
        b["results_accuracy"] = "\n".join(["| Predictor | Right |", "|---|---|", "| Coin flip | 50% |",
                                           f"| Statistical model alone (v2) | {v['acc_v2']} |", f"| Bookmakers' opening line alone | {v['acc_line']} |",
                                           f"| **Statistical model + the betting market (v3)** | **{v['acc_v3']}** |"])
        rows = ["| Fighters' UFC history | Fights | Model alone | Model + market |", "|---|---|---|---|"]
        for t in ("full", "limited", "debut"):
            g = d3[d3.tier == t]
            if len(g):
                rows.append(f"| {TIER_NAMES[t]} | {num(len(g))} | {pct(g.v2_correct.mean())} | {pct(g.correct.mean())} |")
        b["results_tiers"] = "\n".join(rows)
        gain = {t: d3[d3.tier == t].correct.mean() - d3[d3.tier == t].v2_correct.mean() for t in ("full", "limited", "debut") if (d3.tier == t).any()}
        if "debut" in gain and gain["debut"] < max(gain.values()):
            out.broken_claims.append("The README says the market helps most where the statistics are thinnest (debutants), but another group gained more.")

        # calibration of the blended picks
        cal, gaps, top = ["| Stated confidence | Fights | Average stated | Actually right |", "|---|---|---|---|"], [], None
        for lo, hi, label in EDGES:
            g = d3[(d3.confidence >= lo) & (d3.confidence < hi)]
            if len(g):
                cal.append(f"| {label} | {num(len(g))} | {pct(g.confidence.mean())} | {pct(g.correct.mean())} |")
                if len(g) >= CAL_MIN_FIGHTS:
                    gaps.append(abs(g.confidence.mean() - g.correct.mean()))
                top = (g.confidence.mean(), g.correct.mean(), len(g))
        b["calibration"] = "\n".join(cal)
        if gaps and max(gaps) > CAL_MAX_GAP:
            out.broken_claims.append(f"The README says stated confidence tracks how often picks were right, but one bucket is off by {max(gaps) * 100:.1f} points (limit {CAL_MAX_GAP * 100:.0f}).")
        if top and top[2] >= CAL_MIN_FIGHTS and top[1] < top[0] - 0.03:
            out.broken_claims.append(f"The README says the most confident picks were not over-confident, but they were right {pct(top[1])} against a stated {pct(top[0])}.")

        # where the blend and the model alone choose differently
        dis = (d3.p_v2_a >= 0.5) != (d3.p_a_wins >= 0.5)
        if dis.any():
            v.update(dis_share=pct(dis.mean()), dis_blend=pct(d3[dis].correct.mean()), dis_model=pct(d3[dis].v2_correct.mean()))
            if d3[dis].correct.mean() <= d3[dis].v2_correct.mean():
                out.broken_claims.append("The README says that when the blend and the model disagree the blend is right more often, but it no longer is.")

        # scoring the probabilities
        r = model_v3.probability_scores(d3)
        s = r["scores"]
        best = min(s["v2"]["log_loss"], s["line"]["log_loss"], s["v3"]["log_loss"])
        bold = lambda k, text: f"**{text}**" if s[k]["log_loss"] == best else text
        b["scoring_table"] = "\n".join(["| Predictor | Log loss | Brier score |", "|---|---|---|",
                                        f"| Coin flip | {s['coin']['log_loss']:.4f} | {s['coin']['brier']:.4f} |",
                                        f"| Statistical model alone (v2) | {bold('v2', format(s['v2']['log_loss'], '.4f'))} | {bold('v2', format(s['v2']['brier'], '.4f'))} |",
                                        f"| Bookmakers' opening line alone | {bold('line', format(s['line']['log_loss'], '.4f'))} | {bold('line', format(s['line']['brier'], '.4f'))} |",
                                        f"| Model + market (v3) | {bold('v3', format(s['v3']['log_loss'], '.4f'))} | {bold('v3', format(s['v3']['brier'], '.4f'))} |"])
        line = lambda label, x: f"| {label} | {num(x['n'])} | {x['mean']:+.4f} | {x['lo']:+.4f} to {x['hi']:+.4f} | {x['verdict']} |"
        comp = ["| Model + market compared with... (log loss per fight; negative means it is better) | Fights | Average | 95% range | Verdict |", "|---|---|---|---|---|",
                line("The opening line", r["blend_vs_line"]), line("The model alone", r["blend_vs_model"])]
        comp += [line(f"The opening line, where {TIER_SHORT[t]}", x) for t, x in r["tiers"].items()]
        b["scoring_compare"] = "\n".join(comp)
        vl = r["blend_vs_line"]
        if vl["verdict"] != "clearly better":
            out.broken_claims.append("The README says the blend beats the opening line by a small but statistically clear amount, but the 95% range now includes zero.")
        elif vl["mean"] < 0:
            sd = (vl["hi"] - vl["lo"]) / (2 * 1.96) * math.sqrt(vl["n"])
            v["n_live_needed"] = num(round(((1.96 + 0.84) * sd / abs(vl["mean"])) ** 2, -2))
        tv = {t: x["verdict"] for t, x in r["tiers"].items()}
        if tv.get("full") not in (None, "clearly better"):
            out.broken_claims.append("The README says the blend's edge comes from fights where both fighters have a UFC history, but that group is no longer clearly better.")
        for t in ("limited", "debut"):
            if tv.get(t) == "clearly better":
                out.broken_claims.append(f"The README says the blend and the line cannot be told apart where {TIER_SHORT[t]}, but now the blend is clearly better there.")
    else:
        out.skipped.append("the market-informed backtest (fight_picks_v3) is missing or too small")

    # the goes-the-distance model
    dd = frames.get("dist")
    if dd is not None and len(dd) >= 30:
        import distance_scores
        r = distance_scores.distance_scores(dd)
        sc = r["scores"]

        def drow(label: str, key: str, best: bool = False) -> str:
            cells = [format(sc[key]["log_loss"], ".4f"), format(sc[key]["brier"], ".4f"), format(sc[key]["auc"], ".3f")]
            if best:
                label, cells = f"**{label}**", [f"**{x}**" for x in cells]
            return f"| {label} | {' | '.join(cells)} |"
        model_best = sc["model"]["log_loss"] < min(sc["recent"]["log_loss"], sc["coin"]["log_loss"]) - 1e-9      # bold only when it genuinely wins
        b["distance_table"] = "\n".join(["| Predictor | Log loss | Brier score | AUC |", "|---|---|---|---|", drow("Coin flip", "coin"),
                                          drow("Plain decision rate of the previous three years", "recent"), drow("Goes-the-distance model", "model", model_best)])
        vr = r["vs_recent"]
        v.update(dist_n=num(r["n"]), dist_first_year=str(r["first_year"]), dist_last_year=str(r["last_year"]), dist_years_better=str(r["years_better"]),
                 dist_years_total=str(r["years_total"]), dist_gap=f"{r['max_gap'] * 100:.1f}")
        if vr["verdict"] == "clearly better":
            v.update(dist_gain=f"{abs(vr['mean']):.4f}", dist_gain_lo=f"{abs(vr['hi']):.4f}", dist_gain_hi=f"{abs(vr['lo']):.4f}")
        else:
            out.broken_claims.append("The README says the goes-the-distance model beats the plain decision rate by a clear margin, but the 95% range now includes zero.")
        if r["max_gap"] > CAL_MAX_GAP:
            out.broken_claims.append(f"The README says the goes-the-distance probabilities are calibrated, but one bucket is off by {r['max_gap'] * 100:.1f} points (limit {CAL_MAX_GAP * 100:.0f}).")
        if r["years_total"] and r["years_better"] / r["years_total"] < 0.7:
            out.broken_claims.append("The README says the goes-the-distance model beat the plain decision rate in most years, but it no longer does.")
    else:
        out.skipped.append("the goes-the-distance backtest (fight_distance_v1) is missing or too small")

    # the market-informed goes-the-distance prediction: added on a narrow margin, alongside the model-only one
    dm = frames.get("dist2")
    if dm is not None and len(dm) >= 200:
        import distance_scores
        rm = distance_scores.distance_scores(dm)
        cmp_m = distance_scores.paired_vs(dm, "p_distance", "p_v1")
        yb_m, yt_m = distance_scores.years_better_vs(dm, "p_distance", "p_v1")
        v.update(dist2_n=num(rm["n"]), dist2_first_year=str(rm["first_year"]), dist2_last_year=str(rm["last_year"]), dist2_years_better=str(yb_m), dist2_years_total=str(yt_m))
        gain = -cmp_m["mean"]                                    # positive: the market-informed prediction has the lower log loss
        v.update(dist2_gain=f"{gain:.4f}", dist2_gain_lo=f"{-cmp_m['hi']:+.4f}", dist2_gain_hi=f"{-cmp_m['lo']:+.4f}")
        # the README says: better on average, better in most years, but the 95% range includes zero (so the two cannot yet be told apart). Each part is checked.
        if gain <= 0:
            out.broken_claims.append("The README says the market-informed goes-the-distance prediction is better than the model-only one on average, but it no longer is.")
        if cmp_m["verdict"] != "cannot be told apart":
            out.broken_claims.append(f"The README says the market-informed and model-only goes-the-distance predictions cannot be told apart, but now the market-informed one is {cmp_m['verdict']}.")
        if yt_m and yb_m / yt_m < 0.6:
            out.broken_claims.append("The README says the market-informed goes-the-distance prediction was better in most years, but it no longer is.")
    else:
        out.skipped.append("the market-informed goes-the-distance backtest (fight_distance_v2) is missing or too small")

    # the yearly range of the model-only backtest, over years with enough fights
    d2 = frames.get("v2")
    if d2 is not None and len(d2):
        m = d2[d2.method == "model"].assign(year=lambda x: pd.to_datetime(x.event_date).dt.year)
        yr = m.groupby("year").agg(n=("correct", "size"), acc=("correct", "mean"))
        yr = yr[yr.n >= MIN_YEAR_FIGHTS]
        if len(yr):
            v.update(year_min_fights=str(MIN_YEAR_FIGHTS), year_lo=pct(yr.acc.min(), 0), year_hi=pct(yr.acc.max(), 0))
    else:
        out.skipped.append("the model-only backtest (fight_picks_v2) is missing")

    if frames.get("market_share") is not None:
        ms = frames["market_share"]
        v.update(market_weight=pct(ms, 0), model_weight=pct(1 - ms, 0))
        if ms < 0.5:
            out.broken_claims.append("The README says the market carries most of the weight in the blend, but it no longer does.")
    else:
        out.skipped.append("the blend's weights (fight_reasons) are missing")

    # the first live card (UFC 333): fixed history, but it is in the database, so the README is checked against it
    l2, l3 = frames.get("live_v2"), frames.get("live_v3")
    if l2 is not None and len(l2):
        g2 = l2[l2.event_name.astype(str).str.startswith("UFC 333") & (l2.status == "locked") & l2.graded_at.notna()]
        if len(g2):
            v.update(u333_n=num(len(g2)), u333_v2=num(g2.correct.sum()), u333_expected_v2=f"{g2.confidence.sum():.1f}")
            if l3 is not None:
                g3 = l3[l3.event_name.astype(str).str.startswith("UFC 333") & (l3.status == "locked") & l3.graded_at.notna()]
                if len(g3):
                    v.update(u333_v3=num(g3.correct.sum()), u333_expected_v3=f"{g3.confidence.sum():.1f}")
    if "u333_n" not in v:
        out.skipped.append("the UFC 333 picks (upcoming_picks_v2) are missing or not graded yet")

    if n_tests is not None:
        v["n_tests"] = str(n_tests)
    else:
        out.skipped.append("the test count (pytest is not available)")
    return out


# ----------------------------------------------------------------------------------------------
# editing the README text
# ----------------------------------------------------------------------------------------------
def apply(text: str, nums: Numbers) -> tuple[str, list[str], list[str]]:
    """Returns (new text, what changed, markers it could not fill). Line endings are preserved."""
    crlf = "\r\n" in text
    t = text.replace("\r\n", "\n")
    changes, unfilled = [], []

    def tok(m):
        name, old = m.group(1), m.group(2)
        if name not in nums.values:
            unfilled.append(name)
            return m.group(0)
        if nums.values[name] != old:
            changes.append(f"{name}: {old} -> {nums.values[name]}")
        return f"<!--n:{name}-->{nums.values[name]}<!--/n-->"

    def blk(m):
        name, old = m.group(2), m.group(3)
        if name not in nums.blocks:
            unfilled.append(name)
            return m.group(0)
        new = f"\n\n{nums.blocks[name]}\n\n"
        if new.strip() != old.strip():
            changes.append(f"table {name} changed")
        return f"{m.group(1)}{new}{m.group(4)}"
    t = TOKEN_RE.sub(tok, t)
    t = BLOCK_RE.sub(blk, t)
    return (t.replace("\n", "\r\n") if crlf else t), changes, sorted(set(unfilled))


def known_names() -> set:
    """Every marker name the generator can fill (used by a test to catch typos in README.md)."""
    return {"n_fights", "first_year", "last_year", "acc_v2", "acc_line", "acc_v3", "dis_share", "dis_blend", "dis_model", "n_live_needed", "year_min_fights",
            "year_lo", "year_hi", "market_weight", "model_weight", "u333_n", "u333_v2", "u333_v3", "u333_expected_v2", "u333_expected_v3", "n_tests",
            "results_accuracy", "results_tiers", "calibration", "scoring_table", "scoring_compare", "distance_table", "dist_n", "dist_first_year", "dist_last_year",
            "dist_gain", "dist_gain_lo", "dist_gain_hi", "dist_years_better", "dist_years_total", "dist_gap", "dist2_n", "dist2_first_year", "dist2_last_year",
            "dist2_gain", "dist2_gain_lo", "dist2_gain_hi", "dist2_years_better", "dist2_years_total"}


def refresh(engine, path: Path | None = None, write: bool = True, n_tests: int | None = None) -> dict:
    path = Path(path) if path else README                    # looked up when called, so tests can point it at a temporary file
    nums = compute(load_frames(engine), n_tests if n_tests is not None else count_tests())
    text = ""
    if Path(path).exists():
        with open(path, "r", encoding="utf-8", newline="") as fh:         # newline="" keeps Windows line endings as they are
            text = fh.read()
    new, changes, unfilled = apply(text, nums)
    if write and changes:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
    return {"changes": changes, "unfilled": unfilled, "broken": nums.broken_claims, "skipped": nums.skipped, "numbers": nums}


def step_readme(engine, today, rep, dry_run: bool, write: bool, forced: bool = False) -> None:
    """The daily job's step: on Mondays (or when asked) refresh README.md; the workflow commits it if it changed."""
    if today.weekday() != 0 and not forced:
        rep.add("README numbers", "Only refreshed on Mondays.")
        return
    res = refresh(engine, None, write=write and not dry_run)
    lines = []
    if res["changes"]:
        verb = "would change" if (dry_run or not write) else "refreshed"
        lines.append(f"{verb} {len(res['changes'])} number(s) or table(s) in README.md" + ("" if dry_run or not write else "; the workflow commits it"))
        lines += [f"   {c}" for c in res["changes"][:8]]
    else:
        lines.append("README numbers already match the database.")
    if not write and not dry_run:
        lines.append("(only the GitHub job writes README.md; run `python pipeline/readme_numbers.py update` to do it yourself)")
    for c in res["broken"]:
        rep.need(f"README: {c}")
    rep.add("README numbers", *lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("show", "check", "update"):
        print(__doc__)
        return 2
    import model_v2
    engine = model_v2.make_engine()
    if argv[0] == "show":
        nums = compute(load_frames(engine), count_tests())
        for k, x in nums.values.items():
            print(f"{k:18s} {x}")
        for k, x in nums.blocks.items():
            print(f"\n[{k}]\n{x}")
        res = {"broken": nums.broken_claims, "skipped": nums.skipped}
    else:
        res = refresh(engine, write=(argv[0] == "update"))
        if res["changes"]:
            print(("Updated" if argv[0] == "update" else "README is out of date:") + f" {len(res['changes'])} change(s)")
            for c in res["changes"]:
                print("  ", c)
        else:
            print("README numbers match the database.")
        if res["unfilled"]:
            print("Markers I could not fill (left as they were):", ", ".join(res["unfilled"]))
    for s in res["skipped"]:
        print("Skipped:", s)
    for c in res["broken"]:
        print("CLAIM NO LONGER HOLDS:", c)
    stale = argv[0] == "check" and bool(res.get("changes"))
    return 1 if (res["broken"] or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
