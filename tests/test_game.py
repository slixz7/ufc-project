"""Tests for the friends' game: its rules, its database layer, and the recording of how bouts ended. No network and no real database."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import bout_results  # noqa: E402
import game_core as core  # noqa: E402
import game_db  # noqa: E402

T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
EVENT = date(2026, 10, 31)                                    # picks close at 00:00 UTC on this day


# ---------------------------------------------------------------------------------------------- who you are
def test_nicknames_are_plain_unique_and_cannot_pass_for_the_model_or_an_admin():
    assert core.parse_nickname("  Big   Bob ") == ("Big Bob", "big bob")                                    # spaces collapse; the key is lower case
    assert core.parse_nickname("BIG BOB")[1] == core.parse_nickname("big bob")[1]                           # the same person however it is typed
    for bad in ("ab", "x" * 21, "", None, "<script>", "bob<b>", "bob;drop", "Bób", "bob\u200b", "-bob", "bob-", "a  "):
        with pytest.raises(core.GameError):
            core.parse_nickname(bad)
    for reserved in ("Model", "THE MODEL", "Admin", "the model 2", "UFC"):
        with pytest.raises(core.GameError):
            core.parse_nickname(reserved)


def test_pins_are_four_to_eight_digits():
    assert core.parse_pin(" 123456 ") == "123456" and core.parse_pin("0000") == "0000"
    for bad in ("123", "123456789", "12ab", "", None, "12 34", "-1234"):
        with pytest.raises(core.GameError):
            core.parse_pin(bad)


def test_a_pin_is_stored_only_as_a_salted_hash():
    a, b = core.hash_pin("123456"), core.hash_pin("123456")
    assert a != b and a.startswith("scrypt$") and "123456" not in a                                         # a fresh salt every time; the PIN is not in it
    assert core.verify_pin("123456", a) and core.verify_pin("123456", b)
    assert not core.verify_pin("123457", a) and not core.verify_pin("", a)
    for broken in ("", "nonsense", "scrypt$1$2$3", "bcrypt$1$2$3$aa$bb", "scrypt$16384$8$1$zz$zz", None):
        assert core.verify_pin("123456", broken) is False                                                   # a damaged record never lets anyone in, and never crashes


def test_the_invite_code_comparison_forgives_case_and_spaces_but_never_an_empty_code():
    assert core.invite_matches("Blue Falcon 42", "blue falcon 42") and core.invite_matches(" blue falcon42 ", "Blue Falcon 42")
    assert not core.invite_matches("blue falcon 43", "blue falcon 42")
    assert not core.invite_matches("", "") and not core.invite_matches(None, None) and not core.invite_matches("anything", "")      # an unset secret admits nobody


# ---------------------------------------------------------------------------------------------- picks
BOUT = {"bout_id": "b1", "status": "scheduled", "event_date": EVENT, "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 3}


def test_picks_close_at_midnight_utc_on_event_day():
    assert core.cutoff(EVENT) == datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert core.picks_open(EVENT, datetime(2026, 10, 30, 23, 59, 59, tzinfo=timezone.utc))
    assert not core.picks_open(EVENT, datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc))                # the instant it closes
    assert not core.picks_open(EVENT, datetime(2026, 11, 1, tzinfo=timezone.utc))


def test_every_kind_of_invalid_pick_is_refused_with_a_reason():
    ok = lambda **kw: core.validate_pick({**BOUT, **kw.pop("bout", {})}, kw.get("winner", "Alex One"), kw.get("method", "KO/TKO"), kw.get("rnd", 2), kw.get("now", T0))
    assert ok() == (None, 2) and ok(method="Submission", rnd="3") == (None, 3)
    assert ok(method="Decision", rnd=5) == (None, None)                                                     # a decision has no round, whatever was sent
    assert "no longer on the card" in ok(bout={"status": "cancelled"})[0]
    assert "closed" in ok(now=datetime(2026, 10, 31, 0, 0, 1, tzinfo=timezone.utc))[0]
    assert "one of the two fighters" in ok(winner="Somebody Else")[0] and "one of the two fighters" in ok(winner=None)[0]
    assert "how the fight ends" in ok(method="Knockout")[0] and "how the fight ends" in ok(method=None)[0]
    for bad_round in (None, "", "x", 0, 4, -1):
        assert ok(rnd=bad_round)[0], bad_round                                                              # a finish needs a round from 1 to the scheduled rounds
    assert ok(bout={"scheduled_rounds": 5}, rnd=5) == (None, 5)


def _scoring_fixture():
    bouts = pd.DataFrame({"bout_id": ["b1", "b2", "b3", "b4", "b5"], "event_name": ["UFC X"] * 5, "event_date": ["2026-10-31"] * 5,
                          "fighter_a": ["A1", "A2", "A3", "A4", "A5"], "fighter_b": ["B1", "B2", "B3", "B4", "B5"]})
    results = pd.DataFrame({"bout_id": ["b1", "b2", "b3", "b4", "b5"], "winner_side": ["a", "b", "a", None, "a"], "actual_method": ["KO/TKO", "Decision", "DQ", "VOID", "VOID"],
                            "finish_round": [2, np.nan, np.nan, np.nan, np.nan], "went_distance": [0, 1, np.nan, np.nan, np.nan]})
    model = pd.DataFrame({"bout_id": ["b1", "b2", "b3"], "model_winner": ["A1", "A2", "B3"]})                      # right on b1, wrong on b2 and b3
    dist = pd.DataFrame({"bout_id": ["b1", "b2", "b3"], "p_distance": [0.3, 0.4, 0.5]})                            # calls a finish on b1 (right) and b2 (wrong)
    pick = lambda pid, nick, b, name, m, r=None: {"player_id": pid, "nickname": nick, "bout_id": b, "pick_name": name, "pick_method": m, "pick_round": r}
    picks = pd.DataFrame([pick("p1", "Pat", "b1", "A1", "KO/TKO", 2), pick("p1", "Pat", "b2", "B2", "Decision"), pick("p1", "Pat", "b3", "A3", "Submission", 1),
                          pick("p2", "Sam", "b1", "B1", "Submission", 1), pick("p2", "Sam", "b2", "A2", "Decision"),
                          pick("p2", "Sam", "b4", "A4", "KO/TKO", 1), pick("p2", "Sam", "b5", "A5", "KO/TKO", 1), pick("p2", "Sam", "b6", "A6", "KO/TKO", 1)])
    return picks, bouts, results, model, dist


def test_the_scores_match_a_hand_calculation():
    s = core.score_picks(*_scoring_fixture()).set_index(["nickname", "bout_id"])
    assert len(s) == 5                                                                                      # Sam's picks on a voided bout, a no-winner bout and an unknown bout are ignored
    # Pat b1: winner, method and round all right = 3 points; he and the model were both right, and both called a finish: 0 versus the model
    assert (s.loc[("Pat", "b1"), "points"], s.loc[("Pat", "b1"), "vs_model"]) == (3, 0)
    # Pat b2: right winner and method (a decision) = 2 points; right where the model was wrong on the winner AND on the distance call: +1 +1
    assert (s.loc[("Pat", "b2"), "points"], s.loc[("Pat", "b2"), "vs_winner"], s.loc[("Pat", "b2"), "vs_distance"], s.loc[("Pat", "b2"), "vs_model"]) == (2, 1, 1, 2)
    # Pat b3: a DQ: only the winner scores (1 point); it is not a distance question; the model was wrong on the winner: +1
    assert (s.loc[("Pat", "b3"), "points"], s.loc[("Pat", "b3"), "vs_distance"], s.loc[("Pat", "b3"), "vs_model"]) == (1, 0, 1)
    # Sam b1: wrong winner = 0 points, and -1 against the model (which was right); his finish call was right, as was the model's
    assert (s.loc[("Sam", "b1"), "points"], s.loc[("Sam", "b1"), "vs_winner"], s.loc[("Sam", "b1"), "vs_model"]) == (0, -1, -1)
    # Sam b2: wrong winner, but the right distance call where the model was wrong: +1, and a point for nothing else
    assert (s.loc[("Sam", "b2"), "points"], s.loc[("Sam", "b2"), "vs_winner"], s.loc[("Sam", "b2"), "vs_distance"]) == (0, 0, 1)
    lb = core.leaderboard(core.score_picks(*_scoring_fixture()))
    assert lb.nickname.tolist() == ["Pat", "Sam"] and lb.points.tolist() == [6, 0] and lb.vs_model.tolist() == [3, 0] and lb.picks.tolist() == [3, 2]


def test_without_a_connection_string_the_scripts_say_what_to_do_instead_of_a_wall_of_errors(monkeypatch, tmp_path):
    import model_v2
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(model_v2, "DEFAULT_DB_URL", "postgresql+psycopg2://nobody:x@127.0.0.1:1/none")             # nothing is listening there
    with pytest.raises(SystemExit) as stop:
        model_v2.make_engine()
    message = str(stop.value)
    assert "No database connection string is loaded" in message and '$env:DATABASE_URL = "paste-your-string-here"' in message and "Traceback" not in message
    monkeypatch.setattr(model_v2, "DEFAULT_DB_URL", f"sqlite:///{tmp_path / 'local.db'}")                          # a database that IS there (the original local setup) still works
    assert model_v2.make_engine().url.drivername == "sqlite"


def test_a_connection_string_that_is_loaded_is_used_as_given_and_never_second_guessed(monkeypatch):
    import model_v2
    monkeypatch.setenv("DATABASE_URL", "postgres://someone:secret@127.0.0.1:1/neondb?sslmode=require")           # unreachable, but explicit: no probe, no message
    engine = model_v2.make_engine()
    assert engine.url.drivername == "postgresql+psycopg2" and engine.url.host == "127.0.0.1"


def test_the_scored_rows_carry_what_was_picked_and_what_happened():
    s = core.score_picks(*_scoring_fixture()).set_index(["nickname", "bout_id"])
    row = s.loc[("Pat", "b1")]
    assert (row.fighter_a, row.fighter_b, row.pick_method, row.pick_round, row.actual_method, row.finish_round) == ("A1", "B1", "KO/TKO", 2, "KO/TKO", 2)
    assert s.loc[("Pat", "b2")].actual_method == "Decision" and pd.isna(s.loc[("Pat", "b2")].finish_round) and pd.isna(s.loc[("Pat", "b2")].pick_round)


def test_copying_the_model_earns_exactly_nothing_versus_the_model():
    picks, bouts, results, model, dist = _scoring_fixture()
    mw = dict(zip(model.bout_id, model.model_winner))
    copy = pd.DataFrame([{"player_id": "c", "nickname": "Copy", "bout_id": b, "pick_name": mw[b], "pick_method": "Decision" if p >= 0.5 else "KO/TKO", "pick_round": None if p >= 0.5 else 1}
                         for b, p in zip(dist.bout_id, dist.p_distance)])
    s = core.score_picks(copy, bouts, results, model, dist)
    assert len(s) == 3 and s.vs_model.sum() == 0 and (s.vs_winner == 0).all() and (s.vs_distance == 0).all()


def test_missing_model_picks_and_empty_inputs_never_break_the_scoring():
    picks, bouts, results, model, dist = _scoring_fixture()
    s = core.score_picks(picks, bouts, results, model.iloc[0:0], dist.iloc[0:0])
    assert (s.vs_model == 0).all() and s.points.sum() == 6                                                  # no model to compare with: points still count, the comparison is neutral
    assert core.score_picks(picks.iloc[0:0], bouts, results, model, dist).empty
    assert core.leaderboard(core.score_picks(picks.iloc[0:0], bouts, results, model, dist)).empty
    only_void = results.assign(actual_method="VOID")
    assert core.score_picks(picks, bouts, only_void, model, dist).empty


def test_the_leaderboards_rank_by_points_or_by_the_versus_model_score_and_can_be_limited_to_a_year():
    s = core.score_picks(*_scoring_fixture())
    old = s.assign(event_date="2025-03-01", player_id="p3", nickname="Old", points=9, vs_model=-9)
    both = pd.concat([s, old], ignore_index=True)
    assert core.leaderboard(both).nickname.tolist()[0] == "Old" and core.versus_model_board(both).nickname.tolist()[0] == "Pat"
    assert core.leaderboard(both, 2026).nickname.tolist() == ["Pat", "Sam"] and core.leaderboard(both, 2025).nickname.tolist() == ["Old"]
    assert core.leaderboard(both, 1999).empty


# ---------------------------------------------------------------------------------------------- the database layer
@pytest.fixture
def db(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'game.db'}")
    game_db.ensure_tables(eng)
    return eng


def _join(db, nick="Pat", pin="123456", now=T0):
    player, err = game_db.register(db, nick, pin, True, now)
    assert err is None, err
    return player


def test_joining_needs_the_invite_code_a_free_nickname_and_a_valid_pin(db):
    assert game_db.register(db, "Pat", "123456", False, T0) == (None, "That invite code is not right.")
    assert game_db.register(db, "Pat", "12", True, T0)[1].startswith("A PIN is 4 to 8 digits")
    assert game_db.register(db, "x", "123456", True, T0)[1].startswith("A nickname is 3 to 20")
    p = _join(db)
    assert p["nickname"] == "Pat" and len(p["player_id"]) == 32
    assert game_db.register(db, "pat", "654321", True, T0) == (None, "That nickname is taken.")               # case-insensitive
    assert game_db.register(db, "The Model", "654321", True, T0)[0] is None
    assert game_db.register(db, "Late One", "123456", True, T0, max_players=1) == (None, "The game is full.")
    stored = pd.read_sql(sa.text("SELECT * FROM game_players"), db)
    assert len(stored) == 1 and "123456" not in stored.pin_hash[0] and stored.pin_hash[0].startswith("scrypt$")      # only a hash is kept


def test_wrong_invite_codes_pause_joining_for_an_hour_even_for_the_right_code(db):
    for i in range(10):
        assert game_db.register(db, f"Guess{i}", "123456", False, T0 + timedelta(seconds=i))[1] == "That invite code is not right."
    player, err = game_db.register(db, "Real Friend", "123456", True, T0 + timedelta(minutes=5))
    assert player is None and "paused" in err and "minutes" in err
    assert game_db.register(db, "Real Friend", "123456", True, T0 + timedelta(minutes=61))[1] is None            # an hour later it opens again


def test_signing_in_gives_the_same_answer_for_a_wrong_pin_and_an_unknown_nickname(db):
    _join(db)
    assert game_db.login(db, "Pat", "123456", T0)[0]["nickname"] == "Pat" and game_db.login(db, "  PAT ", "123456", T0)[1] is None
    wrong_pin = game_db.login(db, "Pat", "000000", T0)
    unknown = game_db.login(db, "Nobody", "000000", T0)
    assert wrong_pin == unknown == (None, "Wrong nickname or PIN.")
    assert game_db.login(db, "<b>", "12", T0) == (None, "Wrong nickname or PIN.")                               # nothing about the format is revealed either


def test_five_wrong_pins_lock_a_nickname_even_against_the_right_pin_until_they_age_out(db):
    _join(db)
    for i in range(5):
        assert game_db.login(db, "Pat", "000000", T0 + timedelta(minutes=i))[1] == "Wrong nickname or PIN."
    locked = game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=5))                                       # the right PIN, but too soon
    assert locked[0] is None and "Too many wrong attempts" in locked[1]
    minutes = int(locked[1].split("about ")[1].split(" ")[0])
    assert 9 <= minutes <= 11                                                                                    # the first failure was at +0 and ages out at +15, so about 10 more
    assert game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=14))[0] is None                            # still locked
    assert game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=16))[0]["nickname"] == "Pat"               # the failures have aged out
    other = _join(db, "Sam", "654321", T0)
    assert game_db.login(db, "Sam", "654321", T0 + timedelta(minutes=5))[0]["player_id"] == other["player_id"]    # one nickname's lock never affects another's


def test_a_successful_sign_in_clears_the_count_and_a_slow_guesser_still_hits_the_daily_limit(db):
    _join(db)
    for i in range(4):
        game_db.login(db, "Pat", "000000", T0 + timedelta(minutes=i))
    assert game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=5))[1] is None                              # success resets the count
    for i in range(4):
        assert game_db.login(db, "Pat", "000000", T0 + timedelta(minutes=10 + i))[1] == "Wrong nickname or PIN."  # four more are allowed again
    t = T0 + timedelta(hours=1)                                                                                  # a patient attacker: 4 wrong guesses every 15 minutes
    wrong = 0
    for i in range(30):
        player, err = game_db.login(db, "Pat", "000000", t + timedelta(minutes=16 * i))
        wrong += player is None and err == "Wrong nickname or PIN."
    assert wrong == 16                                                                                           # 4 earlier + 16 = the 20th wrong guess of the day; every later one was refused
    assert "Too many wrong attempts" in game_db.login(db, "Pat", "123456", t + timedelta(minutes=16 * 30))[1]    # even the right PIN, until the day is up


def test_a_pick_is_saved_changed_and_validated_again_on_the_server(db):
    p = _join(db)
    later = T0 + timedelta(hours=1)
    assert game_db.save_pick(db, p["player_id"], BOUT, "Alex One", "KO/TKO", 2, T0) is None
    assert game_db.save_pick(db, p["player_id"], BOUT, "Ben Two", "Decision", 4, later) is None                  # a change; a decision drops the round
    row = pd.read_sql(sa.text("SELECT * FROM game_picks"), db).iloc[0]
    assert (row.pick_name, row.pick_method, pd.isna(row.pick_round)) == ("Ben Two", "Decision", True) and len(pd.read_sql(sa.text("SELECT * FROM game_picks"), db)) == 1
    assert pd.Timestamp(row.updated_at) > pd.Timestamp(row.submitted_at)                                          # the first pick time is kept, the change is recorded
    for bad in (("Somebody", "KO/TKO", 1), ("Alex One", "Knockout", 1), ("Alex One", "KO/TKO", 9), ("Alex One", "Submission", None)):
        assert game_db.save_pick(db, p["player_id"], BOUT, *bad, T0)                                              # each is refused with a message...
    assert pd.read_sql(sa.text("SELECT pick_name FROM game_picks"), db).pick_name.tolist() == ["Ben Two"]         # ...and the stored pick is untouched
    assert "closed" in game_db.save_pick(db, p["player_id"], BOUT, "Alex One", "KO/TKO", 1, datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert "no longer on the card" in game_db.save_pick(db, p["player_id"], {**BOUT, "status": "cancelled"}, "Alex One", "KO/TKO", 1, T0)
    assert game_db.save_pick(db, "forged-id", BOUT, "Alex One", "KO/TKO", 1, T0) == "Please sign in again."      # a made-up player cannot write picks


def test_a_pick_can_be_removed_only_by_its_owner_and_only_while_the_card_is_open(db):
    a, b = _join(db, "Pat"), _join(db, "Sam", "654321")
    for p in (a, b):
        game_db.save_pick(db, p["player_id"], BOUT, "Alex One", "KO/TKO", 1, T0)
    assert game_db.delete_pick(db, a["player_id"], BOUT, T0 + timedelta(hours=1)) is None
    assert game_db.all_picks(db).nickname.tolist() == ["Sam"]                                                     # only Pat's pick went; Sam's is untouched
    assert game_db.delete_pick(db, a["player_id"], BOUT, T0) is None                                              # removing what is not there is harmless
    assert "closed" in game_db.delete_pick(db, b["player_id"], BOUT, datetime(2026, 10, 31, tzinfo=timezone.utc))   # too late: the pick stands
    assert "no longer on the card" in game_db.delete_pick(db, b["player_id"], {**BOUT, "status": "cancelled"}, T0)
    assert game_db.all_picks(db).nickname.tolist() == ["Sam"]


def test_picks_can_be_read_back_and_an_account_can_be_deleted_with_its_picks(db):
    a, b = _join(db, "Pat"), _join(db, "Sam", "654321")
    for p in (a, b):
        game_db.save_pick(db, p["player_id"], BOUT, "Alex One", "KO/TKO", 1, T0)
    assert sorted(game_db.all_picks(db).nickname) == ["Pat", "Sam"] and len(game_db.my_picks(db, a["player_id"])) == 1
    game_db.delete_player(db, a["player_id"])
    assert game_db.all_picks(db).nickname.tolist() == ["Sam"] and game_db.login(db, "Pat", "123456", T0)[0] is None
    assert len(pd.read_sql(sa.text("SELECT * FROM game_picks"), db)) == 1                                         # nothing of Pat's is left behind


def test_the_owner_can_reset_a_forgotten_pin_which_also_clears_a_lock_and_list_or_delete_players(db):
    p = _join(db)
    game_db.save_pick(db, p["player_id"], BOUT, "Alex One", "KO/TKO", 1, T0)
    for i in range(5):
        game_db.login(db, "Pat", "000000", T0 + timedelta(minutes=i))
    assert "Too many" in game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=6))[1]
    new_pin = game_db.reset_pin(db, "pat", T0)
    assert len(new_pin) == 6 and new_pin.isdigit()
    assert game_db.login(db, "Pat", new_pin, T0 + timedelta(minutes=7))[0]["nickname"] == "Pat"                  # unlocked at once
    assert game_db.login(db, "Pat", "123456", T0 + timedelta(minutes=8))[0] is None                               # the old PIN no longer works
    assert game_db.reset_pin(db, "Pat", T0, "7777")                                                               # the owner may choose the PIN
    with pytest.raises(core.GameError):
        game_db.reset_pin(db, "Ghost", T0)
    listing = game_db.list_players(db)
    assert listing.nickname.tolist() == ["Pat"] and listing.picks.tolist() == [1]
    assert game_db.delete_by_nickname(db, "PAT") is True and game_db.delete_by_nickname(db, "Pat") is False


# ---------------------------------------------------------------------------------------------- how bouts ended
@pytest.fixture
def results_db(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'results.db'}")
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE fights (fight_url TEXT, event_date TEXT, event_name TEXT, result TEXT, finish_round INTEGER)")
        c.exec_driver_sql("CREATE TABLE fighter_fights (fight_url TEXT, fighter_url TEXT, fighter_name TEXT, side INTEGER, won INTEGER)")
        c.exec_driver_sql("""CREATE TABLE upcoming_fights (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, bout_order INTEGER, weight_class TEXT, title_fight BOOLEAN,
            fighter_a_name TEXT, fighter_a_url TEXT, fighter_b_name TEXT, fighter_b_url TEXT, status TEXT, loaded_at TIMESTAMP)""")
    return eng


def _night(eng, fights, date_="2026-10-24"):
    """fights: (fight_url, result, round, a_url, a_name, a_won, b_url, b_name, b_won)"""
    for fu, result, rnd, au, an, aw, bu, bn, bw in fights:
        pd.DataFrame([dict(fight_url=fu, event_date=date_, event_name="UFC X", result=result, finish_round=rnd)]).to_sql("fights", eng, if_exists="append", index=False)
        pd.DataFrame([dict(fight_url=fu, fighter_url=au, fighter_name=an, side=1, won=aw), dict(fight_url=fu, fighter_url=bu, fighter_name=bn, side=2, won=bw)]).to_sql(
            "fighter_fights", eng, if_exists="append", index=False)


def _announce(eng, bouts):
    rows = [dict(bout_id=b, event_name="UFC X", event_date=d, bout_order=i, weight_class="Lightweight", title_fight=False, fighter_a_name=an, fighter_a_url=au,
                 fighter_b_name=bn, fighter_b_url=bu, status=st) for i, (b, d, an, au, bn, bu, st) in enumerate(bouts)]
    pd.DataFrame(rows).to_sql("upcoming_fights", eng, if_exists="append", index=False)


def test_each_kind_of_result_becomes_a_method_a_round_and_a_distance_flag():
    assert [bout_results.method_group(r) for r in ("KO/TKO", "TKO - Doctor's Stoppage", "Submission", "Decision - Unanimous", "Decision - Split", "DQ", "Overturned", None, np.nan)] == \
        ["KO/TKO", "KO/TKO", "Submission", "Decision", "Decision", "DQ", "Other", "Other", "Other"]


def test_the_recorder_waits_for_the_dataset_records_what_happened_and_marks_fallen_through_bouts_void(results_db):
    eng = results_db
    _announce(eng, [("ko", "2026-10-24", "A Ko", "u1", "B Ko", "u2", "scheduled"), ("dec", "2026-10-24", "A Dec", "u3", "B Dec", "u4", "scheduled"),
                    ("sub", "2026-10-24", "A Sub", "u5", "B Sub", "u6", "scheduled"), ("doc", "2026-10-24", "A Doc", "u7", "B Doc", "u8", "scheduled"),
                    ("dq", "2026-10-24", "A Dq", "u9", "B Dq", "u10", "scheduled"), ("draw", "2026-10-24", "A Draw", "u11", "B Draw", "u12", "scheduled"),
                    ("gone", "2026-10-24", "A Gone", "u13", "B Gone", "u14", "scheduled"), ("cxl", "2026-10-24", "A Cxl", "u15", "B Cxl", "u16", "cancelled"),
                    ("later", "2026-12-12", "A Later", "u17", "B Later", "u18", "scheduled")])
    assert bout_results.run_record(eng, date(2026, 10, 26)) == 0
    assert pd.read_sql(sa.text("SELECT COUNT(*) n FROM bout_results"), eng).n[0] == 0                              # the dataset has nothing from that night: everything waits
    _night(eng, [("f1", "KO/TKO", 2, "u1", "A Ko", 0, "u2", "B Ko", 1),                                             # the card's SECOND fighter won
                 ("f2", "Decision - Unanimous", 3, "u3", "A Dec", 1, "u4", "B Dec", 0), ("f3", "Submission", 1, "u5", "A Sub", 1, "u6", "B Sub", 0),
                 ("f4", "TKO - Doctor's Stoppage", 3, "u7", "A Doc", 0, "u8", "B Doc", 1), ("f5", "DQ", 2, "u9", "A Dq", 1, "u10", "B Dq", 0),
                 ("f6", "Decision - Majority", 3, "u11", "A Draw", 0, "u12", "B Draw", 0)])
    bout_results.run_record(eng, date(2026, 10, 26))
    r = pd.read_sql(sa.text("SELECT * FROM bout_results"), eng).set_index("bout_id")
    assert sorted(r.index) == ["dec", "doc", "dq", "draw", "gone", "ko", "sub"]                                    # the cancelled bout is skipped, the December bout still waits
    assert (r.loc["ko", "winner_side"], r.loc["ko", "method"], r.loc["ko", "finish_round"], r.loc["ko", "went_distance"]) == ("b", "KO/TKO", 2, 0)
    assert (r.loc["dec", "winner_side"], r.loc["dec", "method"], pd.isna(r.loc["dec", "finish_round"]), r.loc["dec", "went_distance"]) == ("a", "Decision", True, 1)
    assert (r.loc["sub", "method"], r.loc["sub", "finish_round"]) == ("Submission", 1) and (r.loc["doc", "method"], r.loc["doc", "winner_side"]) == ("KO/TKO", "b")
    assert (r.loc["dq", "method"], r.loc["dq", "winner_side"], pd.isna(r.loc["dq", "went_distance"])) == ("DQ", "a", True)                  # a DQ names a winner but is not a distance result
    assert (r.loc["draw", "method"], pd.isna(r.loc["draw", "winner_side"])) == ("VOID", True)                      # nobody won
    assert (r.loc["gone", "method"], pd.isna(r.loc["gone", "winner_side"])) == ("VOID", True)                      # the night is there, the bout is not: it fell through
    before = len(r)
    bout_results.run_record(eng, date(2026, 10, 26))
    assert pd.read_sql(sa.text("SELECT COUNT(*) n FROM bout_results"), eng).n[0] == before                          # a second run adds nothing


def test_the_winner_is_placed_by_fighter_id_or_a_clear_name_and_never_by_guessing(results_db):
    eng = results_db
    _announce(eng, [("known", "2026-10-24", "Alex Volkanovski", "new:x", "Movsar Evloev", "new:y", "scheduled"),
                    ("blurry", "2026-10-24", "Chris Smith", "new:z", "Chris Smyth", "new:w", "scheduled")])
    _night(eng, [("f1", "Submission", 1, "d1", "Alexander Volkanovski", 1, "d2", "Movsar Evloev", 0), ("f2", "KO/TKO", 1, "d3", "Chris Smith", 1, "d4", "Chris Smyth", 0)])
    bout_results.run_record(eng, date(2026, 10, 26))
    r = pd.read_sql(sa.text("SELECT * FROM bout_results"), eng).set_index("bout_id")
    assert r.loc["known", "winner_side"] == "a"                                                                      # 'Alex' and 'Alexander' are clearly the first fighter
    assert r.loc["blurry", "method"] == "VOID"                                                                       # two near-identical names: not guessed, so not scored
