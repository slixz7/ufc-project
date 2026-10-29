"""The Discord posts about the friends' game: a reminder before a card's picks close, and the leaderboard after its results. No network."""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import auto_odds  # noqa: E402
import bout_results  # noqa: E402
import digest  # noqa: E402
import game_core as core  # noqa: E402
import game_db  # noqa: E402
from auto_common import Report  # noqa: E402

UTC = timezone.utc
ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
HOOK = "https://discord.com/api/webhooks/123456789/abcdefghij"


# ---------------------------------------------------------------------------------------------- the words (no database)
def test_nicknames_cannot_turn_into_discord_formatting():
    assert digest.md("big_bob*x") == "big\\_bob\\*x" and digest.md("plain name") == "plain name" and digest.md("a~b`c") == "a\\~b\\`c"


def test_the_moment_a_card_closes_reads_the_same_in_discord_and_on_the_page():
    assert core.when_text(datetime(2026, 10, 31, 7, 45, tzinfo=UTC)) == "Sat Oct 31, 12:45 AM Pacific (Sat Oct 31, 7:45 AM UTC)"
    assert core.when_text(datetime(2026, 11, 13, 16, 0, tzinfo=UTC)) == "Fri Nov 13, 8:00 AM Pacific (Fri Nov 13, 4:00 PM UTC)"        # after clocks go back


def test_the_reminder_says_when_picks_close_and_how_many_have_picked_but_never_who():
    closes = datetime(2026, 10, 31, 7, 45, tzinfo=UTC)
    text = digest.close_reminder_message("UFC Fight Night: Moicano vs. Nolan", closes, datetime(2026, 10, 30, 14, 17, tzinfo=UTC), 2, 5)
    assert "**UFC Fight Night: Moicano vs. Nolan**" in text and "in about 17 hours" in text and "Sat Oct 31, 12:45 AM Pacific (Sat Oct 31, 7:45 AM UTC)" in text
    assert "2 of 5 players have made picks so far" in text and digest.SITE in text
    one = digest.close_reminder_message("UFC X", closes, closes - timedelta(minutes=20), 1, 1)
    assert "in about 1 hour," in one and "1 of 1 player has made picks" in one                                          # singular forms


def test_the_leaderboard_ranks_with_medals_escapes_names_caps_the_list_and_names_the_season_leader():
    card = pd.DataFrame({"nickname": [f"player_{i}" for i in range(10)], "picks": [12] * 10, "winners_right": [8] * 10, "points": list(range(20, 10, -1)), "vs_model": list(range(5, -5, -1))})
    season = pd.DataFrame({"nickname": ["player_0"], "picks": [30], "winners_right": [20], "points": [50], "vs_model": [7]})
    text = "\n".join(digest.game_results_messages("2026-10-24", "UFC 333: Volkanovski vs. Evloev", card, season, 2026))
    assert text.index("🥇 player\\_0: +5 vs the model, 20 points (8 of 12 winners)") < text.index("🥈 player\\_1") < text.index("🥉 player\\_2") < text.index("4. player\\_3")
    assert "8. player\\_7" in text and "player\\_8" not in text and "…and 2 more" in text                                  # at most eight lines, then a count
    assert "2026 so far: player\\_0 leads against the model at +7." in text and "UFC 333: Volkanovski vs. Evloev" in text
    assert all(len(m) <= digest.LIMIT for m in digest.game_results_messages("2026-10-24", "UFC X", card, season, 2026))


# ---------------------------------------------------------------------------------------------- the database path, on a real Postgres
@pytest.fixture
def world():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    db = f"dg_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {db}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=db))
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE upcoming_fights (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, bout_order INTEGER, fighter_a_name TEXT, fighter_b_name TEXT, status TEXT)")
    game_db.ensure_tables(eng)
    bout_results.ensure_table(eng)
    auto_odds.ensure_event_starts(eng)
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _bouts(eng, event, d, rows):
    with eng.begin() as c:
        for i, (bid, a, b) in enumerate(rows, start=1):
            c.execute(sa.text("INSERT INTO upcoming_fights VALUES (:i, :e, :d, :o, :a, :b, 'scheduled')"), {"i": bid, "e": event, "d": d, "o": i, "a": a, "b": b})


