"""
The friends' game page. `render` is given the read-only query function and the game's own database engine, so it can be driven by tests.

It is built from the site's own look (the `.ufc` styles in streamlit_app.py): the same headlines, italic first names over bold surnames,
red MODEL PICK tags, number strips and tables as the Upcoming page. Only the NEXT open card is shown for picks; the one after it opens
when this one closes.

Everything dynamic that is shown as HTML (nicknames, fighter and event names) is escaped first: a nickname is exactly the kind of text
someone could try to use to inject markup.
"""
from __future__ import annotations

import hashlib
import html
from datetime import datetime, timedelta, timezone

import pandas as pd
import sqlalchemy as sa
import streamlit as st

import game_core as core
import game_db
from game_scores import DIST_SQL, model_winners as _model_winners, score_all as _scored, starts as _starts

OPEN_COLS = ["bout_id", "event_name", "event_date", "bout_order", "weight_class", "title_fight", "fighter_a", "fighter_b", "status"]
OPEN_SQL = """SELECT bout_id, event_name, event_date, bout_order, weight_class, title_fight, fighter_a_name AS fighter_a, fighter_b_name AS fighter_b, status
              FROM upcoming_fights WHERE status = 'scheduled' AND event_date >= :t ORDER BY event_date, bout_order"""
