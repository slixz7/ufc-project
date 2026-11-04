"""
pipeline/insights.py: the findings behind the website's Insights page. Every test builds a small frame whose exact answer can be checked by hand.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import insights as ins  # noqa: E402
from small_sample import wilson  # noqa: E402


def fights_frame():
    rows = [("A", "2001-12-31", "Lightweight", 3, "Decision - Unanimous", 3),    # before 2002: left out
            ("B", "2002-01-01", "Lightweight", 3, "Decision - Unanimous", 3),
            ("C", "2010-05-01", "Heavyweight", 5, "KO/TKO", 2),
            ("D", "2011-05-01", "Lightweight", 3, "DQ", 1),                       # a disqualification: left out
            ("E", "2012-05-01", "Lightweight", 4, "Decision - Split", 4),         # 4 scheduled rounds: left out
            ("F", "2013-05-01", "Lightweight", 3, "Submission", 1),
            ("G", "2014-05-01", "Heavyweight", 3, "TKO - Doctor's Stoppage", 1),
            ("H", "2015-05-01", "Lightweight", 3, "Overturned", 2),
            ("I", "2016-05-01", "Lightweight", 3, None, None),                    # no result: left out
            ("J", "2017-05-01", "Heavyweight", 3, "Decision - Majority", 3)]
    return pd.DataFrame(rows, columns=["fight_url", "event_date", "weight_class", "num_rounds", "result", "finish_round"])


# ---------------------------------------------------------------------------------------------- which fights count, and how
def test_the_fights_that_count_are_exactly_the_ones_the_distance_model_counts():
    original = fights_frame()
    before = original.copy(deep=True)
    f = ins.prepare_fights(original)
    assert list(f.fight_url) == ["B", "C", "F", "G", "H", "J"]
    assert list(f.went_distance) == [1, 0, 0, 0, 0, 1]
    assert list(f.method) == ["Decision", "KO/TKO", "Submission", "KO/TKO", "Other", "Decision"]                      # a doctor's stoppage is a TKO; "Overturned" is neither
    pd.testing.assert_frame_equal(original, before)                                                                    # the input is never changed


def test_the_definitions_are_the_distance_models_own():
    import model_distance as md
    assert ins.ERA_START == md.ERA_START and tuple(ins.FINISH_RESULTS) == tuple(md.FINISH)
    f = fights_frame()
    f = f[(f.num_rounds.isin([3, 5])) & (pd.to_datetime(f.event_date) >= md.ERA_START) & f.result.notna()].copy()
    flags = md._flags(f.assign(won=1, dur_sec=300.0))
    kept = ins.prepare_fights(fights_frame())
    assert list(flags[flags.elig == 1].dec) == list(kept.went_distance)                                                # the same fights, the same "went the distance"
    assert list(flags[flags.elig == 0].fight_url) == ["D"]                                                             # and the same one thrown out


def test_how_fights_end_adds_up_and_counts_each_way():
    s = ins.method_shares(ins.prepare_fights(fights_frame()))
    assert s["n"] == 6 and {r["method"]: r["k"] for r in s["rows"]} == {"Decision": 2, "KO/TKO": 2, "Submission": 1, "Other": 1}
    assert sum(r["rate"] for r in s["rows"]) == pytest.approx(1.0)
    assert s["rows"][0]["lo"] == pytest.approx(wilson(2, 6)[0]) and s["rows"][0]["hi"] == pytest.approx(wilson(2, 6)[1])


def test_the_round_a_fight_ends_in_is_counted_separately_for_three_and_five_round_fights():
    three, five = ins.finish_round_shares(ins.prepare_fights(fights_frame()))
    assert three["scheduled"] == 3 and three["n"] == 2 and [r["k"] for r in three["rounds"]] == [2, 0, 0]              # F (submission, R1) and G (doctor's stoppage, R1)
    assert five["scheduled"] == 5 and five["n"] == 1 and [r["k"] for r in five["rounds"]] == [0, 1, 0, 0, 0]           # C, round 2


# ---------------------------------------------------------------------------------------------- divisions and years
def _division_frame():
    rows = [("Lightweight", 1)] * 6 + [("Lightweight", 0)] * 4 + [("Heavyweight", 1)] * 2 + [("Heavyweight", 0)] * 8 + [("Flyweight", 1)] * 5
    f = pd.DataFrame(rows, columns=["weight_class", "went_distance"])
    f["fight_url"] = [f"u{i}" for i in range(len(f))]
    return f


def test_divisions_are_ranked_by_how_often_they_go_the_distance_and_small_ones_are_left_out():
    rows = ins.division_rates(_division_frame(), min_fights=10)
    assert [r["division"] for r in rows] == ["Lightweight", "Heavyweight"]                                             # Flyweight has 5 fights
    assert [(r["n"], r["k"], r["rate"]) for r in rows] == [(10, 6, 0.6), (10, 2, 0.2)]
    assert (rows[0]["lo"], rows[0]["hi"]) == pytest.approx(wilson(6, 10))
    assert [r["division"] for r in ins.division_rates(_division_frame(), min_fights=5)] == ["Flyweight", "Lightweight", "Heavyweight"]


def _year_frame(rates, n=100, start=2010):
    rows = []
    for i, p in enumerate(rates):
        k = round(p * n)
        rows += [{"year": start + i, "went_distance": 1}] * k + [{"year": start + i, "went_distance": 0}] * (n - k)
    return pd.DataFrame(rows)


def test_years_with_too_few_fights_are_left_out_and_the_trend_is_only_called_when_it_is_bigger_than_chance():
    rows = ins.yearly_rates(_year_frame([.4, .4, .4, .6, .6, .6]), min_fights=100)
    assert [r["year"] for r in rows] == list(range(2010, 2016)) and rows[0]["rate"] == 0.4
    assert len(ins.yearly_rates(_year_frame([.4] * 5 + [.5], n=99), min_fights=100)) == 0                              # 99 fights a year is not enough
    t = ins.trend_summary(rows)
    assert t["verdict"] == "rose" and (t["early_years"], t["late_years"]) == ((2010, 2012), (2013, 2015)) and t["early"] == pytest.approx(.4) and t["late"] == pytest.approx(.6)
    assert ins.trend_summary(ins.yearly_rates(_year_frame([.6, .6, .6, .4, .4, .4])))["verdict"] == "fell"
    assert ins.trend_summary(ins.yearly_rates(_year_frame([.5, .5, .5, .5, .5, .5])))["verdict"] == "no clear change"
    assert ins.trend_summary(ins.yearly_rates(_year_frame([.5, .5, .5, .5, .5])), span=3) is None                       # five years is not enough for 3 against 3
    small = ins.trend_summary(ins.yearly_rates(_year_frame([.50, .50, .50, .53, .53, .53], n=100)))
    assert small["verdict"] == "no clear change"                                                                       # 3 points on 300 fights each is within chance


REAL_YEARS = {2006: (.33, 158), 2007: (.36, 168), 2008: (.32, 201), 2009: (.43, 212), 2010: (.49, 249), 2011: (.49, 295), 2012: (.47, 331), 2013: (.47, 375), 2014: (.50, 493),
              2015: (.47, 462), 2016: (.50, 483), 2017: (.49, 445), 2018: (.49, 468), 2019: (.54, 505), 2020: (.50, 442), 2021: (.51, 495), 2022: (.47, 505), 2023: (.48, 503),
              2024: (.55, 511), 2025: (.49, 515), 2026: (.42, 473)}                                                          # the live page's yearly table, as first published


def test_a_rise_that_happened_long_ago_is_not_reported_as_a_current_trend_the_real_yearly_table():
    rows = [{"year": y, **ins.rate(round(p * n), n)} for y, (p, n) in REAL_YEARS.items()]
    t = ins.trend_summary(rows)
    assert t["verdict"] == "rose" and t["recent"]["verdict"] == "no clear change" and (t["recent"]["early_years"], t["recent"]["late_years"]) == ((2017, 2019), (2024, 2026))
    assert (round(t["recent"]["lowest"], 2), round(t["recent"]["highest"], 2)) == (0.42, 0.55) and t["recent"]["n_years"] == 10
    s = ins.sentence_trend(t)
    assert "the share rose, by more than chance alone would give." in s and "Over the last 10 years the yearly share stayed between 42% and 55%, with no clear change between 2017 to 2019 and 2024 to 2026." in s


def test_the_recent_view_appears_only_when_there_are_more_years_than_it_covers_and_can_say_rose_or_fell():
    flat = ins.yearly_rates(_year_frame([.5] * 10), min_fights=100)
    assert "recent" not in ins.trend_summary(flat)                                                                        # ten years in all: the recent decade is the whole span
    assert "recent" not in ins.trend_summary(ins.yearly_rates(_year_frame([.4, .4, .4, .6, .6, .6]), min_fights=100))
    rising = ins.trend_summary(ins.yearly_rates(_year_frame([.40] * 8 + [.40, .40, .40, .60, .60, .60]), min_fights=100))     # 14 years, 2010 to 2023: the last ten are 2014 to 2023
    assert rising["recent"]["verdict"] == "rose" and rising["recent"]["n_years"] == 10
    assert "and it rose from 40% in 2014 to 2016 to 60% in 2021 to 2023." in ins.sentence_trend(rising)
    falling = ins.trend_summary(ins.yearly_rates(_year_frame([.60] * 11 + [.40, .40, .40]), min_fights=100))              # 60% every year to 2020, then 40% for 2021 to 2023
    assert falling["recent"]["verdict"] == "fell" and "and it fell from 60% in 2014 to 2016 to 40% in 2021 to 2023." in ins.sentence_trend(falling)


# ---------------------------------------------------------------------------------------------- favourites and upsets
def _picks_and_fights():
    picks, fights = [], []

    def add(i, m, winner, decision):
        picks.append({"fight_url": f"p{i}", "fighter_a": "A", "fighter_b": "B", "market_prob_a": m, "actual_winner": winner})
        fights.append({"fight_url": f"p{i}", "went_distance": decision})
    for i in range(10):                                                                                   # a clear favourite: A at 85%, wins 8 of 10, 3 of 10 go the distance
        add(i, 0.85, "A" if i < 8 else "B", 1 if i < 3 else 0)
    for i in range(10, 20):                                                                               # a narrow favourite: B at 55% (A at 45%), B wins 6 of 10, 5 of 10 go the distance
        add(i, 0.45, "B" if i < 16 else "A", 1 if i < 15 else 0)
    add(20, 0.50, "A", 1)                                                                                 # exactly even: no favourite
    add(21, 0.90, "Draw", 1)                                                                              # no winner
    add(22, None, "A", 1)                                                                                 # no market price
    picks.append({"fight_url": "not-eligible", "fighter_a": "A", "fighter_b": "B", "market_prob_a": 0.9, "actual_winner": "A"})   # not among the eligible fights
    return pd.DataFrame(picks), pd.DataFrame(fights)


def test_favourites_are_grouped_by_the_markets_price_and_only_real_comparable_fights_count():
    picks, f = _picks_and_fights()
    rows = ins.favourite_buckets(f, picks)
    assert [r["label"] for r in rows] == ["Under 60%", "80% or more"] and [r["n"] for r in rows] == [10, 10]
    low, high = rows
    assert (low["fav_won"]["k"], low["decision"]["k"], low["implied"]) == (6, 5, pytest.approx(0.55))                  # B was the 55% favourite and won 6 of 10
    assert (high["fav_won"]["k"], high["decision"]["k"], high["implied"]) == (8, 3, pytest.approx(0.85))
    assert (high["fav_won"]["lo"], high["fav_won"]["hi"]) == pytest.approx(wilson(8, 10))


@pytest.mark.parametrize("m, label", [(0.55, "Under 60%"), (0.60, "60% to 70%"), (0.40, "60% to 70%"), (0.69, "60% to 70%"), (0.70, "70% to 80%"), (0.80, "80% or more"), (0.20, "80% or more"), (0.97, "80% or more")])
def test_the_bucket_edges_are_inclusive_at_the_bottom_and_work_whichever_side_the_favourite_is_on(m, label):
    picks = pd.DataFrame([{"fight_url": "x", "fighter_a": "A", "fighter_b": "B", "market_prob_a": m, "actual_winner": "A"}])
    rows = ins.favourite_buckets(pd.DataFrame([{"fight_url": "x", "went_distance": 0}]), picks)
    assert [r["label"] for r in rows] == [label]


# ---------------------------------------------------------------------------------------------- pairs, experience, age and reach
def _fighter_rows():
    def row(f, who, side, won, dob, reach):
        return {"fight_url": f, "fighter_url": who, "side": side, "won": won, "dob": dob, "reach_cm": reach}
    rows = [row("f1", "x", 1, 1, "1990-01-01", 180.0), row("f1", "y", 2, 0, "1985-01-01", 175.0),
            row("f2", "x", 2, 0, "1990-01-01", 180.0), row("f2", "z", 1, 1, "1980-01-01", None),
            row("f3", "x", 1, 1, "1990-01-01", 180.0), row("f3", "y", 2, 0, "1985-01-01", 175.0),
            row("f4", "x", 1, 0, "1990-01-01", 180.0), row("f4", "y", 2, 0, "1985-01-01", 175.0),            # a draw: both lost
            row("f5", "x", 1, 1, "1990-01-01", 180.0)]                                                        # only one fighter on record
    fights = pd.DataFrame({"fight_url": ["f1", "f2", "f3", "f4", "f5"], "event_date": ["2010-01-01", "2011-01-01", "2012-01-01", "2013-01-01", "2014-01-01"]})
    return pd.DataFrame(rows), fights


def test_pairs_put_both_fighters_side_by_side_count_earlier_fights_and_drop_draws_and_lone_rows():
    rows, fights = _fighter_rows()
    pairs = ins.build_pairs(rows, fights).sort_values("fight_url").reset_index(drop=True)
    assert list(pairs.fight_url) == ["f1", "f2", "f3"]                                                                  # f4 (a draw) and f5 (one fighter) are gone
    assert list(pairs.a_won) == [1, 1, 1]
    assert list(pairs.prior_a) == [0, 0, 2] and list(pairs.prior_b) == [0, 1, 1]                                        # f1: x's 1st fight and y's 1st; f2: z's 1st and x's 2nd; f3: x's 3rd and y's 2nd
    f2 = pairs[pairs.fight_url == "f2"].iloc[0]
    assert f2.prior_b == 1 and f2.prior_a == 0                                                                          # side 2 is x, who had 1 earlier fight; z (side 1) had none
    f1 = pairs[pairs.fight_url == "f1"].iloc[0]
    assert f1.age_a == pytest.approx(20.0, abs=0.05) and f1.age_b == pytest.approx(25.0, abs=0.05) and f1.reach_cm_a == 180 and f1.reach_cm_b == 175
    assert np.isnan(f2.reach_cm_a)                                                                                      # a missing reach stays missing


def _pairs(rows, date="2012-01-01"):
    d = pd.DataFrame(rows, columns=["a_won", "prior_a", "prior_b", "age_a", "age_b", "reach_cm_a", "reach_cm_b"])
    d["event_date"] = pd.Timestamp(date)
    d["fight_url"] = [f"q{i}" for i in range(len(d))]
    return d


def test_experience_compares_the_more_experienced_fighter_whichever_side_they_are_on_and_starts_in_2005():
    rows = [(1, 5, 4, 30, 30, 0, 0)] * 3 + [(0, 5, 4, 30, 30, 0, 0)] * 1 + [(0, 4, 5, 30, 30, 0, 0)] * 3 + [(1, 4, 5, 30, 30, 0, 0)] * 1   # a gap of 1: the more experienced fighter wins 3 + 3 of 8
    rows += [(1, 20, 5, 30, 30, 0, 0)] * 2                                                                                               # a gap of 15
    rows += [(1, 7, 7, 30, 30, 0, 0)]                                                                                                    # no gap: left out
    out = ins.experience_rows(_pairs(rows))
    assert [(r["label"], r["n"], r["k"]) for r in out["gap"]] == [("1 to 2 more", 8, 6), ("11 or more", 2, 2)]
    assert ins.experience_rows(_pairs(rows, date="2004-12-31"))["gap"] == []                                                            # earlier fighters' histories are cut short by the data


def test_debutants_against_experienced_fighters_are_counted_from_either_side():
    rows = [(1, 0, 3, 30, 30, 0, 0)] + [(0, 0, 3, 30, 30, 0, 0)] * 2 + [(0, 6, 0, 30, 30, 0, 0)] + [(1, 0, 2, 30, 30, 0, 0)] + [(1, 0, 0, 30, 30, 0, 0)]
    # debutant on side A: won 1 of 3. Debutant on side B (a_won == 0 means B won): won 1 of 1. So 2 of 4. A 0-vs-2 and a 0-vs-0 fight are not "against 3+".
    d = ins.experience_rows(_pairs(rows))["debut"]
    assert (d["n"], d["k"]) == (4, 2) and d["rate"] == 0.5


def test_experience_also_measures_how_often_the_more_experienced_fighter_was_the_older_one():
    #       a_won prior_a prior_b age_a age_b reach_a reach_b      the gap is 15 for all four; the more experienced fighter is A, then B, then A, then A
    rows = [(1, 20, 5, 35, 28, 0, 0),      # A more experienced and older
            (0, 5, 20, 26, 33, 0, 0),      # B more experienced and older
            (1, 20, 5, 29, 31, 0, 0),      # A more experienced but YOUNGER
            (0, 20, 5, 36, 30, 0, 0)]      # A more experienced and older
    big = ins.experience_rows(_pairs(rows))["gap"][-1]
    assert big["label"] == "11 or more" and big["n"] == 4 and (big["also_older"]["k"], big["also_older"]["n"]) == (3, 4)
    unknown = ins.experience_rows(_pairs(rows + [(1, 20, 5, np.nan, 30, 0, 0)]))["gap"][-1]
    assert unknown["n"] == 5 and unknown["also_older"]["n"] == 4                                                           # an unknown age is left out of the overlap, not counted as "no"


def test_age_and_reach_edges_use_the_older_and_the_longer_fighter_with_exact_boundaries_and_no_ties():
    #       a_won prior_a prior_b age_a age_b reach_a reach_b
    rows = [(1, 0, 0, 32, 30, 0, 0),        # a is older by exactly 2 -> "2 to 5": older wins
            (0, 0, 0, 30, 35, 0, 0),        # b is older by exactly 5 -> "5 to 8": older (b) wins
            (1, 0, 0, 28, 38, 0, 0),        # b is older by 10 -> "8 or more": older (b) LOST
            (1, 0, 0, 31, 31, 0, 0),        # exact tie in age: left out
            (1, 0, 0, np.nan, 30, 0, 0)]    # age unknown: left out
    out = ins.edge_rows(_pairs(rows))
    assert [(r["label"], r["n"], r["k"]) for r in out["age"]] == [("2 to 5 years older", 1, 1), ("5 to 8 years older", 1, 1), ("8 or more years older", 1, 0)]
    inch = lambda n: n * 2.54                                                                                                # reach is whole inches converted to centimetres (the real column's values are 177.80, 180.34, 182.88 ...)
    reach = [(1, 0, 0, 30, 30, inch(71), inch(70)),     # a longer by 1 inch (2.5400000000000205 cm after float noise): "1 inch longer": a wins
             (0, 0, 0, 30, 30, inch(70), inch(74)),     # b longer by 4 inches: "4 to 5 inches longer": b (the longer) wins
             (1, 0, 0, 30, 30, inch(76), inch(70)),     # a longer by 6 inches: "6 or more inches longer": a wins
             (0, 0, 0, 30, 30, inch(72), inch(69)),     # a longer by 3 inches: "2 to 3 inches longer": a (the longer) LOST
             (1, 0, 0, 30, 30, inch(70), inch(70)),     # tie: left out
             (1, 0, 0, 30, 30, 177.8, 178.8),           # 1 cm (0.39 inch) apart: under half an inch, so not a gap: left out
             (1, 0, 0, 30, 30, np.nan, 180.0)]          # unknown: left out
    out = ins.edge_rows(_pairs(reach))
    assert [(r["label"], r["n"], r["k"]) for r in out["reach"]] == [("1 inch longer", 1, 1), ("2 to 3 inches longer", 1, 0), ("4 to 5 inches longer", 1, 1), ("6 or more inches longer", 1, 1)]


def test_every_whole_inch_gap_lands_in_its_bucket_however_the_float_arithmetic_rounds():
    expected = {1: "1 inch longer", 2: "2 to 3 inches longer", 3: "2 to 3 inches longer", 4: "4 to 5 inches longer", 5: "4 to 5 inches longer", 6: "6 or more inches longer", 7: "6 or more inches longer",
                9: "6 or more inches longer"}
    for gap, label in expected.items():
        for base in (68, 70, 72, 75):                                                                                       # different starting reaches give different float noise
            rows = [(1, 0, 0, 30, 30, (base + gap) * 2.54, base * 2.54)]
            assert [r["label"] for r in ins.edge_rows(_pairs(rows))["reach"]] == [label], (gap, base)
    assert ins.REACH_LABELS == ["1 inch longer", "2 to 3 inches longer", "4 to 5 inches longer", "6 or more inches longer"] and ins.INCH_CM == 2.54


# ---------------------------------------------------------------------------------------------- the sentences
def r(n, k):
    return ins.rate(k, n)


def test_the_sentences_state_what_the_numbers_show_and_nothing_more():
    s = ins.method_shares(ins.prepare_fights(fights_frame()))
    s["n"] = 100
    s["rows"] = [{"method": m, **ins.rate(k, 100)} for m, k in (("Decision", 49), ("KO/TKO", 30), ("Submission", 20), ("Other", 1))]
    assert ins.sentence_how_fights_end(s) == "Of 100 fights since 2002, 49% went to a decision, 30% ended in a knockout or TKO and 20% in a submission."
    div = [{"division": "Flyweight", **r(200, 110)}, {"division": "Heavyweight", **r(300, 120)}]
    assert ins.sentence_divisions(div) == "Flyweight fights went the distance most often (55% of 200) and Heavyweight least often (40% of 300): a spread of 15 points between divisions."
    assert ins.sentence_divisions(div[:1]) is None
    t = {"early_years": (2010, 2012), "late_years": (2014, 2016), "early": 0.5, "late": 0.45, "early_n": 900, "late_n": 800, "verdict": "no clear change"}
    assert ins.sentence_trend(t) == "50% of fights went the distance in 2010 to 2012 (900 fights) and 45% in 2014 to 2016 (800): a difference small enough that chance alone could produce it."
    assert ins.sentence_trend({**t, "verdict": "fell"}).endswith("the share fell, by more than chance alone would give.") and ins.sentence_trend(None) is None


def test_the_favourites_and_upsets_sentences_use_the_extreme_groups_and_check_the_markets_price_against_what_happened():
    rows = [{"label": "Under 60%", "n": 400, "implied": 0.55, "fav_won": r(400, 232), "decision": r(400, 220)},
            {"label": "80% or more", "n": 300, "implied": 0.85, "fav_won": r(300, 255), "decision": r(300, 108)}]
    assert ins.sentence_favourites(rows) == ("When the market had a favourite at 80% or more (300 fights), that favourite won 85% (the market implied 85%) and 36% of those fights "
                                             "went the distance. Where the favourite was under 60% (400 fights), 55% did.")
    up = ins.sentence_upsets(rows)
    assert up.startswith("Even in the 80% or more group, the underdog won 15% of the time, about one fight in 7.") and "is consistent with what happened" in up
    off = [{**rows[1], "fav_won": r(300, 210)}]                                                                         # the favourite really won 70%, not 85%
    mismatch = ins.sentence_upsets([rows[0], off[0]])
    assert "The market implied 85% for the favourite; the real figure was 70%." in mismatch and "does not mean the betting line was wrong" in mismatch
    assert ins.sentence_favourites([{**rows[0], "n": 10}, rows[1]]) is None                                               # a group of 10 is shown, never quoted
    assert ins.sentence_upsets([{**rows[1], "n": 10}]) is None


def test_the_experience_and_edge_sentences_say_about_even_when_the_range_includes_a_coin_flip():
    e = {"gap": [{"label": "11 or more", **r(100, 52)}], "debut": r(200, 90)}                                           # 45% of 200: the range is 38% to 52%, which includes a coin flip
    assert ins.sentence_experience(e) == ("Fighters making their UFC debut against someone with 3 or more UFC fights won 45% of 200 (about even: the range includes 50%). "
                                          "The fighter with an edge of 11 or more UFC fights won 52% of 100 (about even: the range includes 50%).")
    withage = ins.sentence_experience({"gap": [{"label": "11 or more", **r(100, 40), "also_older": r(100, 78)}], "debut": r(5, 1)})
    assert withage == ("The fighter with an edge of 11 or more UFC fights won 40% of 100 (less often than not). "
                       "In 78% of those fights the more experienced fighter was also the older one, so this overlaps with the age pattern below.")
    assert "overlaps" not in ins.sentence_experience({"gap": [{"label": "11 or more", **r(100, 40), "also_older": r(100, 50)}], "debut": r(5, 1)})      # no overlap claimed at 50%
    assert "also the older" not in ins.sentence_experience({"gap": [{"label": "11 or more", **r(100, 40), "also_older": r(10, 9)}], "debut": r(5, 1)})   # too few known ages to quote
    clear = ins.sentence_experience({"gap": [{"label": "11 or more", **r(1000, 400)}], "debut": r(1000, 400)})
    assert clear.count("less often than not") == 2                                                                      # 40% of 1,000 is clearly below a coin flip
    assert ins.sentence_experience({"gap": [{"label": "11 or more", **r(10, 9)}], "debut": r(5, 1)}) is None
    ed = {"age": [{"label": "8 or more years older", **r(400, 164)}], "reach": [{"label": "6 or more inches longer", **r(350, 196)}]}
    assert ins.sentence_edges(ed) == ("The older fighter won 41% of 400 fights where the gap was 8 or more years older (less often than not). "
                                      "The fighter with the longer reach won 56% of 350 fights where the gap was 6 or more inches longer (more often than not).")
    assert ins.sentence_edges({"age": [], "reach": []}) is None
    assert ins.vs_even(r(1000, 540)) == "more often than not" and ins.vs_even(r(1000, 460)) == "less often than not"           # 54% and 46% of 1,000: ranges clear of 50%
    assert ins.vs_even(r(1000, 520)) == ins.vs_even(r(1000, 480)) == "about even: the range includes 50%"                    # 52% and 48% of 1,000 are not
    assert ins.vs_even(r(1000, 500)) == "about even: the range includes 50%"


def test_everything_is_silent_on_empty_input_not_a_crash():
    f = ins.prepare_fights(fights_frame().iloc[0:0])
    assert ins.method_shares(f)["n"] == 0 and ins.sentence_how_fights_end(ins.method_shares(f)) is None
    assert ins.division_rates(f) == [] and ins.yearly_rates(f) == [] and ins.trend_summary([]) is None
    assert ins.favourite_buckets(pd.DataFrame(columns=["fight_url", "went_distance"]), pd.DataFrame(columns=["fight_url", "fighter_a", "fighter_b", "market_prob_a", "actual_winner"])) == []
    none = pd.DataFrame(columns=["a_won", "prior_a", "prior_b", "age_a", "age_b", "reach_cm_a", "reach_cm_b", "event_date", "fight_url"])
    none["event_date"] = pd.to_datetime(none["event_date"])
    assert ins.experience_rows(none)["gap"] == [] and ins.experience_rows(none)["debut"]["n"] == 0
    assert ins.edge_rows(none) == {"age": [], "reach": []}
