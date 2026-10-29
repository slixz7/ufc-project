"""Drives the friends' game page through Streamlit's own test harness: real clicks and typing against throwaway databases."""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "app"))
import auto_odds  # noqa: E402
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
LATER = "UFC 335: Later Card"
LATER_DATE = "2026-11-21"
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
    pd.DataFrame([bout("b1", CARD, CARD_DATE, 1, "Alex One", "Ben Two", True), bout("b2", CARD, CARD_DATE, 2, "Cal Three", "Dan Four"), bout("l1", LATER, LATER_DATE, 1, "Lee Nine", "Max Ten"),
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


def _html(at):
    return " ".join(str(h.value) for h in at.html)


def _all_text(at):
    parts = [_html(at)] + [str(e.value) for kind in (at.markdown, at.error, at.info, at.success, at.warning, at.caption) for e in kind]
    return " ".join(parts)


def _chips(at):
    return re.findall(r'gm-chip[^"]*">([^<]+)<', _html(at))


def _button(at, label):
    return [b for b in at.button if b.label == label][0]


def _pick(at, key, value):
    return at.segmented_control(key=key).set_value(value).run()


def _fill_join(at, nick, pin, pin2, code):
    at.text_input(key="g_jn_nick").input(nick)
    at.text_input(key="g_jn_pin").input(pin)
    at.text_input(key="g_jn_pin2").input(pin2)
    at.text_input(key="g_jn_code").input(code)
    return _button(at, "Join").click().run()


def _save(at):
    return at.button(key=game_ui.save_key(CARD_DATE, CARD)).click().run()


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
    assert "A PIN is 4 to 8 digits" in _all_text(_fill_join(_run(), "Newbie", "Rish20", "Rish20", CODE))     # letters in a PIN: the message says what a PIN is
    assert "nickname is taken" in _all_text(_fill_join(_run(), "ann", "123456", "123456", CODE))
    at = _fill_join(_run(), "Newbie", "123456", "123456", " blue FALCON 42 ")                              # the code forgives case and spaces
    assert not at.exception and at.session_state["g_player"]["nickname"] == "Newbie" and "Signed in as <b>Newbie</b>" in _html(at)
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


def test_only_the_next_card_is_offered_and_the_one_after_it_opens_when_this_one_closes(world, monkeypatch):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)
    text = _html(at)
    assert not at.exception and CARD in text and "Next card &middot; In 18 days" in text
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_b1", "g_w_b2"]          # only the next card's fights have pickers
    assert LATER not in text.split("After this card:")[0] and "After this card: UFC 335: Later Card (Nov 21)" in text
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 14, 0, 0, 1, tzinfo=timezone.utc).isoformat())              # the first card has just closed
    at = _run(me)
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_l1"] and LATER in _html(at) and "After this card" not in _html(at)
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 21, 0, 0, 1, tzinfo=timezone.utc).isoformat())              # and now the last one has too
    at = _run(me)
    assert "No card is open for picks" in _html(at) and not at.segmented_control


def test_a_pick_with_method_and_round_is_saved_and_comes_back_next_time(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)
    text = _html(at)
    assert "MODEL PICK &middot; 55%" in text and "46%" not in text and "50%" in text and "5 rounds" in text and "Title" in text        # the model's pick, its distance chance, the rounds, the title tag
    assert "Fri Nov 13, 10:00 AM Pacific (Fri Nov 13, 6:00 PM UTC)" in text and "We do not have this card&#x27;s schedule yet" in text            # no start time recorded: 6 hours before 00:00 UTC on the event date
    at = _pick(at, "g_w_b1", "Ben Two")
    at = _pick(at, "g_m_b1", "KO/TKO")
    assert [o.content for o in at.segmented_control(key="g_r_b1").proto.options] == ["R1", "R2", "R3", "R4", "R5"]     # a 5-round fight offers rounds 1 to 5
    at = _pick(at, "g_r_b1", 4)
    at = _save(at)
    assert "Saved 1 pick." in " ".join(e.value for e in at.success)
    row = pd.read_sql(sa.text(f"SELECT * FROM game_picks WHERE player_id = '{me['player_id']}'"), world["game"]).iloc[0]
    assert (row.bout_id, row.pick_name, row.pick_method, row.pick_round) == ("b1", "Ben Two", "KO/TKO", 4)
    again = _run(me)                                                                                         # a new session: the saved pick is pre-filled
    assert (again.segmented_control(key="g_w_b1").value, again.segmented_control(key="g_m_b1").value, again.segmented_control(key="g_r_b1").value) == ("Ben Two", "KO/TKO", 4)
    again = _pick(again, "g_m_b1", "Decision")
    assert not [s for s in again.segmented_control if s.key == "g_r_b1"]                                     # a decision has no round
    _save(again)
    row = pd.read_sql(sa.text(f"SELECT * FROM game_picks WHERE player_id = '{me['player_id']}' AND bout_id = 'b1'"), world["game"]).iloc[0]
    assert (row.pick_method, pd.isna(row.pick_round)) == ("Decision", True)                                  # the pick was changed, not duplicated


