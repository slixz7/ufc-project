"""app/distance_why_ui.py: the "Why this distance prediction?" panel. Pure functions that return HTML, so every property is checked on hand-built breakdowns (bar widths worked out by hand)."""
import html
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "app"):
    sys.path.insert(0, str(ROOT / sub))
import distance_reasons as dr  # noqa: E402
import distance_why_ui as ui  # noqa: E402

APP = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")


def d1(**kw):
    d = {"schema": 1, "kind": "d1", "a": "Silva Santos", "b": "Jon Jones", "rounds": 3, "intercept": -0.33, "base": 0.43, "total": 0.19, "p": 0.55, "rest": -0.06,
         "factors": [{"key": "decisions", "label": "Going to a decision", "c": 0.66, "toward": "decision", "detail": "Earlier fights that went to a decision: Santos 8 of 9, Jones 6 of 7", "net": False},
                     {"key": "pace", "label": "How long fights last", "c": -0.21, "toward": "finish", "detail": "Average share of the scheduled time their earlier fights used: Santos 90%, Jones 79%", "net": True}]}
    d.update(kw)
    return d


def d2(**kw):
    d = {"schema": 1, "kind": "d2", "a": "Silva Santos", "b": "Jon Jones", "p_v1": 0.45, "fav_prob": 0.73, "typical_fav": 0.66, "p_typical": 0.43, "p_v2": 0.41, "line_effect": -0.02, "rest_effect": -0.02}
    d.update(kw)
    return d


def rows_of(h: str) -> list:
    """Each factor row: it starts at its own div and ends after its label span and the two divs that close it."""
    return re.findall(r'<div class="fr(?: rest)?">.*?</span></div></div>', h)


def text(h: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))).strip()


# ---------------------------------------------------------------------------------------------- the summary and the bars
def test_the_summary_compares_an_average_fight_with_this_one_and_adds_the_line_only_when_there_is_one():
    h = ui.why_html(d1())
    assert "<summary>Why this distance prediction?</summary>" in h and "<details>" in h
    assert [(k, v) for k, v in re.findall(r'<span class="k">([^<]+)</span><b>(\d+%)</b>', h)] == [("Average fight", "43%"), ("This fight, model only", "55%")]
    assert "With the betting line" not in h and "The betting line makes" not in h
    both = ui.why_html(d1(), d2())
    assert re.findall(r'<span class="k">([^<]+)</span><b>(\d+%)</b>', both) == [("Average fight", "43%"), ("This fight, model only", "55%"), ("With the betting line", "41%")]


def test_each_factor_is_a_row_with_a_bar_toward_a_decision_from_the_left_or_a_finish_from_the_right():
    rows = rows_of(ui.why_html(d1(rest=0.0)))                                                                                   # no "rest" row: just the two factors
    assert len(rows) == 2
    assert 'class="fb a"' in rows[0] and "Goes the distance" in rows[0] and 'width:100%' in rows[0]                           # 0.66 is the longest bar: the track is max(0.66, 0.35)
    assert 'class="fb b"' in rows[1] and "Ends early" in rows[1] and 'width:32%' in rows[1]                                    # 0.21 / 0.66 = 32%
    assert "Going to a decision" in rows[0] and "Santos 8 of 9, Jones 6 of 7" in rows[0]
    assert ui.NET_NOTE in html.unescape(rows[1]) and ui.NET_NOTE not in html.unescape(rows[0])                                 # only the flagged factor says "net effect"


def test_when_every_bar_is_small_the_track_does_not_stretch_a_tiny_push_to_look_big():
    small = d1(factors=[{"key": "title", "label": "Title fight", "c": 0.07, "toward": "decision", "detail": "A title fight", "net": False}], rest=0.0)
    assert "width:20%" in ui.why_html(small)                                                                                  # 0.07 / 0.35: the same scale the winner panel uses


def test_small_factors_added_together_appear_as_one_row_only_when_they_add_up_to_something():
    assert "All the rest" in ui.why_html(d1(rest=-0.06)) and "All the rest" not in ui.why_html(d1(rest=0.04)) and "All the rest" not in ui.why_html(d1(rest=-0.049))
    row = rows_of(ui.why_html(d1(rest=-0.06)))[-1]
    assert 'class="fb b"' in row and "Ends early" in row and "the smaller factors added together" in row and "width:9%" in row   # 0.06 / 0.66
    assert 'class="fb a"' in rows_of(ui.why_html(d1(rest=+0.2)))[-1]


def test_a_breakdown_with_no_factors_still_says_what_it_can():
    h = ui.why_html(d1(factors=[], rest=0.0))
    assert "Average fight" in h and "What moves the model-only estimate" not in h and ui.FOOTNOTE in html.unescape(h)


def test_the_panel_is_closed_unless_asked_to_be_open():
    assert "<details>" in ui.why_html(d1()) and "<details open>" in ui.why_html(d1(), open_=True)


# ---------------------------------------------------------------------------------------------- the betting-line sentence
def test_the_line_sentence_uses_only_the_stored_numbers():
    assert ui.line_sentence(d2()) == ("The betting line makes the favourite 73% (a typical fight in the model's history: 66%). With it the estimate goes from 45% to 41%. "
                                      "The favourite's strength moves it about 2 points lower; the rest of the change is the second stage re-weighting the model-only number (about 2 points lower).")
    up = ui.line_sentence(d2(p_v1=0.51, p_v2=0.57, fav_prob=0.55, typical_fav=0.69, line_effect=0.06, rest_effect=0.004))
    assert "from 51% to 57%" in up and "moves it about 6 points higher" in up and "re-weighting the model-only number (less than a point)" in up
    assert ui.line_sentence(d2(line_effect=0.003, rest_effect=-0.002)).endswith("With it the estimate goes from 45% to 41%: the line changes almost nothing here.")