def _players(eng, names, when=datetime(2026, 10, 1, tzinfo=UTC)):
    return {n: game_db.register(eng, n, "123456", True, when)[0] for n in names}


def _pick(eng, p, bid, d, a, b, winner, method, rnd, now=datetime(2026, 10, 2, tzinfo=UTC)):
    assert game_db.save_pick(eng, p["player_id"], {"bout_id": bid, "status": "scheduled", "event_date": d, "fighter_a": a, "fighter_b": b, "scheduled_rounds": 3}, winner, method, rnd, now) is None


def _groups(eng, today, now, kinds=("game_close", "game_results")):
    return [g for g in digest.pending(eng, today, now=now) if g["kind"] in kinds]


def test_the_reminder_comes_once_within_30_hours_of_the_close_only_when_someone_plays(world):
    _bouts(world, "UFC Fight Night: Moicano vs. Nolan", date(2026, 10, 31), [("n1", "Renato Moicano", "Tom Nolan"), ("n2", "Mario Bautista", "Vinicius Oliveira")])
    auto_odds.save_event_starts(world, [{"event_date": date(2026, 10, 31), "first_start": datetime(2026, 10, 31, 13, 45, tzinfo=UTC), "bouts": 2, "distinct_times": 2}], datetime(2026, 10, 28, tzinfo=UTC))
    run_day = datetime(2026, 10, 30, 14, 17, tzinfo=UTC)
    assert _groups(world, date(2026, 10, 30), run_day) == []                                                          # nobody has joined yet: nothing is posted
    ann, bob, _cyd = _players(world, ["Ann", "Bob", "Cyd"]).values()
    _pick(world, ann, "n1", date(2026, 10, 31), "Renato Moicano", "Tom Nolan", "Tom Nolan", "KO/TKO", 2)
    _pick(world, bob, "n2", date(2026, 10, 31), "Mario Bautista", "Vinicius Oliveira", "Mario Bautista", "Decision", None)
    g = _groups(world, date(2026, 10, 30), run_day)
    assert [x["kind"] for x in g] == ["game_close"] and g[0]["keys"] == ["game_close:2026-10-31"]
    text = g[0]["messages"][0]
    assert "2 of 3 players have made picks so far" in text and "in about 17 hours" in text and "Sat Oct 31, 12:45 AM Pacific" in text
    assert "Ann" not in text and "Bob" not in text and "Tom Nolan" not in text                                       # counts only: never who picked, or what
    assert _groups(world, date(2026, 10, 29), datetime(2026, 10, 29, 14, 17, tzinfo=UTC)) == []                      # 41 hours before the close: too early
    assert _groups(world, date(2026, 10, 31), datetime(2026, 10, 31, 8, 0, tzinfo=UTC)) == []                        # already closed
    digest.mark_sent(world, g[0]["keys"])
    assert _groups(world, date(2026, 10, 30), run_day) == []                                                          # said once, never again


def test_a_card_with_no_start_time_is_reminded_by_the_fallback_close(world):
    _bouts(world, "UFC 334", date(2026, 11, 14), [("m1", "Ciryl Gane", "Josh Hokit")])
    _players(world, ["Ann"])
    g = _groups(world, date(2026, 11, 13), datetime(2026, 11, 13, 14, 17, tzinfo=UTC))                                  # no event_starts row: closes 18:00 UTC the day before, 3.7 hours away
    assert len(g) == 1 and "in about 4 hours" in g[0]["messages"][0] and "Fri Nov 13, 10:00 AM Pacific" in g[0]["messages"][0]


