"""
The friends' game page. `render` is given the read-only query function and the game's own database engine, so it can be driven by tests.

Everything dynamic that is shown as HTML (nicknames, fighter and event names) is escaped first: the site renders raw HTML, and a nickname
is exactly the kind of text someone could try to use to inject markup.
"""
from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone

import pandas as pd
import sqlalchemy as sa
import streamlit as st

import game_core as core
import game_db

OPEN_SQL = """SELECT bout_id, event_name, event_date, bout_order, title_fight, fighter_a_name AS fighter_a, fighter_b_name AS fighter_b, status
              FROM upcoming_fights WHERE status = 'scheduled' AND event_date > :t ORDER BY event_date, bout_order"""
CLOSED_SQL = """SELECT bout_id, event_name, event_date, bout_order, fighter_a_name AS fighter_a, fighter_b_name AS fighter_b
                FROM upcoming_fights WHERE status = 'scheduled' AND event_date <= :t ORDER BY event_date DESC, bout_order"""
RESULTS_SQL = """SELECT u.bout_id, u.event_name, u.event_date, u.fighter_a_name AS fighter_a, u.fighter_b_name AS fighter_b, r.winner_side,
                        r.method AS actual_method, r.finish_round, r.went_distance
                 FROM upcoming_fights u JOIN bout_results r ON r.bout_id = u.bout_id"""