def test_incomplete_picks_are_refused_with_a_reason_and_nothing_is_stored(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _save(_run(me))
    assert "Nothing to save yet" in " ".join(e.value for e in at.warning)
    at = _pick(at, "g_w_b2", "Cal Three")
    at = _save(at)
    assert "how the fight ends" in " ".join(e.value for e in at.error)
    at = _pick(at, "g_m_b2", "Submission")
    at = _save(at)
    assert "Pick the round the finish happens in" in " ".join(e.value for e in at.error)
    assert pd.read_sql(sa.text(f"SELECT COUNT(*) n FROM game_picks WHERE player_id = '{me['player_id']}'"), world["game"]).n[0] == 0


def test_each_fight_shows_what_is_saved_against_what_is_on_screen(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)
    assert _chips(at) == ["NO PICK", "NO PICK"] and "Everything on screen is saved." in _html(at) and "0</b> of 2 saved" in _html(at)
    at = _pick(at, "g_w_b1", "Ben Two")
    assert _chips(at)[0] == "PICK A METHOD" and "1 fight still needs a method or round" in _html(at)
    at = _pick(at, "g_m_b1", "KO/TKO")
    assert _chips(at)[0] == "PICK A ROUND"
    at = _pick(at, "g_r_b1", 2)
    assert _chips(at)[0] == "NOT SAVED" and "1 unsaved change" in _html(at)
    at = _save(at)
    assert _chips(at) == ["SAVED", "NO PICK"] and "1</b> of 2 saved" in _html(at) and "Everything on screen is saved." in _html(at)
    at = _pick(at, "g_m_b1", "Decision")
    assert _chips(at)[0] == "NOT SAVED"                                                                      # changed since it was saved


def test_clearing_a_saved_pick_and_saving_removes_it(world):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    bout = {"bout_id": "b1", "status": "scheduled", "event_date": CARD_DATE, "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 5}
    game_db.save_pick(world["game"], me["player_id"], bout, "Alex One", "KO/TKO", 1, NOW)
    at = _run(me)
    assert _chips(at)[0] == "SAVED"
    at = _pick(at, "g_w_b1", None)                                                                           # tap the chosen fighter again to clear it
    assert _chips(at)[0] == "NOT SAVED"                                                                      # saving would remove the saved pick
    at = _save(at)
    assert "removed 1" in " ".join(e.value for e in at.success) and _chips(at) == ["NO PICK", "NO PICK"]
    assert pd.read_sql(sa.text(f"SELECT COUNT(*) n FROM game_picks WHERE player_id = '{me['player_id']}'"), world["game"]).n[0] == 0


def test_once_a_card_has_closed_its_picks_are_revealed_and_the_server_refuses_a_late_one(world, monkeypatch):
    bo_open = {"bout_id": "b1", "status": "scheduled", "event_date": CARD_DATE, "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 5}
    assert game_db.save_pick(world["game"], world["bo"]["player_id"], bo_open, "Alex One", "KO/TKO", 3, NOW) is None          # Bob's secret pick on the OPEN card
    at = _run(world["ann"])
    assert "KO/TKO R3" not in _all_text(at) and "Everyone&#x27;s picks: UFC 334" not in _all_text(at)       # hidden while the card is open
    late = datetime(2026, 11, 14, 0, 0, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("G_NOW", late.isoformat())
    at = _run(world["ann"])
    assert "KO/TKO R3" in _html(at) and not [s for s in at.segmented_control if s.key == "g_w_b1"]            # revealed once it closes, and no longer pickable
    assert "closed" in game_db.save_pick(world["game"], world["ann"]["player_id"], bo_open, "Alex One", "KO/TKO", 1, late)       # and the server refuses a late pick even if the page were tricked


def _set_start(world, event_date, first_start):
    """Record a card's first-fight start time the way the daily odds step does."""
    auto_odds.save_event_starts(world["read"], [{"event_date": pd.Timestamp(event_date).date(), "first_start": first_start, "bouts": 5, "distinct_times": 4}], NOW)


def test_a_card_that_starts_early_in_utc_closes_before_its_event_date_reveals_its_picks_and_the_next_card_takes_over(world, monkeypatch):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    bout = {"bout_id": "b1", "status": "scheduled", "event_date": CARD_DATE, "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 5}
    game_db.save_pick(world["game"], world["bo"]["player_id"], bout, "Alex One", "KO/TKO", 3, NOW)                       # Bob's pick, hidden while the card is open
    _set_start(world, CARD_DATE, datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc))                                  # first fight Fri 22:00 UTC, the evening BEFORE the event date (like UFC 325)
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 13, 15, 59, 0, tzinfo=timezone.utc).isoformat())
    at = _run(me)
    text = _html(at)
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_b1", "g_w_b2"]                  # still open, one minute before the close
    assert "Fri Nov 13, 8:00 AM Pacific (Fri Nov 13, 4:00 PM UTC)" in text and "Closes in <b>1 min</b>" in text and "Picks close 6 hours before the first fight." in text
    assert "KO/TKO R3" not in text
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 13, 16, 0, 0, tzinfo=timezone.utc).isoformat())
    at = _run(me)
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_l1"]                           # closed, a day before its event date: the next card is offered
    assert "KO/TKO R3" in _html(at) and any(CARD in e.label for e in at.expander)                                    # and the closed card's picks are revealed under its name
    refused = game_db.save_pick(world["game"], me["player_id"], {**bout, "first_start": datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)}, "Alex One", "KO/TKO", 1,
                                datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc))
    assert "closed" in refused