def test_the_leaderboard_is_posted_when_every_result_is_in_and_not_before_or_twice(world):
    d = date(2026, 10, 24)
    rows = [("y1", "Eli Five", "Fox Six"), ("y2", "Gus Seven", "Hal Eight")]
    _bouts(world, "UFC 333: Old", d, rows)
    p = _players(world, ["Ann", "Bob_x"])
    _pick(world, p["Ann"], "y1", d, "Eli Five", "Fox Six", "Eli Five", "KO/TKO", 2)                                      # all three points
    _pick(world, p["Bob_x"], "y1", d, "Eli Five", "Fox Six", "Fox Six", "Submission", 1)                                 # wrong
    with world.begin() as c:                                                                                         # only the first result is in
        c.execute(sa.text("INSERT INTO bout_results (bout_id, event_date, winner_side, method, finish_round, went_distance) VALUES ('y1', :d, 'a', 'KO/TKO', 2, 0)"), {"d": d})
    assert _groups(world, date(2026, 10, 26), datetime(2026, 10, 26, 14, 17, tzinfo=UTC)) == []                         # the dataset does not have every result yet
    with world.begin() as c:
        c.execute(sa.text("INSERT INTO bout_results (bout_id, event_date, winner_side, method, finish_round, went_distance) VALUES ('y2', :d, 'b', 'Decision', NULL, 1)"), {"d": d})
    g = _groups(world, date(2026, 10, 26), datetime(2026, 10, 26, 14, 17, tzinfo=UTC))
    assert [x["kind"] for x in g] == ["game_results"] and g[0]["keys"] == [f"game_results:{d}:UFC 333: Old"]
    text = "\n".join(g[0]["messages"])
    assert "Game results: UFC 333: Old" in text and "🥇 Ann: +0 vs the model, 3 points (1 of 1 winners)" in text and "🥈 Bob\\_x: +0 vs the model, 0 points (0 of 1 winners)" in text
    assert "Fox Six" not in text and "Eli Five" not in text                                                          # scores only, not picks
    digest.mark_sent(world, g[0]["keys"])
    assert _groups(world, date(2026, 10, 26), datetime(2026, 10, 26, 14, 17, tzinfo=UTC)) == []


def test_a_card_nobody_played_gets_no_leaderboard(world):
    d = date(2026, 10, 24)
    _bouts(world, "UFC 333: Old", d, [("y1", "Eli Five", "Fox Six")])
    _players(world, ["Ann"])
    with world.begin() as c:
        c.execute(sa.text("INSERT INTO bout_results (bout_id, event_date, winner_side, method, finish_round, went_distance) VALUES ('y1', :d, 'a', 'KO/TKO', 2, 0)"), {"d": d})
    assert _groups(world, date(2026, 10, 26), datetime(2026, 10, 26, 14, 17, tzinfo=UTC)) == []


class _Fake:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json["content"], json["allowed_mentions"]))
        return type("R", (), {"status_code": 204})()


def test_a_full_run_posts_each_game_announcement_once_records_it_and_a_dry_run_posts_nothing(world, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    _bouts(world, "UFC Fight Night: Moicano vs. Nolan", date(2026, 10, 31), [("n1", "Renato Moicano", "Tom Nolan")])
    _players(world, ["Ann"])
    auto_odds.save_event_starts(world, [{"event_date": date(2026, 10, 31), "first_start": datetime(2026, 10, 31, 13, 45, tzinfo=UTC), "bouts": 1, "distinct_times": 2}], datetime(2026, 10, 28, tzinfo=UTC))
    now, today = datetime(2026, 10, 30, 14, 17, tzinfo=UTC), date(2026, 10, 30)
    rep = Report()
    digest.run_digest(world, today, rep, dry_run=True, session=(fake := _Fake()), sleep=lambda s: None, now=now)
    assert fake.sent == [] and "1 announcement(s) would be posted" in "\n".join(" ".join(l) for _, l in rep.sections)     # a dry run says what it would do and sends nothing
    assert digest.sent_keys(world) == set()
    digest.run_digest(world, today, Report(), dry_run=False, session=fake, pause=0, sleep=lambda s: None, now=now)
    assert len(fake.sent) == 1 and fake.sent[0][0] == HOOK and "closes picks in about 17 hours" in fake.sent[0][1] and fake.sent[0][2] == {"parse": []}      # nobody can be pinged
    assert digest.sent_keys(world) == {"game_close:2026-10-31"}
    rep2 = Report()
    digest.run_digest(world, today, rep2, dry_run=False, session=fake, pause=0, sleep=lambda s: None, now=now)         # the next run
    assert len(fake.sent) == 1 and "Nothing new to announce" in "\n".join(" ".join(l) for _, l in rep2.sections)
