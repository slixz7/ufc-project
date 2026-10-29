"""
UFC Fight Predictor - shareable web app (Streamlit).

Read-only: it only runs SELECT queries against the Postgres views and tables built by the rest of the project.
Pages: Upcoming card (locked pre-fight predictions + tale of the tape), Past events, Fighters, Track record.

Run locally:   streamlit run app/streamlit_app.py      (from the project folder, with DATABASE_URL set)
Deployed:      put DATABASE_URL in the host's secrets (use a READ-ONLY database user, see sql/readonly_role.sql)

Look: a printed fight-night programme. Paper, black ink, one red accent, big type, ruled lines.
"""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import altair as alt
import numpy as np
import pandas as pd
import sqlalchemy as sa
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))      # the shared scoring maths (numpy and pandas only)
import distance_scores  # noqa: E402
from distance_ui import line_html as dist_line_html, section_html as dist_section_html, section_v2_html as dist_section_v2_html  # noqa: E402
import game_ui  # noqa: E402
import title_rules  # noqa: E402

st.set_page_config(page_title="UFC Fight Predictor", page_icon="🥊", layout="wide", initial_sidebar_state="collapsed")

DISCLAIMER = ("Model predictions for fun, not betting advice. Over the long run the model is right roughly 6 times in 10, "
              "and it knows nothing about injuries, weight cuts or short-notice changes.")

INK, ACCENT, MUTED, HAIR = "#171717", "#c4361f", "#6b675d", "#cdc7b8"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:ital,wght@0,400;0,600;1,400&display=swap');
.stApp {background:#f3f0e8;}
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="stToolbar"],
[data-testid="stDecoration"], footer {display:none !important;}
[data-testid="stHeader"] {background:transparent; height:0;}
[data-testid="stMainBlockContainer"], .block-container {max-width:1100px; padding:1.4rem 1.4rem 4rem;}

