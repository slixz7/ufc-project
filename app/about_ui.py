"""The "About this project" row under the navigation (a pure function that returns HTML; no Streamlit, no database).

A first-time visitor (often someone who opened the live site before the repository) sees a collapsed one-line row on every page. Opened, it says what the project is, how to read its
record honestly, what it will not do, what it is built with, and where to read more. It carries no number that could go stale (the live record's size is on the Track record page, which
this points to), and every claim in it is one the site itself makes elsewhere; a test checks that against the app's own text. The demo says it is the demo.
"""
from __future__ import annotations

from credits_ui import REPO, link

WRITEUP = f"{REPO}/blob/main/docs/writeup.md"
MODEL_CARDS = f"{REPO}/blob/main/docs/MODEL_CARDS.md"
DEMO_GUIDE = f"{REPO}/blob/main/demo/README.md"

WHAT_IT_IS = ("A web app that predicts UFC fights <i>before</i> they happen, locks each pick with a timestamp, explains it in plain language, and grades itself against the real results. "
              "It is a portfolio project built end to end: the data pipeline, the models, the tests, the automation and this site.")
WHAT_THE_DEMO_IS = ("Every fighter, fight and result on this site is invented. The data pipeline, the models and these pages are the real ones, running on made-up data, "
                    "so the numbers show how the system works and say nothing about real fights.")
HOW_TO_READ = ("The Track record page keeps two things apart. The <i>backtest</i> predicts every fight in its test period using only what was known before it. The <i>live record</i> is made of "
               "picks locked before a fight and graded afterwards: only that is a pre-fight record, and it is still small, which the evidence meter on that page makes visible.")
WHAT_IT_WILL_NOT_DO = ("It is not betting advice, it never shows betting odds, and it knows nothing about injuries, camps or how two styles fit together. "
                       "Where a pick uses the betting line, the page says so.")
BUILT_WITH = ("Python (pandas, scikit-learn), PostgreSQL and Streamlit, with GitHub Actions running the daily update, the weekly backup, the monthly dependency refresh and hundreds of "
              "automated tests. Independent project, not affiliated with the UFC.")


def about_html(demo: bool = False) -> str:
    lead = ("This is the demo.", WHAT_THE_DEMO_IS) if demo else ("What it is.", WHAT_IT_IS)
    paragraphs = [lead, ("How to read the record.", HOW_TO_READ), ("What it will not do.", WHAT_IT_WILL_NOT_DO), ("Built with.", BUILT_WITH)]
    body = "".join(f'<div class="aside" style="max-width:780px;margin-top:8px"><b>{h}</b> {t}</div>' for h, t in paragraphs)
    links = " &nbsp;·&nbsp; ".join((link(REPO, "Source code"), link(WRITEUP, "The write-up"), link(MODEL_CARDS, "Model cards"), link(DEMO_GUIDE, "Run the demo yourself")))
    return f'<details class="about"><summary>About this project</summary><div class="why">{body}<div class="note" style="margin-top:10px">{links}</div></div></details>'
