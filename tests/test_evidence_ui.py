"""app/evidence_ui.py: the evidence meter's HTML. Pure functions, so every property is checked on a hand-built set of entries (positions worked out by hand)."""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "app"):
    sys.path.insert(0, str(ROOT / sub))
import evidence as ev  # noqa: E402
import evidence_ui as eu  # noqa: E402

TIE = {"mean": -0.01, "lo": -0.05, "hi": 0.03, "sd": 0.14, "n": 26, "verdict": "cannot be told apart"}


def E(n=26, needed=1000, bt=-0.0065, live=TIE, per_year=480.0, question="Does it beat the line?", sentence="A sentence."):
    return {"key": "v3", "question": question, "model": "the model", "baseline": "the line", "unit": "winner picks", "n_live": n, "live": live if n else None,
            "backtest": None if bt is None else {"mean": bt, "sd": 0.14, "n": 5000}, "needed": needed, "per_year": per_year, "progress": (min(1.0, n / needed) if needed else None),
            "years_needed": (needed / per_year if needed else None), "sentence": sentence}


# ---------------------------------------------------------------------------------------------- the progress bar
def test_the_progress_bar_shows_graded_fights_over_fights_needed_with_a_minimum_visible_sliver():
    assert 'width:0.0%' in eu.progress_html(E(n=0)) and "0 of about 1,000 graded fights (0%)" in eu.progress_html(E(n=0))
    tiny = eu.progress_html(E(n=3))                                                                                           # 0.3%: still visible, and honest about it
    assert "width:1.0%" in tiny and "3 of about 1,000 graded fights (under 1%)" in tiny
    half = eu.progress_html(E(n=500))
    assert "width:50.0%" in half and "500 of about 1,000 graded fights (50%)" in half
    assert "width:100.0%" in eu.progress_html(E(n=2000)) and "(100%)" in eu.progress_html(E(n=2000))                          # never over a full bar
    assert eu.progress_html(E(n=26, needed=None)) == ""                                                                       # nothing to confirm: no bar to fill


# ---------------------------------------------------------------------------------------------- the range chart
def test_the_range_chart_places_zero_the_backtest_and_the_live_range_where_the_numbers_say():
    h = eu.range_html(E())                                                                                                    # half-width = 0.05 x 1.15 = 0.0575
    assert "left:50%;top:0;bottom:0;border-left:2px solid" in h                                                               # no difference: the middle
    assert "left:44.3%;top:0;bottom:0;border-left:2px dashed" in h                                                            # the backtest: 50 + 50 x (-0.0065 / 0.0575)
    assert "left:6.5%;width:69.6%;top:12px" in h                                                                              # the live range from -0.05 to +0.03
    assert "left:41.3%;top:7px;width:4px" in h                                                                                # the live average, -0.01
    assert "-0.057 better" in h and "no difference" in h and "worse +0.057" in h                                               # 0.0575 as the computer holds it prints as 0.057
    assert "live record, 95% range" in h and "what the backtest found" in h and "axis: log-loss difference per fight" in h        # the legend names the unit


def test_with_fewer_than_two_graded_fights_there_is_no_live_range_to_draw_and_without_a_backtest_no_dashed_line():
    for n in (0, 1):
        h = eu.range_html(E(n=n))
        assert "top:12px" not in h and "width:4px" not in h and "dashed" in h                                                # no range bar and no average marker; the backtest line is still there
        assert "live record, 95% range" not in h and "what the backtest found" in h and "axis: log-loss difference per fight" in h    # and the legend does not mention a bar that is not drawn
    none = eu.range_html(E(bt=None))
    assert "dashed" not in none and "what the backtest found" not in none and "top:12px" in none                              # the live range alone
    assert "live record, 95% range" in none and "axis: log-loss difference per fight" in none


def test_everything_stays_inside_the_track_however_wide_the_live_range_is():
    wide = {"mean": 0.0, "lo": -3.0, "hi": 3.0, "sd": 1.0, "n": 3, "verdict": "cannot be told apart"}
    for entry in (E(live=wide), E(live=wide, bt=-0.0001), E(live={**wide, "lo": -1e-9, "hi": 1e-9})):
        positions = [float(x) for x in re.findall(r"left:([\d.]+)%", eu.range_html(entry))]
        assert positions and all(0.0 <= x <= 100.0 for x in positions), positions
        for left, width in re.findall(r"left:([\d.]+)%;width:([\d.]+)%", eu.range_html(entry)):
            assert float(left) + float(width) <= 100.1


# ---------------------------------------------------------------------------------------------- the explanation under it
def test_the_explanation_gives_the_fights_needed_the_backtests_gain_and_the_pace_in_words():
    e = E(needed=13924, per_year=443.0, bt=-0.0028)
    assert eu.needed_html(e) == ('<div class="note">About 13,924 graded fights would give an 80% chance of confirming a gain as big as the backtest found '
                                 '(a difference of -0.0028 per fight). At the recent pace of about 443 fights a year, that is about 31 years.</div>')
    assert "that is about 4 months" in eu.needed_html(E(needed=150, per_year=480.0))
    assert eu.needed_html(E(needed=None)) == '<div class="note">The backtest found no gain to confirm, so there is no number of fights to wait for.</div>'


# ---------------------------------------------------------------------------------------------- cards and the section
def test_a_card_escapes_what_it_shows():
    h = eu.card_html(E(question="Is <b>it</b> better & fair?", sentence='A "quoted" <i>claim</i> & more.'))
    assert "<b>it</b>" not in h and "Is &lt;b&gt;it&lt;/b&gt; better &amp; fair?" in h and "<i>claim</i>" not in h and "&lt;i&gt;claim&lt;/i&gt; &amp; more." in h


def test_the_section_has_a_heading_the_introduction_one_card_per_claim_and_how_to_read_the_figures():
    entries = [E(question=f"Question {i}?") for i in range(4)]
    h = eu.section_html(entries)
    assert h.count('<div class="sect"><h3>How much evidence is there?</h3></div>') == 1 and h.count("border-top:1px solid") == 4
    for i in range(4):
        assert f"Question {i}?" in h
    assert eu.INTRO.split(" The bars")[0] in h and "lower is better" in eu.HOW_TO_READ and "the same figures as the README's" in eu.HOW_TO_READ
    assert html.escape(eu.HOW_TO_READ) in h and html.escape(eu.INTRO) in h                                                  # both are actually on the page, not only defined
    assert not re.search(r"[+-]\d{3}\b|decimal odds|american odds", re.sub(r"<[^>]+>", " ", h), flags=re.I)                    # log-loss figures only, never betting odds
    assert "ins-bar" not in h                                                                                                 # the section is not hidden on a phone


def test_with_no_data_at_all_the_whole_section_still_renders_and_says_so():
    h = eu.section_html(ev.compute_all({}, {}))
    assert h.count("No graded fights yet, so there is nothing to show.") == 4 and h.count("border-top:1px solid") == 4
    assert "graded fights (" not in h                                                                                         # no bar to fill without a backtest to size it by


def test_nothing_in_it_is_wider_than_a_phone_can_show():
    h = eu.section_html([E(), E(n=0), E(needed=None)])
    assert all(int(x) <= 20 for x in re.findall(r"(?<!max-)width:(\d+)px", h))                                                # only the legend swatch and the average marker are fixed, and tiny (a max-width shrinks)
    assert "min-width" not in h and "white-space:nowrap" not in h
