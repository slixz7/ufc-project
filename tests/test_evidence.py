"""
pipeline/evidence.py: the evidence meter's maths. Every frame is built so the exact answer is known, and the "fights needed" figure is checked against the README generator's.
"""
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import db_roles  # noqa: E402
import evidence as ev  # noqa: E402
import model_health as mh  # noqa: E402
import readme_numbers as rn  # noqa: E402

SPEC = {s["key"]: s for s in ev.CLAIMS}


def winners(p_a, y, fighter_a="A", **extra):
    """A winner frame: probabilities for fighter A, and who won (1: A)."""
    n = len(p_a)
    f = pd.DataFrame({"fighter_a": fighter_a, "actual_winner": np.where(np.array(y) == 1, "A", "B"), "p_v2_a": p_a, "p_a_wins": p_a, "market_prob_a": extra.get("market", p_a)})
    f["event_date"] = pd.date_range("2016-01-01", periods=n, freq="D")
    return f


# ---------------------------------------------------------------------------------------------- one comparison, by hand
def test_the_model_against_a_coin_flip_is_the_log_loss_difference_worked_by_hand():
    d = ev.paired(SPEC["v2"], winners([0.7, 0.6, 0.4, 0.2], [1, 1, 0, 0]), "backtest")
    model_loss = (-math.log(0.7) - math.log(0.6) - math.log(0.6) - math.log(0.8)) / 4                                   # 0.400368
    assert d["n"] == 4 and d["mean"] == pytest.approx(model_loss - math.log(2)) and d["mean"] == pytest.approx(-0.292779, abs=1e-5)
    assert d["verdict"] == "clearly better"                                                                                 # all four fights beat a coin flip, each by 0.18 to 0.47: a range of about -0.29 plus or minus 0.13


def test_it_does_not_matter_which_fighter_is_called_a():
    d = winners([0.7, 0.6, 0.4, 0.2], [1, 1, 0, 0])
    swapped = d.assign(fighter_a="B", p_v2_a=1 - d.p_v2_a, p_a_wins=1 - d.p_a_wins, market_prob_a=1 - d.market_prob_a)           # the other person is now "A": his chance is 1 minus; the winner's name is unchanged
    for key in ("v2", "v3"):
        assert ev.paired(SPEC[key], swapped, "backtest")["mean"] == pytest.approx(ev.paired(SPEC[key], d, "backtest")["mean"])


def test_each_claim_reads_its_own_columns_for_the_backtest_and_for_the_live_record():
    f = winners([0.7, 0.6, 0.4, 0.2], [1, 1, 0, 0], market=[0.6, 0.6, 0.5, 0.3]).assign(p_a_wins=[0.65, 0.6, 0.45, 0.25])
    assert ev.paired(SPEC["v3"], f, "backtest")["mean"] != ev.paired(SPEC["v2"], f, "backtest")["mean"]                   # the blend against the line, not the model against a coin
    blend = (-math.log(0.65) - math.log(0.6) - math.log(0.55) - math.log(0.75)) / 4                                         # 0.45678
    line = (-math.log(0.6) - math.log(0.6) - math.log(0.5) - math.log(0.7)) / 4                                             # 0.51787
    assert ev.paired(SPEC["v3"], f, "backtest")["mean"] == pytest.approx(blend - line) == pytest.approx(-0.06109, abs=1e-4)   # negative: the blend is better than the line here
    live = pd.DataFrame({"fighter_a": "A", "actual_winner": ["A", "A", "B", "B"], "p_a_wins": [0.7, 0.6, 0.4, 0.2]})        # the live model-only table has no p_v2_a column
    assert ev.paired(SPEC["v2"], live, "live")["mean"] == pytest.approx(ev.paired(SPEC["v2"], winners([0.7, 0.6, 0.4, 0.2], [1, 1, 0, 0]), "backtest")["mean"])
    d1 = pd.DataFrame({"p_distance": [0.7, 0.3, 0.6], "p_base": [0.5, 0.5, 0.5], "went_distance": [1, 0, 1]})
    assert ev.paired(SPEC["d1"], d1, "live")["mean"] < 0
    d2_live = pd.DataFrame({"p_v2": [0.7, 0.3], "p_v1": [0.6, 0.4], "went_distance": [1, 0]})
    d2_back = pd.DataFrame({"p_distance": [0.7, 0.3], "p_v1": [0.6, 0.4], "went_distance": [1, 0]})
    assert ev.paired(SPEC["d2"], d2_live, "live")["mean"] == pytest.approx(ev.paired(SPEC["d2"], d2_back, "backtest")["mean"])
    assert ev.paired(SPEC["d2"], d2_live, "live")["mean"] < 0


def test_missing_columns_empty_frames_and_unplayed_rows_give_nothing_rather_than_a_guess():
    assert ev.paired(SPEC["v3"], None, "live") is None and ev.paired(SPEC["v3"], winners([], []), "live") is None
    assert ev.paired(SPEC["d1"], pd.DataFrame({"p_distance": [0.5]}), "live") is None                                       # no outcome column
    f = winners([0.7, 0.6, 0.4], [1, 1, 0]).assign(actual_winner=["A", None, "B"])                                          # one fight not played yet
    assert ev.paired(SPEC["v2"], f, "backtest")["n"] == 2