def test_a_us_card_stays_open_into_its_event_day_and_an_unknown_start_is_said_plainly(world, monkeypatch):
    me, _ = game_db.register(world["game"], "Cyd", "333333", True, NOW)
    at = _run(me)                                                                                                    # no start time recorded for this card at all
    assert "We do not have this card&#x27;s schedule yet" in _html(at) and "Fri Nov 13, 10:00 AM Pacific (Fri Nov 13, 6:00 PM UTC)" in _html(at)
    _set_start(world, CARD_DATE, datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc))                                  # first fight Saturday 20:00 UTC (noon Pacific)
    at = _run(me)
    text = _html(at)
    assert "Sat Nov 14, 6:00 AM Pacific (Sat Nov 14, 2:00 PM UTC)" in text and "do not have" not in text and "Picks close 6 hours before the first fight." in text
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 14, 13, 59, 0, tzinfo=timezone.utc).isoformat())                  # the card's own date, in UTC: still open (the old rule had closed it the day before)
    at = _run(me)
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_b1", "g_w_b2"] and "Closes in <b>1 min</b>" in _html(at)
    monkeypatch.setenv("G_NOW", datetime(2026, 11, 14, 14, 0, 0, tzinfo=timezone.utc).isoformat())
    at = _run(me)
    assert [s.key for s in at.segmented_control if s.key.startswith("g_w_")] == ["g_w_l1"]                           # six hours before the first fight: closed, and the next card takes over


