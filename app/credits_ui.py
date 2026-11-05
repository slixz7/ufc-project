"""The site's "Data and credits" footer (a pure function that returns HTML; no Streamlit, no database).

The credits are conditions, not courtesies: the Kaggle dataset's creator allowed this use on condition of credit with a link back and non-commercial use, the betting-line
dataset is CC BY 4.0 (which requires attribution), the announced cards come from Wikipedia (CC BY-SA 4.0), and The Odds API asked for nothing but appreciates a credit. The README
holds the full text of each; the footer must name every source and link to it, and a test keeps the two from drifting apart. On the DEMO site nothing is real, so the footer
says so instead of crediting data it does not use.
"""
from __future__ import annotations

import html

REPO = "https://github.com/slixz7/ufc-project"
SOURCES = {
    "kaggle": ("https://www.kaggle.com/datasets/scarekrow/ufc-data", "Kaggle, scarekrow/ufc-data"),
    "bair": ("https://github.com/nlbair/ufc-data", "UFC Data by N. Bair"),
    "odds": ("https://the-odds-api.com", "The Odds API"),
    "wikipedia": ("https://en.wikipedia.org", "Wikipedia"),
}
LICENCES = {"cc_by": ("https://creativecommons.org/licenses/by/4.0/", "CC BY 4.0"), "cc_by_sa": ("https://creativecommons.org/licenses/by-sa/4.0/", "CC BY-SA 4.0")}


def _a(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(text)}</a>'


link = _a                                                                                    # the same safe link, for the other pages that need one


def credits_html(demo: bool = False) -> str:
    if demo:
        return ('<div class="foot credits"><b>Data and credits.</b> Everything on this demo site is invented. The real project credits its data sources and their licences in the '
                f'{_a(REPO + "#data-and-credits", "README")}.</div>')
    k, b, o, w = (_a(*SOURCES[s]) for s in ("kaggle", "bair", "odds", "wikipedia"))
    return ('<div class="foot credits"><b>Data and credits.</b> '
            f'Fight data: {k}, used with its creator\'s permission for non-commercial use. '
            f'Historical betting lines: {b} ({_a(*LICENCES["cc_by"])}), built from ufcstats.com, MMA Decisions and BestFightOdds, among others. '
            f'Live betting lines: {o}. Announced cards: {w} ({_a(*LICENCES["cc_by_sa"])}). '
            'Independent project, not affiliated with or endorsed by the UFC. '
            f'{_a(REPO, "Code and full credits on GitHub")}.</div>')
