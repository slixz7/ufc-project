"""Offline tests for pipeline/auto_update.py (no network, no database).

The Wikipedia pages below are small hand-built examples of the kind of table Wikipedia uses; they are NOT copies of live
pages. If a scheduled run reports that it could not read a page, run
    python pipeline/auto_update.py cards --dry-run --save-raw raw
and look at the saved page.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import auto_update as au  # noqa: E402
import model_v3  # noqa: E402

SCHEDULE = """
<div class="mw-heading mw-heading2"><h2 id="Scheduled_events">Scheduled events</h2></div>
<table class="sortable wikitable"><tbody>
<tr><th>Event</th><th>Date</th><th>Venue</th><th>Location</th><th>Ref.</th></tr>
<tr><td><a href="/wiki/UFC_333" title="UFC 333">UFC 333: Volkanovski vs. Evloev</a></td><td>Oct 24, 2026</td><td>Etihad Arena</td><td>Abu Dhabi</td><td>[1]</td></tr>
<tr><td><a href="/wiki/UFC_Fight_Night:_Test_vs._Sample">UFC Fight Night: Test vs. Sample</a></td><td>November 7, 2026</td><td>Arena</td><td>Las Vegas</td><td>[2]</td></tr>
<tr><td>UFC 335</td><td>Nov 21, 2026</td><td>TBD</td><td>TBD</td><td></td></tr>
<tr><td><a href="/wiki/UFC_400">UFC 400</a></td><td>Mar 14, 2027</td><td>TBD</td><td>TBD</td><td></td></tr>
<tr><td><a href="/wiki/UFC_320">UFC 320</a></td><td>Oct 4, 2026</td><td>Past</td><td>Past</td><td></td></tr>
</tbody></table>
<h2 id="Past_events">Past events</h2>
<table><tr><th>Event</th><th>Date</th></tr><tr><td><a href="/wiki/UFC_1">UFC 1</a></td><td>Nov 12, 1993</td></tr></table>
"""

CARD = """
<table class="toccolours">
<tr><th colspan="8">Main card</th></tr>
<tr><th>Weight class</th><th></th><th></th><th></th><th>Method</th><th>Round</th><th>Time</th><th>Notes</th></tr>
<tr><td>Featherweight</td><td><a href="/wiki/A">Alexander Volkanovski</a> (c)</td><td>vs.</td><td><a href="/wiki/B">Movsar Evloev</a></td><td>–</td><td>–</td><td>–</td><td>For the UFC Featherweight Championship<sup>[1]</sup></td></tr>
<tr><td>Bantamweight</td><td>Petr Yan (c)</td><td>vs.</td><td>Merab Dvalishvili</td><td></td><td></td><td></td><td>For the UFC Bantamweight Championship</td></tr>
<tr><td>Flyweight</td><td>Lone&#8217;er Kavanagh</td><td>vs.</td><td>Ramazonbek Temirov[a]</td><td></td><td></td><td></td><td></td></tr>
<tr><th colspan="8">Preliminary card</th></tr>
<tr><td>Light Heavyweight</td><td>Dominick Reyes</td><td>vs.</td><td>Azamat Murzakanov</td><td></td><td></td><td></td><td></td></tr>
<tr><td>Middleweight</td><td>TBA</td><td>vs.</td><td>Someone Else</td><td></td><td></td><td></td><td></td></tr>
<tr><td>Lightweight</td><td>Grant Dawson</td><td>vs.</td><td>Nurullo Aliev</td><td></td><td></td><td></td><td></td></tr>
<tr><td>Lightweight</td><td>Nurullo Aliev</td><td>vs.</td><td>Grant Dawson</td><td></td><td></td><td></td><td>duplicate row</td></tr>
</table>
<table><tr><th>Bonus</th><th>Fighter</th></tr><tr><td>Fight of the Night</td><td>Someone</td></tr></table>
"""


def test_schedule_keeps_future_events_inside_the_horizon():
    ev = au.parse_scheduled_events(SCHEDULE, date(2026, 10, 21), horizon=75)
    assert [e["name"] for e in ev] == ["UFC 333: Volkanovski vs. Evloev", "UFC Fight Night: Test vs. Sample", "UFC 335"]
    assert ev[0]["date"] == date(2026, 10, 24)
    assert ev[0]["page"] == "UFC 333"
    assert ev[1]["page"] == "UFC Fight Night: Test vs. Sample"
    assert ev[2]["page"] is None                       # no page yet
    assert all(e["date"] > date(2026, 10, 21) for e in ev)


def test_schedule_falls_back_to_the_table_header_when_the_heading_is_missing():
    html = SCHEDULE.replace('id="Scheduled_events"', 'id="Something_else"')
    assert len(au.parse_scheduled_events(html, date(2026, 10, 21), 75)) == 3


def test_card_parsing():
    b = au.parse_card(CARD)
    assert [(x["fighter_a"], x["fighter_b"]) for x in b] == [
        ("Alexander Volkanovski", "Movsar Evloev"), ("Petr Yan", "Merab Dvalishvili"), ("Lone\u2019er Kavanagh", "Ramazonbek Temirov"),
        ("Dominick Reyes", "Azamat Murzakanov"), ("Grant Dawson", "Nurullo Aliev")]
    assert [x["title_fight"] for x in b] == [True, True, False, False, False]
    assert b[0]["weight_class"] == "Featherweight"
    assert all("(c)" not in x["fighter_a"] + x["fighter_b"] for x in b)        # champion marker removed
    assert not any("TBA" in x["fighter_a"] for x in b)                           # unannounced opponents skipped
    assert len(b) == 5                                                           # the duplicate row and the bonus table add nothing


def test_card_with_no_table_returns_nothing():
    assert au.parse_card("<p>The card has not been announced.</p>") == []


IDX = pd.DataFrame({"fighter_url": ["u1", "u2", "u3", "u4", "u5", "u6"],
                    "fighter_name": ["Alexander Volkanovski", "Movsar Evloev", "Cameron Rowston", "Mike Davis", "Mike Davis", "Ilia Topuria"],
                    "n_fights": [18, 10, 3, 5, 2, 6], "last_fight": ["2026-01-31"] * 6, "weight_class": ["Featherweight"] * 6})
IDX["norm"] = IDX.fighter_name.map(au.upcoming.norm)


def test_exact_names_resolve():
    r = au.resolve_name("Movsar Evloev", IDX, {})
    assert (r["status"], r["url"]) == ("ok", "u2")
    assert au.resolve_name("ILIA  TOPURIA", IDX, {})["url"] == "u6"


def test_near_misses_are_held_never_guessed():
    for name in ["Alex Volkanovski", "Cam Rowston", "Cameron Rowston Jr"]:
        r = au.resolve_name(name, IDX, {})
        assert r["status"] == "held" and r["url"] is None, name


def test_a_one_letter_typo_is_matched_automatically_and_reported():
    r = au.resolve_name("Alexander Volkanovsky", IDX, {})
    assert r["status"] == "auto" and r["url"] == "u1" and "similar" in r["note"]


def test_two_fighters_with_the_same_name_are_held():
    assert au.resolve_name("Mike Davis", IDX, {})["status"] == "held"


def test_an_unrelated_name_is_a_debut():
    r = au.resolve_name("Zed Newcomer", IDX, {})
    assert r["status"] == "new" and r["url"] == "new:zed-newcomer"


def test_aliases_win():
    aliases = {"alex volkanovski": "Alexander Volkanovski", "asaf chopurov": "NEW"}
    assert au.resolve_name("Alex Volkanovski", IDX, aliases)["url"] == "u1"
    assert au.resolve_name("Asaf Chopurov", IDX, aliases)["status"] == "new"


EVENT = {"id": "x", "commence_time": "2026-10-25T02:00:00Z", "home_team": "Alexander Volkanovski", "away_team": "Movsar Evloev",
         "bookmakers": [
             {"key": "a", "markets": [{"key": "h2h", "outcomes": [{"name": "Alexander Volkanovski", "price": -150}, {"name": "Movsar Evloev", "price": 130}]}]},
             {"key": "b", "markets": [{"key": "h2h", "outcomes": [{"name": "Movsar Evloev", "price": 125}, {"name": "Alexander Volkanovski", "price": -145}]}]},
             {"key": "c", "markets": [{"key": "h2h", "outcomes": [{"name": "Alexander Volkanovski", "price": -160}, {"name": "Movsar Evloev", "price": 135}]}]},
             {"key": "d", "markets": [{"key": "spreads", "outcomes": []}]}]}


def test_consensus_is_a_margin_free_median_across_books():
    home, away, p, n = au.event_consensus(EVENT)
    assert (home, away, n) == ("Alexander Volkanovski", "Movsar Evloev", 3)
    assert abs(p - model_v3.devig(-150, 130)) < 0.01 or abs(p - model_v3.devig(-145, 125)) < 0.01
    assert 0.55 < p < 0.62


def test_consensus_ignores_events_without_both_prices():
    assert au.event_consensus({"home_team": "A", "away_team": "B", "bookmakers": []}) is None
    assert au.event_consensus({"home_team": "A", "away_team": "B", "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -110}]}]}]}) is None


BOUTS = pd.DataFrame({"bout_id": ["b1", "b2"], "event_date": [date(2026, 10, 24), date(2026, 11, 7)],
                      "a": ["Movsar Evloev", "Zed Newcomer"], "b": ["Alex Volkanovski", "Someone Else"]})


def test_a_line_finds_its_bout_in_either_order_and_with_a_one_day_date_difference():
    bout, home_is_a = au.match_line_to_bout("Alexander Volkanovski", "Movsar Evloev", date(2026, 10, 25), BOUTS)
    assert bout.bout_id == "b1"
    assert home_is_a is False                           # the API's home fighter is the card's fighter B
    bout, home_is_a = au.match_line_to_bout("Movsar Evloev", "Alexander Volkanovski", date(2026, 10, 24), BOUTS)
    assert bout.bout_id == "b1" and home_is_a is True


def test_a_line_for_an_unknown_or_wrong_date_bout_is_not_matched():
    assert au.match_line_to_bout("Nobody One", "Nobody Two", date(2026, 10, 24), BOUTS) is None
    assert au.match_line_to_bout("Alexander Volkanovski", "Movsar Evloev", date(2026, 12, 1), BOUTS) is None


def test_report_lists_what_needs_attention():
    r = au.Report()
    r.add("Cards", "one line")
    assert "Nothing." in r.render(date(2026, 10, 21), False)
    r.need("a name needs review")
    text = r.render(date(2026, 10, 21), True)
    assert "a name needs review" in text and "DRY RUN" in text


def test_lock_and_safety_defaults_are_sane():
    assert au.LOCK_DAYS >= 1 and au.MAX_SHRINK >= 1 and au.HORIZON_DAYS >= 30


def test_betting_site_spellings_match_the_card_spellings():
    assert au.name_sim("Alexander Volkanovski", "Alex Volkanovski") >= 0.95
    assert au.name_sim("Abus Magomedov", "Abusupiyan Magomedov") >= 0.95
    assert au.name_sim("Cameron Rowston", "Cam Rowston") >= 0.95
    assert au.name_sim("Ramazonbek Temirov", "Ramazan Temirov") >= 0.88
    assert au.name_sim("Merab Dvalishvili", "Petr Yan") < 0.5
    assert au.name_sim("Mike Davis", "Mark Davis") < 0.88                         # a different first name is not a spelling variant


def test_names_you_already_resolved_are_remembered_but_an_exact_match_wins():
    remembered = {"daniel gustavo santos": "u9", "zed newcomer": "new:zed-newcomer", "movsar evloev": "stale-url"}
    assert au.resolve_name("Daniel Gustavo Santos", IDX, {}, remembered)["url"] == "u9"
    assert au.resolve_name("Zed Newcomer", IDX, {}, remembered)["status"] == "new"
    assert au.resolve_name("Movsar Evloev", IDX, {}, remembered)["url"] == "u2"        # the dataset's exact match beats an old note


IDX2 = pd.DataFrame({"fighter_url": ["k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8"],
                     "fighter_name": ["Seokhyeon Ko", "Jeong Yeong Lee", "Jose Souza", "Jacare Souza", "Wes Schultz", "Bia Mesquita", "Jose Delano", "Old Timer"],
                     "n_fights": [5, 1, 6, 12, 8, 7, 9, 20], "last_fight": ["2026-03-01", "2026-05-01", "2025-06-01", "2016-01-01", "2026-01-01", "2026-02-01", "2026-04-01", "2015-01-01"],
                     "weight_class": ["Bantamweight"] * 8})
IDX2["norm"] = IDX2.fighter_name.map(au.upcoming.norm)
IDX2["lk"] = IDX2.norm.map(au.letters_key)
TODAY = date(2026, 10, 22)


def test_family_name_first_and_hyphenated_spellings_match_the_same_letters():
    for card in ["Ko Seok-hyeon", "Seok Hyeon Ko", "KO SEOK HYEON"]:
        r = au.resolve_name(card, IDX2, {}, None, TODAY)
        assert (r["status"], r["url"]) == ("auto", "k1"), card
    r = au.resolve_name("Lee Jeong-yeong", IDX2, {}, None, TODAY)
    assert (r["status"], r["url"]) == ("auto", "k2")


def test_middle_names_are_ignored_only_for_one_recently_active_fighter():
    assert au.resolve_name("José Henrique Souza", IDX2, {}, None, TODAY)["url"] == "k3"            # not Jacare Souza
    assert au.resolve_name("José Mauro Delano", IDX2, {}, None, TODAY)["url"] == "k7"
    old = au.resolve_name("Old Middle Timer", IDX2.assign(norm=IDX2.norm.replace({"old timer": "old timer"})), {}, None, TODAY)
    assert old["status"] in ("held", "new")                                                         # a fighter inactive for years is never matched silently


def test_nicknames_and_shortened_first_names_still_need_one_confirmation():
    for card in ["Beatriz Mesquita", "Wesley Schultz"]:
        assert au.resolve_name(card, IDX2, {}, None, TODAY)["status"] == "held", card
    aliases = {"beatriz mesquita": "Bia Mesquita", "wesley schultz": "Wes Schultz"}
    assert au.resolve_name("Beatriz Mesquita", IDX2, aliases, None, TODAY)["url"] == "k6"
    assert au.resolve_name("Wesley Schultz", IDX2, aliases, None, TODAY)["url"] == "k5"


def test_a_real_debut_is_still_a_debut():
    for card in ["Joseph Kropschot", "Lucia Szabov\u00e1"]:
        assert au.resolve_name(card, IDX2, {}, None, TODAY)["status"] == "new", card


def test_lines_from_other_promotions_and_rumours_are_ignored_not_flagged():
    saved = pd.DataFrame({"bout_id": ["b1", "b2"], "event_date": [date(2026, 10, 24)] * 2,
                          "a": ["Alexander Volkanovski", "Petr Yan"], "b": ["Movsar Evloev", "Merab Dvalishvili"]})
    d = date(2026, 10, 23)
    assert au.classify_unmatched("Bella Mir", "Chase Hooper", d, saved) == "ignored"                          # another promotion
    assert au.classify_unmatched("Khamzat Chimaev", "Robbie Lawler", d, saved) == "ignored"                   # known UFC names, but not on this card
    assert au.classify_unmatched("Justin Gaethje", "Ilia Topuria", date(2026, 12, 31), saved) == "ignored"    # rumoured, far away


def test_a_line_listing_one_of_our_fighters_against_someone_else_is_flagged():
    saved = pd.DataFrame({"bout_id": ["b1"], "event_date": [date(2026, 10, 24)], "a": ["Alexander Volkanovski"], "b": ["Movsar Evloev"]})
    assert au.classify_unmatched("Alex Volkanovski", "Replacement Guy", date(2026, 10, 25), saved) == "card_changed"
    assert au.classify_unmatched("Alex Volkanovski", "Replacement Guy", date(2026, 11, 20), saved) == "ignored"   # a different night