def test_the_leaderboards_score_the_closed_card_and_mark_you(world):
    at = _run(world["ann"])
    assert not at.exception
    tables = [str(h.value) for h in at.html if '<table class="hist">' in str(h.value) and "Vs model" in str(h.value)]
    assert len(tables) == 1 and tables[0].count('<table class="hist">') == 3                                # the three boards
    first = tables[0].split('<table class="hist">')[1]                                                      # Beat the model: Ann 5 points and +2, Bob 0 points
    assert first.index("Ann") < first.index("Bob") and '<tr class="you">' in first and "(you)" in first
    assert ">5<" in first and ">+2<" in first and "mk good" in first
    assert "Beat the model" in tables[0] and "Latest card: UFC 333: Old" in tables[0]
    header = _html(at).split("Signed in as")[1].split("</div>")[0]                                          # the header uses the same ranking as the headline board
    assert "#1</b> of 2 beating the model" in header and "+2</b> vs the model" in header and "5</b> points" in header and "2</b> scored picks" in header


def test_the_header_ranks_you_the_way_the_headline_board_does_even_when_points_would_rank_you_differently(world):
    long_ago = NOW - timedelta(days=6)
    old = lambda b, a, bb: {"bout_id": b, "status": "scheduled", "event_date": "2026-10-24", "fighter_a": a, "fighter_b": bb, "scheduled_rounds": 3}
    eve, _ = game_db.register(world["game"], "Eve", "444444", True, long_ago)
    fay, _ = game_db.register(world["game"], "Fay", "555555", True, long_ago)
    game_db.save_pick(world["game"], eve["player_id"], old("p1", "Eli Five", "Fox Six"), "Eli Five", "KO/TKO", 2, long_ago)       # 3 points, but only what the model also got: 0 against it
    game_db.save_pick(world["game"], fay["player_id"], old("p2", "Gus Seven", "Hal Eight"), "Hal Eight", "Decision", None, long_ago)   # 2 points, but right where the model was wrong: +2
    at = _run(eve)
    header = _html(at).split("Signed in as")[1].split("</div>")[0]
    assert "#3</b> of 4 beating the model" in header and "3</b> points" in header                    # third against the model, although second on points (Ann 5, Eve 3, Fay 2)
    points_board = [str(h.value) for h in at.html if "Points" in str(h.value) and '<table class="hist">' in str(h.value)][0].split('<table class="hist">')[2]
    assert points_board.index("Ann") < points_board.index("Eve") < points_board.index("Fay")        # the Points board does rank her second: the two boards really differ


def test_my_results_show_what_you_picked_against_what_happened_grouped_by_card(world):
    at = _run(world["ann"])
    mine = [str(h.value) for h in at.html if "What happened" in str(h.value)][0]
    assert mine.count('class="grp"') == 1 and "UFC 333: Old" in mine
    assert "Eli Five &middot; KO/TKO R2" in mine and "Hal Eight &middot; Decision" in mine                      # the pick and the real result, method and round included
    assert mine.count('<td class="mk good">') == 2 and "5 points and +2 against the model over 2 picks" in mine           # both of Ann's winners were right
    bob = _run(world["bo"])
    assert '<td class="mk bad">' in [str(h.value) for h in bob.html if "What happened" in str(h.value)][0]    # Bob's Fox Six pick was wrong


def test_hostile_text_is_escaped_everywhere_it_is_shown(world):
    with world["read"].begin() as c:
        c.exec_driver_sql("UPDATE upcoming_fights SET event_name = 'UFC <script>alert(1)</script> & co', fighter_a_name = 'Evil <img src=x onerror=alert(1)>' WHERE bout_id IN ('b1', 'p1')")
    at = _run(world["ann"])
    shown = _html(at)
    assert not at.exception and "<script>" not in shown and "<img" not in shown                              # nothing the site is told to render as markup
    assert "&lt;script&gt;" in shown and "&lt;img" in shown


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
    assert not at.exception and "No scores yet" in _html(at) and "Nothing scored yet" in _html(at)           # a fresh install: no results table, nothing breaks


def test_the_site_has_a_game_page_wired_to_its_own_database_user_and_secrets():
    src = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert '"Game": page_game' in src and '"Upcoming", "Past events", "Fighters", "Track record", "Game"' in src
    assert 'game_ui.render(q, get_game_engine(), _secret("GAME_INVITE_CODE"))' in src and 'url = _secret("GAME_DATABASE_URL")' in src
    assert "pool_size=3, max_overflow=3" in src                                                  # at most 6 connections, under the game user's limit of 10
