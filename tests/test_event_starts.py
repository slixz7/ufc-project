"""When each card's first fight starts, taken from the odds feed: what closes the friends' game picks. Plain logic and a throwaway database; no network."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import auto_odds  # noqa: E402

NOW = datetime(2026, 1, 28, 14, 17, tzinfo=timezone.utc)
UTC = timezone.utc


def snap(d, hh, mm=0, day=None, tz=UTC):
    dd = pd.Timestamp(d)
    return {"d": dd.date(), "ct": datetime(dd.year, dd.month, day or dd.day, hh, mm, tzinfo=tz)}


def test_the_earliest_start_on_a_card_is_its_first_fight_and_a_card_with_one_shared_time_gets_a_safety_margin():
    ufc325 = [snap("2026-01-31", 22, day=30), snap("2026-01-31", 0, day=31), snap("2026-01-31", 3, day=31)]       # three different times: the earliest is the first fight
    rows = auto_odds.event_start_rows(ufc325)
    assert rows == [{"event_date": date(2026, 1, 31), "first_start": datetime(2026, 1, 30, 22, 0, tzinfo=UTC), "bouts": 3, "distinct_times": 3, "card_size": None, "margin": False}]
    shared = [snap("2026-01-31", 3, day=31) for _ in range(6)]                                                      # six bouts, ONE time: probably the main card; the early prelims are earlier
    row = auto_odds.event_start_rows(shared)[0]
    assert row["distinct_times"] == 1 and row["first_start"] == datetime(2026, 1, 30, 22, 0, tzinfo=UTC)           # 03:00 minus the 5-hour margin


def test_a_card_the_feed_lists_only_in_part_gets_the_safety_margin_but_a_fully_listed_card_does_not():
    from game_core import closes_at
    d = date(2026, 1, 31)
    main_card_only = [snap("2026-01-31", 3, day=31), snap("2026-01-31", 4, day=31), snap("2026-01-31", 5, day=31), snap("2026-01-31", 5, 30, day=31), snap("2026-01-31", 6, day=31)]
    partial = auto_odds.event_start_rows(main_card_only, {d: 12})[0]                                          # 5 of the card's 12 fights listed: the early prelims are not in the feed yet
    assert partial["margin"] and partial["card_size"] == 12 and partial["first_start"] == datetime(2026, 1, 30, 22, 0, tzinfo=UTC)
    assert closes_at(d, partial["first_start"]) == datetime(2026, 1, 30, 16, 0, tzinfo=UTC)                   # the Sydney card closes well before its first fight, even with the early prelims unlisted
    full = auto_odds.event_start_rows(main_card_only, {d: 5})[0]                                              # all 5 listed: the earliest IS the first fight
    assert not full["margin"] and full["first_start"] == datetime(2026, 1, 31, 3, 0, tzinfo=UTC)
    assert not auto_odds.event_start_rows(main_card_only)[0]["margin"]                                        # no card size known: judged on the times alone


def test_the_real_feed_numbers_give_sensible_close_times_for_the_cards_coming_up():
    """Replayed from the query on the real odds history: bouts listed and earliest start (UTC) per card; card sizes are our scheduled bouts."""
    from game_core import closes_at
    real = {  # event date: (bouts listed, scheduled bouts, earliest start, expected close)
        date(2026, 10, 24): (14, 14, datetime(2026, 10, 24, 14, 0, tzinfo=UTC), datetime(2026, 10, 24, 8, 0, tzinfo=UTC)),       # UFC 333, Abu Dhabi
        date(2026, 10, 31): (13, 13, datetime(2026, 10, 31, 13, 45, tzinfo=UTC), datetime(2026, 10, 31, 7, 45, tzinfo=UTC)),     # the card on Saturday
        date(2026, 11, 14): (5, 12, datetime(2026, 11, 14, 23, 0, tzinfo=UTC), datetime(2026, 11, 14, 12, 0, tzinfo=UTC)),       # only the top five fights listed so far: 5 hours more caution
        date(2026, 12, 12): (2, 12, datetime(2026, 12, 13, 3, 30, tzinfo=UTC), datetime(2026, 12, 12, 16, 30, tzinfo=UTC)),      # two fights listed, a US main card
    }
    for d, (listed, scheduled, earliest, expected) in real.items():
        snaps = [{"d": d, "ct": earliest + timedelta(minutes=15 * i)} for i in range(listed)]                  # different times, the earliest as reported
        row = auto_odds.event_start_rows(snaps, {d: scheduled})[0]
        assert closes_at(d, row["first_start"]) == expected, (d, row)
        assert closes_at(d, row["first_start"]) < earliest                                                    # always before the earliest listed fight, by hours
        assert closes_at(d, row["first_start"]) > closes_at(d)                                                # and later than the no-information fallback: friends get more time


def test_a_us_card_closes_the_morning_of_its_event_day_however_the_feed_reports_it():
    from game_core import closes_at
    d = date(2026, 10, 31)
    varied = auto_odds.event_start_rows([snap("2026-10-31", 20), snap("2026-10-31", 23), snap("2026-11-01", 2, day=1)])[0]      # prelims 20:00 UTC, main card 02:00 UTC next day
    shared = auto_odds.event_start_rows([snap("2026-11-01", 2, day=1) for _ in range(5)])[0]                                    # only the main-card time, shared by all
    assert closes_at(d, varied["first_start"]) == datetime(2026, 10, 31, 14, 0, tzinfo=UTC)                                     # six hours before the 20:00 UTC first fight
    assert closes_at(d, shared["first_start"]) == datetime(2026, 10, 31, 15, 0, tzinfo=UTC)                                     # cautious (02:00 minus 5 hours, minus 6), but before the real first fight
    for row in (varied, shared):
        assert closes_at(d) < closes_at(d, row["first_start"]) < datetime(2026, 10, 31, 20, 0, tzinfo=UTC)                      # more time than the fallback, still before the first fight


def test_nonsense_start_times_are_ignored_and_different_cards_are_kept_apart():
    glitch = [snap("2026-01-31", 12, day=20)]                                                                       # ten days before the event: a feed error
    assert auto_odds.event_start_rows(glitch) == []
    mixed = auto_odds.event_start_rows([snap("2026-01-31", 12, day=20), snap("2026-01-31", 22, day=30), snap("2026-02-07", 1, day=7), snap("2026-02-07", 23, day=6)])
    assert [(r["event_date"], r["bouts"]) for r in mixed] == [(date(2026, 1, 31), 1), (date(2026, 2, 7), 2)]        # the glitch dropped, the cards separate
    assert auto_odds.event_start_rows([]) == []
    offset = auto_odds.event_start_rows([{"d": "2026-01-31", "ct": datetime(2026, 1, 31, 9, 0, tzinfo=timezone(timedelta(hours=11)))}, snap("2026-01-31", 5, day=31)])[0]
    assert offset["first_start"] == datetime(2026, 1, 30, 22, 0, tzinfo=UTC)                                        # a time with a +11:00 offset is converted to UTC


def test_a_saved_start_time_follows_the_best_estimate_while_the_card_is_open_and_never_reopens_a_closed_card(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 's.db'}")
    row = lambda h, n=3, d=date(2026, 1, 31): {"event_date": d, "first_start": datetime(2026, 1, 30, h, 0, tzinfo=UTC), "bouts": n, "distinct_times": 2}
    read = lambda: pd.read_sql(sa.text("SELECT * FROM event_starts"), eng)
    start = lambda: auto_odds._utc(read().first_start[0])
    assert auto_odds.save_event_starts(eng, [row(22)], NOW) == 1 and start() == datetime(2026, 1, 30, 22, 0, tzinfo=UTC)
    assert auto_odds.save_event_starts(eng, [row(22)], NOW + timedelta(days=1)) == 1 and len(read()) == 1           # the same run again changes nothing
    assert auto_odds.save_event_starts(eng, [row(23, 2)], NOW + timedelta(days=1)) == 1                             # a later estimate while the card is open (the rest of the card got listed)
    assert start() == datetime(2026, 1, 30, 23, 0, tzinfo=UTC) and int(read().bouts[0]) == 2
    assert auto_odds.save_event_starts(eng, [row(21)], datetime(2026, 1, 30, 14, 17, tzinfo=UTC)) == 1              # an earlier one is followed too; the card closes at 15:00 UTC
    assert start() == datetime(2026, 1, 30, 21, 0, tzinfo=UTC)
    closed_at = datetime(2026, 1, 30, 15, 30, tzinfo=UTC)                                                           # picks have now closed
    assert auto_odds.save_event_starts(eng, [row(23)], closed_at) == 0 and start() == datetime(2026, 1, 30, 21, 0, tzinfo=UTC)   # a later start would reopen the card: refused
    assert auto_odds.save_event_starts(eng, [row(20)], closed_at) == 0 and start() == datetime(2026, 1, 30, 21, 0, tzinfo=UTC)   # (and the early fights dropping out of the feed changes nothing)
    other = row(22, d=date(2026, 2, 7))                                                                              # a card with no recorded start that is already closed by the fallback rule
    assert auto_odds.save_event_starts(eng, [other], datetime(2026, 2, 6, 18, 30, tzinfo=UTC)) == 0 and len(read()) == 1       # is not given a start that could reopen it
    assert auto_odds.save_event_starts(eng, [other], datetime(2026, 2, 6, 12, 0, tzinfo=UTC)) == 1 and len(read()) == 2        # but one still open is


# ---------------------------------------------------------------------------------------------- the real odds step, end to end, on a real Postgres
import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")


class _Resp:
    def __init__(self, events):
        self._e, self.status_code, self.headers = events, 200, {"x-requests-remaining": "77"}
        self.text = "[]"

    def raise_for_status(self):
        pass

    def json(self):
        return self._e


class _Feed:
    def __init__(self, events):
        self.events = events

    def get(self, *a, **k):
        return _Resp(self.events)


def _line(home, away, when):
    prices = lambda p_home, p_away: {"key": "h2h", "outcomes": [{"name": home, "price": p_home}, {"name": away, "price": p_away}]}
    return {"home_team": home, "away_team": away, "commence_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": [{"key": "a", "markets": [prices(-150, 130)]}, {"key": "b", "markets": [prices(-160, 135)]}]}


@pytest.fixture
def odds_db():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    db = f"odds_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {db}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=db))
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE upcoming_fights (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, fighter_a_name TEXT, fighter_b_name TEXT, status TEXT)")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v3 (bout_id TEXT)")
        c.exec_driver_sql("CREATE TABLE upcoming_odds (bout_id TEXT PRIMARY KEY, odds_a REAL, odds_b REAL, market_prob_a REAL, source TEXT, entered_at TIMESTAMPTZ DEFAULT now())")
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_the_odds_step_records_when_each_card_starts_and_says_so_when_a_close_card_has_no_start_time(odds_db, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    now = datetime.now(UTC)
    today = now.date()
    card = today + timedelta(days=5)                                                                                # a card whose first fight is the evening BEFORE its date, in UTC
    midnight = datetime(card.year, card.month, card.day, tzinfo=UTC)
    soon = today + timedelta(days=2)                                                                                # a card within three days that the feed does not list at all
    with odds_db.begin() as c:
        for i, (a, b, d) in enumerate([("Alpha One", "Bravo Two", card), ("Charlie Three", "Delta Four", card), ("Echo Five", "Foxtrot Six", soon)]):
            c.execute(sa.text("INSERT INTO upcoming_fights VALUES (:i, 'Test Card', :d, :a, :b, 'scheduled')"), {"i": f"b{i}", "d": d, "a": a, "b": b})
    feed = _Feed([_line("Alpha One", "Bravo Two", midnight - timedelta(hours=2)), _line("Charlie Three", "Delta Four", midnight + timedelta(hours=3))])
    rep = Report()
    auto_odds.step_odds(odds_db, today, rep, dry_run=False, save_raw=None, session=feed)
    row = pd.read_sql(sa.text("SELECT * FROM event_starts"), odds_db).iloc[0]
    assert row.event_date == card and int(row.bouts) == 2 and int(row.distinct_times) == 2
    assert auto_odds._utc(row.first_start) == midnight - timedelta(hours=2)                                        # the earliest of the two fights, on real Postgres
    text = "\n".join(" ".join(lines) for _, lines in rep.sections)
    assert "first-fight start times recorded for 1 card(s)" in text and f"{card} 22:00 UTC" in text
    assert f"no first-fight start time from the odds feed yet for the card(s) on {soon}" in text                      # the close card with no start time is flagged in the daily report
    auto_odds.step_odds(odds_db, today, Report(), dry_run=False, save_raw=None, session=feed)                       # the next day's run: nothing changes
    assert len(pd.read_sql(sa.text("SELECT * FROM event_starts"), odds_db)) == 1
    rep2 = Report()                                                                                                 # a dry run records nothing and says "would be"
    with odds_db.begin() as c:
        c.exec_driver_sql("DELETE FROM event_starts")
    auto_odds.step_odds(odds_db, today, rep2, dry_run=True, save_raw=None, session=feed)
    assert pd.read_sql(sa.text("SELECT COUNT(*) n FROM event_starts"), odds_db).n[0] == 0 and "would be recorded" in "\n".join(" ".join(l) for _, l in rep2.sections)