MODEL_SQL = "SELECT bout_id, predicted_winner AS model_winner, confidence FROM {table} WHERE status = 'locked'"
DIST_SQL = "SELECT bout_id, p_distance, scheduled_rounds FROM upcoming_distance_v1 WHERE status = 'locked'"
RULES = """
**What you pick.** For each fight on an open card: the winner, how it ends (KO/TKO, Submission or Decision) and, for a finish, the round. Change your picks as often as you like until the card closes at **00:00 UTC on the day of the event**. Nobody can see anyone else's picks until then.

**Points.** Winner right: **+1**. Method right (and the winner too): **+1**. Round right (winner and method too, for a finish): **+1**. That is up to 3 a fight.

**Versus the model.** This is the one that matters. The winner call and the goes-the-distance call (a Decision pick means "goes the distance") are each scored as *your result minus the model's*: **+1** if you are right and the model is wrong, **-1** if you are wrong and the model is right, **0** otherwise. Copying the model earns exactly 0, so the only way up is to disagree with it and be right.

**Fine print.** A cancelled bout, a bout that never happened, or one with no winner is ignored. A fight that ends in a disqualification scores the winner only. Only fights you picked count, and you are never penalised for skipping one.

**Privacy.** The site stores your nickname, a scrambled (hashed) PIN and your picks, nothing else: no email, no real name. Your PIN cannot be recovered, so if you forget it ask whoever runs the game to reset it. You can delete your account and picks yourself at any time.
"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def _html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


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


def _cutoff_text(event_date) -> str:
    c = core.cutoff(event_date)
    text = c.strftime("%a %b %d, 00:00 UTC")
    try:
        from zoneinfo import ZoneInfo
        text += f" ({c.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%a %b %d, %I:%M %p')} Pacific)"
    except Exception:                                                           # no time zone database on this machine: UTC is still right
        pass
    return text


def _table(head: list[str], rows: list[list[str]]) -> str:
    """`rows` are lists of ALREADY ESCAPED strings."""
    return ('<table class="hist"><tr>' + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>"
            + "".join("<tr>" + "".join(f'<td class="mono">{c}</td>' if i else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows) + "</table>")


def _model_winners(q) -> pd.DataFrame:
    v2 = _safe(q, MODEL_SQL.format(table="upcoming_picks_v2"), columns=["bout_id", "model_winner", "confidence"])
    v3 = _safe(q, MODEL_SQL.format(table="upcoming_picks_v3"), columns=["bout_id", "model_winner", "confidence"])
    both = pd.concat([v3.assign(rank=0), v2.assign(rank=1)], ignore_index=True)                    # the pick the site shows: market-informed when there is one
    return both.sort_values("rank").drop_duplicates("bout_id")[["bout_id", "model_winner", "confidence"]].reset_index(drop=True)


def _rounds(row, dist: dict) -> int:
    if row.bout_id in dist and pd.notna(dist[row.bout_id]):
        return int(dist[row.bout_id])
    return 5 if (bool(row.title_fight) or (pd.notna(row.bout_order) and int(row.bout_order) == 1)) else 3


def _reset_widgets() -> None:
    for k in [k for k in st.session_state if str(k).startswith("g_")]:
        del st.session_state[k]


# ----------------------------------------------------------------------------------------------
# signing in and joining
# ----------------------------------------------------------------------------------------------
def _sign_in_or_join(engine, invite_code: str, clock) -> None:
    st.markdown("### Beat the model")
    st.write("Pick the winner, the method and the round for each upcoming fight, and see who beats the model. It is just for fun, and nothing is wagered.")
    tab_in, tab_join = st.tabs(["Sign in", "Join"])
    with tab_in:
        with st.form("g_form_signin"):
            nick = st.text_input("Nickname", key="g_si_nick", max_chars=20)
            pin = st.text_input("PIN", type="password", key="g_si_pin", max_chars=8)
            go = st.form_submit_button("Sign in")
        if go:
            player, err = game_db.login(engine, nick, pin, clock())
            if err:
                st.error(err)
            else:
                st.session_state["g_player"] = player
                st.rerun()
    with tab_join:
        st.caption("You need the invite code from whoever runs the game. Choose a nickname and a PIN (4 to 8 digits, 6 is a good length) and remember it: it cannot be recovered, only reset.")
        with st.form("g_form_join"):
            nick = st.text_input("Nickname", key="g_jn_nick", max_chars=20)
            pin = st.text_input("PIN", type="password", key="g_jn_pin", max_chars=8)
            pin2 = st.text_input("Repeat the PIN", type="password", key="g_jn_pin2", max_chars=8)
            code = st.text_input("Invite code", type="password", key="g_jn_code", max_chars=60)
            go = st.form_submit_button("Join")
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
# making picks
# ----------------------------------------------------------------------------------------------
def _picks_tab(q, engine, player: dict, clock) -> None:
    now = clock()
    bouts = _safe(q, OPEN_SQL, {"t": now.date()}, columns=["bout_id", "event_name", "event_date", "bout_order", "title_fight", "fighter_a", "fighter_b", "status"])
    if bouts.empty:
        st.info("No card is open for picks right now. A card opens when it is announced and closes at 00:00 UTC on the day of the event.")
        return
    model = _model_winners(q).set_index("bout_id")
    dist = _safe(q, DIST_SQL, columns=["bout_id", "p_distance", "scheduled_rounds"]).set_index("bout_id")
    saved = game_db.my_picks(engine, player["player_id"]).set_index("bout_id")
    for (edate, ename), grp in bouts.groupby(["event_date", "event_name"], sort=True):
        st.markdown(f"#### {esc(ename)}")
        st.caption(f"Picks close {_cutoff_text(edate)}. You can change them until then.")
        specs = []
        for b in grp.itertuples():
            rounds = _rounds(b, dist.scheduled_rounds.to_dict() if len(dist) else {})
            with st.container(border=True):
                head = f"**{esc(b.fighter_a)}** vs **{esc(b.fighter_b)}**" + (" &nbsp;`TITLE`" if bool(b.title_fight) else "")
                _html(head)
                note = []
                if b.bout_id in model.index:
                    note.append(f"the model picks {esc(model.loc[b.bout_id, 'model_winner'])} ({float(model.loc[b.bout_id, 'confidence']):.0%})")
                if b.bout_id in dist.index:
                    note.append(f"it goes the distance {float(dist.loc[b.bout_id, 'p_distance']):.0%} of the time")
                if note:
                    _html(f'<span class="muted">{"; ".join(note)} &middot; {rounds} rounds</span>')
                kw, km, kr = f"g_w_{b.bout_id}", f"g_m_{b.bout_id}", f"g_r_{b.bout_id}"
                if b.bout_id in saved.index:
                    s = saved.loc[b.bout_id]
                    st.session_state.setdefault(kw, s.pick_name)
                    st.session_state.setdefault(km, s.pick_method)
                    st.session_state.setdefault(kr, None if pd.isna(s.pick_round) else int(s.pick_round))
                st.radio("Winner", [b.fighter_a, b.fighter_b], index=None, horizontal=True, key=kw)
                c1, c2 = st.columns(2)
                with c1:
                    st.selectbox("How it ends", list(core.METHODS), index=None, placeholder="Choose", key=km)
                with c2:
                    if st.session_state.get(km) in core.FINISHES:
                        st.selectbox("Round", list(range(1, rounds + 1)), index=None, placeholder="Choose", key=kr)
            specs.append((b, rounds, kw, km, kr))
        if st.button(f"Save my picks for {ename}", key=save_key(edate, ename)):
            done, problems = 0, []
            for b, rounds, kw, km, kr in specs:
                winner = st.session_state.get(kw)
                if winner is None:
                    continue                                                   # no pick on this fight
                bout = {"bout_id": b.bout_id, "status": "scheduled", "event_date": edate, "fighter_a": b.fighter_a, "fighter_b": b.fighter_b, "scheduled_rounds": rounds}
                err = game_db.save_pick(engine, player["player_id"], bout, winner, st.session_state.get(km), st.session_state.get(kr), clock())
                if err:
                    problems.append(f"{b.fighter_a} vs {b.fighter_b}: {err}")
                else:
                    done += 1
            if done:
                st.success(f"Saved {done} pick{'s' if done != 1 else ''}.")
            for p in problems:
                st.error(p)
            if not done and not problems:
                st.warning("Choose a winner for at least one fight first.")


# ----------------------------------------------------------------------------------------------
# the leaderboards and everyone's picks
# ----------------------------------------------------------------------------------------------
def _scored(q, engine) -> pd.DataFrame:
    res = _safe(q, RESULTS_SQL, columns=["bout_id", "event_name", "event_date", "fighter_a", "fighter_b", "winner_side", "actual_method", "finish_round", "went_distance"])
    picks = game_db.all_picks(engine)
    if res.empty or picks.empty:
        return pd.DataFrame(columns=core.SCORED_COLUMNS)
    dist = _safe(q, DIST_SQL, columns=["bout_id", "p_distance", "scheduled_rounds"])
    return core.score_picks(picks, res[["bout_id", "event_name", "event_date", "fighter_a", "fighter_b"]],
                            res[["bout_id", "winner_side", "actual_method", "finish_round", "went_distance"]], _model_winners(q)[["bout_id", "model_winner"]], dist)


def _board_html(board: pd.DataFrame, me: str) -> str:
    if board.empty:
        return '<div class="note">Nothing to show yet.</div>'
    rows = []
    for i, r in enumerate(board.itertuples(), start=1):
        name = esc(r.nickname) + (" <b>(you)</b>" if r.nickname == me else "")
        rows.append([str(i), name, str(int(r.picks)), str(int(r.winners_right)), f"{int(r.points)}", f"{int(r.vs_model):+d}"])
    return _table(["#", "Player", "Picks", "Winners right", "Points", "Vs the model"], rows)


def _leaderboard_tab(q, engine, player: dict, clock) -> None:
    scored = _scored(q, engine)
    if scored.empty:
        st.info("No results to score yet. Once a card you picked has happened and its results are in, the leaderboards appear here.")
    else:
        years = sorted({int(y) for y in pd.to_datetime(scored.event_date).dt.year}, reverse=True)
        choice = st.selectbox("Season", ["All time"] + years, key="g_season")
        year = None if choice == "All time" else int(choice)
        st.markdown("##### Beat the model")
        st.caption("Your result minus the model's, on the winner and on goes-the-distance. Copying the model scores 0.")
        _html(_board_html(core.versus_model_board(scored, year), player["nickname"]))
        st.markdown("##### Points")
        st.caption("Winner +1, method +1, round +1 (each needs the one before it to be right).")
        _html(_board_html(core.leaderboard(scored, year), player["nickname"]))
        latest = scored.sort_values("event_date").event_name.iloc[-1]
        st.markdown(f"##### Latest card: {esc(latest)}")
        _html(_board_html(core.versus_model_board(scored[scored.event_name == latest]), player["nickname"]))
    _reveal(q, engine, clock)


def _reveal(q, engine, clock) -> None:
    """Everyone's picks for cards that have closed (hidden until then so nobody can copy a friend)."""
    closed = _safe(q, CLOSED_SQL, {"t": clock().date()}, columns=["bout_id", "event_name", "event_date", "bout_order", "fighter_a", "fighter_b"])
    picks = game_db.all_picks(engine)
    if closed.empty or picks.empty:
        return
    events = closed.drop_duplicates(["event_date", "event_name"]).head(2)
    for e in events.itertuples():
        grp = closed[(closed.event_name == e.event_name) & (closed.event_date == e.event_date)]
        mine = picks[picks.bout_id.isin(grp.bout_id)]
        if mine.empty:
            continue
        with st.expander(f"Everyone's picks: {e.event_name}"):
            players = sorted(mine.nickname.unique(), key=str.lower)
            rows = []
            for b in grp.itertuples():
                cells = [f"{esc(b.fighter_a)} vs {esc(b.fighter_b)}"]
                for p in players:
                    r = mine[(mine.bout_id == b.bout_id) & (mine.nickname == p)]
                    if r.empty:
                        cells.append("-")
                    else:
                        r = r.iloc[0]
                        cells.append(f"{esc(r.pick_name)} &middot; {esc(r.pick_method)}" + (f" R{int(r.pick_round)}" if pd.notna(r.pick_round) else ""))
                rows.append(cells)
            _html(_table(["Fight"] + players, rows))


