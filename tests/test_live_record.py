"""
The running live record in the README, the realistic "how long would it take" figures for the distance prediction, and the retirement rule fixed in advance.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "app"):
    sys.path.insert(0, str(ROOT / sub))
import distance_ui  # noqa: E402
import distance_scores  # noqa: E402
import model_distance_v2 as mdv2  # noqa: E402
import readme_numbers as rn  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")


def _live(n_right: int, n: int, conf: float, cards: int = 2, noise: bool = True) -> pd.DataFrame:
    rows = [{"event_name": f"UFC {330 + i % cards}", "event_date": pd.Timestamp("2026-10-24") + pd.Timedelta(days=7 * (i % cards)), "status": "locked", "confidence": conf,
             "correct": 1 if i < n_right else 0, "graded_at": pd.Timestamp("2026-11-01")} for i in range(n)]
    if noise:                                                                                                         # none of these may be counted
        rows += [{"event_name": "UFC 340", "event_date": pd.Timestamp("2026-11-07"), "status": "cancelled", "confidence": .6, "correct": 1, "graded_at": pd.Timestamp("2026-11-01")},
                 {"event_name": "UFC 340", "event_date": pd.Timestamp("2026-11-07"), "status": "locked", "confidence": .6, "correct": None, "graded_at": pd.Timestamp("2026-11-01")},      # a draw: no result
                 {"event_name": "UFC 341", "event_date": pd.Timestamp("2026-11-14"), "status": "locked", "confidence": .6, "correct": None, "graded_at": pd.NaT}]                         # not played yet
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------- the running live record
def test_the_wilson_range_matches_known_values_and_behaves_at_the_edges():
    lo, hi = rn.wilson(17, 26)
    assert (round(lo, 2), round(hi, 2)) == (0.46, 0.81)
    lo, hi = rn.wilson(9, 12)
    assert (round(lo, 2), round(hi, 2)) == (0.47, 0.91)
    assert rn.wilson(0, 10)[0] == pytest.approx(0, abs=1e-9) and rn.wilson(10, 10)[1] == pytest.approx(1, abs=1e-9)
    assert 0 < rn.wilson(0, 10)[1] < 0.4 and 0.6 < rn.wilson(10, 10)[0] < 1                                           # even 0 of 10 does not claim certainty
    assert rn.whole_pct(0.654) == "65%" and rn.whole_pct(0.6923) == "69%"


def test_the_live_record_line_counts_only_locked_graded_picks_with_the_right_ranges_and_expectations():
    nums = rn.compute({"live_v2": _live(17, 26, 15.9 / 26), "live_v3": _live(18, 26, 17.3 / 26)}, 1)
    line = nums.values["live_record"]
    assert "Model-only picks: **17 of 26** right (65%; its own stated confidences predicted 15.9; 95% range 46% to 81%)" in line
    assert "Market-informed picks: **18 of 26** right (69%; its own stated confidences predicted 17.3; 95% range 50% to 83%)" in line
    assert "Across 2 cards, every pick locked before its event." in line and "[First live test](#first-live-test)" in line
    assert "\n" not in line                                                                                            # one line, so it fits in a table cell


def test_one_card_is_singular_and_a_missing_market_set_just_drops_that_half():
    line = rn.compute({"live_v2": _live(9, 12, 0.62, cards=1)}, 1).values["live_record"]
    assert "Across 1 card, " in line and "Model-only picks: **9 of 12**" in line and "Market-informed" not in line


def test_the_live_record_is_skipped_not_a_crash_before_anything_is_graded():
    for frames in ({}, {"live_v2": _live(0, 0, .6, noise=False)}, {"live_v2": _live(0, 0, .6).iloc[1:]}):
        nums = rn.compute(frames, 1)
        assert "live_record" not in nums.values and any("running live record" in s for s in nums.skipped)


def test_the_readme_is_told_to_stop_saying_far_too_few_once_the_record_is_large_enough():
    assert not [c for c in rn.compute({"live_v2": _live(150, 299, .6, noise=False)}, 1).broken_claims if "live record" in c]
    big = [c for c in rn.compute({"live_v2": _live(180, 300, .6, noise=False)}, 1).broken_claims if "live record" in c]
    assert len(big) == 1 and "300 graded picks" in big[0]


def test_the_block_replaces_only_its_own_line_and_the_table_row_stays_intact():
    frames = {"live_v2": _live(17, 26, 15.9 / 26), "live_v3": _live(18, 26, 17.3 / 26)}
    updated, changes, _ = rn.apply(README, rn.compute(frames, 1))
    rows = [l for l in updated.splitlines() if l.startswith("| **Live record so far**")]
    assert len(rows) == 1 and rows[0].rstrip().endswith("|") and rows[0].count("|") == 3                                # still a two-cell row
    assert "<!--n:live_record-->" in rows[0] and rows[0].count("<!--/n-->") == 1 and "**17 of 26**" in rows[0] and "**18 of 26**" in rows[0]


# ---------------------------------------------------------------------------------------------- the README itself
def test_the_readme_shows_the_live_record_row_and_makes_the_realistic_claim_about_the_distance_record():
    assert "live_record" in rn.known_names() and {"dist2_live_needed", "dist2_years_needed"} <= rn.known_names()
    row = [l for l in README.splitlines() if l.startswith("| **Live record so far**")]
    assert len(row) == 1 and "<!--n:live_record-->" in row[0] and "(#first-live-test)" in row[0]
    assert "exactly what the live record is for" not in README                                                        # the overpromise is gone
    assert "<!--n:dist2_live_needed-->" in README and "<!--n:dist2_years_needed-->" in README and "the live record cannot settle it soon" in README
    assert "retired only if, over at least 300 graded live fights, it is clearly worse than the model-only one" in README
    assert 'in the "At a glance" table at the top' in README


def test_the_distance_needed_figures_follow_the_same_convention_as_the_winner_models_and_exist_only_when_there_is_a_gain():
    rng = np.random.default_rng(3)
    n = 4000
    y = rng.integers(0, 2, n)
    base = dict(event_date=pd.date_range("2014-01-01", periods=n, freq="1D"), p_base=0.5, went_distance=y, tier="full", p_v1=0.5)
    d = pd.DataFrame(dict(base, p_distance=np.clip(0.5 + rng.normal(0, 0.03, n) + 0.01 * (2 * y - 1), .05, .95)))
    c = distance_scores.paired_vs(d, "p_distance", "p_v1")
    gain = -c["mean"]
    assert gain > 0                                                                                                   # the premise: v2 is better on average here
    sd = (c["hi"] - c["lo"]) / (2 * 1.96) * np.sqrt(n)
    expected = round(((1.96 + 0.84) * sd / gain) ** 2, -2)                                                            # 80% power at the 5% level, the same convention as the winner model's figure
    values = rn.compute({"dist2": d}, 1).values
    assert values["dist2_live_needed"] == rn.num(expected) and int(values["dist2_years_needed"]) == round(expected / (n / (d.event_date.dt.year.max() - d.event_date.dt.year.min() + 1)))
    worse = rn.compute({"dist2": pd.DataFrame(dict(base, p_distance=0.5))}, 1)
    assert "dist2_live_needed" not in worse.values                                                                     # no gain, no promise of a figure


# ---------------------------------------------------------------------------------------------- the retirement rule
def _pair(n, p2_of_y, seed=2):
    y = np.random.default_rng(seed).integers(0, 2, n)
    return pd.DataFrame({"p_v2": p2_of_y(y), "p_v1": 0.5, "went_distance": y})


def test_the_rule_says_nothing_until_there_are_enough_graded_live_fights():
    live = _pair(299, lambda y: 0.5 + 0.2 * (1 - 2 * y))                                                               # wildly worse, but only 299 fights
    r = mdv2.retirement_status(live)
    assert r["retire"] is False and "299 graded fights so far" in r["text"] and "at least 300" in r["text"] and r["verdict"] is None
    assert mdv2.retirement_status(live.head(12))["text"].startswith("keep both running (12 graded fights so far")


def test_the_rule_retires_v2_only_when_it_is_clearly_worse_over_enough_fights():
    worse = mdv2.retirement_status(_pair(400, lambda y: 0.5 + 0.15 * (1 - 2 * y)))
    assert worse["retire"] is True and worse["verdict"] == "clearly worse" and worse["text"].startswith("RETIRE the market-informed prediction: over 400 graded fights")
    better = mdv2.retirement_status(_pair(400, lambda y: 0.5 + 0.15 * (2 * y - 1)))
    assert better["retire"] is False and "is clearly better than the model-only one" in better["text"]
    tied = mdv2.retirement_status(_pair(400, lambda y: np.full(len(y), 0.5)))
    assert tied["retire"] is False and "cannot be told apart from the model-only one" in tied["text"]


def test_the_rule_is_stated_the_same_way_everywhere_it_appears():
    assert mdv2.RETIRE_AFTER_FIGHTS == 300
    assert "THE RETIREMENT RULE" in mdv2.__doc__ and "300 graded live fights" in mdv2.__doc__
    d = pd.DataFrame({"event_date": pd.date_range("2020-01-01", periods=300, freq="5D"), "p_distance": 0.5, "p_v1": 0.5, "p_base": 0.5, "went_distance": np.tile([0, 1], 150), "tier": "full"})
    s2 = distance_scores.distance_scores(d)
    page = distance_ui.section_v2_html(s2, s2["scores"]["model"], {"mean": -0.003, "lo": -0.006, "hi": 0.0002, "verdict": "cannot be told apart"}, (10, 12), None)
    assert "retired only if it is clearly worse over at least 300 graded live fights" in page and "will take years to tell the two apart" in page
    assert "earns its place" not in page
