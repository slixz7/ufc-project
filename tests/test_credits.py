"""
app/credits_ui.py: the site's "Data and credits" footer. The credits are conditions (the Kaggle creator's, CC BY, CC BY-SA), so the footer must name and link every source, say
the same things the README says, open links safely, and never claim real sources on the demo.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import credits_ui as cu  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
APP = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")


def plain(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h))


def test_the_real_footer_names_and_links_every_source_and_licence_and_keeps_the_conditions():
    h = cu.credits_html()
    for url, name in list(cu.SOURCES.values()) + list(cu.LICENCES.values()):
        assert f'href="{url}"' in h and name in plain(h), (url, name)
    text = plain(h)
    for phrase in ("Data and credits.", "used with its creator's permission for non-commercial use", "Historical betting lines", "Live betting lines", "Announced cards",
                   "built from ufcstats.com, MMA Decisions and BestFightOdds", "not affiliated with or endorsed by the UFC", "Code and full credits on GitHub"):
        assert phrase in text, phrase
    assert "CC BY 4.0" in text and "CC BY-SA 4.0" in text


def test_every_source_the_footer_links_is_credited_in_the_readme_too_so_the_two_cannot_drift():
    for key in ("kaggle", "bair", "odds", "wikipedia"):
        assert cu.SOURCES[key][0] in README, key
    assert "CC BY 4.0" in README and "CC BY-SA 4.0" in README and "non-commercial" in README
    assert "## Data and credits" in README                                                                                  # the anchor the demo footer links to exists
    assert cu.REPO in (ROOT / "docs" / "writeup.md").read_text(encoding="utf-8")                                           # and the repository link is the one the write-up gives


def test_the_demo_footer_claims_no_real_data_and_points_to_the_readme_credits():
    h = cu.credits_html(demo=True)
    text = plain(h)
    assert text.startswith("Data and credits. Everything on this demo site is invented.") and cu.REPO + "#data-and-credits" in h
    for real in ("Kaggle", "Bair", "Odds API", "Wikipedia", "permission"):
        assert real not in text, real


def test_every_link_is_https_opens_safely_and_is_escaped():
    for demo in (False, True):
        h = cu.credits_html(demo=demo)
        hrefs = re.findall(r'href="([^"]+)"', h)
        assert hrefs and all(u.startswith("https://") for u in hrefs) and "javascript:" not in h.lower()
        anchors = re.findall(r"<a [^>]*>", h)
        assert len(anchors) == len(hrefs) and all('target="_blank"' in a and 'rel="noopener noreferrer"' in a for a in anchors)
    a = cu._a('https://x.test/?a=1&b="2"', 'A & <b>x</b>')
    assert 'href="https://x.test/?a=1&amp;b=&quot;2&quot;"' in a and ">A &amp; &lt;b&gt;x&lt;/b&gt;</a>" in a              # nothing in a link can break out of its tag


def test_the_app_draws_it_after_the_disclaimer_and_marks_the_demo_only_when_demo_mode_is_set():
    assert "import credits_ui" in APP
    disclaimer = APP.index("module_html(f'<div class=\"foot\">{esc(DISCLAIMER)}</div>')")
    credits = APP.index('module_html(credits_ui.credits_html(demo=bool(os.getenv("DEMO_MODE"))))')
    assert disclaimer < credits
    assert APP.count("credits_ui.credits_html(") == 1                                                                       # one footer, on every page


def test_the_footer_style_continues_the_disclaimer_instead_of_drawing_a_second_rule():
    assert ".ufc .foot.credits {margin-top:12px; border-top:none; padding-top:0;" in APP
    assert ".ufc .foot a {color:inherit; text-decoration:underline;}" in APP