CLOSED_SQL = """SELECT bout_id, event_name, event_date, bout_order, fighter_a_name AS fighter_a, fighter_b_name AS fighter_b
                FROM upcoming_fights WHERE status = 'scheduled' AND event_date <= :t ORDER BY event_date DESC, bout_order"""
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
FINE_PRINT = """
**What you pick.** For each fight on the next card: the winner, how it ends (KO/TKO, Submission or Decision) and, for a finish, the round. Change your picks, or clear one, as often as you like until the card closes: **6 hours before its first fight starts**, the same for every card (the time comes from the betting feed; until it is known, picks close at 18:00 UTC the day before the event). Nobody can see anyone else's picks until then.

**Points.** Winner right: **+1**. Method right (and the winner too): **+1**. Round right (winner and method too, for a finish): **+1**. That is up to 3 a fight.

**Versus the model.** The winner call and the goes-the-distance call (a Decision pick means "goes the distance") are each scored as *your result minus the model's*: **+1** if you are right and the model is wrong, **-1** if you are wrong and the model is right, **0** otherwise. Copying the model earns exactly 0, so the only way up is to disagree with it and be right.

**Fine print.** A cancelled bout, a bout that never happened, or one with no winner is ignored. A fight that ends in a disqualification scores the winner only. Only fights you picked count, and you are never penalised for skipping one.

**Privacy.** The site stores your nickname, a scrambled (hashed) PIN and your picks, nothing else: no email, no real name. Your PIN cannot be recovered, so if you forget it ask whoever runs the game to reset it. You can delete your account and picks yourself at any time.
"""
GAME_CSS = """<style>
.ufc .gm-chip {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.64rem; font-weight:600; letter-spacing:.14em; text-transform:uppercase; white-space:nowrap;
  padding:1px 8px; border:1px solid var(--hair); color:var(--muted); margin-left:auto;}
.ufc .gm-chip.good {color:var(--good); border-color:var(--good);}
.ufc .gm-chip.warn {color:#fff; background:var(--accent); border-color:var(--accent);}
.ufc .bout.gm {padding-bottom:6px;}
.ufc .bout.gm .label {margin-bottom:10px;}
.ufc .bout.gm .label .tag {margin-left:10px;}
.ufc .gm-hero h1 {margin-bottom:8px;}
.ufc .gm-steps .l {margin-top:6px; max-width:26ch;}
.ufc .gm-steps .small {margin-top:2px; max-width:30ch;}
.ufc .gm-save {margin:6px 0 8px;}
.ufc table.hist .num {text-align:right; padding-right:14px;}
.ufc table.hist td.rk {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; font-size:1.35rem; width:2.4rem; line-height:1;}
.ufc table.hist tr.you td {background:rgba(196,54,31,.09); font-weight:600;}
.ufc table.hist td.who {font-weight:600;}
.ufc .mk.zero {color:var(--muted);}
.ufc table.hist td.grp {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; font-weight:800; text-transform:uppercase; font-size:1.25rem; padding:16px 0 4px; border-bottom:2px solid var(--ink);}
.ufc table.hist td.mk, .ufc table.hist td.mono {font-size:.84rem;}
.ufc .gm-empty {border-top:3px solid var(--ink); border-bottom:1px solid var(--hair); padding:22px 0; margin-top:12px;}
.ufc .gm-empty .disp {font-size:1.8rem; font-weight:800; line-height:1;}
/* Streamlit's own widgets, restyled to match the site (scoped to the game page by its container key, so no other page changes) */
.st-key-gm [role="tablist"] {gap:22px; border-bottom:1px solid #171717;}
.st-key-gm [data-testid="stTab"] p {font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; text-transform:uppercase; letter-spacing:.07em; font-size:1.15rem; font-weight:600; color:#6b675d; margin:0;}
.st-key-gm [data-testid="stTab"][aria-selected="true"] p {color:#171717; font-weight:700;}
.st-key-gm [data-testid="stWidgetLabel"] p {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; color:#6b675d;}
.st-key-gm button[data-testid^="stBaseButton"] {border-radius:0; font-family:'Barlow Condensed','Arial Narrow',Arial,sans-serif; text-transform:uppercase; letter-spacing:.08em;
  font-weight:700; font-size:1.05rem; padding:.3rem 1.3rem; min-height:2.5rem;}
.st-key-gm button[data-testid^="stBaseButton"] p {font-family:inherit; font-size:inherit; letter-spacing:inherit; font-weight:inherit;}
.st-key-gm [role="radiogroup"] {gap:0; flex-wrap:wrap;}
.st-key-gm button[data-variant="segmented_control"] {border-radius:0; border:1px solid #cdc7b8; background:transparent; color:#171717; margin:0 -1px 0 0; padding:.4rem .95rem;}
.st-key-gm button[data-variant="segmented_control"] p {font-family:'IBM Plex Mono','Courier New',monospace; font-size:.82rem;}
.st-key-gm button[data-variant="segmented_control"]:hover {background:rgba(23,23,23,.06);}
.st-key-gm button[data-variant="segmented_control"][aria-checked="true"] {background:#171717; border-color:#171717; color:#fff; position:relative; z-index:1;}
.st-key-gm button[data-variant="segmented_control"][aria-checked="true"] p {color:#fff; font-weight:600;}
.st-key-gm [class*="st-key-g_w_"] button[data-variant="segmented_control"][aria-checked="true"]:nth-of-type(2) {background:#c4361f; border-color:#c4361f;}
.st-key-gm [data-testid="stTextInputRootElement"], .st-key-gm [data-baseweb="input"], .st-key-gm [data-baseweb="base-input"] {background:#faf8f2 !important; border-radius:0 !important;}
.st-key-gm [data-testid="stTextInputRootElement"] {border:1px solid #cdc7b8 !important;}
.st-key-gm [data-testid="stTextInputRootElement"] input {background:transparent !important;}
.st-key-gm [data-testid="stForm"] {border:1px solid #cdc7b8; border-radius:0; background:rgba(255,255,255,.35);}
.st-key-gm [data-testid="stAlert"] {border-radius:0;}
.st-key-gm [data-testid="stExpander"] {border:none !important; background:transparent !important; box-shadow:none !important;}
.st-key-gm [data-testid="stExpander"] details {border:1px solid #cdc7b8 !important; border-radius:0 !important; background:transparent !important;}
.st-key-gm [data-testid="stSelectbox"] [data-baseweb="select"] > div {border-radius:0 !important; background:#faf8f2 !important; border:1px solid #cdc7b8 !important;}
</style>"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def _ufc(markup: str) -> None:
    """Shown inside the site's `.ufc` scope, exactly as the other pages do."""
    st.html(f'<div class="ufc">{markup}</div>')


