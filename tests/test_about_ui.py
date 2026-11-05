"""app/about_ui.py: the collapsed "About this project" row under the navigation. Every claim in it must be one the site makes itself, and it must carry no number that can go stale."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import about_ui as ui  # noqa: E402
import credits_ui  # noqa: E402

APP = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")


def plain(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def headings(h: str) -> list:
    return re.findall(r'<div class="aside"[^>]*><b>([^<]+)</b>', h)


def test_it_is_a_collapsed_row_with_four_short_paragraphs_and_a_row_of_links():
    h = ui.about_html()
    assert h.startswith('<details class="about"><summary>About this project</summary>') and "<details open" not in h and h.endswith("</div></details>")
    assert headings(h) == ["What it is.", "How to read the record.", "What it will not do.", "Built with."]
    assert plain(h).count("Source code") == 1 and h.count('<div class="note"') == 1


def test_the_demo_says_it_is_the_demo_and_keeps_the_rest():
    d = ui.about_html(demo=True)
    assert headings(d) == ["This is the demo.", "How to read the record.", "What it will not do.", "Built with."]
    assert "Every fighter, fight and result on this site is invented" in plain(d) and "predicts UFC fights" not in plain(d)
    assert "predicts UFC fights" in plain(ui.about_html()) and "invented" not in plain(ui.about_html())


def test_the_four_links_are_the_projects_own_open_safely_and_point_at_files_that_exist():
    h = ui.about_html()
    urls = re.findall(r'href="([^"]+)"', h)
    assert urls == [credits_ui.REPO, ui.WRITEUP, ui.MODEL_CARDS, ui.DEMO_GUIDE]
    anchors = re.findall(r"<a [^>]*>", h)
    assert len(anchors) == 4 and all('target="_blank"' in a and 'rel="noopener noreferrer"' in a for a in anchors) and all(u.startswith("https://") for u in urls)
    for url in urls[1:]:
        assert (ROOT / url.split("/blob/main/")[1]).exists(), url                                                              # a link to a file that was renamed would be a dead link on the front door


def test_nothing_in_it_can_go_stale_it_contains_no_number_at_all():
    assert re.findall(r"\d+", plain(ui.about_html())) == [] and re.findall(r"\d+", plain(ui.about_html(demo=True))) == []
    assert "using only data from before its event" in APP                                                                      # the page's own wording for what the backtest does (its start year is computed there)


def test_every_claim_is_one_the_site_makes_itself():
    insights = (ROOT / "app" / "insights_ui.py").read_text(encoding="utf-8")
    assert "The site never shows odds" in insights                                                                           # "never shows betting odds"
    assert "not betting advice" in APP                                                                                           # "not betting advice"
    assert "market-informed" in APP.lower()                                                                                      # "where a pick uses the betting line, the page says so"
    assert "How much evidence is there?" in (ROOT / "app" / "evidence_ui.py").read_text(encoding="utf-8") and "evidence meter" in plain(ui.about_html())
    assert "injuries, camps or how two styles fit together" in (ROOT / "app" / "distance_why_ui.py").read_text(encoding="utf-8") and "injuries, camps or how two styles fit together" in plain(ui.about_html())
    for workflow in ("auto-update.yml", "backup.yml", "refresh-lock.yml"):                                                       # the daily update, the weekly backup, the monthly refresh
        assert (ROOT / ".github" / "workflows" / workflow).exists()
    reqs = (ROOT / "pipeline" / "requirements.txt").read_text(encoding="utf-8").lower() + (ROOT / "app" / "requirements.txt").read_text(encoding="utf-8").lower()
    assert all(name in reqs for name in ("pandas", "scikit-learn", "streamlit", "sqlalchemy"))


def test_it_contains_no_script_and_nothing_unescaped():
    h = ui.about_html()
    assert "<script" not in h.lower() and "javascript:" not in h.lower() and "onclick" not in h.lower()
    assert re.findall(r"&(?!nbsp;|amp;|lt;|gt;|quot;|#)", h) == []


def test_the_app_shows_it_under_the_navigation_on_every_page_and_styles_its_links():
    assert "import about_ui" in APP
    between = APP[APP.index('page = st.radio("Navigate"'):APP.index("{\"Upcoming\": page_upcoming")]
    assert 'module_html(about_ui.about_html(demo=bool(os.getenv("DEMO_MODE"))))' in between                                       # after the navigation, before the page
    assert ".ufc .about a {color:inherit; text-decoration:underline;}" in APP


def test_the_runbook_says_where_the_text_lives_and_why_a_test_guards_it():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "## The About section" in runbook and "app/about_ui.py" in runbook and "tests/test_about_ui.py" in runbook and "free of numbers that go stale" in runbook