# ---------------------------------------------------------------------------------------------- how many fights are needed
def test_the_fights_needed_follow_the_readmes_80_percent_power_convention():
    needed = ev.needed_fights({"mean": -0.0028, "sd": 0.118})
    assert 13900 <= needed <= 13950 and ev.POWER_Z == 1.96 + 0.84                                                          # (2.8 x 0.118 / 0.0028) squared, about 13,924
    assert ev.needed_fights({"mean": -0.0065, "sd": 0.14}) == math.ceil((2.8 * 0.14 / 0.0065) ** 2)
    for none in (None, {"mean": 0.001, "sd": 0.1}, {"mean": 0.0, "sd": 0.1}, {"mean": -0.01, "sd": 0.0}, {"mean": -0.01, "sd": float("nan")}):
        assert ev.needed_fights(none) is None                                                                              # a gain to confirm is needed, and a spread to judge it by
    assert "1.96 + 0.84" in (ROOT / "pipeline" / "readme_numbers.py").read_text(encoding="utf-8")                           # the README's own constants


def test_the_meters_figure_equals_the_readmes_for_the_same_data():
    rng = np.random.default_rng(5)
    n = 4000
    y = rng.integers(0, 2, n)
    d = pd.DataFrame({"event_date": pd.date_range("2014-01-01", periods=n, freq="1D"), "p_base": 0.5, "went_distance": y, "tier": "full", "p_v1": 0.5,
                      "p_distance": np.clip(0.5 + rng.normal(0, 0.03, n) + 0.01 * (2 * y - 1), .05, .95)})
    readme = int(rn.compute({"dist2": d}, 1).values["dist2_live_needed"].replace(",", ""))
    meter = ev.needed_fights(ev.paired(SPEC["d2"], d, "backtest"))
    assert meter is not None and round(meter, -2) == readme                                                                  # one convention, two places, one answer


def test_the_pace_and_the_years_in_words():
    f = pd.DataFrame({"event_date": pd.date_range("2014-01-01", "2016-12-31", periods=300)})
    assert ev.fights_per_year(f) == pytest.approx(100) and ev.fights_per_year(f.iloc[0:0]) is None and ev.fights_per_year(pd.DataFrame({"x": [1]})) is None and ev.fights_per_year(None) is None
    for years, words in ((0.04, "about 1 month"), (0.3, "about 4 months"), (0.89, "about 11 months"), (0.9, "about a year"), (1.49, "about a year"), (1.5, "about 2 years"), (31.4, "about 31 years")):
        assert ev.years_text(years) == words, years


# ---------------------------------------------------------------------------------------------- one entry, and all four
def test_every_claim_always_gets_an_entry_in_the_same_order_even_with_no_data():
    entries = ev.compute_all({}, {})
    assert [e["key"] for e in entries] == ["v2", "v3", "d1", "d2"]
    assert all(e["n_live"] == 0 and e["live"] is None and e["backtest"] is None and e["needed"] is None and e["progress"] is None for e in entries)
    assert all(e["sentence"] == "No graded fights yet, so there is nothing to show. The bar fills as cards are graded." for e in entries)


def test_with_a_backtest_but_nothing_graded_the_bar_exists_at_zero_and_the_goal_is_known():
    rng = np.random.default_rng(1)
    n = 3000
    y = rng.integers(0, 2, n)
    bt = winners(np.clip(0.5 + 0.1 * (2 * y - 1) + rng.normal(0, 0.1, n), 0.05, 0.95), y)
    e = ev.compute_all({"v2": bt}, {})[0]
    assert e["n_live"] == 0 and e["needed"] and e["needed"] > 0 and e["progress"] == 0.0 and e["years_needed"] and e["per_year"]
    assert e["per_year"] == pytest.approx(n / 9)                                                                           # 3,000 daily fights span 9 calendar years, 2016 to 2024


def right_sixty_percent(n):
    """A model that points the right way on 3 fights in 5, always at 60% confidence: a small effect (about -0.02 per fight), so the fights needed run to hundreds, as in the real project."""
    y = np.array([i % 2 for i in range(n)])
    right = np.array([(i % 5) < 3 for i in range(n)])
    pick_a = np.where(right, y == 1, y == 0)
    return winners(np.where(pick_a, 0.6, 0.4), y)


