"""app/insights_ui.py: the HTML of the Insights page. Pure functions, so every property is checked on a hand-built set of findings."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "app"):
    sys.path.insert(0, str(ROOT / sub))
import insights as ins  # noqa: E402
import insights_ui as ui  # noqa: E402

r = ins.rate


def findings():
    return {"n_fights": 1000, "n_with_market": 300,
            "method": {"n": 1000, "rows": [{"method": m, **r(k, 1000)} for m, k in (("Decision", 490), ("KO/TKO", 300), ("Submission", 200), ("Other", 10))]},
            "rounds": [{"scheduled": 3, "n": 100, "rounds": [{"round": i, **r(k, 100)} for i, k in ((1, 50), (2, 30), (3, 20))]},
                       {"scheduled": 5, "n": 0, "rounds": [{"round": i, **r(0, 0)} for i in range(1, 6)]}],
            "divisions": [{"division": "Featherweight", **r(55, 100)}, {"division": "A&B <i>x</i>", **r(40, 100)}],
            "years": [{"year": 2014, **r(50, 100)}, {"year": 2015, **r(52, 100)}], "trend": None,
            "favourites": [{"label": "Under 60%", "n": 100, "implied": 0.55, "fav_won": r(55, 100), "decision": r(50, 100)},
                           {"label": "80% or more", "n": 100, "implied": 0.85, "fav_won": r(85, 100), "decision": r(35, 100)}],
            "experience": {"gap": [{"label": "1 to 2 more", **r(50, 100)}, {"label": "11 or more", **r(60, 100)}], "debut": r(10, 20)},
            "edges": {"age": [{"label": "Under 2 years older", **r(50, 100)}], "reach": [{"label": "2.5 to 5 cm longer", **r(55, 100)}]},
            "sentences": {"method": "Of 1,000 fights since 2002, 49% went to a decision.", "rounds": None, "divisions": "Featherweight fights went the distance most often & A&B least.",
                          "trend": None, "favourites": "When the market had a favourite...", "upsets": None, "experience": "Fighters making their UFC debut...", "edges": "The older fighter won..."}}


def nothing():
    d = findings()
    d.update(n_fights=0, n_with_market=0, method={"n": 0, "rows": []}, rounds=[], divisions=[], years=[], trend=None, favourites=[], experience={"gap": [], "debut": r(0, 0)}, edges={"age": [], "reach": []})
    d["sentences"] = {k: None for k in d["sentences"]}
    return d


def test_a_bar_is_clamped_to_the_track_and_only_shows_the_coin_flip_line_when_asked():
    assert "width:62.0%" in ui.bar(0.62) and "width:100.0%" in ui.bar(1.7) and "width:0.0%" in ui.bar(-0.4)
    assert "dashed" not in ui.bar(0.5) and "left:50%" in ui.bar(0.5, even_marker=True)


def test_rows_are_escaped_use_fixed_columns_and_show_the_count_that_was_asked_for():
    rows = [{"label": "<script>alert(1)</script>", **r(30, 120)}]
    h = ui.bars_html(rows, "label", "Head & tail")
    assert "<script>" not in h and "&lt;script&gt;" in h and "Head &amp; tail" in h
    assert 'table-layout:fixed' in h and h.count("<col ") == 4 and "<b>25%</b>" in h and "120" in h and ">30<" not in h          # n by default
    assert ">30<" in ui.bars_html(rows, "label", "H", count="k") and "120" not in ui.bars_html(rows, "label", "H", count="k")      # k when every row shares one group


def test_the_page_has_six_sections_in_order_each_with_its_heading_and_a_note_on_how_to_read_it():
    secs = ui.sections(findings())
    assert len(secs) == 6
    heads = [re.search(r"<h3>(.*?)</h3>", s).group(1) for s in secs]
    assert heads == ["How fights end", "Which divisions go the distance", "Are fights going the distance more or less often?", "Favourites, upsets and finishing",
                     "Does UFC experience matter?", "Age and reach: how much do they matter?"]
    assert all(s.count('<div class="note">') >= 1 for s in secs)


def test_every_row_label_reaches_the_page_as_written_the_regression_for_a_label_that_was_overwritten():
    page = " ".join(re.sub(r"<[^>]+>", " ", s) for s in ui.sections(findings()))
    for label in ("Round 1", "Round 3", "2014", "2015", "Featherweight", "Under 60%", "80% or more", "Under 2 years older", "2.5 to 5 cm longer",
                  "1 to 2 more UFC fights", "11 or more UFC fights"):
        assert label in page, label                                                                                      # the experience rows once lost their "more UFC fights"
    assert "more more" not in page and "11 or more UFC fights" in page                                                       # no doubled word, which an earlier version had
    assert "Round 4" not in page and "5-round fights that ended early" not in page                                         # a group with no fights is not drawn


def test_how_fights_end_and_the_rounds_count_fights_that_ended_that_way_not_the_same_total_on_every_row():
    method = ui.method_html(findings()["method"])
    assert ">490<" in method and ">300<" in method and ">200<" in method and ">1,000<" not in method
    assert ">50<" in ui.rounds_html(findings()["rounds"])


def test_the_sentence_is_shown_in_bold_and_escaped_and_an_empty_one_becomes_a_plain_note():
    secs = ui.sections(findings())
    assert "<b>Featherweight fights went the distance most often &amp; A&amp;B least.</b>" in secs[1]
    assert "A&amp;B &lt;i&gt;x&lt;/i&gt;" in secs[1] and "<i>x</i>" not in secs[1]
    assert ui.NOT_ENOUGH in secs[2] and "<b>" not in secs[2].split("</h3>")[1].split("<table")[0]                          # no trend sentence: a note instead of a bold line


def test_with_no_data_at_all_every_section_still_explains_itself_instead_of_crashing_or_drawing_empty_tables():
    secs = ui.sections(nothing())
    assert len(secs) == 6 and all(ui.NOT_ENOUGH in s for s in secs)
    assert not any("<table" in s for s in secs)
    assert all(len(re.sub(r"<[^>]+>", "", s)) > 120 for s in secs)                                                        # a heading, the note and how to read it: the page is never blank


def test_the_page_shows_groups_of_fights_never_betting_odds():
    page = " ".join(ui.sections(findings()))
    assert not re.search(r"[+-]\d{3}\b|\b\d\.\d{2}x\b|decimal odds|american odds", page, flags=re.I)
    assert page.count("never shows odds") == 1 and "market 55%" in page and "market 85%" in page                              # only the average price of a whole group, as a percentage


@pytest.mark.parametrize("value", [0.0, 0.004, 0.5, 0.999, 1.0])
def test_bars_never_overflow_their_track(value):
    w = float(re.search(r"width:([\d.]+)%", ui.bar(value)).group(1))
    assert 0 <= w <= 100


def test_the_readme_runbook_and_demo_describe_the_insights_page_without_quoting_results():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    row = [l for l in readme.splitlines() if l.startswith("| **Insights page**")]
    assert len(row) == 1 and "95% range" in row[0] and "silent where there are too few fights" in row[0]
    assert not re.search(r"\d+%", row[0].replace("95% range", ""))                                                      # a description, never a result that could go stale (95% is the confidence level)
    assert "![The demo's Insights page, with invented data](docs/images/demo-insights.png)" in readme
    assert "## The Insights page" in runbook and "pipeline/insights.py" in runbook and "Not enough fights yet to say anything here" in runbook
    assert "Track record, Insights and Game" in (ROOT / "demo" / "README.md").read_text(encoding="utf-8")


def test_the_note_about_how_the_margin_is_removed_is_true_of_the_code_that_removes_it():
    import model_v3
    imp = lambda american: 100 / (american + 100) if american > 0 else -american / (-american + 100)                      # an American price as an implied chance
    for a, b in ((-300, 250), (-150, 130), (110, -130), (-1000, 600)):
        assert model_v3.devig(a, b) == pytest.approx(imp(a) / (imp(a) + imp(b)))                                          # divided out evenly across both sides: the note says exactly that
    favourites = ui.sections(findings())[3]
    assert "divided out evenly across both fighters" in favourites and "does not by itself mean the line was wrong" in favourites
    assert "beside the age pattern below" in ui.sections(findings())[4]
