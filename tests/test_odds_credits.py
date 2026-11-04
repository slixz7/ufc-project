"""
The early warning about the Odds API's monthly credits. Running out is already loud (the request fails and the run ends in failure), but only after the lines have stopped;
this warns while there is still time.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_odds  # noqa: E402
from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_pg = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")


@pytest.mark.parametrize("header, expected", [("77", 77), (77, 77), ("0", 0), ("30.0", 30), (" 12 ", 12), ("475", 475), ("-1", -1),
                                              (None, None), ("", None), ("unknown", None), ("nan", None), ("inf", None), ("-inf", None), ("12 credits", None)])
def test_the_remaining_credits_header_is_read_as_a_whole_number_or_not_at_all(header, expected):
    assert auto_odds.credits_left(header) == expected


def test_the_warning_level_is_thirty_credits():
    assert auto_odds.LOW_CREDITS == 30                                                                                    # at 1 to 3 credits a day, about ten days of notice


@pytest.fixture
def db():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    name = f"oc_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=name))
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE upcoming_fights (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, fighter_a_name TEXT, fighter_b_name TEXT, status TEXT)")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v3 (bout_id TEXT)")
        c.exec_driver_sql("CREATE TABLE upcoming_odds (bout_id TEXT PRIMARY KEY, odds_a REAL, odds_b REAL, market_prob_a REAL, source TEXT, entered_at TIMESTAMPTZ DEFAULT now())")
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _setup(db, days_ahead):
    today = datetime.now(timezone.utc).date()
    card = today + timedelta(days=days_ahead)
    with db.begin() as c:
        c.execute(sa.text("INSERT INTO upcoming_fights VALUES ('ko', 'Test Card', :d, 'Ko Seok-hyeon', 'Wellington Turman', 'scheduled')"), {"d": card})
    return today, datetime(card.year, card.month, card.day, tzinfo=timezone.utc) + timedelta(hours=3)


def _line(home, away, when):
    prices = lambda ph, pa: {"key": "h2h", "outcomes": [{"name": home, "price": ph}, {"name": away, "price": pa}]}
    return {"home_team": home, "away_team": away, "commence_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": [{"key": "a", "markets": [prices(-200, 170)]}, {"key": "b", "markets": [prices(-210, 175)]}]}


class _Resp:
    def __init__(self, events, remaining):
        self._e, self.status_code, self.text = events, 200, "[]"
        self.headers = {} if remaining is None else {"x-requests-remaining": remaining}

    def raise_for_status(self):
        pass

    def json(self):
        return self._e


class _Feed:
    def __init__(self, events, remaining):
        self.events, self.remaining = events, remaining

    def get(self, *a, **k):
        return _Resp(self.events, self.remaining)


def _run(db, monkeypatch, remaining, dry_run=False):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    today, late = _setup(db, 3)
    rep = Report()
    auto_odds.step_odds(db, today, rep, dry_run=dry_run, save_raw=None, session=_Feed([_line("Seok Hyun Ko", "Wellington Turman", late)], remaining))
    return rep, "\n".join(" ".join(lines) for _, lines in rep.sections)


@needs_pg
@pytest.mark.parametrize("remaining", ["29", "12", "1", "0", "-3"])
def test_below_the_warning_level_the_run_lists_it_under_needs_your_attention(db, monkeypatch, remaining):
    rep, text = _run(db, monkeypatch, remaining)
    low = [a for a in rep.attention if "Odds API credits are left" in a]
    assert len(low) == 1 and f"only {remaining} Odds API credits are left this month (the warning level is 30)" in low[0]
    assert "new bouts stay model-only" in low[0] and "wait for the monthly reset or add credits" in low[0]
    assert f"API credits left this month: {remaining}" in text                                                          # the report line is unchanged


@needs_pg
@pytest.mark.parametrize("remaining", ["30", "31", "77", "475", "500"])
def test_at_the_warning_level_and_above_nothing_is_raised(db, monkeypatch, remaining):
    rep, text = _run(db, monkeypatch, remaining)
    assert not [a for a in rep.attention if "credits" in a] and f"API credits left this month: {remaining}" in text


@needs_pg
@pytest.mark.parametrize("remaining", [None, "unknown", "", "12 credits", "nan"])
def test_a_missing_or_unreadable_figure_is_reported_as_unknown_and_never_raises_a_false_alarm(db, monkeypatch, remaining):
    rep, text = _run(db, monkeypatch, remaining)
    assert not [a for a in rep.attention if "credits" in a]
    assert "API credits left this month: unknown" in text                                                                 # never a blank, never the raw text of the header


@needs_pg
def test_a_readable_figure_is_shown_as_a_whole_number(db, monkeypatch):
    assert "API credits left this month: 475" in _run(db, monkeypatch, "475.0")[1]


@needs_pg
def test_a_dry_run_warns_too_because_it_reads_the_same_header(db, monkeypatch):
    rep, _ = _run(db, monkeypatch, "5", dry_run=True)
    assert any("only 5 Odds API credits" in a for a in rep.attention)
