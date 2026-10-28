"""Drives the friends' game page through Streamlit's own test harness: real clicks and typing against throwaway databases."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "app"))
import bout_results  # noqa: E402
import game_db  # noqa: E402
import model_distance  # noqa: E402

try:
    __import__("streamlit")
    HAVE_STREAMLIT = True
except ImportError:
    HAVE_STREAMLIT = False
# The daily job does not install Streamlit. These tests are then SKIPPED one by one (not dropped at import), so every environment counts the same number of tests.
pytestmark = pytest.mark.skipif(not HAVE_STREAMLIT, reason="Streamlit is not installed here")
if HAVE_STREAMLIT:
    import game_ui  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
CARD = "UFC 334: Test Card"
CARD_DATE = "2026-11-14"
CODE = "Blue Falcon 42"


def page():
    """The script the harness runs: the game page against the throwaway databases named in the environment."""
    import os
    from datetime import datetime
    import pandas as pd
    import sqlalchemy as sa
    import game_ui
    r, g = sa.create_engine(os.environ["G_READ"]), sa.create_engine(os.environ["G_GAME"])
    now = datetime.fromisoformat(os.environ["G_NOW"])
    code = os.environ.get("G_CODE") or None
    game_ui.render(lambda sql, params=None: pd.read_sql(sa.text(sql), r, params=params or {}), g if os.environ.get("G_ON", "1") == "1" else None, code, clock=lambda: now)


@pytest.fixture
def world(tmp_path, monkeypatch):
    read = sa.create_engine(f"sqlite:///{tmp_path / 'read.db'}")
    game = sa.create_engine(f"sqlite:///{tmp_path / 'game.db'}")
    game_db.ensure_tables(game)
    model_distance.ensure_tables(read)
    bout_results.ensure_table(read)
    with read.begin() as c:
        c.exec_driver_sql("""CREATE TABLE upcoming_fights (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, bout_order INTEGER, weight_class TEXT, title_fight BOOLEAN,
            fighter_a_name TEXT, fighter_a_url TEXT, fighter_b_name TEXT, fighter_b_url TEXT, status TEXT, loaded_at TIMESTAMP)""")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v2 (bout_id TEXT, predicted_winner TEXT, confidence REAL, status TEXT)")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v3 (bout_id TEXT, predicted_winner TEXT, confidence REAL, status TEXT)")
    bout = lambda i, ev, d, order, a, b, title=False: dict(bout_id=i, event_name=ev, event_date=d, bout_order=order, weight_class="Lightweight", title_fight=title, fighter_a_name=a,
                                                         fighter_a_url=f"u{i}a", fighter_b_name=b, fighter_b_url=f"u{i}b", status="scheduled")
    pd.DataFrame([bout("b1", CARD, CARD_DATE, 1, "Alex One", "Ben Two", True), bout("b2", CARD, CARD_DATE, 2, "Cal Three", "Dan Four"),
                  bout("p1", "UFC 333: Old", "2026-10-24", 1, "Eli Five", "Fox Six"), bout("p2", "UFC 333: Old", "2026-10-24", 2, "Gus Seven", "Hal Eight")]).to_sql("upcoming_fights", read, if_exists="append", index=False)
    pd.DataFrame([dict(bout_id=b, predicted_winner=w, confidence=c, status="locked") for b, w, c in (("b1", "Alex One", 0.55), ("p1", "Eli Five", 0.6), ("p2", "Gus Seven", 0.58))]).to_sql(
        "upcoming_picks_v2", read, if_exists="append", index=False)
    pd.DataFrame([dict(bout_id=b, event_name="x", event_date=d, bout_order=1, weight_class="x", scheduled_rounds=r, fighter_a="a", fighter_b="b", p_distance=p, p_base=0.48, status="locked",
                       predicted_at="2026-10-20 10:00:00") for b, d, r, p in (("b1", CARD_DATE, 5, 0.5), ("b2", CARD_DATE, 3, 0.6), ("p1", "2026-10-24", 3, 0.3), ("p2", "2026-10-24", 3, 0.4))]).to_sql(
        "upcoming_distance_v1", read, if_exists="append", index=False)
    pd.DataFrame([dict(bout_id="p1", event_date="2026-10-24", winner_side="a", method="KO/TKO", finish_round=2, went_distance=0, result_raw="KO/TKO", recorded_at="2026-10-25 10:00:00"),
                  dict(bout_id="p2", event_date="2026-10-24", winner_side="b", method="Decision", finish_round=None, went_distance=1, result_raw="Decision - Unanimous", recorded_at="2026-10-25 10:00:00")]).to_sql(
        "bout_results", read, if_exists="append", index=False)
    ann, _ = game_db.register(game, "Ann", "111111", True, NOW - timedelta(days=5))
    bo, _ = game_db.register(game, "Bob", "222222", True, NOW - timedelta(days=5))
    old = lambda b, a, bb, ev="2026-10-24": {"bout_id": b, "status": "scheduled", "event_date": ev, "fighter_a": a, "fighter_b": bb, "scheduled_rounds": 3}
    long_ago = NOW - timedelta(days=6)
    for pl, picks in ((ann, [("p1", "Eli Five", "Fox Six", "Eli Five", "KO/TKO", 2), ("p2", "Gus Seven", "Hal Eight", "Hal Eight", "Decision", None)]),
                      (bo, [("p1", "Eli Five", "Fox Six", "Fox Six", "Submission", 1), ("p2", "Gus Seven", "Hal Eight", "Gus Seven", "KO/TKO", 1)])):
        for b, a, bb, w, m, r in picks:
            assert game_db.save_pick(game, pl["player_id"], old(b, a, bb), w, m, r, long_ago) is None
    env = {"G_READ": str(read.url), "G_GAME": str(game.url), "G_NOW": NOW.isoformat(), "G_CODE": CODE, "G_ON": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return {"read": read, "game": game, "ann": ann, "bo": bo}


def _run(as_player=None, **session):
    at = AppTest.from_function(page, default_timeout=30)
    if as_player:
        at.session_state["g_player"] = as_player
    for k, v in session.items():
        at.session_state[k] = v
    return at.run()


def _all_text(at):
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(e.value) for e in at.error) + " " + " ".join(str(e.value) for e in at.info) + " " + \
        " ".join(str(e.value) for e in at.success) + " " + " ".join(str(e.value) for e in at.warning) + " " + " ".join(str(e.value) for e in at.caption)


def _button(at, label):
    return [b for b in at.button if b.label == label][0]


def _fill_join(at, nick, pin, pin2, code):
    at.text_input(key="g_jn_nick").input(nick)
    at.text_input(key="g_jn_pin").input(pin)
    at.text_input(key="g_jn_pin2").input(pin2)
    at.text_input(key="g_jn_code").input(code)
    return _button(at, "Join").click().run()


def test_the_page_says_so_when_the_game_is_not_switched_on(world, monkeypatch):
    monkeypatch.setenv("G_CODE", "")
    at = _run()
    assert not at.exception and "not switched on" in _all_text(at) and len(at.text_input) == 0
    monkeypatch.setenv("G_CODE", CODE)
    monkeypatch.setenv("G_ON", "0")
    assert "not switched on" in _all_text(_run())                                                          # no game database configured either


def test_joining_needs_the_invite_code_matching_pins_and_a_clean_nickname(world):
    assert "invite code is not right" in _all_text(_fill_join(_run(), "Newbie", "123456", "123456", "wrong code"))
    assert "two PINs are different" in _all_text(_fill_join(_run(), "Newbie", "123456", "123457", CODE))
    assert "3 to 20 characters" in _all_text(_fill_join(_run(), "<script>", "123456", "123456", CODE))
    assert "nickname is taken" in _all_text(_fill_join(_run(), "ann", "123456", "123456", CODE))
    at = _fill_join(_run(), "Newbie", "123456", "123456", " blue FALCON 42 ")                              # the code forgives case and spaces
    assert not at.exception and at.session_state["g_player"]["nickname"] == "Newbie" and "Signed in as <b>Newbie</b>" in _all_text(at)
    stored = pd.read_sql(sa.text("SELECT nickname, pin_hash FROM game_players WHERE nickname = 'Newbie'"), world["game"]).iloc[0]
    assert stored.pin_hash.startswith("scrypt$") and "123456" not in stored.pin_hash


def test_signing_in_signing_out_and_the_lockout(world):
    at = _run()
    at.text_input(key="g_si_nick").input("Ann"); at.text_input(key="g_si_pin").input("000000")
    assert "Wrong nickname or PIN." in _all_text(_button(at, "Sign in").click().run())
    for _ in range(4):                                                                                       # four more wrong attempts: five in all
        at.text_input(key="g_si_nick").input("Ann"); at.text_input(key="g_si_pin").input("000000")
        _button(at, "Sign in").click().run()
    at.text_input(key="g_si_nick").input("Ann"); at.text_input(key="g_si_pin").input("111111")
    assert "Too many wrong attempts" in _all_text(_button(at, "Sign in").click().run())                      # locked, even with the right PIN
    assert "g_player" not in at.session_state
    at = _run()
    at.text_input(key="g_si_nick").input("Bob"); at.text_input(key="g_si_pin").input("222222")
    at = _button(at, "Sign in").click().run()                                                                # another nickname is unaffected
    assert at.session_state["g_player"]["nickname"] == "Bob"
    at = _button(at, "Sign out").click().run()
    assert "g_player" not in at.session_state and len(at.text_input) > 0


def test_a_pick_with_method_and_round_is_saved_and_comes_back_next_time(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)
    assert not at.exception and CARD in _all_text(at) and "the model picks Alex One (55%)" in _all_text(at) and "it goes the distance 50%" in _all_text(at) and "5 rounds" in _all_text(at)
    assert "Picks close Sat Nov 14, 00:00 UTC" in _all_text(at)
    at.radio(key="g_w_b1").set_value("Ben Two").run()
    at.selectbox(key="g_m_b1").select("KO/TKO").run()
    assert len(at.selectbox(key="g_r_b1").options) == 5                                                      # a 5-round title fight offers rounds 1 to 5
    at.selectbox(key="g_r_b1").select(4).run()
    at = at.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()
    assert "Saved 1 pick." in _all_text(at)
    row = pd.read_sql(sa.text(f"SELECT * FROM game_picks WHERE player_id = '{me['player_id']}'"), world["game"]).iloc[0]
    assert (row.bout_id, row.pick_name, row.pick_method, row.pick_round) == ("b1", "Ben Two", "KO/TKO", 4)
    again = _run(me)                                                                                         # a new session: the saved pick is pre-filled
    assert (again.radio(key="g_w_b1").value, again.selectbox(key="g_m_b1").value, again.selectbox(key="g_r_b1").value) == ("Ben Two", "KO/TKO", 4)
    again.selectbox(key="g_m_b1").select("Decision").run()
    assert not [s for s in again.selectbox if s.key == "g_r_b1"]                                             # a decision has no round
    again = again.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()
    row = pd.read_sql(sa.text(f"SELECT * FROM game_picks WHERE player_id = '{me['player_id']}' AND bout_id = 'b1'"), world["game"]).iloc[0]
    assert (row.pick_method, pd.isna(row.pick_round)) == ("Decision", True)                                  # the pick was changed, not duplicated


def test_incomplete_picks_are_refused_with_a_reason_and_nothing_is_stored(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)
    at = at.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()
    assert "Choose a winner for at least one fight" in _all_text(at)
    at.radio(key="g_w_b2").set_value("Cal Three").run()
    at = at.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()
    assert "how the fight ends" in _all_text(at)
    at.selectbox(key="g_m_b2").select("Submission").run()
    at = at.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()
    assert "Pick the round the finish happens in" in _all_text(at)
    assert pd.read_sql(sa.text(f"SELECT COUNT(*) n FROM game_picks WHERE player_id = '{me['player_id']}'"), world["game"]).n[0] == 0


def test_once_a_card_has_closed_it_cannot_be_picked_and_the_picks_are_revealed(world, monkeypatch):
    bo_open = {"bout_id": "b1", "status": "scheduled", "event_date": CARD_DATE, "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 5}
    assert game_db.save_pick(world["game"], world["bo"]["player_id"], bo_open, "Alex One", "KO/TKO", 3, NOW) is None          # Bob's secret pick on the OPEN card
    at = _run(world["ann"])
    assert "KO/TKO R3" not in _all_text(at) and "Bob" not in " ".join(str(m.value) for m in at.markdown if "Everyone" in str(m.value))   # hidden while the card is open
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 14, 0, 0, 1, tzinfo=timezone.utc).isoformat())
    at = _run(world["ann"])
    assert "No card is open for picks" in _all_text(at) and not [r for r in at.radio if r.key == "g_w_b1"]
    assert "KO/TKO R3" in " ".join(str(m.value) for m in at.markdown)                                          # revealed once it closes
    refused = game_db.save_pick(world["game"], world["ann"]["player_id"], bo_open, "Alex One", "KO/TKO", 1, datetime(2026, 11, 14, 0, 0, 1, tzinfo=timezone.utc))
    assert "closed" in refused                                                                                 # and the server refuses a late pick even if the page were tricked


def test_the_leaderboards_score_the_closed_card_and_mark_you(world):
    at = _run(world["ann"])
    text = " ".join(str(m.value) for m in at.markdown)
    assert not at.exception and "Beat the model" in text and "Points" in text and "Latest card: UFC 333: Old" in text
    tables = [str(m.value) for m in at.markdown if '<table class="hist">' in str(m.value) and "Vs the model" in str(m.value)]
    assert len(tables) >= 2
    first = tables[0]                                                                                          # Ann: 3 + 2 points; Bob: 0 + 0, so Ann leads on both boards
    assert first.index("Ann") < first.index("Bob") and "<b>(you)</b>" in first
    # Ann: p1 all three right (3) and p2 winner+method right (2) = 5 points; versus the model: p1 0, p2 +1 (winner) +1 (distance, the model called a finish) = +2
    assert ">5<" in first and ">+2<" in first
    assert "5 points and +2 against the model over 2 picks" in " ".join(str(c.value) for c in at.caption)


def test_hostile_text_is_escaped_everywhere_it_is_shown(world):
    with world["read"].begin() as c:
        c.exec_driver_sql("UPDATE upcoming_fights SET event_name = 'UFC <script>alert(1)</script> & co', fighter_a_name = 'Evil <img src=x onerror=alert(1)>' WHERE bout_id IN ('b1', 'p1')")
    at = _run(world["ann"])
    shown = " ".join(str(m.value) for m in at.markdown)
    assert not at.exception and "<script>" not in shown and "<img" not in shown                                 # nothing the site is told to render as markup
    assert "&lt;script&gt;" in shown or "&amp;lt;script" in shown


def test_deleting_an_account_needs_the_pin_and_removes_the_player_and_their_picks(world):
    at = _run(world["ann"])
    at.text_input(key="g_del_pin").input("999999")
    assert "Wrong nickname or PIN." in _all_text(_button(at, "Delete my account and picks").click().run())
    assert len(game_db.all_picks(world["game"])) == 4
    at.text_input(key="g_del_pin").input("111111")
    at = _button(at, "Delete my account and picks").click().run()
    assert "g_player" not in at.session_state and sorted(game_db.all_picks(world["game"]).nickname.unique()) == ["Bob"]
    assert game_db.login(world["game"], "Ann", "111111", NOW)[0] is None


def test_the_page_survives_tables_that_do_not_exist_yet(world):
    with world["read"].begin() as c:
        for t in ("bout_results", "upcoming_distance_v1", "upcoming_picks_v3"):
            c.exec_driver_sql(f"DROP TABLE {t}")
    at = _run(world["ann"])
    assert not at.exception and "No results to score yet" in _all_text(at)                                      # a fresh install: no results table, nothing breaks


def test_the_site_has_a_game_page_wired_to_its_own_database_user_and_secrets():
    src = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert '"Game": page_game' in src and '"Upcoming", "Past events", "Fighters", "Track record", "Game"' in src
    assert 'game_ui.render(q, get_game_engine(), _secret("GAME_INVITE_CODE"))' in src and 'url = _secret("GAME_DATABASE_URL")' in src
    assert "pool_size=3, max_overflow=3" in src                                                  # at most 6 connections, under the game user's limit of 10