def _safe(q, sql: str, params: dict | None = None, columns=()) -> pd.DataFrame:
    """A read that returns an empty table instead of an error when a table does not exist yet."""
    try:
        return q(sql, params or {})
    except (sa.exc.SQLAlchemyError, pd.errors.DatabaseError, RuntimeError):      # pandas wraps a missing table in its own DatabaseError
        return pd.DataFrame(columns=list(columns))


def save_key(event_date, event_name) -> str:
    """The widget key of an event's Save button (stable between runs, so a test can find it)."""
    return f"g_save_{pd.Timestamp(event_date).date()}_{hashlib.md5(str(event_name).encode('utf-8')).hexdigest()[:8]}"


def _clock() -> datetime:
    return datetime.now(timezone.utc)


_cutoff_text = core.when_text                                      # the same wording the Discord reminders use


def _countdown(closes: datetime, now: datetime) -> str:
    secs = int((closes - now).total_seconds())
    d, rem = divmod(max(secs, 0), 86400)
    h, m = rem // 3600, (rem % 3600) // 60
    return f"{d} day{'s' if d != 1 else ''} {h} h" if d else (f"{h} h {m} min" if h else f"{m} min")


def _split(name: str):
    """First names and surname, as the Upcoming page splits them."""
    parts = str(name).split()
    if len(parts) <= 1:
        return "", str(name)
    k = 2 if (len(parts) > 2 and parts[-1].lower() in SUFFIXES) else 1
    return " ".join(parts[:-k]), " ".join(parts[-k:])


def _side(name: str, side: str, pick_note: str | None) -> str:
    first, last = _split(name)
    pk = f'<div class="pk">MODEL PICK &middot; {esc(pick_note)}</div>' if pick_note else ""
    return f'<div class="side {side}{" pick" if pick_note else ""}">{pk}<div class="first">{esc(first)}</div><div class="last">{esc(last)}</div></div>'


def _close_reason(event_date, first_start) -> str:
    """One sentence on when picks close and why, so a friend is never surprised."""
    if first_start is None:
        return ("We do not have this card's schedule yet, so for now picks close at 18:00 UTC the day before the event. "
                f"Once the schedule is published they close {core.buffer_text()} before the first fight, which is usually later.")
    return f"Picks close {core.buffer_text()} before the first fight. Change or clear them until then."


def _rounds(row, dist: dict) -> int:
    if row.bout_id in dist and pd.notna(dist[row.bout_id]):
        return int(dist[row.bout_id])
    return 5 if (bool(row.title_fight) or (pd.notna(row.bout_order) and int(row.bout_order) == 1)) else 3


def _reset_widgets() -> None:
    for k in [k for k in st.session_state if str(k).startswith("g_")]:
        del st.session_state[k]


def _signed(n: int) -> str:
    cls = "good" if n > 0 else ("bad" if n < 0 else "zero")
    return f'<span class="mk {cls}">{n:+d}</span>'