/* navigation: the radio group becomes plain section links with a red underline */
div[data-testid="stRadioGroup"] {display:flex; flex-direction:row; flex-wrap:wrap; gap:28px; justify-content:flex-start; border-bottom:1px solid #171717;}
label[data-testid="stRadioOption"] {background:none; border:none; border-bottom:4px solid transparent; border-radius:0; padding:8px 0 3px; margin:0 0 -1px; cursor:pointer;}
label[data-testid="stRadioOption"] > div > div:first-child {display:none;}
label[data-testid="stRadioOption"] p {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-size:1.3rem; font-weight:600;
  letter-spacing:.07em; text-transform:uppercase; margin:0; color:#6b675d;}
label[data-testid="stRadioOption"]:hover p {color:#171717;}
label[data-testid="stRadioOption"][data-selected="true"] {border-bottom-color:#c4361f;}
label[data-testid="stRadioOption"][data-selected="true"] p {color:#171717; font-weight:700;}
.stTabs [data-baseweb="tab-list"] {gap:22px; border-bottom:1px solid #171717;}
.stTabs [data-baseweb="tab"] p {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; text-transform:uppercase; letter-spacing:.07em; font-size:1.15rem; font-weight:600;}

.ufc {--ink:#171717; --muted:#6b675d; --hair:#cdc7b8; --accent:#c4361f; --good:#2e6b4a; --paper:#f3f0e8;
  font-family:'Source Serif 4',Georgia,'Times New Roman',serif; color:var(--ink); font-size:1.02rem; line-height:1.45;}
.ufc .disp {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:700; text-transform:uppercase; letter-spacing:.01em;}
.ufc .mono {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.8rem; letter-spacing:.02em;}
.ufc .muted {color:var(--muted);}
.ufc .small {font-size:.88rem;}

/* masthead */
.ufc .mast {border-top:6px solid var(--ink); padding-top:8px;}
.ufc .mast .top {display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:4px 20px; margin-bottom:6px;}
.ufc .brand {font-size:2.6rem; font-weight:800; line-height:1; letter-spacing:.02em;}
.ufc .dateline {color:var(--muted);}
.ufc .demo-banner {border:2px solid var(--accent); color:var(--accent); background:rgba(196,54,31,.06); padding:9px 14px; margin:12px 0 2px; font-size:.86rem; line-height:1.45;}
.ufc .demo-banner b {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-size:1.15rem; letter-spacing:.1em; margin-right:8px;}

/* event headline */
.ufc .hero {padding:26px 0 4px;}
.ufc .kicker {color:var(--accent); font-weight:600; text-transform:uppercase; letter-spacing:.14em;}
.ufc .hero h1 {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; text-transform:uppercase;
  font-size:clamp(2.2rem,6.4vw,4.6rem); line-height:.94; margin:6px 0 12px; letter-spacing:.005em;}
.ufc .facts {display:flex; flex-wrap:wrap; gap:4px 26px; padding:8px 0; border-top:1px solid var(--ink); border-bottom:1px solid var(--hair);
  font-family:'IBM Plex Mono','Courier New',monospace; font-size:.82rem;}
.ufc .facts b {font-weight:600;}
.ufc .aside {color:var(--muted); font-style:italic; font-size:.92rem; margin-top:10px; max-width:64ch;}
.ufc .sect {margin:36px 0 10px; border-top:3px solid var(--ink); padding-top:6px; display:flex; align-items:baseline; gap:14px;}
.ufc .sect h3 {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; font-size:1.8rem; text-transform:uppercase; margin:0; line-height:1;}

/* fighter names */
.ufc .duel {display:grid; grid-template-columns:1fr auto 1fr; gap:14px; align-items:end;}
.ufc .side {--c:var(--ink); text-align:left;}
.ufc .side.b {--c:var(--accent); text-align:right;}
.ufc .side .first {font-style:italic; color:var(--muted); font-size:1.05rem; line-height:1.1; min-height:1.1em;}
.ufc .side .last {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; text-transform:uppercase; line-height:.92;
  font-size:var(--ln); color:var(--c);}
.ufc .side .rec {color:var(--muted); margin-top:5px;}
.ufc .side.pick .last {display:inline-block; border-bottom:.09em solid var(--c); padding-bottom:.02em;}
.ufc .side .pk {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.64rem; font-weight:600; letter-spacing:.14em;
  background:var(--c); color:#fff; padding:1px 8px; display:inline-block; margin-bottom:5px;}
.ufc .vs {font-style:italic; color:var(--muted); font-size:1.35rem; padding-bottom:22px;}

/* probability scale */
.ufc .scale .nums {display:flex; justify-content:space-between; font-family:'IBM Plex Mono','Courier New',monospace; font-weight:600;
  font-size:var(--pn,1.5rem); line-height:1; margin-bottom:5px;}
.ufc .scale .nums .na {color:var(--ink);} .ufc .scale .nums .nb {color:var(--accent);}
.ufc .scale .bar {position:relative; display:flex; height:var(--bh,12px);}
.ufc .scale .sa {background:var(--ink);} .ufc .scale .sb {background:var(--accent);}
.ufc .scale .tick {position:absolute; left:50%; top:-3px; bottom:-3px; width:3px; margin-left:-1px; background:var(--paper);}
.ufc .verdict {margin-top:10px;} .ufc .verdict i {color:var(--muted);}
.ufc .stamp {display:block; color:var(--muted); margin-top:3px;}
.ufc .tag {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.68rem; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
  border:1px solid var(--ink); padding:0 7px; margin-left:8px;}
.ufc .mk {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.8rem; font-weight:600;}
.ufc .mk.good {color:var(--good);} .ufc .mk.bad {color:var(--accent);}
.ufc .outcome {margin-top:6px;}

/* main event, main card, prelims */
.ufc .main {border-top:3px solid var(--ink); border-bottom:1px solid var(--ink); padding:12px 0 18px; margin-top:22px; --ln:clamp(2.4rem,7vw,5.2rem);}
.ufc .label {display:flex; align-items:center; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.74rem; letter-spacing:.14em;
  text-transform:uppercase; color:var(--muted); margin-bottom:14px;}
.ufc .bgrid {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 44px;}
.ufc .bout {border-top:2px solid var(--ink); padding:10px 0 18px; --ln:clamp(1.7rem,3.3vw,2.5rem); --pn:1.2rem; --bh:10px;}
.ufc .prow {border-top:1px solid var(--hair); padding:12px 0 8px; --ln:1.55rem; --pn:.95rem; --bh:8px;}
.ufc .prow .line {display:grid; grid-template-columns:30px 1fr 220px 1fr; gap:16px; align-items:center;}
.ufc .prow .num {font-family:'IBM Plex Mono','Courier New',monospace; color:var(--muted); font-size:.8rem;}
.ufc .prow .side.a {text-align:right;} .ufc .prow .side.b {text-align:left;}
.ufc .prow .side .first {display:none;} .ufc .prow .side .rec {margin-top:2px;}
.ufc .prow .side.pick .last {border-bottom-width:3px;}
.ufc .prow .pk {display:none;}
.ufc .prow .mid .small {text-align:center; font-size:.85rem; margin-top:6px;}

/* tale of the tape */
.ufc details {margin-top:8px;}
.ufc summary {cursor:pointer; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.74rem; letter-spacing:.14em; text-transform:uppercase;
  color:var(--muted); list-style:none;}
.ufc summary::-webkit-details-marker {display:none;}
.ufc summary::before {content:'+\\00a0';} .ufc details[open] summary::before {content:'\\2212\\00a0';}
.ufc summary:hover {color:var(--ink);}
.ufc .tape {margin-top:10px;}
.ufc .tape-head {display:grid; grid-template-columns:1fr 1.15fr 1fr; gap:12px; font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif;
  font-weight:700; text-transform:uppercase; font-size:1.15rem; padding-bottom:4px; border-bottom:2px solid var(--ink);}
.ufc .tape-head .b {text-align:right; color:var(--accent);}
.ufc .tr {display:grid; grid-template-columns:1fr 1.15fr 1fr; gap:12px; align-items:center; padding:5px 0; border-bottom:1px solid var(--hair);}
.ufc .tr .lab {text-align:center; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.66rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted);}
.ufc .tr .cell {display:flex; align-items:center; gap:8px; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.84rem;}
.ufc .tr .cell.a {flex-direction:row-reverse;}
.ufc .tr .track {flex:1; height:5px; display:flex; background:rgba(23,23,23,.08);}
.ufc .tr .cell.a .track {justify-content:flex-end;}
.ufc .tr .fill {height:100%; opacity:.38;}
.ufc .tr .cell.a .fill {background:var(--ink);} .ufc .tr .cell.b .fill {background:var(--accent);}
.ufc .tr .cell.best .fill {opacity:1;} .ufc .tr .cell.best {font-weight:600;}
.ufc .tr .val {min-width:54px; text-align:center;}
.ufc .tr.textonly .cell {justify-content:center;}

/* result rows, tables, figures */
.ufc .res {display:grid; grid-template-columns:150px 1fr 150px 270px; gap:14px; align-items:baseline; padding:9px 0; border-top:1px solid var(--hair);}
.ufc .res.three {grid-template-columns:1fr 240px 250px;}
.ufc .res .div {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.7rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted);}
.ufc .res .w {font-weight:600; font-size:1.1rem;} .ufc .res .lo {color:var(--muted);}
.ufc .res .how {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.8rem;}
.ufc .res > div:last-child {text-align:right;}
.ufc .figs {display:flex; flex-wrap:wrap; border-top:3px solid var(--ink); border-bottom:1px solid var(--ink); margin:10px 0 14px;}
.ufc .figs .c {padding:10px 30px 10px 0; margin-right:30px; border-right:1px solid var(--hair);}
.ufc .figs .c:last-child {border-right:none;}
.ufc .figs .v, .ufc .sheet .v {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; font-size:3rem; line-height:1;}
.ufc .figs .l, .ufc .sheet .l {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.68rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin-top:3px;}
.ufc .sheet {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:3px solid var(--ink); margin:18px 0 6px;}
.ufc .sheet .c {padding:10px 12px 10px 0; border-bottom:1px solid var(--hair);}
.ufc .sheet .v {font-size:2.4rem;}
.ufc table.hist {width:100%; border-collapse:collapse;}
.ufc table.hist th {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; color:var(--muted);
  text-align:left; padding:6px 8px 6px 0; border-bottom:2px solid var(--ink);}
.ufc table.hist td {padding:7px 8px 7px 0; border-bottom:1px solid var(--hair); font-size:.95rem;}
.ufc .note {color:var(--muted); font-style:italic; font-size:.9rem; margin-top:10px;}
.ufc .foot {margin-top:42px; border-top:1px solid var(--ink); padding-top:10px; color:var(--muted); font-style:italic; font-size:.88rem;}

/* goes the distance */
.ufc .dist {margin-top:10px; font-size:.98rem;}
.ufc .dist .k {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.64rem; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin-right:10px;}
/* why this pick */
.ufc .whyrow {padding:0 0 12px 0;} .ufc .whyrow details {margin-top:0;}
.ufc .why {margin-top:6px;}
.ufc .two {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:10px 0 6px;}
.ufc .two > div {border-top:2px solid var(--ink); padding-top:6px; font-size:1.08rem;}
.ufc .two .k {display:block; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.64rem; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin-bottom:2px;}
.ufc .fac {margin-top:8px;}
.ufc .fac .hd {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin:10px 0 2px;}
.ufc .fr {display:grid; grid-template-columns:150px 1fr 190px; gap:14px; align-items:center; padding:7px 0; border-bottom:1px solid var(--hair);}
.ufc .fr .fl {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:700; text-transform:uppercase; font-size:1.08rem; line-height:1.05;}
.ufc .fr .fd {font-size:.84rem; color:var(--muted); line-height:1.3;}
.ufc .fb {display:flex; align-items:center; gap:8px; font-family:'IBM Plex Mono','Courier New',monospace; font-size:.76rem; font-weight:600;}
.ufc .fb .tr2 {flex:1; height:6px; background:rgba(23,23,23,.08); display:flex;}
.ufc .fb.a .tr2 {justify-content:flex-start;} .ufc .fb.b .tr2 {justify-content:flex-end;}
.ufc .fb i {display:block; height:100%;} .ufc .fb.a i {background:var(--ink);} .ufc .fb.b i {background:var(--accent);}
.ufc .fb.a span {color:var(--ink);} .ufc .fb.b span {color:var(--accent);}
.ufc .fr.rest {opacity:.75;}
@media (max-width:760px) {
  .ufc .two {grid-template-columns:1fr;} .ufc .fr {grid-template-columns:1fr; gap:3px;}
  .ufc .brand {font-size:1.9rem;} .ufc .main {--ln:2.3rem;}
  .ufc .bgrid {grid-template-columns:1fr; gap:0;}
  .ufc .prow .line {grid-template-columns:1fr; gap:6px;} .ufc .prow .num {display:none;}
  .ufc .prow .side.a, .ufc .prow .side.b {text-align:left;}
  .ufc .res, .ufc .res.three {grid-template-columns:1fr; gap:2px;} .ufc .res > div:last-child {text-align:left;}
  .ufc .sheet {grid-template-columns:repeat(2,minmax(0,1fr));} .ufc .figs .v {font-size:2.3rem;}
  .ufc .figs .c {padding-right:16px; margin-right:16px;}
  .ufc .tr {gap:6px;} .ufc .tr .val {min-width:42px; font-size:.78rem;}
  label[data-testid="stRadioOption"] p {font-size:1.02rem; letter-spacing:.04em;} div[data-testid="stRadioGroup"] {gap:13px;}
}
</style>
"""


# ------------------------------------------------------------------------------------------------
# database
# ------------------------------------------------------------------------------------------------
@st.cache_resource
def _make_engine(url: str, pool_size: int = 5, max_overflow: int = 10):
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    # connect_timeout: a database that does not answer shows the error message instead of an endless blank page
    return sa.create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow, connect_args={"connect_timeout": 12})


def _secret(name: str):
    try:
        value = st.secrets.get(name)
    except Exception:                      # no secrets file when running locally
        value = None
    return value or os.getenv(name)


def get_game_engine():
    """The friends' game's own database user (it can touch only the game tables), or None until the secret is set. Kept to at most 6 connections."""
    url = _secret("GAME_DATABASE_URL")
    return _make_engine(url, pool_size=3, max_overflow=3) if url else None


def page_game():
    game_ui.render(q, get_game_engine(), _secret("GAME_INVITE_CODE"))


def get_engine():
    """Only a real engine is cached, so fixing a missing/wrong secret takes effect right away."""
    url = None
    try:
        url = st.secrets.get("DATABASE_URL")
    except Exception:                      # no secrets file when running locally
        url = None
    url = url or os.getenv("DATABASE_URL")
    return _make_engine(url) if url else None


@st.cache_data(ttl=300, show_spinner=False)
def q(sql: str, params: dict | None = None) -> pd.DataFrame:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("DATABASE_URL is not set")
    with engine.connect() as conn:
        return pd.read_sql(sa.text(sql), conn, params=params or {})


def has_v3() -> bool:
    try:
        r = q("""SELECT to_regclass('public.upcoming_picks_v3') IS NOT NULL AS a, to_regclass('public.fight_picks_v3') IS NOT NULL AS b,
                        to_regclass('public.v3_summary') IS NOT NULL AS c""")
        return bool(r.a.iloc[0] and r.b.iloc[0] and r.c.iloc[0])
    except Exception:
        return False


NO_V3_UPCOMING = """(SELECT NULL::text AS bout_id, NULL::float8 AS p_a_wins, NULL::text AS predicted_winner, NULL::float8 AS confidence,
                     NULL::timestamptz AS predicted_at, NULL::text AS actual_winner, NULL::int AS correct WHERE false)"""
NO_V3_PAST = """(SELECT NULL::text AS fight_url, NULL::text AS predicted_winner, NULL::float8 AS confidence, NULL::int AS correct WHERE false)"""


# ------------------------------------------------------------------------------------------------
# formatting helpers
# ------------------------------------------------------------------------------------------------
def isnull(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA or v is pd.NaT


def f_pct(v):    return "–" if isnull(v) else f"{float(v):.0%}"
def f_num(v, d=1): return "–" if isnull(v) else f"{float(v):.{d}f}"
def f_int(v):    return "–" if isnull(v) else f"{int(v)}"
def f_streak(v):
    if isnull(v) or int(v) == 0:
        return "–"
    return f"W{int(v)}" if int(v) > 0 else f"L{-int(v)}"
def f_height(cm):
    if isnull(cm):
        return "–"
    inches = round(float(cm) / 2.54)
    return f"{inches // 12}' {inches % 12}\""
def f_reach(cm):  return "–" if isnull(cm) else f"{round(float(cm) / 2.54)}\""
def f_text(v):    return "–" if isnull(v) or str(v).strip() == "" else str(v)
def f_date(d):    return "–" if isnull(d) else pd.Timestamp(d).strftime("%b %d, %Y")




# ------------------------------------------------------------------------------------------------
# presentation helpers
# ------------------------------------------------------------------------------------------------
esc = html.escape
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def split_name(name: str):
    parts = name.split()
    if len(parts) <= 1:
        return "", name
    k = 2 if (len(parts) > 2 and parts[-1].lower() in SUFFIXES) else 1
    return " ".join(parts[:-k]), " ".join(parts[-k:])


def side_html(name: str, rec: str, side: str, picked: bool = False) -> str:
    first, last = split_name(name)
    pk = '<div class="pk">MODEL PICK</div>' if picked else ""
    return (f'<div class="side {side}{" pick" if picked else ""}">{pk}<div class="first">{esc(first)}</div>'
            f'<div class="last">{esc(last)}</div><div class="rec mono">{esc(rec)}</div></div>')


def scale_html(p_a: float) -> str:
    pa = max(0.02, min(0.98, float(p_a)))
    return (f'<div class="scale"><div class="nums"><span class="na">{pa:.0%}</span><span class="nb">{1 - pa:.0%}</span></div>'
            f'<div class="bar"><i class="sa" style="width:{pa * 100:.1f}%"></i><i class="sb" style="width:{(1 - pa) * 100:.1f}%"></i>'
            f'<b class="tick"></b></div></div>')


def lean(conf: float) -> str:
    return "Toss-up" if conf < 0.55 else ("Slight lean" if conf < 0.65 else "Clear lean")


def days_label(d: date, today: date) -> str:
    n = (d - today).days
    return "Today" if n == 0 else "Tomorrow" if n == 1 else f"In {n} days" if n > 1 else f"{-n} days ago"


def method_text(result) -> str:
    r = str(result or "")
    if "KO" in r or "TKO" in r:
        return "KO/TKO"
    if "Sub" in r:
        return "SUB"
    if "Decision" in r:
        return "DEC"
    return r[:10].upper() or "–"


# (key, label, formatter, "high"/"low" = which value is better / None, draw bars?)
TAPE = [
    ("n_prev", "UFC fights", f_int, "high", True), ("age", "Age on fight night", lambda v: f_num(v, 1), None, True),
    ("height", "Height", f_height, None, True), ("reach", "Reach", f_reach, "high", True),
    ("stance", "Stance", f_text, None, False),
    ("prev_win_rate", "UFC win rate", f_pct, "high", True), ("last3_win_rate", "Last 3 fights", f_pct, "high", True),
    ("streak", "Current streak", f_streak, None, False), ("days_since_last", "Days since last fight", f_int, None, False),
    ("slpm", "Strikes landed / min", lambda v: f_num(v, 2), "high", True),
    ("sapm", "Strikes absorbed / min", lambda v: f_num(v, 2), "low", True),
    ("sig_acc", "Striking accuracy", f_pct, "high", True), ("td_per15", "Takedowns / 15 min", lambda v: f_num(v, 2), "high", True),
    ("td_def", "Takedown defense", f_pct, "high", True), ("ctrl_share", "Control time", f_pct, "high", True),
    ("ko_win_rate", "KO/TKO win rate", f_pct, "high", True), ("sub_win_rate", "Submission win rate", f_pct, "high", True),
    ("lastfight", "Last fight", f_text, None, False), ("fin3", "Finishes, last 3 fights", f_text, None, False),
]


def tape_html(row, name_a: str, name_b: str, wanted: set | None = None) -> str:
    out = [f'<div class="tape"><div class="tape-head"><div>{esc(name_a)}</div><div></div><div class="b">{esc(name_b)}</div></div>']
    for key, label, fmt, better, bars in TAPE:
        if wanted is not None and key not in wanted:
            continue
        a, b = row.get(f"a_{key}"), row.get(f"b_{key}")
        if isnull(a) and isnull(b):
            continue
        best_a = best_b = False
        wa = wb = 0.0
        numeric = bars and not isnull(a) and not isnull(b) and not isinstance(a, str)
        if numeric:
            fa, fb = float(a), float(b)
            top = max(fa, fb, 1e-9)
            wa, wb = max(3.0, fa / top * 100), max(3.0, fb / top * 100)
            if better and fa != fb:
                best_a = (fa > fb) if better == "high" else (fa < fb)
                best_b = not best_a
        if numeric:
            out.append(f'<div class="tr"><div class="cell a{" best" if best_a else ""}"><span class="val">{esc(fmt(a))}</span>'
                       f'<div class="track"><div class="fill" style="width:{wa:.0f}%"></div></div></div>'
                       f'<div class="lab">{esc(label)}</div>'
                       f'<div class="cell b{" best" if best_b else ""}"><span class="val">{esc(fmt(b))}</span>'
                       f'<div class="track"><div class="fill" style="width:{wb:.0f}%"></div></div></div></div>')
        else:
            out.append(f'<div class="tr textonly"><div class="cell a" style="justify-content:flex-start"><span class="val">{esc(fmt(a))}</span></div>'
                       f'<div class="lab">{esc(label)}</div>'
                       f'<div class="cell b" style="justify-content:flex-end"><span class="val">{esc(fmt(b))}</span></div></div>')
    out.append("</div>")
    return "".join(out)


def tape_details(row, name_a: str, name_b: str, open_: bool = False) -> str:
    if isnull(row.get("a_n_prev")):
        return ""
    return f'<details{" open" if open_ else ""}><summary>Tale of the tape</summary>{tape_html(row, name_a, name_b)}</details>'


def module_html(s: str):
    st.html(f'<div class="ufc">{s}</div>')


REASONS: dict = {}          # bout_id -> {"v2": reasons, "v3": reasons}, filled by the Upcoming page for the card on screen


def has_reasons() -> bool:
    try:
        return bool(q("SELECT to_regclass('public.pick_reasons') IS NOT NULL AS ok").ok.iloc[0])
    except Exception:
        return False


def prob_scores(d: pd.DataFrame) -> dict:
    """Log loss and Brier score (lower is better) with paired 95% intervals. The same maths as `model_v3.py scoring`."""
    y = (d.actual_winner == d.fighter_a).astype(float).to_numpy()
    clip = lambda p: np.clip(pd.to_numeric(p).to_numpy(dtype=float), 1e-4, 1 - 1e-4)
    P = {"coin": np.full(len(d), 0.5), "v2": clip(d.p_v2_a), "line": clip(d.market_prob_a), "v3": clip(d.p_a_wins)}
    ll = {k: -(y * np.log(p) + (1 - y) * np.log(1 - p)) for k, p in P.items()}
    br = {k: (p - y) ** 2 for k, p in P.items()}

    def paired(a: str, b: str, mask=None) -> dict:
        diff = ll[a] - ll[b]
        diff = diff if mask is None else diff[mask]
        n, m = len(diff), float(diff.mean())
        se = float(diff.std(ddof=1) / np.sqrt(n))
        lo, hi = m - 1.96 * se, m + 1.96 * se
        return {"n": n, "mean": m, "lo": lo, "hi": hi, "sd": se * np.sqrt(n),
                "verdict": "clearly better" if hi < 0 else ("clearly worse" if lo > 0 else "cannot be told apart")}
    tiers = d.tier.to_numpy()
    return {"n": len(d), "scores": {k: (float(ll[k].mean()), float(br[k].mean())) for k in P}, "vs_line": paired("v3", "line"),
            "vs_model": paired("v3", "v2"), "tiers": {t: paired("v3", "line", tiers == t) for t in ("full", "limited", "debut") if (tiers == t).any()}}


def scoring_html(r: dict) -> str:
    labels = [("coin", "Coin flip"), ("v2", "Model-only (v2)"), ("line", "Opening line alone"), ("v3", "Model + market (v3)")]
    best = min(r["scores"][k][0] for k, _ in labels if k != "coin")
    rows = ""
    for k, label in labels:
        ll_, br_ = r["scores"][k]
        cell = (lambda t: f"<b>{t}</b>") if (k != "coin" and ll_ == best) else (lambda t: t)
        rows += f'<tr><td>{label}</td><td class="mono">{cell(f"{ll_:.4f}")}</td><td class="mono">{cell(f"{br_:.4f}")}</td></tr>'
    tn = {"full": "both fighters have 3+ UFC fights", "limited": "a fighter has 1-2", "debut": "a fighter is a debut"}
    one = lambda label, x: (f'<tr><td>{label}</td><td class="mono">{x["n"]:,}</td><td class="mono">{x["mean"]:+.4f}</td>'
                            f'<td class="mono">{x["lo"]:+.4f} to {x["hi"]:+.4f}</td><td>{x["verdict"]}</td></tr>')
    diffs = one("Model + market vs. opening line", r["vs_line"]) + one("Model + market vs. model-only", r["vs_model"])
    diffs += "".join(one(f"&nbsp;&nbsp;vs. line, {tn[t]}", x) for t, x in r["tiers"].items())
    vl = r["vs_line"]
    need = ""
    if vl["mean"] < 0 and vl["sd"] > 0:
        n = int(round(((1.96 + 0.84) * vl["sd"] / abs(vl["mean"])) ** 2, -2))
        need = (f" A gap this small would take roughly {n:,} live fights to confirm, which is years of cards, so this backtest is the evidence and the "
                "locked record above is for transparency.")
    return ('<div class="sect"><h3>Scoring the probabilities, not just the winners</h3></div>'
            '<div class="aside" style="margin-top:0">Accuracy only counts whether the favourite won. These scores judge the probability itself, so a 90% pick '
            'that loses costs far more than a 55% pick that loses. Lower is better, and a coin flip scores 0.693 on log loss.</div>'
            f'<table class="hist"><tr><th>Predictor</th><th>Log loss</th><th>Brier score</th></tr>{rows}</table>'
            '<div class="note" style="margin-top:14px">How much better is the blend? Negative numbers mean the first predictor is better; the range is a 95% interval.</div>'
            f'<table class="hist"><tr><th>Comparison</th><th>Fights</th><th>Average per fight</th><th>95% range</th><th>Verdict</th></tr>{diffs}</table>'
            '<div class="note">The blend’s edge over the line is small but statistically clear, and it comes from fights where both fighters have a UFC history. '
            'For debutants and thin histories the two cannot be told apart, so there the market is doing the work. The comparison uses the opening line, which '
            f'is less sharp than the closing line.{need} Not betting advice.</div>')


DISTANCE: dict = {}         # bout_id -> (chance it goes the distance, scheduled rounds, tier, market-informed chance or None), filled by the Upcoming page


def has_table(name: str) -> bool:
    try:
        return bool(q("SELECT to_regclass(:t) IS NOT NULL AS ok", {"t": f"public.{name}"}).ok.iloc[0])
    except Exception:
        return False


def dist_html(r) -> str:
    try:
        x = DISTANCE.get(getattr(r, "bout_id", None))
        return dist_line_html(x[0], x[1], x[2], x[3] if len(x) > 3 else None) if x else ""
    except Exception:
        return ""                         # a prediction line must never break the page


def has_fight_reasons() -> bool:
    try:
        return bool(q("SELECT to_regclass('public.fight_reasons') IS NOT NULL AS ok").ok.iloc[0])
    except Exception:
        return False


def why_html(r, open_: bool = False) -> str:
    """'Why this pick?': the two opinions (when the betting market was used) and what drove the statistical model."""
    try:
        rs = REASONS.get(getattr(r, "bout_id", None))
        if not rs or isnull(r.predicted_winner):
            return ""
        v2d, v3d = rs.get("v2"), rs.get("v3")
        last = lambda n: split_name(n)[1]
        parts = []
        if v3d and getattr(r, "pick_src", None) == "market":
            side = lambda p: (r.fighter_a if p >= 0.5 else r.fighter_b, max(p, 1 - p))
            (mk, mkp), (md, mdp), (fn, fp) = side(v3d["market_p_a"]), side(v3d["model_p_a"]), side(v3d["final_p_a"])
            share = float(v3d["market_share"])
            parts.append(f'<div class="two"><div><span class="k">Betting market</span><b>{esc(last(mk))}</b> {mkp:.0%}</div>'
                         f'<div><span class="k">Statistical model</span><b>{esc(last(md))}</b> {mdp:.0%}</div>'
                         f'<div><span class="k">Combined pick</span><b>{esc(last(fn))}</b> {fp:.0%}</div></div>'
                         f'<div class="note" style="margin-top:2px">The pick blends the two opinions. The market carries about {share:.0%} of the weight and the '
                         f'model {1 - share:.0%}, as learned from thousands of past fights. When they disagree, the pick leans toward the market and '
                         f'its confidence drops.</div>')
        if v2d and v2d.get("factors"):
            mx = max([abs(f["c"]) for f in v2d["factors"]] + [0.35])
            rows = []
            for f in v2d["factors"]:
                a_side = f["toward"] == "A"
                who = r.fighter_a if a_side else r.fighter_b
                net = ' <span class="muted">(net effect: the model also weighs related stats not shown here)</span>' if f.get("net") else ""
                rows.append(f'<div class="fr"><div class="fl">{esc(f["label"])}</div><div class="fd">{esc(f["detail"])}{net}</div>'
                            f'<div class="fb {"a" if a_side else "b"}"><div class="tr2"><i style="width:{min(100, abs(f["c"]) / mx * 100):.0f}%"></i></div>'
                            f'<span>{esc(last(who))}</span></div></div>')
            rest = float(v2d.get("rest", 0))
            if abs(rest) >= 0.05:
                a_side = rest > 0
                rows.append(f'<div class="fr rest"><div class="fl">All the rest</div><div class="fd">the smaller factors added together</div>'
                            f'<div class="fb {"a" if a_side else "b"}"><div class="tr2"><i style="width:{min(100, abs(rest) / mx * 100):.0f}%"></i></div>'
                            f'<span>{esc(last(r.fighter_a if a_side else r.fighter_b))}</span></div></div>')
            parts.append('<div class="fac"><div class="hd">What drives the statistical model\'s view (longer bar = bigger push)</div>' + "".join(rows) + "</div>")
        if not parts:
            return ""
        parts.append('<div class="note">These are tendencies in past fights, using only the numbers listed. The model knows nothing about injuries, camps or '
                     'how two styles fit together' + ("; the betting market, where used, may." if v3d else ".") + '</div>')
        return f'<details{" open" if open_ else ""}><summary>Why this pick?</summary><div class="why">{"".join(parts)}</div></details>'
    except Exception:
        return ""                         # an explanation must never break the page


def figs(items) -> str:
    return '<div class="figs">' + "".join(f'<div class="c"><div class="v">{esc(v)}</div><div class="l">{esc(l)}</div></div>' for v, l in items) + "</div>"


def sheet(items) -> str:
    return '<div class="sheet">' + "".join(f'<div class="c"><div class="v">{esc(v)}</div><div class="l">{esc(l)}</div></div>' for v, l in items) + "</div>"


def mark(ok: bool, text: str) -> str:
    return f'<span class="mk {"good" if ok else "bad"}">{"✓" if ok else "✗"} {esc(text)}</span>'


STAT_COLS = ", ".join(f"m.a_{k} AS a_{k}, m.b_{k} AS b_{k}" for k, *_ in TAPE if k not in ("stance", "lastfight", "fin3"))


# ------------------------------------------------------------------------------------------------
# page: upcoming card
# ------------------------------------------------------------------------------------------------
TIER_TEXT = {"limited": "Limited data: one of these fighters has only 1 or 2 UFC fights.",
             "debut": "Very little to go on: a fighter here has no earlier UFC fights."}


def tier_note(tier) -> str:
    if isnull(tier) or tier not in TIER_TEXT:
        return ""
    acc = q("SELECT tier, accuracy FROM v2_tier_accuracy WHERE method = 'model'")
    a = acc[acc.tier == tier]
    extra = f" Picks like this have been right about {float(a.accuracy.iloc[0]):.0%} of the time." if len(a) else ""
    return f'<div class="note" style="margin-top:6px">{esc(TIER_TEXT[tier])}{esc(extra)}</div>'


def src_tag(r) -> str:
    src = getattr(r, "pick_src", None)
    if isnull(src):
        return ""
    return f'<span class="tag" style="margin:0 8px 0 0">{"Market-informed" if src == "market" else "Model-only"}</span>'


def pick_parts(r):
    """(probability scale, verdict text, picked_a, picked_b)"""
    picked_a = (not isnull(r.predicted_winner)) and r.predicted_winner == r.fighter_a
    picked_b = (not isnull(r.predicted_winner)) and r.predicted_winner == r.fighter_b
    scale = ""
    if not isnull(r.p_a_wins):
        scale = scale_html(r.p_a_wins)
        stamp = pd.Timestamp(r.predicted_at).strftime("%b %d, %Y, %H:%M UTC")
        verdict = (src_tag(r) + f'The model leans <b>{esc(r.predicted_winner)}</b>, {float(r.confidence):.0%}. '
                   f'<i>{lean(float(r.confidence))}.</i><span class="stamp mono">Locked {stamp}</span>') + tier_note(r.tier)
        v2w = getattr(r, "v2_winner", None)
        if getattr(r, "pick_src", None) == "market" and not isnull(v2w) and v2w != r.predicted_winner:
            verdict += (f'<div class="note" style="margin-top:4px">The model-only pick was {esc(v2w)} ({float(r.v2_conf):.0%}); '
                        f'the betting market shifted it.</div>')
    else:
        verdict = '<span class="muted">Prediction not made yet.</span>'
    if not isnull(r.actual_winner):
        verdict += f'<div class="outcome">{mark(int(r.correct) == 1, "Model right" if int(r.correct) == 1 else "Model wrong")} &nbsp;{esc(r.actual_winner)} won</div>'
    return scale, verdict, picked_a, picked_b


def main_event_html(r, rec: dict) -> str:
    scale, verdict, pa, pb = pick_parts(r)
    title = '<span class="tag">Championship</span>' if r.title_fight else ""
    return (f'<div class="main"><div class="label"><span>Main event &nbsp;·&nbsp; {esc(f_text(r.weight_class))}</span>{title}</div>'
            f'<div class="duel">{side_html(r.fighter_a, rec.get(r.fighter_a_url, "UFC debut"), "a", pa)}<div class="vs">vs.</div>'
            f'{side_html(r.fighter_b, rec.get(r.fighter_b_url, "UFC debut"), "b", pb)}</div>'
            f'<div style="margin-top:20px">{scale}</div><div class="verdict">{verdict}</div>'
            f'{dist_html(r)}{why_html(r, True)}{tape_details(pd.Series(r._asdict()), r.fighter_a, r.fighter_b, open_=True)}</div>')


def bout_html(r, rec: dict) -> str:
    scale, verdict, pa, pb = pick_parts(r)
    num = f"{int(r.bout_order):02d}" if not isnull(r.bout_order) else ""
    title = '<span class="tag">Title</span>' if r.title_fight else ""
    return (f'<div class="bout"><div class="label"><span>{num} &nbsp;·&nbsp; {esc(f_text(r.weight_class))}</span>{title}</div>'
            f'<div class="duel">{side_html(r.fighter_a, rec.get(r.fighter_a_url, "UFC debut"), "a", pa)}<div class="vs">vs.</div>'
            f'{side_html(r.fighter_b, rec.get(r.fighter_b_url, "UFC debut"), "b", pb)}</div>'
            f'<div style="margin-top:14px">{scale}</div><div class="verdict">{verdict}</div>'
            f'{dist_html(r)}{why_html(r)}{tape_details(pd.Series(r._asdict()), r.fighter_a, r.fighter_b)}</div>')


def prow_html(r, rec: dict) -> str:
    scale, verdict, pa, pb = pick_parts(r)
    num = f"{int(r.bout_order):02d}" if not isnull(r.bout_order) else ""
    small = ""
    if not isnull(r.predicted_winner):
        bits = ([] if isnull(r.tier) or r.tier == "full" else [f"{r.tier} data"]) + (["market-informed"] if getattr(r, "pick_src", None) == "market" else [])
        tag = (", " + ", ".join(bits)) if bits else ""
        small = f'<div class="small">Leans <b>{esc(r.predicted_winner)}</b>, {float(r.confidence):.0%} <i class="muted">({lean(float(r.confidence)).lower()}{esc(tag)})</i></div>'
    if not isnull(r.actual_winner):
        small += f'<div class="small">{mark(int(r.correct) == 1, "Right" if int(r.correct) == 1 else "Wrong")} &nbsp;{esc(r.actual_winner)} won</div>'
    div = esc(f_text(r.weight_class)) + (' <span class="tag">Title</span>' if r.title_fight else "")
    return (f'<div class="prow"><div class="line"><div class="num">{num}</div>'
            f'{side_html(r.fighter_a, rec.get(r.fighter_a_url, "UFC debut"), "a", pa)}'
            f'<div class="mid"><div class="label" style="justify-content:center;margin:0 0 6px">{div}</div>{scale}{small}</div>'
            f'{side_html(r.fighter_b, rec.get(r.fighter_b_url, "UFC debut"), "b", pb)}</div>'
            f'{dist_html(r)}{why_html(r)}{tape_details(pd.Series(r._asdict()), r.fighter_a, r.fighter_b)}</div>')


def page_upcoming():
    events = q("""SELECT event_name, event_date, COUNT(*) FILTER (WHERE status <> 'cancelled') AS bouts
                  FROM upcoming_fights GROUP BY event_name, event_date ORDER BY event_date DESC""")
    if events.empty:
        st.info("No cards have been loaded yet.")
        return
    today = date.today()
    events["event_date"] = pd.to_datetime(events.event_date).dt.date
    future = events[events.event_date >= today].sort_values("event_date")
    past = events[events.event_date < today]
    ordered = pd.concat([future, past])
    labels = [f"{r.event_name}  -  {f_date(r.event_date)}" + ("  (upcoming)" if r.event_date >= today else "")
              for r in ordered.itertuples()]
    choice = st.selectbox("Choose a card", labels, index=0)
    ev = ordered.iloc[labels.index(choice)]

    v3 = has_v3()
    p3_src = "upcoming_picks_v3" if v3 else NO_V3_UPCOMING
    bouts = q(f"""
        SELECT u.bout_id, u.bout_order, u.weight_class, u.title_fight, u.status AS bout_status,
               u.fighter_a_name AS fighter_a, u.fighter_b_name AS fighter_b, u.fighter_a_url, u.fighter_b_url,
               COALESCE(p3.p_a_wins, p.p_a_wins) AS p_a_wins, COALESCE(p3.predicted_winner, p.predicted_winner) AS predicted_winner,
               COALESCE(p3.confidence, p.confidence) AS confidence, COALESCE(p3.predicted_at, p.predicted_at) AS predicted_at,
               p.tier, p.method,
               CASE WHEN p3.bout_id IS NOT NULL THEN 'market' WHEN p.bout_id IS NOT NULL THEN 'model' END AS pick_src,
               p.predicted_winner AS v2_winner, p.confidence AS v2_conf,
               CASE WHEN p3.bout_id IS NOT NULL THEN p3.actual_winner ELSE p.actual_winner END AS actual_winner,
               CASE WHEN p3.bout_id IS NOT NULL THEN p3.correct ELSE p.correct END AS correct,
               p.a_prior_fights, p.b_prior_fights, {STAT_COLS}
        FROM upcoming_fights u
        LEFT JOIN upcoming_picks_v2 p ON p.bout_id = u.bout_id
        LEFT JOIN {p3_src} p3 ON p3.bout_id = u.bout_id
        LEFT JOIN upcoming_model_rows m ON m.bout_id = u.bout_id
        WHERE u.event_name = :e AND u.event_date = :d
        ORDER BY u.bout_order""", {"e": ev.event_name, "d": ev.event_date})
    all_urls = list(set(bouts.fighter_a_url) | set(bouts.fighter_b_url))
    stance = q("""SELECT DISTINCT ON (ff.fighter_url) ff.fighter_url, ff.stance FROM fighter_fights ff
                  JOIN fights f USING (fight_url) WHERE ff.fighter_url = ANY(:u)
                  ORDER BY ff.fighter_url, f.event_date DESC""", {"u": all_urls})
    stance_map = dict(zip(stance.fighter_url, stance.stance))
    bouts["a_stance"] = bouts.fighter_a_url.map(stance_map)
    bouts["b_stance"] = bouts.fighter_b_url.map(stance_map)

    # context lines (shown next to the model's pick, not used by it): last fight and recent finishes
    last3 = q("""SELECT fighter_url, event_date, won, result, rn FROM (
                   SELECT ff.fighter_url, f.event_date, ff.won, f.result,
                          ROW_NUMBER() OVER (PARTITION BY ff.fighter_url ORDER BY f.event_date DESC, f.fight_url DESC) AS rn
                   FROM fighter_fights ff JOIN fights f USING (fight_url) WHERE ff.fighter_url = ANY(:u)) t
                 WHERE rn <= 3""", {"u": all_urls})
    ctx = {}
    for url, g in last3.groupby("fighter_url"):
        g = g.sort_values("rn")
        lf = g.iloc[0]
        months = (pd.Timestamp(ev.event_date) - pd.Timestamp(lf.event_date)).days / 30.44
        ago = f"{months:.0f} mo ago" if months < 24 else f"{months / 12:.1f} yr ago"
        fins = int(((g.won == 1) & g.result.isin(["KO/TKO", "Submission"])).sum())
        ctx[url] = (f"{'W' if int(lf.won) == 1 else 'L'} {method_text(lf.result)}, {ago}", f"{fins} of {len(g)}")
    for side in "ab":
        bouts[f"{side}_lastfight"] = bouts[f"fighter_{side}_url"].map(lambda u: ctx.get(u, (None, None))[0])
        bouts[f"{side}_fin3"] = bouts[f"fighter_{side}_url"].map(lambda u: ctx.get(u, (None, None))[1])
    live = bouts[bouts.bout_status != "cancelled"]
    gone = bouts[bouts.bout_status == "cancelled"]
    REASONS.clear()
    if has_reasons():
        rr = q("SELECT bout_id, version, reasons FROM pick_reasons WHERE bout_id = ANY(:ids)", {"ids": [str(x) for x in bouts.bout_id]})
        for x in rr.itertuples():
            REASONS.setdefault(x.bout_id, {})[x.version] = x.reasons if isinstance(x.reasons, dict) else json.loads(x.reasons)

    DISTANCE.clear()
    if has_table("upcoming_distance_v1"):
        dd = q("SELECT bout_id, p_distance, scheduled_rounds, tier FROM upcoming_distance_v1 WHERE status = 'locked' AND bout_id = ANY(:ids)",
               {"ids": [str(x) for x in bouts.bout_id]})
        for x in dd.itertuples():
            DISTANCE[x.bout_id] = (float(x.p_distance), int(x.scheduled_rounds), str(x.tier), None)
        try:                               # the market-informed prediction, where one is locked: shown beside the model-only one, and never able to break the page
            if has_table("upcoming_distance_v2"):
                for x in q("SELECT bout_id, p_distance FROM upcoming_distance_v2 WHERE status = 'locked' AND bout_id = ANY(:ids)", {"ids": [str(b) for b in bouts.bout_id]}).itertuples():
                    if x.bout_id in DISTANCE:
                        DISTANCE[x.bout_id] = DISTANCE[x.bout_id][:3] + (float(x.p_distance),)
        except Exception:
            pass

    urls = list(set(live.fighter_a_url) | set(live.fighter_b_url))
    rec = {}
    if urls:
        r = q("""SELECT fighter_url, SUM(won) AS w, COUNT(*) - SUM(won) AS l FROM fighter_fights
                 WHERE fighter_url = ANY(:u) GROUP BY fighter_url""", {"u": urls})
        rec = {x.fighter_url: f"UFC {int(x.w)}-{int(x.l)}" for x in r.itertuples()}

    graded = live[live.correct.notna()]
    n_picks = int(live.p_a_wins.notna().sum())
    n_thin = int(live.tier.isin(["limited", "debut"]).sum())
    n_mkt = int((live.pick_src == "market").sum())
    right = f"{int(graded.correct.sum())} of {len(graded)}" if len(graded) else "waiting for results"
    when = pd.Timestamp(ev.event_date).strftime("%A, %B %d, %Y")
    module_html(f'<div class="hero"><div class="kicker mono">{esc(days_label(ev.event_date, today))} &nbsp;·&nbsp; {esc(when)}</div>'
                f'<h1>{esc(ev.event_name)}</h1>'
                f'<div class="facts"><span><b>{len(live)}</b> bouts</span><span><b>{n_picks}</b> model picks</span>'
                f'<span><b>{n_mkt}</b> market-informed</span><span><b>{n_thin}</b> on limited data</span><span>Picks right: <b>{esc(right)}</b></span></div>'
                f'<div class="aside">Left and right is just the order on the card, not the favourite. Each pick is saved with a timestamp '
                f'before the event and never changed.</div></div>')
    rows = list(live.itertuples())
    if rows:
        module_html(main_event_html(rows[0], rec))
    main_card, prelims = rows[1:5], rows[5:]
    if main_card:
        module_html('<div class="sect"><h3>Main card</h3></div><div class="bgrid">' + "".join(bout_html(r, rec) for r in main_card) + "</div>")
    if prelims:
        module_html('<div class="sect"><h3>Prelims</h3></div>' + "".join(prow_html(r, rec) for r in prelims))
    if not gone.empty:
        names = ", ".join(f"{esc(r.fighter_a)} vs {esc(r.fighter_b)}" for r in gone.itertuples())
        module_html(f'<div class="note">Removed from the card: {names}.</div>')


# ------------------------------------------------------------------------------------------------
# page: past events
# ------------------------------------------------------------------------------------------------
def page_past():
    ev = q("""SELECT event_name, event_date, COUNT(*) AS fights FROM fights
              GROUP BY event_name, event_date ORDER BY event_date DESC""")
    ev["event_date"] = pd.to_datetime(ev.event_date)
    years = sorted(ev.event_date.dt.year.unique(), reverse=True)
    c1, c2 = st.columns([1, 3])
    year = c1.selectbox("Year", years, index=0)
    sub = ev[ev.event_date.dt.year == year]
    labels = [f"{r.event_date.strftime('%b %d')}  -  {r.event_name}  ({int(r.fights)} fights)" for r in sub.itertuples()]
    choice = c2.selectbox("Event", labels, index=0)
    row = sub.iloc[labels.index(choice)]
    p3_src = "fight_picks_v3" if has_v3() else NO_V3_PAST
    fights = q(f"""
        SELECT f.weight_class, {title_rules.championship_sql()} AS championship, {title_rules.tournament_sql()} AS tournament,
               w.fighter_name AS winner, l.fighter_name AS loser, f.result,
               f.finish_round, f.finish_time, f.fight_url AS fight_url, p.fighter_a AS pa, p.fighter_b AS pb,
               COALESCE(p3.predicted_winner, p.predicted_winner) AS predicted_winner, COALESCE(p3.confidence, p.confidence) AS confidence,
               CASE WHEN p3.fight_url IS NOT NULL THEN p3.correct ELSE p.correct END AS correct, p.tier, p.method,
               CASE WHEN p3.fight_url IS NOT NULL THEN 'market' END AS src
        FROM fights f
        JOIN fighter_fights w ON w.fight_url = f.fight_url AND w.won = 1
        JOIN fighter_fights l ON l.fight_url = f.fight_url AND l.won = 0
        LEFT JOIN fight_picks_v2 p ON p.fight_url = f.fight_url
        LEFT JOIN {p3_src} p3 ON p3.fight_url = f.fight_url
        WHERE f.event_name = :e AND f.event_date = :d
        ORDER BY {title_rules.order_sql()}, f.fight_url""", {"e": row.event_name, "d": row.event_date.date()})
    first_model = q("SELECT MIN(event_date) AS d FROM fight_picks_v2 WHERE method = 'model'").iloc[0, 0]
    REASONS.clear()
    if has_fight_reasons():
        rr = q("SELECT fight_url, version, reasons FROM fight_reasons WHERE fight_url = ANY(:ids)", {"ids": [str(x) for x in fights.fight_url]})
        for x in rr.itertuples():
            REASONS.setdefault(x.fight_url, {})[x.version] = x.reasons if isinstance(x.reasons, dict) else json.loads(x.reasons)
    scored = fights[fights.correct.notna()]
    model_fact = f'<span>Model: <b>{int(scored.correct.sum())} of {len(scored)}</b> right</span>' if len(scored) else ""
    body = []
    for r in fights.itertuples():
        title = ' <span class="tag">Title</span>' if r.championship else (' <span class="tag">Tournament</span>' if r.tournament else "")
        if isnull(r.correct):
            model = '<span class="muted small">no pick stored yet</span>'
        else:
            ok = int(r.correct) == 1
            bits = []
            if r.tier in ("limited", "debut"):
                bits.append(f"{r.tier} data")
            if r.method == "early_rule":
                bits.append("early-era rule")
            if getattr(r, "src", None) == "market":
                bits.append("market-informed")
            tag = f' <span class="muted small">· {esc(", ".join(bits))}</span>' if bits else ""
            model = mark(ok, f"{str(r.predicted_winner)} ({float(r.confidence):.0%})") + tag
        why = ""
        if not isnull(r.correct) and r.fight_url in REASONS:
            why = why_html(SimpleNamespace(bout_id=r.fight_url, predicted_winner=r.predicted_winner, fighter_a=r.pa, fighter_b=r.pb,
                                           pick_src="market" if getattr(r, "src", None) == "market" else "model"))
        body.append(f'<div class="res"><div class="div">{esc(f_text(r.weight_class))}{title}</div>'
                    f'<div><span class="w">{esc(r.winner)}</span> <span class="lo">def. {esc(r.loser)}</span></div>'
                    f'<div class="how">{method_text(r.result)} &nbsp;R{f_int(r.finish_round)} {esc(f_text(r.finish_time))}</div><div>{model}</div></div>'
                    + (f'<div class="whyrow">{why}</div>' if why else ""))
    early = ""
    if not isnull(first_model):
        early = (f" Fights before {pd.Timestamp(first_model).strftime('%B %Y')} use a simple rule (higher Elo rating, then the younger fighter) "
                 f"because the model had too few earlier fights to learn from.")
    module_html(f'<div class="hero"><div class="kicker mono">{esc(row.event_date.strftime("%A, %B %d, %Y"))}</div><h1>{esc(row.event_name)}</h1>'
                f'<div class="facts"><span><b>{len(fights)}</b> fights</span>{model_fact}</div></div>'
                f'<div style="margin-top:8px">{"".join(body)}</div>'
                f'<div class="note">Every pick here is an after-the-fact test: the model is retrained using only fights from before the event, and the '
                f'"Why this pick?" panels show what it saw at that time. '
                f'Fights with a debut or newcomer carry less information, so their picks are less reliable.{esc(early)}'
                f'{" From 2016 on, picks also use the fight’s opening betting line where one exists (market-informed)." if has_v3() else ""}</div>')


# ------------------------------------------------------------------------------------------------
# page: fighters
# ------------------------------------------------------------------------------------------------
def career(url: str) -> dict:
    r = q("""SELECT COUNT(*) AS fights, SUM(won) AS wins, SUM(ko_win) AS ko_wins, SUM(sub_win) AS sub_wins,
                    SUM(sig_land)::float8 / NULLIF(SUM(dur_sec) / 60.0, 0) AS slpm,
                    SUM(sig_absorbed)::float8 / NULLIF(SUM(dur_sec) / 60.0, 0) AS sapm,
                    SUM(sig_land)::float8 / NULLIF(SUM(sig_att), 0) AS sig_acc,
                    SUM(td_land)::float8 / NULLIF(SUM(dur_sec) / 900.0, 0) AS td15,
                    1 - SUM(opp_td_land)::float8 / NULLIF(SUM(opp_td_att), 0) AS td_def,
                    SUM(ctrl_sec)::float8 / NULLIF(SUM(dur_sec), 0) AS ctrl_share
             FROM fighter_fights_enriched WHERE fighter_url = :u""", {"u": url}).iloc[0].to_dict()
    bio = q("""SELECT ff.height_cm, ff.reach_cm, ff.stance, ff.dob FROM fighter_fights ff JOIN fights f USING (fight_url)
               WHERE ff.fighter_url = :u ORDER BY f.event_date DESC LIMIT 1""", {"u": url}).iloc[0].to_dict()
    return {**r, **bio}


def page_fighters():
    idx = q("""SELECT ff.fighter_url, MAX(ff.fighter_name) AS name, COUNT(*) AS fights, MAX(f.event_date) AS last_fight
               FROM fighter_fights ff JOIN fights f USING (fight_url) GROUP BY ff.fighter_url ORDER BY MAX(ff.fighter_name)""")
    idx["last_fight"] = pd.to_datetime(idx.last_fight)
    labels = [f"{r.name}  -  {int(r.fights)} UFC fights (last {r.last_fight.year})" for r in idx.itertuples()]
    # open on the headliner of the nearest upcoming card (and their opponent for the comparison) when there is one
    d_a, d_b = 0, min(1, len(labels) - 1)
    try:
        hl = q("""SELECT fighter_a_url, fighter_b_url FROM upcoming_fights WHERE status = 'scheduled'
                  ORDER BY (event_date < CURRENT_DATE), event_date, bout_order LIMIT 1""")
        if not hl.empty:
            pos = {u: i for i, u in enumerate(idx.fighter_url)}
            d_a = pos.get(hl.iloc[0].fighter_a_url, d_a)
            d_b = pos.get(hl.iloc[0].fighter_b_url, d_b)
    except Exception:
        pass
    tab_profile, tab_compare = st.tabs(["Profile", "Compare two fighters"])

    with tab_profile:
        choice = st.selectbox("Search for a fighter", labels, index=d_a, key="fighter_one")
        fr = idx.iloc[labels.index(choice)]
        c = career(fr.fighter_url)
        wins, fights = int(c["wins"]), int(c["fights"])
        hist = q("""SELECT f.event_date, f.event_name, o.fighter_name AS opponent, a.won, f.result, f.finish_round,
                           f.finish_time, f.weight_class
                    FROM fighter_fights a JOIN fights f ON f.fight_url = a.fight_url
                    JOIN fighter_fights o ON o.fight_url = a.fight_url AND o.fighter_url <> a.fighter_url
                    WHERE a.fighter_url = :u ORDER BY f.event_date DESC""", {"u": fr.fighter_url})
        trs = "".join(
            f'<tr><td class="mono">{pd.Timestamp(h.event_date).strftime("%Y-%m-%d")}</td><td>{esc(h.event_name)}</td><td>{esc(h.opponent)}</td>'
            f'<td>{mark(int(h.won) == 1, "W" if int(h.won) == 1 else "L")}</td><td class="mono">{method_text(h.result)}</td>'
            f'<td class="mono">R{f_int(h.finish_round)} {esc(f_text(h.finish_time))}</td><td class="muted">{esc(f_text(h.weight_class))}</td></tr>'
            for h in hist.itertuples())
        stats = [(f_num(c["slpm"], 2), "Strikes landed / min"), (f_num(c["sapm"], 2), "Strikes absorbed / min"),
                 (f_pct(c["sig_acc"]), "Striking accuracy"), (f_num(c["td15"], 2), "Takedowns / 15 min"),
                 (f_pct(c["td_def"]), "Takedown defense"), (f_pct(c["ctrl_share"]), "Control time"),
                 (f"{int(c['ko_wins'])}", "KO/TKO wins"), (f"{int(c['sub_wins'])}", "Submission wins")]
        module_html(f'<div class="hero"><div class="kicker mono">UFC record &nbsp;{wins}-{fights - wins}</div><h1>{esc(fr["name"])}</h1>'
                    f'<div class="facts"><span>Height <b>{esc(f_height(c["height_cm"]))}</b></span><span>Reach <b>{esc(f_reach(c["reach_cm"]))}</b></span>'
                    f'<span>Stance <b>{esc(f_text(c["stance"]))}</b></span></div></div>{sheet(stats)}'
                    f'<div class="sect"><h3>Fight history</h3></div>'
                    f'<table class="hist"><tr><th>Date</th><th>Event</th><th>Opponent</th><th>Result</th><th>Method</th><th>Round / time</th>'
                    f'<th>Division</th></tr>{trs}</table>'
                    f'<div class="note">Career numbers come from UFC fights in the dataset only; earlier pro fights are not included.</div>')

    with tab_compare:
        col1, col2 = st.columns(2)
        ca = col1.selectbox("Fighter A", labels, index=d_a, key="cmp_a")
        cb = col2.selectbox("Fighter B", labels, index=d_b, key="cmp_b")
        ra, rb = idx.iloc[labels.index(ca)], idx.iloc[labels.index(cb)]
        sa_, sb_ = career(ra.fighter_url), career(rb.fighter_url)
        row = {}
        for key, val in (("a", sa_), ("b", sb_)):
            w, n = val["wins"], val["fights"]
            row.update({f"{key}_n_prev": n, f"{key}_prev_win_rate": w / n if n else None,
                        f"{key}_height": val["height_cm"], f"{key}_reach": val["reach_cm"], f"{key}_stance": val["stance"],
                        f"{key}_slpm": val["slpm"], f"{key}_sapm": val["sapm"], f"{key}_sig_acc": val["sig_acc"],
                        f"{key}_td_per15": val["td15"], f"{key}_td_def": val["td_def"], f"{key}_ctrl_share": val["ctrl_share"],
                        f"{key}_ko_win_rate": val["ko_wins"] / n if n else None,
                        f"{key}_sub_win_rate": val["sub_wins"] / n if n else None})
        wanted = {"n_prev", "prev_win_rate", "height", "reach", "stance", "slpm", "sapm", "sig_acc", "td_per15", "td_def",
                  "ctrl_share", "ko_win_rate", "sub_win_rate"}
        rec_a = f"UFC {int(sa_['wins'])}-{int(sa_['fights']) - int(sa_['wins'])}"
        rec_b = f"UFC {int(sb_['wins'])}-{int(sb_['fights']) - int(sb_['wins'])}"
        module_html(f'<div class="main" style="--ln:clamp(2rem,5.5vw,3.8rem)"><div class="duel">{side_html(ra["name"], rec_a, "a")}'
                    f'<div class="vs">vs.</div>{side_html(rb["name"], rec_b, "b")}</div>'
                    f'<div style="margin-top:14px">{tape_html(row, ra["name"], rb["name"], wanted)}</div></div>'
                    f'<div class="note">Career numbers across all their UFC fights.</div>')


# ------------------------------------------------------------------------------------------------
# page: track record
# ------------------------------------------------------------------------------------------------
def style_chart(ch):
    mono = "IBM Plex Mono, Courier New, monospace"
    return (ch.configure(background="transparent").configure_view(stroke=None)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor="#d9d4c7", domainColor=INK, tickColor=INK,
                            labelFont=mono, titleFont=mono, labelFontSize=11)
            .configure_legend(labelColor=INK, titleColor=MUTED))


def page_track():
    up = q("""SELECT event_name, event_date, fighter_a, fighter_b, tier, status, predicted_winner, confidence, actual_winner, correct,
                     predicted_at, graded_at FROM upcoming_picks_v2""")
    graded = up[up.correct.notna()]
    pending = up[(up.status == "locked") & up.graded_at.isna()]
    acc = f"{graded.correct.mean():.0%}" if len(graded) else "–"
    module_html('<div class="hero"><div class="kicker mono">The record that counts</div><h1>Locked pre-fight predictions</h1>'
                '<div class="aside" style="margin-top:0">Made before each card and never changed. Every fight gets a pick.</div></div>'
                + figs([(str(len(graded)), "Graded"), (str(int(graded.correct.sum())) if len(graded) else "–", "Right"),
                        (acc, "Accuracy"), (str(len(pending)), "Waiting for results")]))
    if len(graded):
        g = graded.sort_values("event_date", ascending=False)
        rows = "".join(
            f'<div class="res three"><div><span class="w">{esc(r.fighter_a)}</span> <span class="lo">vs</span> <span class="w">{esc(r.fighter_b)}</span>'
            f'<div class="div">{esc(r.event_name)}</div></div>'
            f'<div class="how">Pick: {esc(str(r.predicted_winner))} ({float(r.confidence):.0%}){"" if r.tier == "full" else " · " + esc(str(r.tier)) + " data"}</div>'
            f'<div>{mark(int(r.correct) == 1, ("Right" if int(r.correct) == 1 else "Wrong") + ", " + str(r.actual_winner) + " won")}</div></div>'
            for r in g.itertuples())
        module_html(rows + f'<div class="note">{len(graded)} graded predictions is a small sample, so expect this number to swing a lot at first.</div>')
    else:
        module_html('<div class="note">No locked predictions have been graded yet. The first ones appear after the next card finishes and '
                    'the dataset updates (usually a day or two).</div>')

    if has_v3():
        l3 = q("""SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE correct IS NOT NULL) AS g, COALESCE(SUM(correct), 0) AS c,
                         COUNT(*) FILTER (WHERE graded_at IS NULL AND status = 'locked') AS w FROM upcoming_picks_v3""").iloc[0]
        if int(l3.n) > 0:
            module_html(f'<div class="note">Market-informed picks (they also use the betting line): {int(l3.n)} locked, {int(l3.g)} graded '
                        f'({int(l3.c)} right), {int(l3.w)} waiting for results. The picks above are the model-only ones.</div>')

    sb = q("SELECT event_date, event_name, fights, correct, running_accuracy, total_fights FROM v2_scoreboard_running ORDER BY event_date")
    if sb.empty:
        module_html('<div class="sect"><h3>Backtest scoreboard</h3></div>')
        st.info("No backtest data yet.")
        return
    start_year = pd.Timestamp(sb.event_date.min()).year
    module_html(f'<div class="sect"><h3>Backtest scoreboard</h3></div><div class="aside" style="margin-top:0">Every fight from {start_year} onward, '
                'predicted using only data from before its event, with no fights left out. Generated after the fact, so it is a fair test but '
                'not a pre-fight record.</div>')
    sb["running_accuracy"] = pd.to_numeric(sb.running_accuracy)
    last = sb.iloc[-1]
    module_html(figs([(f"{int(last.total_fights):,}", "Fights scored"), (f"{last.running_accuracy:.1%}", "Accuracy so far"), ("50%", "Coin flip")]))
    line_df = sb.assign(event_date=pd.to_datetime(sb.event_date))[["event_date", "running_accuracy"]]
    base = alt.Chart(line_df).encode(x=alt.X("event_date:T", title=None, axis=alt.Axis(format="%Y", labelOverlap=True)))
    yenc = alt.Y("running_accuracy:Q", scale=alt.Scale(domain=[0.4, 0.8], clamp=True),
                 axis=alt.Axis(format="%", title=None, values=[0.4, 0.5, 0.6, 0.7, 0.8]))
    area = base.mark_area(opacity=.07, color=INK, clip=True).encode(y=yenc)
    line = base.mark_line(color=INK, strokeWidth=2.5, clip=True).encode(y=yenc)
    rule = alt.Chart(pd.DataFrame({"running_accuracy": [0.5]})).mark_rule(color=ACCENT, strokeDash=[6, 5], strokeWidth=2).encode(y=yenc)
    module_html('<div class="note" style="margin:0 0 4px">Running accuracy by event. The dashed red line is a coin flip.</div>')
    st.altair_chart(style_chart((area + line + rule).properties(height=300)), theme=None)

    tiers = q("SELECT tier, fights, accuracy FROM v2_tier_accuracy WHERE method = 'model'")
    order = {"full": 0, "limited": 1, "debut": 2}
    names = {"full": "Both have 3+ UFC fights", "limited": "A fighter has 1-2", "debut": "A fighter is a debut"}
    tiers = tiers.sort_values("tier", key=lambda c: c.map(order))
    module_html('<div class="sect"><h3>Accuracy by how much history the fighters have</h3></div>'
                + figs([(f"{float(t.accuracy):.0%}", f"{names.get(t.tier, t.tier)} ({int(t.fights):,} fights)") for t in tiers.itertuples()])
                + '<div class="note">Fights with less history are harder, so those picks are less reliable.</div>')

    if has_v3():
        s3 = q("SELECT fights, first_date, v2_accuracy, v3_accuracy, market_accuracy FROM v3_summary")
        if len(s3) and not isnull(s3.iloc[0].fights) and int(s3.iloc[0].fights) > 0:
            r3 = s3.iloc[0]
            module_html('<div class="sect"><h3>Does the betting market add information?</h3></div>'
                        f'<div class="aside" style="margin-top:0">Since {pd.Timestamp(r3.first_date).year}, on the {int(r3.fights):,} fights that have an opening '
                        'betting line, each pick was made using only earlier fights. The line is used only as a forecasting input. This is not betting advice, '
                        'and the app never shows odds.</div>'
                        + figs([(f"{float(r3.v2_accuracy):.1%}", "Model-only (v2)"), (f"{float(r3.v3_accuracy):.1%}", "Model + market (v3)"),
                                (f"{float(r3.market_accuracy):.1%}", "Opening line alone")]))
            y3 = q("SELECT year, fights, v2_accuracy, v3_accuracy, market_accuracy FROM v3_year_accuracy ORDER BY year")
            names3 = {"v2_accuracy": "Model-only (v2)", "v3_accuracy": "Model + market (v3)", "market_accuracy": "Opening line alone"}
            long3 = y3.melt(id_vars=["year", "fights"], value_vars=list(names3), var_name="series", value_name="accuracy")
            long3["accuracy"] = pd.to_numeric(long3.accuracy)
            long3["series"] = long3.series.map(names3)
            lines3 = alt.Chart(long3).mark_line(point=True, strokeWidth=2.5).encode(
                x=alt.X("year:O", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("accuracy:Q", scale=alt.Scale(domain=[0.5, 0.78], clamp=True), axis=alt.Axis(format="%", title=None)),
                color=alt.Color("series:N", scale=alt.Scale(domain=list(names3.values()), range=[MUTED, ACCENT, INK]),
                                legend=alt.Legend(orient="bottom", title=None)),
                tooltip=["series", "year", alt.Tooltip("accuracy:Q", format=".1%"), "fights"])
            st.altair_chart(style_chart(lines3.properties(height=260)), theme=None)
            t3 = q("SELECT tier, fights, v2_accuracy, v3_accuracy, market_accuracy FROM v3_tier_accuracy")
            t3 = t3.sort_values("tier", key=lambda c: c.map({"full": 0, "limited": 1, "debut": 2}))
            tn = {"full": "Both have 3+ UFC fights", "limited": "A fighter has 1-2", "debut": "A fighter is a debut"}
            trs3 = "".join(f'<tr><td>{esc(tn.get(t.tier, t.tier))}</td><td class="mono">{int(t.fights):,}</td><td class="mono">{float(t.v2_accuracy):.1%}</td>'
                           f'<td class="mono"><b>{float(t.v3_accuracy):.1%}</b></td><td class="mono">{float(t.market_accuracy):.1%}</td></tr>' for t in t3.itertuples())
            module_html(f'<table class="hist"><tr><th>Fighters\' history</th><th>Fights</th><th>Model-only</th><th>Model + market</th><th>Line alone</th></tr>{trs3}</table>'
                        '<div class="note">The market knows things stats cannot, such as injuries, camp news and pre-UFC records, so the gain is largest for '
                        'debutants. Backtest picks use each fight’s opening line; picks for upcoming cards use the line loaded when they are locked. '
                        'Betting lines: BestFightOdds, via the UFC Data set by N. Bair (CC BY 4.0, github.com/nlbair/ufc-data), built from ufcstats.com, '
                        'MMA Decisions, BestFightOdds and public ranking archives.</div>')
            d3 = q("SELECT fighter_a, actual_winner, p_v2_a, market_prob_a, p_a_wins, tier FROM fight_picks_v3")
            if len(d3) >= 200:
                module_html(scoring_html(prob_scores(d3)))

    cb = q("SELECT confidence_bucket, fights, accuracy FROM v2_confidence_buckets ORDER BY confidence_bucket")
    cb["accuracy"] = pd.to_numeric(cb.accuracy)
    cb["label"] = cb.confidence_bucket.str[3:].str.replace(" pct", "%", regex=False).str.replace(r"\+%$", "%+", regex=True)
    cb["hi"] = [i >= 3 for i in range(len(cb))]
    module_html('<div class="sect"><h3>Accuracy by how confident the model was</h3></div>')
    bars = alt.Chart(cb).mark_bar(size=54).encode(
        x=alt.X("label:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("accuracy:Q", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%", title=None)),
        color=alt.Color("hi:N", scale=alt.Scale(domain=[False, True], range=[INK, ACCENT]), legend=None),
        tooltip=["label", alt.Tooltip("accuracy:Q", format=".0%"), "fights"])
    text = bars.mark_text(dy=-9, color=INK, fontWeight="bold", font="IBM Plex Mono, monospace").encode(text=alt.Text("accuracy:Q", format=".0%"), color=alt.value(INK))
    st.altair_chart(style_chart((bars + text).properties(height=260)), theme=None)
    lo, hi = cb.accuracy.min(), cb.accuracy.max()
    module_html(f'<div class="note">Accuracy rises with confidence: from about {lo:.0%} for the closest calls to about {hi:.0%} for the most '
                'confident picks (65% and up in red). The very top picks run a few points below the number they state, so a stated 80% is '
                'closer to 77% in practice.</div>')

    yr = q("SELECT year, fights, accuracy FROM v2_year_accuracy ORDER BY year")
    yr["accuracy"] = pd.to_numeric(yr.accuracy)
    module_html('<div class="sect"><h3>Accuracy by year</h3></div>')
    ybars = alt.Chart(yr).mark_bar(size=22, color=INK).encode(
        x=alt.X("year:O", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("accuracy:Q", scale=alt.Scale(domain=[0.4, 0.8], clamp=True), axis=alt.Axis(format="%", title=None)),
        tooltip=["year", alt.Tooltip("accuracy:Q", format=".0%"), "fights"])
    yrule = alt.Chart(pd.DataFrame({"y": [0.5]})).mark_rule(color=ACCENT, strokeDash=[6, 5], strokeWidth=2).encode(y="y:Q")
    st.altair_chart(style_chart((ybars + yrule).properties(height=220)), theme=None)
    module_html('<div class="note">Single years swing by several points because each has only a few hundred fights.</div>')

    old = q("""SELECT COUNT(*) AS n, SUM(correct) AS c FROM upcoming_predictions WHERE correct IS NOT NULL""")
    if len(old) and not isnull(old.iloc[0].n) and int(old.iloc[0].n) > 0:
        module_html(f'<div class="note">Model v1 (the first version, which skipped fights with limited history) has {int(old.iloc[0].n)} graded locked picks, '
                    f'{int(old.iloc[0].c)} right. Version 2 replaced it.</div>')

    if has_table("fight_distance_v1"):
        dd = q("SELECT event_date, p_distance, p_base, went_distance, tier FROM fight_distance_v1")
        if len(dd) >= 200:
            live_d = None
            if has_table("upcoming_distance_v1"):
                lv = q("SELECT event_date, p_distance, p_base, went_distance FROM upcoming_distance_v1 WHERE status = 'locked' AND graded_at IS NOT NULL")
                live_d = distance_scores.distance_scores(lv) if len(lv) >= 5 else None
            module_html(dist_section_html(distance_scores.distance_scores(dd), live_d))
            try:                           # the market-informed section: a new part of the page must never be able to break it
                if has_table("fight_distance_v2"):
                    d2 = q("SELECT event_date, p_distance, p_v1, p_base, went_distance, tier FROM fight_distance_v2")
                    if len(d2) >= 200:
                        live2 = q("""SELECT m.p_distance AS p_v2, v.p_distance AS p_v1, m.went_distance FROM upcoming_distance_v2 m JOIN upcoming_distance_v1 v ON v.bout_id = m.bout_id
                                     WHERE m.status = 'locked' AND m.graded_at IS NOT NULL AND m.went_distance IS NOT NULL""") if has_table("upcoming_distance_v2") else None
                        module_html(dist_section_v2_html(distance_scores.distance_scores(d2), distance_scores.distance_scores(d2.assign(p_distance=d2.p_v1))["scores"]["model"],
                                                         distance_scores.paired_vs(d2, "p_distance", "p_v1"), distance_scores.years_better_vs(d2, "p_distance", "p_v1"),
                                                         distance_scores.pair_summary(live2, "p_v2", "p_v1") if live2 is not None and len(live2) >= 5 else None))
            except Exception:
                pass


# ------------------------------------------------------------------------------------------------
# layout
# ------------------------------------------------------------------------------------------------
def main():
    st.markdown(CSS, unsafe_allow_html=True)
    if get_engine() is None:
        st.error("The app isn't connected to a database (DATABASE_URL is missing).")
        st.stop()
    newest = None
    try:
        newest = q("SELECT MAX(event_date) AS d FROM fights").iloc[0, 0]
    except Exception:
        pass
    today = date.today()
    module_html(f'<div class="mast"><div class="top"><div class="brand disp">UFC Fight Predictor</div>'
                f'<div class="dateline mono">{esc(today.strftime("%A, %B %d, %Y"))} &nbsp;·&nbsp; Results through {esc(f_date(newest))}</div></div></div>')
    if os.getenv("DEMO_MODE"):                       # set by demo/run_demo.py: never on the real site
        module_html('<div class="demo-banner"><b>DEMO</b>Every fighter, fight and result on this site is invented. The data pipeline, the models and these pages are the real ones, '
                    'running on made-up data, so the numbers show how the system works and say nothing about real fights.</div>')
    page = st.radio("Navigate", ["Upcoming", "Past events", "Fighters", "Track record", "Game"],
                    horizontal=True, label_visibility="collapsed")
    try:
        {"Upcoming": page_upcoming, "Past events": page_past, "Fighters": page_fighters,
         "Track record": page_track, "Game": page_game}[page]()
    except (sa.exc.SQLAlchemyError, RuntimeError):
        st.error("Couldn't load data right now. If the database was idle it may need a few seconds to wake up; "
                 "refresh the page and try again.")
    module_html(f'<div class="foot">{esc(DISCLAIMER)}</div>')


main()
