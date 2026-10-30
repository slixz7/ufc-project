"""
Betting-feed name aliases (upcoming/odds_aliases.csv). The feed spells "Ko Seok-hyeon" as "Seok Hyun Ko", which the fuzzy matcher scores at 0.64 (it needs 0.88),
so his bout got no line. An alias fixes exactly that, and nothing else: it must not loosen the matching, must not help a line whose other fighter or date
does not fit, and a mistake in the file must never stop the daily job.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_names  # noqa: E402
import auto_odds  # noqa: E402
import upcoming  # noqa: E402
from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
CARD = date(2026, 11, 7)


def bouts(*rows):
    return pd.DataFrame([{"bout_id": f"b{i}", "event_date": d, "a": a, "b": b, "locked": 0} for i, (a, b, d) in enumerate(rows)])


CARD_BOUTS = bouts(("Ko Seok-hyeon", "Wellington Turman", CARD), ("Alpha One", "Bravo Two", CARD))


# ---------------------------------------------------------------------------------------------- the situation from the real report
def test_without_an_alias_the_real_case_is_unmatched_and_looks_like_a_changed_card():
    assert auto_names.name_sim("Seok Hyun Ko", "Ko Seok-hyeon") < 0.88                                              # why it failed: 0.64
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 8), CARD_BOUTS, {}) is None
    assert auto_odds.classify_unmatched("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 8), CARD_BOUTS, {}) == "card_changed"      # the CHECK THE CARD warning


def test_with_the_alias_the_line_attaches_to_his_bout_with_the_right_fighter_first():
    aliases = {"seok hyun ko": "Ko Seok-hyeon"}
    hit = auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 8), CARD_BOUTS, aliases)       # the feed's date is a day later in UTC
    assert hit is not None and hit[0].a == "Ko Seok-hyeon" and hit[1] is True                                       # the feed's home team is our fighter A
    swapped = auto_odds.match_line_to_bout("Wellington Turman", "Seok Hyun Ko", date(2026, 11, 7), CARD_BOUTS, aliases)
    assert swapped is not None and swapped[0].a == "Ko Seok-hyeon" and swapped[1] is False                          # the feed's home team is our fighter B
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 7), CARD_BOUTS, aliases)[0].bout_id == "b0"


def test_the_shipped_alias_file_contains_that_case_and_loads_cleanly():
    aliases = auto_names.load_odds_aliases()
    assert aliases.get("seok hyun ko") == "Ko Seok-hyeon"
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 8), CARD_BOUTS) is not None            # with no table passed, it reads the real file
    for feed_key, card in aliases.items():
        assert card == card.strip() and feed_key == upcoming.norm(feed_key) and feed_key != upcoming.norm(card)                   # no pointless alias: the two spellings differ


def test_the_second_real_case_the_feed_drops_a_second_surname(tmp_path=None):
    nov7 = bouts(("Jonny Parsons", "José Henrique Souza", CARD), ("Alpha One", "Bravo Two", CARD))
    assert auto_names.name_sim("José Henrique", "José Henrique Souza") < 0.88                                       # why it failed: 0.81
    assert auto_odds.match_line_to_bout("Jonny Parsons", "José Henrique", CARD, nov7, {}) is None
    assert auto_odds.classify_unmatched("Jonny Parsons", "José Henrique", CARD, nov7, {}) == "card_changed"         # the CHECK THE CARD warning of 2026-10-30
    hit = auto_odds.match_line_to_bout("Jonny Parsons", "José Henrique", CARD, nov7)                                 # with the shipped alias file
    assert hit is not None and hit[0].a == "Jonny Parsons" and hit[0].b == "José Henrique Souza" and hit[1] is True
    swapped = auto_odds.match_line_to_bout("José Henrique", "Jonny Parsons", CARD, nov7)
    assert swapped is not None and swapped[0].bout_id == "b0" and swapped[1] is False
    assert auto_odds.match_line_to_bout("Jose Henrique", "Jonny Parsons", CARD, nov7) is not None                  # the feed may or may not keep the accent
    assert auto_odds.match_line_to_bout("José Henrique", "Someone Else", CARD, nov7) is None                        # and an alias never rescues a different opponent


def test_accented_names_survive_the_file_and_a_windows_editors_start_of_file_mark(tmp_path):
    text = "feed_name,card_name\nJosé Henrique,José Henrique Souza\n"
    plain, with_bom = tmp_path / "plain.csv", tmp_path / "bom.csv"
    plain.write_bytes(text.encode("utf-8"))
    with_bom.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))                                                    # what Notepad's "UTF-8 with BOM" writes
    for f in (plain, with_bom):
        assert auto_names.load_odds_aliases(f) == {"jose henrique": "José Henrique Souza"}, f.name


def test_a_file_saved_in_the_wrong_encoding_says_so_instead_of_failing_obscurely(tmp_path):
    f = tmp_path / "old.csv"
    f.write_bytes("feed_name,card_name\nJosé Henrique,José Henrique Souza\n".encode("cp1252"))                       # what an old-style command writes on Windows
    with pytest.raises(ValueError, match="must be saved as UTF-8"):
        auto_names.load_odds_aliases(f)


def test_the_shipped_file_is_plain_utf8_with_both_known_cases():
    raw = (ROOT / "upcoming" / "odds_aliases.csv").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and raw.decode("utf-8")                                              # no mark, valid UTF-8
    aliases = auto_names.load_odds_aliases()
    assert aliases["seok hyun ko"] == "Ko Seok-hyeon" and aliases["jose henrique"] == "José Henrique Souza"


# ---------------------------------------------------------------------------------------------- what an alias must NOT do
def test_an_alias_does_not_help_a_line_whose_other_fighter_or_date_does_not_fit():
    aliases = {"seok hyun ko": "Ko Seok-hyeon"}
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Somebody New", date(2026, 11, 8), CARD_BOUTS, aliases) is None            # a different opponent: a real card change
    assert auto_odds.classify_unmatched("Seok Hyun Ko", "Somebody New", date(2026, 11, 8), CARD_BOUTS, aliases) == "card_changed"  # and it is still reported as one
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 10), CARD_BOUTS, aliases) is None      # more than a day from the card
    assert auto_odds.match_line_to_bout("Seok Hyun Ko", "Wellington Turman", date(2026, 11, 8), bouts(("Alpha One", "Bravo Two", CARD)), aliases) is None   # not on the card at all


def test_an_alias_changes_nothing_for_other_names_and_the_matcher_is_not_loosened():
    aliases = {"seok hyun ko": "Ko Seok-hyeon"}
    assert auto_odds.match_line_to_bout("Alpha One", "Bravo Two", CARD, CARD_BOUTS, aliases)[0].bout_id == "b1"                    # everything else as before
    assert auto_odds.match_line_to_bout("Alpha One", "Bravo Two", CARD, CARD_BOUTS, {})[0].bout_id == "b1"
    assert auto_odds.match_line_to_bout("Alfred One", "Bravo Two", CARD, CARD_BOUTS, aliases) is None                             # a different first name stays unmatched
    lines = [("Alpha One", "Bravo Two"), ("Alpha Won", "Bravo Too"), ("Alfred One", "Bravo Two"), ("Wellington Turman", "Ko Seok-hyeon"), ("Wellington Turman", "Someone"),
             ("Cam Rowston", "Bravo Two"), ("Seok Hyun Kim", "Wellington Turman"), ("Hyun Ko", "Wellington Turman")]
    for home, away in lines:                                                                                      # for every line that is not the aliased name, adding the alias changes nothing
        with_alias = auto_odds.match_line_to_bout(home, away, CARD, CARD_BOUTS, aliases)
        without = auto_odds.match_line_to_bout(home, away, CARD, CARD_BOUTS, {})
        assert (with_alias is None) == (without is None) and (with_alias is None or (with_alias[0].bout_id, with_alias[1]) == (without[0].bout_id, without[1])), (home, away)
    assert auto_names.canonical_feed_name("Alpha One", aliases) == "Alpha One"
    for pair in (("Seok Hyun Ko", "Ko Seok-hyeon"), ("Ryan Bader", "Dean Barry")):                                               # the threshold itself is untouched
        assert auto_names.name_sim(*pair) < 0.88


def test_spelling_variations_of_the_feed_name_still_find_the_alias():
    aliases = auto_names.load_odds_aliases()
    for spelled in ("seok hyun ko", "SEOK HYUN KO", "Seok-Hyun Ko", "  Seok  Hyun Ko ", "Seök Hyun Ko"):
        assert auto_names.canonical_feed_name(spelled, aliases) == "Ko Seok-hyeon", spelled


# ---------------------------------------------------------------------------------------------- the file's rules
def test_the_file_format_notes_blank_rows_and_missing_file(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("# a note\nfeed_name,card_name\n# another note\nSeok Hyun Ko, Ko Seok-hyeon \n\nCam Rowston,Cameron Rowston\nSeok Hyun Ko,Ko Seok-hyeon\n", encoding="utf-8")
    assert auto_names.load_odds_aliases(f) == {"seok hyun ko": "Ko Seok-hyeon", "cam rowston": "Cameron Rowston"}                  # notes and blanks skipped, spaces trimmed, a repeat of the same line is fine
    assert auto_names.load_odds_aliases(tmp_path / "missing.csv") == {}


@pytest.mark.parametrize("text, message", [
    ("name,other\nA,B\n", "the first line must be exactly feed_name,card_name"),
    ("feed_name,card_name\nOnly One,\n", "both feed_name and card_name are needed"),
    ("feed_name,card_name\n,Only Card\n", "both feed_name and card_name are needed"),
    ("feed_name,card_name\nSeok Hyun Ko,Ko Seok-hyeon\nSeok-Hyun Ko,Someone Else\n", "is given two different card names"),
])
def test_a_wrong_file_says_exactly_what_is_wrong(tmp_path, text, message):
    f = tmp_path / "a.csv"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        auto_names.load_odds_aliases(f)


# ---------------------------------------------------------------------------------------------- the whole step, on a real Postgres
class _Resp:
    def __init__(self, events):
        self._e, self.status_code, self.headers, self.text = events, 200, {"x-requests-remaining": "77"}, "[]"

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
    prices = lambda ph, pa: {"key": "h2h", "outcomes": [{"name": home, "price": ph}, {"name": away, "price": pa}]}
    return {"home_team": home, "away_team": away, "commence_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": [{"key": "a", "markets": [prices(-200, 170)]}, {"key": "b", "markets": [prices(-210, 175)]}]}


@pytest.fixture
def db():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    name = f"al_{uuid.uuid4().hex[:8]}"
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
    late = datetime(card.year, card.month, card.day, tzinfo=timezone.utc) + timedelta(hours=3)                    # 03:00 UTC: the next calendar day in UTC, as in the real feed
    return today, late


def _text(rep):
    return "\n".join(" ".join(lines) for _, lines in rep.sections)


@pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
def test_the_odds_step_attaches_the_line_through_the_shipped_alias_with_the_right_orientation(db, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    today, late = _setup(db, 3)
    rep = Report()
    auto_odds.step_odds(db, today, rep, dry_run=False, save_raw=None, session=_Feed([_line("Seok Hyun Ko", "Wellington Turman", late)]))
    row = pd.read_sql(sa.text("SELECT * FROM upcoming_odds"), db)
    assert len(row) == 1 and row.bout_id[0] == "ko" and row.market_prob_a[0] > 0.6                                    # the feed's favourite is his: fighter A is the favourite
    text = _text(rep)
    assert "CHECK THE CARD" not in text and rep.attention == []
    assert "feed spellings read through upcoming/odds_aliases.csv: 'Seok Hyun Ko' as 'Ko Seok-hyeon'" in text      # and the report says an alias was used
    with db.begin() as c:
        c.exec_driver_sql("DELETE FROM upcoming_odds")
    auto_odds.step_odds(db, today, Report(), dry_run=False, save_raw=None, session=_Feed([_line("Wellington Turman", "Seok Hyun Ko", late)]))
    assert pd.read_sql(sa.text("SELECT market_prob_a FROM upcoming_odds"), db).market_prob_a[0] < 0.4                # the feed's favourite is Turman: fighter A is the underdog


@pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
def test_without_the_alias_the_step_warns_and_escalates_when_the_card_is_close(db, monkeypatch, tmp_path):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setattr(auto_names, "ODDS_ALIASES_PATH", tmp_path / "none.csv")                                       # no alias file: the behaviour before this change
    today, late = _setup(db, 3)
    rep = Report()
    auto_odds.step_odds(db, today, rep, dry_run=False, save_raw=None, session=_Feed([_line("Seok Hyun Ko", "Wellington Turman", late)]))
    assert pd.read_sql(sa.text("SELECT COUNT(*) AS n FROM upcoming_odds"), db).n[0] == 0
    assert "CHECK THE CARD" in _text(rep) and any("replacement opponent" in a for a in rep.attention)                  # within five days: it appears under "Needs your attention"


@pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
def test_a_mistake_in_the_alias_file_is_reported_and_never_stops_the_job(db, monkeypatch, tmp_path):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    bad = tmp_path / "odds_aliases.csv"
    bad.write_text("feed,card\nSeok Hyun Ko,Ko Seok-hyeon\n", encoding="utf-8")
    monkeypatch.setattr(auto_names, "ODDS_ALIASES_PATH", bad)
    today, late = _setup(db, 3)
    rep = Report()
    auto_odds.step_odds(db, today, rep, dry_run=False, save_raw=None, session=_Feed([_line("Alpha One", "Bravo Two", late), _line("Seok Hyun Ko", "Wellington Turman", late)]))
    assert any("odds_aliases.csv: the first line must be exactly feed_name,card_name" in a and "ignored until it is fixed" in a for a in rep.attention)      # said, in plain words
    assert any(t.startswith("Betting lines") for t, _ in rep.sections) and "2 fights listed by the API" in _text(rep)   # and the step still ran to the end
    assert "0 matched" in _text(rep) and "CHECK THE CARD" in _text(rep)                                                # (the aliased line is simply unmatched, as before the alias existed)