# ----------------------------------------------------------------------------------------------
# signing in and joining
# ----------------------------------------------------------------------------------------------
def _sign_in_or_join(engine, invite_code: str, clock) -> None:
    _ufc("""<div class="hero gm-hero"><div class="kicker mono">The game</div><h1>Beat the model</h1>
            <div class="aside">Pick the winner, the method and the round for each fight on the next card, then see who out-thinks the model. It is just for fun, and nothing is wagered.</div></div>
            <div class="figs gm-steps">
              <div class="c"><div class="v">1</div><div class="l">Pick</div><div class="small muted">Winner, how it ends and the round, for every fight.</div></div>
              <div class="c"><div class="v">2</div><div class="l">Lock</div><div class="small muted">Picks close before the first fight and are revealed then.</div></div>
              <div class="c"><div class="v">3</div><div class="l">Score</div><div class="small muted">Points for being right, and a bonus for beating the model.</div></div>
            </div>""")
    tab_in, tab_join = st.tabs(["Sign in", "Join"])
    with tab_in:
        with st.form("g_form_signin"):
            nick = st.text_input("Nickname", key="g_si_nick", max_chars=20)
            pin = st.text_input("PIN", type="password", key="g_si_pin", max_chars=8)
            go = st.form_submit_button("Sign in", type="primary")
        if go:
            player, err = game_db.login(engine, nick, pin, clock())
            if err:
                st.error(err)
            else:
                st.session_state["g_player"] = player
                st.rerun()
    with tab_join:
        st.caption("You need the invite code from whoever runs the game. Choose a nickname and a PIN of 4 to 8 digits (numbers only; 6 is a good length) and remember it: it cannot be recovered, only reset.")
        with st.form("g_form_join"):
            nick = st.text_input("Nickname", key="g_jn_nick", max_chars=20)
            pin = st.text_input("PIN (digits only)", type="password", key="g_jn_pin", max_chars=8)
            pin2 = st.text_input("Repeat the PIN", type="password", key="g_jn_pin2", max_chars=8)
            code = st.text_input("Invite code", type="password", key="g_jn_code", max_chars=60)
            go = st.form_submit_button("Join", type="primary")
        if go:
            if pin != pin2:
                st.error("The two PINs are different.")
            else:
                player, err = game_db.register(engine, nick, pin, core.invite_matches(code, invite_code), clock())
                if err:
                    st.error(err)
                else:
                    st.session_state["g_player"] = player
                    st.rerun()


# ----------------------------------------------------------------------------------------------
# making picks (the next card only)
# ----------------------------------------------------------------------------------------------
def _chip(saved: tuple | None, w, m, r) -> tuple[str, str]:
    """The status of one fight's picker: what is saved against what is on screen."""
    if w is None:
        return ("NOT SAVED", "warn") if saved else ("NO PICK", "")                 # clearing a saved pick: saving will remove it
    if m is None:
        return "PICK A METHOD", "warn"
    if m in core.FINISHES and r is None:
        return "PICK A ROUND", "warn"
    return ("SAVED", "good") if saved == (w, m, r if m in core.FINISHES else None) else ("NOT SAVED", "warn")