def _my_results_tab(q, engine, player: dict) -> None:
    scored = _scored(q, engine)
    mine = scored[scored.player_id == player["player_id"]] if not scored.empty else scored
    if mine.empty:
        st.info("Your scored picks will appear here once a card you picked has happened.")
        return
    mine = mine.sort_values("event_date", ascending=False)
    rows = [[esc(r.event_name), esc(r.pick_name), esc(r.winner), "right" if r.winner_ok else "wrong", str(int(r.points)), f"{int(r.vs_model):+d}"] for r in mine.itertuples()]
    _html(_table(["Card", "You picked", "Winner", "Result", "Points", "Vs the model"], rows))
    st.caption(f"{int(mine.points.sum())} points and {int(mine.vs_model.sum()):+d} against the model over {len(mine)} picks.")


# ----------------------------------------------------------------------------------------------
# the page
# ----------------------------------------------------------------------------------------------
def render(q, engine, invite_code: str | None, clock=_clock) -> None:
    """q(sql, params) reads the public data; `engine` is the game's own database user; `invite_code` is the shared secret."""
    if engine is None or not invite_code:
        st.info("The game is not switched on yet. Whoever runs this site needs to finish its setup (see the runbook).")
        return
    player = st.session_state.get("g_player")
    if not player:
        _sign_in_or_join(engine, invite_code, clock)
        return
    left, right = st.columns([4, 1])
    with left:
        _html(f"Signed in as <b>{esc(player['nickname'])}</b>")
    with right:
        if st.button("Sign out", key="g_signout"):
            _reset_widgets()
            st.session_state.pop("g_player", None)
            st.rerun()
    tabs = st.tabs(["Make picks", "Leaderboards", "My results", "How it works"])
    with tabs[0]:
        _picks_tab(q, engine, player, clock)
    with tabs[1]:
        _leaderboard_tab(q, engine, player, clock)
    with tabs[2]:
        _my_results_tab(q, engine, player)
    with tabs[3]:
        st.markdown(RULES)
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