@pytest.mark.parametrize("x, words", [(0.0, "less than a point"), (0.0049, "less than a point"), (0.005, "about 1 point higher"), (0.0149, "about 1 point higher"), (0.015, "about 2 points higher"),
                                      (-0.014, "about 1 point lower"), (-0.0349, "about 3 points lower"), (0.12, "about 12 points higher")])
def test_a_change_is_said_in_whole_points_with_half_a_point_rounding_up(x, words):
    assert ui._points(x) == words


# ---------------------------------------------------------------------------------------------- never break a card
@pytest.mark.parametrize("bad", [None, {}, [], "text", 5, d1(schema=2), d1(kind="d2"), d1(kind=None), {k: v for k, v in d1().items() if k != "base"}, d1(base="not a number"),
                                 d1(factors=[{"label": "x"}]), d1(factors="oops"), d1(factors=[{"key": "x", "label": "x", "c": "NaNx", "toward": "decision", "detail": "d", "net": False}])])
def test_a_missing_or_damaged_breakdown_gives_nothing_rather_than_an_error(bad):
    assert ui.why_html(bad) == "" and ui.why_html(bad, d2()) == ""


@pytest.mark.parametrize("bad", [None, {}, "x", d2(schema=0), d2(kind="d1"), {k: v for k, v in d2().items() if k != "typical_fav"}, d2(line_effect=None), d2(p_v2="n/a")])
def test_a_damaged_betting_line_comparison_is_left_out_and_the_rest_of_the_panel_stands(bad):
    h = ui.why_html(d1(), bad)
    assert h == ui.why_html(d1()) and "With the betting line" not in h and "The betting line makes" not in h and "Going to a decision" in h


def test_nothing_is_ever_spliced_in_unescaped():
    nasty = d1(factors=[{"key": "x", "label": "<b>Label</b>", "c": 0.3, "toward": "decision", "detail": '<script>alert("x")</script> & more', "net": False}])
    h = ui.why_html(nasty, d2())
    assert "<script>" not in h and "<b>Label</b>" not in h and "&lt;script&gt;" in h and "&lt;b&gt;Label&lt;/b&gt;" in h


# ---------------------------------------------------------------------------------------------- it fits the site as it is
def test_the_panel_uses_only_styles_the_winner_panel_already_uses_so_no_new_css_is_needed():
    winner = APP[APP.index("def why_html(r, open_: bool = False)"):APP.index("def figs(items)")]
    used = lambda s: set(t for c in re.findall(r'class=\\?"([^"\\]+)\\?"', s) for t in c.split())
    mine = used(ui.why_html(d1(rest=-0.2), d2()))
    assert mine and mine - {"a", "b"} <= used(winner) | {"rest"}, mine - used(winner)
    assert ".ufc .fb.a" in APP and ".fb.b" in APP                                                                              # the two bar directions (set inside an f-string in the winner panel) exist in the stylesheet
    assert not re.search(r"(?<!max-)width:\d+px", ui.why_html(d1(), d2()))                                                     # nothing fixed in pixels: it reflows on a phone


def test_no_betting_odds_appear_only_probabilities_as_the_winner_panel_does():
    plain = text(ui.why_html(d1(), d2()))
    assert not re.search(r"[+-]\d{3}\b|\bodds\b|decimal|american", plain, flags=re.I)
    assert "73%" in plain                                                                                                       # the line's implied chance for the favourite, margin removed: the same kind of number the winner panel shows


def test_the_app_shows_it_beside_the_distance_line_and_hides_it_when_it_disagrees_with_the_card():
    assert "import distance_why_ui" in APP
    body = APP[APP.index("def dist_why_html(r)"):APP.index("def has_fight_reasons()")]
    assert 'abs(float(d1["p"]) - x[0]) > 0.005' in body and 'abs(float(d2["p_v2"]) - x[3]) > 0.005' in body and "return distance_why_ui.why_html(d1, d2)" in body
    assert "if not x or not rs.get(\"d1\"):" in body and "except Exception:" in body                                         # only for a bout whose line is shown; never an error
    assert APP.count("{dist_html(r)}{dist_why_html(r)}{why_html(r") == 3                                                      # all three card layouts, straight after the distance line


def test_the_reasons_table_the_page_already_loads_carries_the_new_versions_with_no_new_query():
    loader = APP[APP.index("REASONS.clear()"):APP.index("DISTANCE.clear()")]
    assert 'SELECT bout_id, version, reasons FROM pick_reasons WHERE bout_id = ANY(:ids)' in loader and "version =" not in loader  # every version stored for the bouts on screen arrives together
    assert dr.SCHEMA == ui.SCHEMA == 1


def test_the_readme_and_runbook_describe_the_panel_the_way_it_works():
    readme, runbook = (ROOT / "README.md").read_text(encoding="utf-8"), (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "Why this distance prediction?" in readme and "built from the model's own arithmetic" in readme
    for needle in ("## Why this distance prediction?", "pipeline/distance_reasons.py", "app/distance_why_ui.py", "tests/test_distance_reasons.py", "tests/test_distance_why_ui.py",
                   "explain-upcoming", "An explanation exists only if it reproduces the locked number.", "`net` flag", "pick_reasons", "`d1`", "`d2`", "never fails the run"):
        assert needle in runbook, needle