def _picks_tab(q, engine, player: dict, clock) -> None:
    now = clock()
    for kind, text in st.session_state.pop("g_flash", []):
        {"success": st.success, "error": st.error, "warning": st.warning}[kind](text)
    every = _safe(q, OPEN_SQL, {"t": (now - timedelta(days=1)).date()}, columns=OPEN_COLS)          # a card can still be open on its event date (or just after it, in UTC)
    if every.empty:
        _ufc('<div class="gm-empty"><div class="disp">No card is open for picks</div><div class="note">A card opens for picks as soon as the one before it closes, '
             f'and every card closes {core.buffer_text()} before its first fight. Check back soon.</div></div>')
        return
    starts = _starts(q)
    dates = every.event_date.map(lambda d: pd.Timestamp(d).date())
    open_now = dates.map(lambda d: now < core.closes_at(d, starts.get(d)))          # a card can close before its event date (a card that starts the evening before, in UTC)
    every, dates = every[open_now], dates[open_now]
    if every.empty:
        _ufc('<div class="gm-empty"><div class="disp">No card is open for picks</div><div class="note">The next card opens for picks as soon as it is announced, '
             f'and every card closes {core.buffer_text()} before its first fight. Check back soon.</div></div>')
        return
    first = dates.min()
    first_start = starts.get(first)
    closes = core.closes_at(first, first_start)
    bouts = every[dates == first]                                                   # only the NEXT card; the one after it opens when this one closes
    later = every[dates > first].drop_duplicates(["event_date", "event_name"])
    model = _model_winners(q).set_index("bout_id")
    dist = _safe(q, DIST_SQL, columns=["bout_id", "p_distance", "scheduled_rounds"]).set_index("bout_id")
    saved = game_db.my_picks(engine, player["player_id"]).set_index("bout_id")
    for ename, grp in bouts.groupby("event_name", sort=False):
        days = (first - now.date()).days
        when = "Tomorrow" if days == 1 else f"In {days} days"
        saved_n = int(grp.bout_id.isin(saved.index).sum())
        _ufc(f'<div class="hero gm-hero"><div class="kicker mono">Next card &middot; {esc(when)}</div><h1>{esc(ename)}</h1></div>'
             f'<div class="facts"><span><b>{len(grp)}</b> fights</span><span>Picks close <b>{esc(_cutoff_text(closes))}</b></span>'
             f'<span>Closes in <b>{esc(_countdown(closes, now))}</b></span><span><b>{saved_n}</b> of {len(grp)} saved</span></div>'
             f'<div class="aside">{esc(_close_reason(first, first_start))} Nobody sees anyone else\'s picks until then.</div>')
        specs, unsaved, incomplete = [], 0, 0
        for b in grp.itertuples():
            rounds = _rounds(b, dist.scheduled_rounds.to_dict() if len(dist) else {})
            kw, km, kr = f"g_w_{b.bout_id}", f"g_m_{b.bout_id}", f"g_r_{b.bout_id}"
            sv = None
            if b.bout_id in saved.index:
                s = saved.loc[b.bout_id]
                sv = (s.pick_name, s.pick_method, None if pd.isna(s.pick_round) else int(s.pick_round))
                if s.pick_name in (b.fighter_a, b.fighter_b):
                    st.session_state.setdefault(kw, s.pick_name)
                if s.pick_method in core.METHODS:
                    st.session_state.setdefault(km, s.pick_method)
                if sv[2] is not None and 1 <= sv[2] <= rounds:
                    st.session_state.setdefault(kr, sv[2])
            w, m, r = st.session_state.get(kw), st.session_state.get(km), st.session_state.get(kr)
            text, tone = _chip(sv, w, m, r)
            unsaved += text == "NOT SAVED"
            incomplete += text.startswith("PICK A")
            mp = model.loc[b.bout_id] if b.bout_id in model.index else None
            pa = f"{float(mp.confidence):.0%}" if mp is not None and mp.model_winner == b.fighter_a else None
            pb = f"{float(mp.confidence):.0%}" if mp is not None and mp.model_winner == b.fighter_b else None
            label = "Main event" if pd.notna(b.bout_order) and int(b.bout_order) == 1 else f"{int(b.bout_order):02d}" if pd.notna(b.bout_order) else "Fight"
            wc = f" &middot; {esc(str(b.weight_class).title())}" if pd.notna(b.weight_class) and str(b.weight_class) else ""
            title = '<span class="tag">Title</span>' if bool(b.title_fight) else ""
            far = (f'<div class="dist"><span class="k">Goes the distance</span>{float(dist.loc[b.bout_id, "p_distance"]):.0%} <span class="muted">(model)</span></div>' if b.bout_id in dist.index else "")
            _ufc(f'<div class="bout gm"><div class="label"><span>{label}{wc} &middot; {rounds} rounds</span>{title}<span class="gm-chip {tone}">{text}</span></div>'
                 f'<div class="duel">{_side(b.fighter_a, "a", pa)}<div class="vs">vs.</div>{_side(b.fighter_b, "b", pb)}</div>{far}</div>')
            st.segmented_control("Winner", [b.fighter_a, b.fighter_b], key=kw)
            c1, c2 = st.columns([3, 2])
            with c1:
                st.segmented_control("How it ends", list(core.METHODS), key=km)
            with c2:
                if st.session_state.get(km) in core.FINISHES:
                    st.segmented_control("Round", list(range(1, rounds + 1)), key=kr, format_func=lambda n: f"R{n}")
            specs.append((b, rounds, kw, km, kr))
        notes = []
        if unsaved:
            notes.append(f"{unsaved} unsaved change{'s' if unsaved != 1 else ''}")
        if incomplete:
            notes.append(f"{incomplete} fight{'s' if incomplete != 1 else ''} still need{'' if incomplete != 1 else 's'} a method or round")
        _ufc(f'<div class="gm-save note">{esc("; ".join(notes)) if notes else "Everything on screen is saved."}</div>')
        if st.button("Save my picks", key=save_key(first, ename), type="primary"):
            done = removed = 0
            problems = []
            for b, rounds, kw, km, kr in specs:
                w = st.session_state.get(kw)
                bout = {"bout_id": b.bout_id, "status": "scheduled", "event_date": first, "first_start": first_start, "fighter_a": b.fighter_a, "fighter_b": b.fighter_b, "scheduled_rounds": rounds}
                if w is None:
                    if b.bout_id in saved.index:                                   # cleared on screen: remove it, so what you see is what is saved
                        err = game_db.delete_pick(engine, player["player_id"], bout, clock())
                        removed += err is None
                        if err:
                            problems.append(f"{b.fighter_a} vs {b.fighter_b}: {err}")
                    continue
                err = game_db.save_pick(engine, player["player_id"], bout, w, st.session_state.get(km), st.session_state.get(kr), clock())
                if err:
                    problems.append(f"{b.fighter_a} vs {b.fighter_b}: {err}")
                else:
                    done += 1
            flash = []
            if done or removed:
                parts = ([f"Saved {done} pick{'s' if done != 1 else ''}"] if done else []) + ([f"removed {removed}"] if removed else [])
                flash.append(("success", (" and ".join(parts)) + "."))
            flash += [("error", p) for p in problems]
            if not flash:
                flash.append(("warning", "Nothing to save yet: choose a winner for at least one fight."))
            st.session_state["g_flash"] = flash
            st.rerun()
    if not later.empty:
        nxt = "; ".join(f"{esc(r.event_name)} ({pd.Timestamp(r.event_date):%b} {pd.Timestamp(r.event_date).day})" for r in later.head(2).itertuples())
        _ufc(f'<div class="note">After this card: {nxt}. It opens for picks when this one closes.</div>')