def test_progress_is_graded_fights_over_fights_needed_and_stops_at_one():
    bt = right_sixty_percent(3000)
    needed = ev.compute_all({"v2": bt}, {})[0]["needed"]
    assert 700 < needed < 830                                                                                               # (2.8 x 0.199 / 0.0201) squared, about 765
    half = ev.compute_all({"v2": bt}, {"v2": right_sixty_percent(needed // 2)})[0]
    assert half["n_live"] == needed // 2 and half["progress"] == pytest.approx((needed // 2) / needed) and 0.45 < half["progress"] < 0.55
    assert ev.compute_all({"v2": bt}, {"v2": right_sixty_percent(needed * 2)})[0]["progress"] == 1.0                      # twice the fights needed: the bar is full, not overfull


# ---------------------------------------------------------------------------------------------- the sentences
def E(n, live=None, bt=-0.0065, unit="winner picks"):
    return {"n_live": n, "live": live, "backtest": None if bt is None else {"mean": bt, "sd": 0.14}, "unit": unit, "model": "the market-informed winner pick", "baseline": "the bookmakers' line alone"}


def test_the_sentence_says_only_what_the_numbers_support():
    assert ev.sentence(E(0)) == "No graded fights yet, so there is nothing to show. The bar fills as cards are graded."
    assert ev.sentence(E(9, {"mean": 0, "lo": -1, "hi": 1, "verdict": "cannot be told apart"})) == "Only 9 graded winner picks so far: too few to say anything."
    tie = {"mean": -0.01, "lo": -0.05, "hi": 0.03, "verdict": "cannot be told apart"}
    assert ev.sentence(E(26, tie)) == ("After 26 graded fights, the market-informed winner pick cannot be told apart from the bookmakers' line alone: the difference is -0.0100 per fight, "
                                       "with a 95% range from -0.0500 to +0.0300. That range includes what the backtest found (-0.0065), so nothing here contradicts it.")
    better = {"mean": -0.04, "lo": -0.06, "hi": -0.02, "verdict": "clearly better"}
    s = ev.sentence(E(400, better, bt=-0.065))
    assert s.startswith("So far the market-informed winner pick is clearly better than the bookmakers' line alone: 0.0400 less log loss per fight (95% range 0.0200 to 0.0600 less).")
    assert "does not include what the backtest found (-0.0650)" in s and "about one time in twenty" in s
    worse = {"mean": 0.05, "lo": 0.02, "hi": 0.08, "verdict": "clearly worse"}
    assert "The retirement rule acts only after 300 graded fights, and there are 120." in ev.sentence(E(120, worse))
    assert "That is what the retirement rule is for." in ev.sentence(E(300, worse)) and "clearly worse than the bookmakers' line alone: 0.0500 more log loss per fight (95% range 0.0200 to 0.0800 more)" in ev.sentence(E(300, worse))
    assert "backtest" not in ev.sentence(E(26, tie, bt=None))                                                               # no backtest to compare with: nothing is said about one


def test_the_thresholds_are_the_ones_the_rest_of_the_project_uses():
    assert ev.RETIRE_AFTER_FIGHTS == mh.RETIRE_AFTER_FIGHTS == 300 and ev.MIN_FIGHTS_TO_SAY == 10


def test_the_claims_are_the_four_comparisons_the_health_rules_and_model_cards_name():
    assert [c["model"] for c in ev.CLAIMS] == ["the model-only winner pick", "the market-informed winner pick", "the goes-the-distance prediction", "the market-informed distance prediction"]
    assert [c["baseline"] for c in ev.CLAIMS] == ["a coin flip", "the bookmakers' line alone", "the plain decision rate of the previous three years", "the model-only one"]
    cards = (ROOT / "docs" / "MODEL_CARDS.md").read_text(encoding="utf-8")
    for needle in ("A coin flip", "The bookmakers' opening line alone", "The plain decision rate of the previous three years", "Goes the distance v1"):
        assert needle in cards                                                                                              # the same four comparisons the retirement rules make


# ---------------------------------------------------------------------------------------------- what the page may read
def test_the_meter_reads_only_public_tables():
    app = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    body = app[app.index("def evidence_data()"):app.index("def insights_data()")]
    sql = " ".join(re.findall(r'"(SELECT [^"]+)"', body)) + " " + " ".join(mh.QUERIES.values())
    tables = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_0-9]+)", sql))
    assert {"fight_picks_v3", "fight_distance_v1", "fight_distance_v2", "upcoming_picks_v2", "upcoming_picks_v3", "upcoming_distance_v1", "upcoming_distance_v2"} <= tables
    assert not tables & set(db_roles.PRIVATE_TABLES), tables & set(db_roles.PRIVATE_TABLES)                                # the site's login cannot read those, and the page must not try


def test_a_failure_in_the_meter_can_never_take_the_page_down():
    app = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert re.search(r"try:\s*# the evidence meter[^\n]*\n\s*module_html\(evidence_ui\.section_html\(evidence_data\(\)\)\)\s*\n\s*except Exception:\s*\n\s*pass", app)


def test_the_readme_and_runbook_describe_the_meter_without_quoting_results():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    row = [l for l in readme.splitlines() if l.startswith("| **Evidence meter**")]
    assert len(row) == 1 and "Track record page" in row[0] and "how many graded fights it would take to be reasonably sure" in row[0]
    assert not re.search(r"\d", row[0])                                                                                     # a description, never a result that could go stale
    assert "## The evidence meter" in runbook and "pipeline/evidence.py" in runbook and "app/evidence_ui.py" in runbook and "tests/test_evidence.py" in runbook
    assert "((1.96 + 0.84) x sd / effect) squared" in runbook and "reads no private table" in runbook