# ----------------------------------------------------------------------------------------------
# the leaderboards and everyone's picks
# ----------------------------------------------------------------------------------------------
def _board_html(board: pd.DataFrame, me: str) -> str:
    if board.empty:
        return '<div class="note">Nothing to show yet.</div>'
    rows = "".join(f'<tr class="{"you" if r.nickname == me else ""}"><td class="rk">{i}</td><td class="who">{esc(r.nickname)}{" (you)" if r.nickname == me else ""}</td>'
                   f'<td class="num mono">{int(r.picks)}</td><td class="num mono">{int(r.winners_right)}</td><td class="num mono">{int(r.points)}</td><td class="num">{_signed(int(r.vs_model))}</td></tr>'
                   for i, r in enumerate(board.itertuples(), start=1))
    return ('<div style="overflow-x:auto"><table class="hist"><tr><th></th><th>Player</th><th class="num">Picks</th><th class="num">Right</th><th class="num">Points</th>'
            '<th class="num">Vs model</th></tr>' + rows + "</table></div>")


def _section(title: str, note: str) -> str:
    return f'<div class="sect"><h3>{esc(title)}</h3></div><div class="note" style="margin-top:0">{esc(note)}</div>'


def _leaderboard_tab(q, engine, player: dict, scored: pd.DataFrame, clock) -> None:
    if scored.empty:
        _ufc('<div class="gm-empty"><div class="disp">No scores yet</div><div class="note">Once a card you picked has happened and its results are in, the leaderboards appear here. '
             'Make your picks for the next card first.</div></div>')
    else:
        years = sorted({int(y) for y in pd.to_datetime(scored.event_date).dt.year}, reverse=True)
        choice = st.selectbox("Season", ["All time"] + years, key="g_season")
        year = None if choice == "All time" else int(choice)
        me = player["nickname"]
        latest = scored.sort_values("event_date").event_name.iloc[-1]
        _ufc(f'{_section("Beat the model", "Your result minus the model’s, on the winner and on goes-the-distance. Copying the model scores 0.")}{_board_html(core.versus_model_board(scored, year), me)}'
             f'{_section("Points", "Winner +1, method +1, round +1 (each needs the one before it to be right).")}{_board_html(core.leaderboard(scored, year), me)}'
             f'{_section("Latest card: " + str(latest), "Everyone who picked it, ranked against the model.")}{_board_html(core.versus_model_board(scored[scored.event_name == latest]), me)}')
    _reveal(q, engine, clock)


def _reveal(q, engine, clock) -> None:
    """Everyone's picks for cards that have closed (hidden until then so nobody can copy a friend)."""
    now = clock()
    closed = _safe(q, CLOSED_SQL, {"t": (now + timedelta(days=2)).date()}, columns=["bout_id", "event_name", "event_date", "bout_order", "fighter_a", "fighter_b"])
    starts = _starts(q)
    closed = closed[closed.event_date.map(lambda d: now >= core.closes_at(pd.Timestamp(d).date(), starts.get(pd.Timestamp(d).date())))] if not closed.empty else closed
    picks = game_db.all_picks(engine)
    if closed.empty or picks.empty:
        return
    for e in closed.drop_duplicates(["event_date", "event_name"]).head(2).itertuples():
        grp = closed[(closed.event_name == e.event_name) & (closed.event_date == e.event_date)]
        mine = picks[picks.bout_id.isin(grp.bout_id)]
        if mine.empty:
            continue
        with st.expander(f"Everyone's picks: {e.event_name}"):
            players = sorted(mine.nickname.unique(), key=str.lower)
            rows = []
            for b in grp.itertuples():
                cells = [f"<td>{esc(b.fighter_a)} vs {esc(b.fighter_b)}</td>"]
                for p in players:
                    r = mine[(mine.bout_id == b.bout_id) & (mine.nickname == p)]
                    if r.empty:
                        cells.append('<td class="mono muted">-</td>')
                    else:
                        r = r.iloc[0]
                        cells.append(f'<td class="mono">{esc(r.pick_name)} &middot; {esc(r.pick_method)}' + (f" R{int(r.pick_round)}" if pd.notna(r.pick_round) else "") + "</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            _ufc('<div style="overflow-x:auto"><table class="hist"><tr><th>Fight</th>' + "".join(f"<th>{esc(p)}</th>" for p in players) + "</tr>" + "".join(rows) + "</table></div>")


def _my_results_tab(scored: pd.DataFrame, player: dict) -> None:
    mine = scored[scored.player_id == player["player_id"]] if not scored.empty else scored
    if mine.empty:
        _ufc('<div class="gm-empty"><div class="disp">Nothing scored yet</div><div class="note">Your scored picks appear here once a card you picked has happened.</div></div>')
        return
    mine = mine.assign(_d=pd.to_datetime(mine.event_date)).sort_values(["_d", "event_name"], ascending=[False, True], kind="stable")
    how = lambda who, method, rnd: f"{esc(who)} &middot; {esc(method)}" + (f" R{int(rnd)}" if pd.notna(rnd) else "")
    rows = []
    for ev, grp in mine.groupby("event_name", sort=False):
        rows.append(f'<tr><td class="grp" colspan="5">{esc(ev)}</td></tr>')
        for r in grp.itertuples():
            tone = "good" if r.winner_ok else "bad"
            rows.append(f'<tr><td>{esc(r.fighter_a)} vs {esc(r.fighter_b)}</td><td class="mk {tone}">{how(r.pick_name, r.pick_method, r.pick_round)}</td>'
                        f'<td class="mono">{how(r.winner, r.actual_method, r.finish_round)}</td><td class="num mono">{int(r.points)}</td><td class="num">{_signed(int(r.vs_model))}</td></tr>')
    _ufc('<div style="overflow-x:auto"><table class="hist"><tr><th>Fight</th><th>You picked</th><th>What happened</th><th class="num">Points</th><th class="num">Vs the model</th></tr>'
         + "".join(rows) + f'</table></div><div class="note">{int(mine.points.sum())} points and {int(mine.vs_model.sum()):+d} against the model over {len(mine)} picks. '
         'Green means you had the winner.</div>')


def _how_tab(engine, player: dict, clock) -> None:
    _ufc("""<div class="two"><div><span class="k">Pick</span>The winner, how it ends and, for a finish, the round. Up to 3 points a fight.</div>
            <div><span class="k">Lock</span>Picks close 6 hours before the first fight and everyone's picks are revealed.</div>
            <div><span class="k">Beat the model</span>Score +1 each time you are right and the model is wrong. Copying it scores 0.</div></div>""")
    with st.expander("The fine print"):
        st.markdown(FINE_PRINT)
    with st.expander("Delete my account"):
        st.write("This removes your nickname and all your picks, permanently.")
        pin = st.text_input("Your PIN", type="password", key="g_del_pin", max_chars=8)
        if st.button("Delete my account and picks", key="g_delete"):
            ok, err = game_db.login(engine, player["nickname"], pin, clock())
            if err:
                st.error(err)
            else:
                game_db.delete_player(engine, player["player_id"])
                _reset_widgets()
                st.session_state.pop("g_player", None)
                st.rerun()


# ----------------------------------------------------------------------------------------------
# the page
# ----------------------------------------------------------------------------------------------
def render(q, engine, invite_code: str | None, clock=_clock) -> None:
    """q(sql, params) reads the public data; `engine` is the game's own database user; `invite_code` is the shared secret."""
    st.html(GAME_CSS)
    with st.container(key="gm"):                                                    # the container's key is what scopes the widget styles above to this page
        _page(q, engine, invite_code, clock)


def _page(q, engine, invite_code: str | None, clock) -> None:
    if engine is None or not invite_code:
        _ufc('<div class="gm-empty"><div class="disp">The game is not switched on yet</div><div class="note">Whoever runs this site needs to finish its setup (see the runbook).</div></div>')
        return
    player = st.session_state.get("g_player")
    if not player:
        _sign_in_or_join(engine, invite_code, clock)
        return
    scored = _scored(q, engine)
    mine = scored[scored.player_id == player["player_id"]] if not scored.empty else scored
    left, right = st.columns([5, 1])
    with left:
        stats = ""
        if not mine.empty:
            board = core.versus_model_board(scored)                                      # ranked as the page's headline board is: against the model
            rank = int(board.index[board.nickname == player["nickname"]][0]) + 1
            stats = (f'<span><b>#{rank}</b> of {len(board)} beating the model</span><span><b>{int(mine.vs_model.sum()):+d}</b> vs the model</span>'
                     f'<span><b>{int(mine.points.sum())}</b> points</span><span><b>{len(mine)}</b> scored picks</span>')
        _ufc(f'<div class="facts"><span>Signed in as <b>{esc(player["nickname"])}</b></span>{stats}</div>')
    with right:
        if st.button("Sign out", key="g_signout"):
            _reset_widgets()
            st.session_state.pop("g_player", None)
            st.rerun()
    tabs = st.tabs(["Make picks", "Leaderboards", "My results", "How it works"])
    with tabs[0]:
        _picks_tab(q, engine, player, clock)
    with tabs[1]:
        _leaderboard_tab(q, engine, player, scored, clock)
    with tabs[2]:
        _my_results_tab(scored, player)
    with tabs[3]:
        _how_tab(engine, player, clock)
