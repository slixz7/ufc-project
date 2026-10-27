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


def test_kaggle_credentials_new_token_or_old_pair(monkeypatch):
    for k in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert au.kaggle_configured() is False
    monkeypatch.setenv("KAGGLE_API_TOKEN", "  ")
    assert au.kaggle_configured() is False                              # a blank secret does not count
    monkeypatch.setenv("KAGGLE_API_TOKEN", "abc")
    assert au.kaggle_configured() is True
    monkeypatch.delenv("KAGGLE_API_TOKEN")
    monkeypatch.setenv("KAGGLE_USERNAME", "u")
    assert au.kaggle_configured() is False                              # the old pair needs both halves
    monkeypatch.setenv("KAGGLE_KEY", "k")
    assert au.kaggle_configured() is True


def test_a_kaggle_sign_in_that_calls_exit_fails_only_its_step(monkeypatch):
    import types
    mod = types.ModuleType("kaggle.api.kaggle_api_extended")

    class Api:
        def authenticate(self):
            raise SystemExit(1)                                         # what the real package does when it cannot sign in
    mod.KaggleApi = Api
    for n in ("kaggle", "kaggle.api"):
        monkeypatch.setitem(sys.modules, n, types.ModuleType(n))
    monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", mod)
    with pytest.raises(RuntimeError, match="did not accept the token"):
        au.kaggle_api()


# ---------------------------------------------------------------------------------------------- "Why this pick?"
import numpy as np  # noqa: E402
import model_v2  # noqa: E402


def test_every_model_input_belongs_to_exactly_one_reason_group():
    expected = {f"d_{k}" for k in model_v2.SH} | {"d_age", "d_logn", "d_streak", "d_reach", "d_height", "d_layoff", "d_elo", "d_sos", "d_age35", "d_age2", "d_elo_x_new"}
    grouped = [f for feats in model_v2.GROUPS.values() for f in feats]
    assert set(grouped) == expected, "the model's inputs changed: update GROUPS in model_v2.py"
    assert len(grouped) == len(set(grouped))


def _row(**over):
    base = {"age": 32.7, "streak": 10, "last3_win_rate": 1.0, "days_since_last": 217.0, "n_prev": 10, "prev_win_rate": 1.0, "elo": 1700.0, "sos": 1589.0,
            "slpm": 3.91, "sapm": 2.8, "td_per15": 4.78, "td_def": 0.61, "sub_att_per15": 0.1, "ko_win_rate": 0.0, "reach": 183.0, "height": 170.0,
            "sig_acc": 0.48, "kd_per15": 0.1, "ctrl_share": 0.36, "grappling_share": 0.3, "td_acc": 0.4, "sub_win_rate": 0.0}
    base.update(over)
    r = {}
    for k, v in base.items():
        r[f"{k}_a"] = v
        r[f"{k}_b"] = v
    r["age_a"], r["streak_a"], r["elo_a"] = 38.1, 2, 1714.0
    r["elo_b"] = 1700.0
    return pd.Series(r)


def test_reasons_add_up_to_the_pick_and_are_ordered_by_size():
    feats = [f for fs in model_v2.GROUPS.values() for f in fs]
    c = pd.Series({f: 0.0 for f in feats})
    c["d_age"], c["d_streak"], c["d_slpm"], c["d_td_per15"], c["d_sos"], c["d_height"] = -0.46, -0.31, 0.30, -0.27, 0.22, 0.02
    d = model_v2.build_reasons(_row(), c, "Alex Volkanovski", "Movsar Evloev")
    assert [f["key"] for f in d["factors"]] == ["age", "results", "striking", "grappling", "competition"]      # biggest push first; the size factor is too small to list
    assert abs(sum(f["c"] for f in d["factors"]) + d["rest"] - d["total"]) < 1e-3                             # nothing is lost
    assert abs(d["p_a"] - 1 / (1 + np.exp(-c.sum()))) < 1e-3
    assert d["factors"][0]["toward"] == "B" and d["factors"][2]["toward"] == "A"
    assert "Volkanovski 38.1" in d["factors"][0]["detail"] and "Evloev 32.7" in d["factors"][0]["detail"]


def test_reasons_cope_with_a_debutant_who_has_no_data():
    r = _row(age=np.nan, slpm=np.nan, sapm=np.nan, td_per15=np.nan, td_def=np.nan, last3_win_rate=np.nan, prev_win_rate=np.nan, n_prev=0, reach=np.nan, height=np.nan)
    c = pd.Series({f: 0.1 for fs in model_v2.GROUPS.values() for f in fs})
    d = model_v2.build_reasons(r, c, "Asaf Chopurov", "Colby Thicknesse")
    text = " ".join(f["detail"] for f in d["factors"])
    assert "–" in text and "no UFC fights yet" in text


def test_a_bar_that_opposes_every_number_beside_it_is_flagged_as_a_net_effect():
    c = pd.Series({f: 0.0 for fs in model_v2.GROUPS.values() for f in fs})
    c["d_elo"], c["d_sos"] = -0.5, -0.2                                      # the model's net effect favours B on "level of competition"
    r = _row(elo=1500.0, sos=1500.0)
    r["elo_a"], r["elo_b"], r["sos_a"], r["sos_b"] = 1700.0, 1500.0, 1650.0, 1500.0   # but A is ahead on BOTH numbers shown
    d = model_v2.build_reasons(r, c, "A One", "B Two")
    comp = next(f for f in d["factors"] if f["key"] == "competition")
    assert comp["toward"] == "B" and comp["net"] is True
    c["d_elo"], c["d_sos"] = 0.5, 0.2                                        # now the bar and the numbers agree
    assert not next(f for f in model_v2.build_reasons(r, c, "A One", "B Two")["factors"] if f["key"] == "competition")["net"]


def test_reasons_carry_a_schema_version_so_old_ones_can_be_rebuilt():
    c = pd.Series({f: 0.1 for fs in model_v2.GROUPS.values() for f in fs})
    assert model_v2.build_reasons(_row(), c, "A One", "B Two")["schema"] == model_v2.REASONS_SCHEMA


def test_the_blend_reasons_are_exact():
    d = model_v3.v3_reasons("A One", "B Two", 0.59, 0.81, 0.83, 0.41, 0.99)
    assert abs(d["market_share"] - 0.99 / 1.40) < 0.001
    z = d["model_c"] + d["market_c"]
    assert abs(1 / (1 + np.exp(-z)) - 0.83) < 0.01                                                            # 0.41 x logit(59%) + 0.99 x logit(81%) is about 83%


def test_last_names_handle_suffixes():
    assert model_v2.last_name("Alexander Volkanovski") == "Volkanovski"
    assert model_v2.last_name("Raul Rosas Jr.") == "Rosas Jr."


# ---------------------------------------------------------------------------------------------- odds history (daily snapshots)
def test_every_bookmakers_prices_are_captured_not_just_the_median():
    books = au.event_books(EVENT)
    assert [b["book"] for b in books] == ["a", "b", "c"]                          # the spreads-only book is ignored
    assert books[0]["home"] == -150 and books[0]["away"] == 130
    assert abs(books[0]["p_home"] - model_v3.devig(-150, 130)) < 1e-9
    assert books[1]["home"] == -145 and books[1]["away"] == 125                   # the API listed this book's outcomes in the other order


def test_snapshot_prices_are_flipped_into_card_order():
    books = au.event_books(EVENT)
    same = au.orient_books(books, home_is_a=True)
    flipped = au.orient_books(books, home_is_a=False)
    assert same[0]["a"] == -150 and same[0]["b"] == 130
    assert flipped[0]["a"] == 130 and flipped[0]["b"] == -150                     # fighter A is the API's away fighter
    assert abs(same[0]["p_a"] + flipped[0]["p_a"] - 1.0) < 1e-3


def test_snapshots_are_rate_limited_so_a_repeated_run_adds_nothing():
    assert 1 <= au.SNAPSHOT_GAP_HOURS <= 12


def test_reasons_can_be_stored_as_json():
    """The past-fight backfill writes ~14,000 of these into a JSONB column; a stray numpy value would break it."""
    import json
    c = pd.Series({f: 0.1 for fs in model_v2.GROUPS.values() for f in fs}).astype("float32")
    d = model_v2.build_reasons(_row(), c, "A One", "B Two")
    assert json.loads(json.dumps(d))["schema"] == model_v2.REASONS_SCHEMA
    json.dumps(model_v3.v3_reasons("A One", "B Two", np.float32(0.59), np.float64(0.81), np.float32(0.83), 0.41, 0.99))


def test_a_line_with_the_family_name_first_and_a_one_letter_spelling_difference_still_matches():
    assert au.name_sim("Jeongyoung Lee", "Lee Jeong-yeong") >= 0.88
    assert au.name_sim("Ko Seok-hyeon", "Seokhyeon Ko") >= 0.88
    assert au.name_sim("Ryan Bader", "Dean Barry") < 0.88                      # same letters, different people: no shared name part, so no match
    saved = pd.DataFrame({"bout_id": ["b1"], "event_date": [date(2026, 10, 31)], "a": ["Julian Erosa"], "b": ["Lee Jeong-yeong"]})
    bout, home_is_a = au.match_line_to_bout("Julian Erosa", "Jeongyoung Lee", date(2026, 11, 1), saved)
    assert bout.bout_id == "b1" and home_is_a is True
    # and it is therefore not reported as a card problem
    assert au.match_line_to_bout("Julian Erosa", "Jeongyoung Lee", date(2026, 11, 1), saved) is not None


# ---------------------------------------------------------------------------------------------- scoring the probabilities
def _scored(rows):
    return pd.DataFrame(rows, columns=["fighter_a", "actual_winner", "p_v2_a", "market_prob_a", "p_a_wins", "tier"])


def test_log_loss_and_brier_match_hand_calculations():
    d = _scored([("A", "A", 0.8, 0.7, 0.9, "full"), ("A", "B", 0.3, 0.4, 0.2, "full")])
    r = model_v3.probability_scores(d)
    assert abs(r["scores"]["v2"]["log_loss"] - (-(np.log(0.8) + np.log(0.7)) / 2)) < 1e-9
    assert abs(r["scores"]["v2"]["brier"] - ((0.2 ** 2) + (0.3 ** 2)) / 2) < 1e-9
    assert abs(r["scores"]["coin"]["log_loss"] - np.log(2)) < 1e-9 and r["scores"]["coin"]["brier"] == 0.25


def test_the_verdict_says_clearly_better_only_when_the_interval_excludes_zero():
    rng = np.random.default_rng(1)
    n = 2000
    p = rng.uniform(0.3, 0.7, n)
    y = (rng.uniform(size=n) < p)
    d = pd.DataFrame({"fighter_a": "A", "actual_winner": np.where(y, "A", "B"), "p_v2_a": 0.5, "market_prob_a": np.clip(p + rng.normal(0, 0.08, n), 0.05, 0.95),
                      "p_a_wins": p, "tier": "full"})                              # the "blend" is the true probability: it must beat a noisy line
    r = model_v3.probability_scores(d)
    assert r["blend_vs_line"]["verdict"] == "clearly better" and r["blend_vs_line"]["hi"] < 0
    same = d.assign(p_a_wins=d.market_prob_a)                                      # identical predictions: no difference to find
    assert model_v3.probability_scores(same)["blend_vs_line"]["mean"] == 0


# ---------------------------------------------------------------------------------------------- the public pick log
import subprocess  # noqa: E402
import pick_log  # noqa: E402


def _picks(rows):
    return pd.DataFrame(rows, columns=["pick_type", "bout_id", "event_date", "event_name", "bout_order", "fighter_a", "fighter_b", "predicted_winner", "p_a_wins", "locked_at_utc"])


def test_the_log_only_ever_appends_and_never_adds_a_pick_twice(tmp_path):
    path = tmp_path / "picks_log" / "locked_picks.csv"
    a = _picks([("model-only", "b1", "2026-10-31", "UFC X", 1, "A One", "B Two", "A One", 0.61234, "2026-10-22T10:00:00Z")])
    assert pick_log.append(pick_log.new_rows(a, pick_log.read_log(path)), path) == 1
    first = path.read_text(encoding="utf-8")
    b = pd.concat([a, _picks([("market-informed", "b1", "2026-10-31", "UFC X", 1, "A One", "B Two", "B Two", 0.4, "2026-10-29T10:00:00Z")])], ignore_index=True)
    assert pick_log.append(pick_log.new_rows(b, pick_log.read_log(path)), path) == 1       # only the new kind of pick for the same bout
    assert path.read_text(encoding="utf-8").startswith(first)                              # what was there is untouched
    assert pick_log.append(pick_log.new_rows(b, pick_log.read_log(path)), path) == 0        # nothing new: nothing written
    assert path.read_text(encoding="utf-8").splitlines()[0].split(",") == pick_log.COLUMNS
    assert "0.6123" in path.read_text(encoding="utf-8") and "odds" not in path.read_text(encoding="utf-8").lower()


def test_a_logged_pick_that_changes_in_the_database_is_caught():
    db = _picks([("model-only", "b1", "2026-10-31", "UFC X", 1, "A One", "B Two", "A One", 0.6123, "t")])
    logged = db.assign(logged_at_utc="x")
    assert pick_log.changed_rows(db, logged) == []
    assert len(pick_log.changed_rows(db.assign(predicted_winner="B Two"), logged)) == 1        # the pick flipped
    assert len(pick_log.changed_rows(db.assign(p_a_wins=0.70), logged)) == 1                   # the probability moved
    assert pick_log.changed_rows(db.assign(p_a_wins=0.61234), logged.assign(p_a_wins="0.6123")) == []   # rounding to 4 decimals is not a change


def test_the_verifier_reads_when_each_pick_first_appeared_in_git_history(tmp_path):
    import os
    def git(*a, when=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        if when:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when
        subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True, capture_output=True)
    git("init", "-q")
    log = tmp_path / "picks_log" / "locked_picks.csv"
    early = _picks([("model-only", "b1", "2026-10-31", "UFC X", 1, "A One", "B Two", "A One", 0.6, "2026-10-22T10:00:00Z")])
    late = _picks([("model-only", "b2", "2026-10-24", "UFC Y", 1, "C Three", "D Four", "C Three", 0.55, "2026-10-20T10:00:00Z")])
    pick_log.append(early.assign(), log, now=pd.Timestamp("2026-10-22T11:00:00Z").to_pydatetime())
    git("add", "."); git("commit", "-qm", "1", when="2026-10-22T11:00:00+00:00")                 # committed a week BEFORE its event
    pick_log.append(late, log, now=pd.Timestamp("2026-10-25T09:00:00Z").to_pydatetime())
    git("add", "."); git("commit", "-qm", "2", when="2026-10-25T09:00:00+00:00")                 # committed AFTER its event
    seen = pick_log.first_seen(tmp_path)
    assert seen[("model-only", "b1")][1].startswith("2026-10-22") and seen[("model-only", "b2")][1].startswith("2026-10-25")
    report = pick_log.verify_report(tmp_path)
    assert "1 of 2 logged picks were first committed before their event date" in report
    assert pick_log.verify_report(tmp_path / "nowhere").startswith("No picks")


# ---------------------------------------------------------------------------------------------- the Discord digest
import digest  # noqa: E402


def test_long_announcements_are_split_without_cutting_a_line():
    lines = [f"• Fighter {i} {60 + i % 20}% over Opponent {i}" for i in range(300)]
    parts = digest.chunk(lines)
    assert len(parts) > 1 and all(len(p) <= digest.LIMIT for p in parts)
    assert [ln for p in parts for ln in p.splitlines()] == lines


def test_the_locked_picks_message_notes_where_the_market_changed_the_pick():
    picks = pd.DataFrame({"bout_id": ["b1", "b2"], "bout_order": [1, 2], "fighter_a": ["A One", "C Three"], "fighter_b": ["B Two", "D Four"],
                          "predicted_winner": ["B Two", "C Three"], "confidence": [0.55, 0.7]})
    model_only = picks.assign(predicted_winner=["A One", "C Three"])
    text = "\n".join(digest.locked_messages("2026-10-31", "UFC X", "market-informed", picks, model_only))
    lines = text.splitlines()
    assert "• B Two 55% over A One (model-only said A One)" in lines            # the market flipped this one
    assert "• C Three 70% over D Four" in lines                                  # this one agrees with model-only: no note
    assert "UFC X" in text and "Not betting advice" in text and "odds" not in text.lower().replace("not betting advice", "")


def test_the_results_message_counts_both_kinds_of_pick():
    v2 = pd.DataFrame({"bout_id": ["b1", "b2"], "bout_order": [1, 2], "fighter_a": ["A One", "C Three"], "fighter_b": ["B Two", "D Four"],
                       "actual_winner": ["B Two", "C Three"], "correct": [False, True]})
    v3 = pd.DataFrame({"bout_id": ["b1"], "correct": [True]})
    text = "\n".join(digest.results_messages("2026-10-24", "UFC Y", v2, v3))
    assert "Model-only picks: 1 of 2 right (50%)" in text and "Market-informed picks: 1 of 1 right (100%)" in text
    assert "❌ ✅  B Two def. A One" in text and "✅ ➖  C Three def. D Four" in text and "2 fights cannot say" in text


def test_the_webhook_address_is_checked_and_never_leaks_through_an_error():
    assert digest.valid_webhook("https://discord.com/api/webhooks/123456789/abcDEF")
    assert not digest.valid_webhook("https://evil.example.com/api/webhooks/123456789/abc")
    assert not digest.valid_webhook("")

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("Max retries exceeded with url: /api/webhooks/123456789/SECRET")
    assert digest.post("https://discord.com/api/webhooks/123456789/SECRET", "hi", Boom()) == "RuntimeError"    # only the error's name comes back


def test_a_rate_limited_post_waits_as_asked_and_tries_once_more():
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body or {}
        def json(self):
            return self._b

    class S:
        def __init__(self):
            self.n = 0
        def post(self, *a, **k):
            self.n += 1
            return R(429, {"retry_after": 2}) if self.n == 1 else R(204)
    waited = []
    s = S()
    assert digest.post("https://discord.com/api/webhooks/123456789/abc", "hi", s, sleep=waited.append) == 204 and waited == [2.0] and s.n == 2


# ---------------------------------------------------------------------------------------------- README numbers from the database
import readme_numbers  # noqa: E402


def _real_ddl(tables):
    """CREATE TABLE statements built from the project's real SQL files, so a renamed column breaks a test."""
    import re
    root = Path(__file__).resolve().parent.parent
    out = {}
    for f in ("sql/model_v2.sql", "sql/model_v3.sql"):
        s = (root / f).read_text(encoding="utf-8")
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", s, re.S):
            if m.group(1) in tables:
                cols = []
                for ln in m.group(2).splitlines():
                    ln = ln.split("--")[0].strip().rstrip(",")
                    if ln and not ln.upper().startswith(("PRIMARY", "UNIQUE", "CONSTRAINT")):
                        cols.append(f"{ln.split()[0]} {ln.split()[1]}")
                out[m.group(1)] = f"CREATE TABLE {m.group(1)} ({', '.join(cols)})"
    return out


def _empty_engine(tmp_path):
    import sqlalchemy as sa
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    ddl = _real_ddl({"fight_picks_v2", "fight_picks_v3", "upcoming_picks_v2", "upcoming_picks_v3"})
    assert len(ddl) == 4
    with eng.begin() as c:
        for s in ddl.values():
            c.exec_driver_sql(s)
        c.exec_driver_sql("CREATE TABLE fight_reasons (fight_url TEXT, version TEXT, reasons TEXT)")
    return eng


def test_the_generator_only_reads_columns_that_exist_in_the_real_schema(tmp_path):
    frames = readme_numbers.load_frames(_empty_engine(tmp_path))             # raises if any column name is wrong
    assert frames["v3"].empty and frames["market_share"] is None
    nums = readme_numbers.compute(frames, n_tests=1)
    assert nums.skipped and "acc_v3" not in nums.values                      # nothing to compute from empty tables, and it says so


def _synthetic(tmp_path, claims_hold=True, seed=3, n=6000):
    """A database with known answers: the blend is better than the line only where both fighters have history."""
    import sqlalchemy as sa
    eng = _empty_engine(tmp_path)
    rng = np.random.default_rng(seed)
    p_true = rng.uniform(0.25, 0.75, n)
    y = rng.uniform(size=n) < p_true
    tier = rng.choice(["full", "limited", "debut"], n, p=[0.5, 0.3, 0.2])
    p_v2 = np.clip(p_true + rng.normal(0, 1, n) * np.select([tier == "full", tier == "limited"], [0.08, 0.14], 0.30), 0.05, 0.95)   # the model is weakest on debutants
    noisy = np.clip(p_true + rng.normal(0, 0.12, n), 0.05, 0.95)
    mkt = np.where(tier == "full", noisy, p_true)                             # the line is only poor where both fighters have history
    blend = p_true if claims_hold else mkt                                    # a calibrated blend: better than the line only in the "full" group
    dates = pd.Timestamp("2016-01-01") + pd.to_timedelta(rng.integers(0, 3600, n), unit="D")
    pick = lambda p: np.where(p >= 0.5, 1, 0)
    d3 = pd.DataFrame({"fight_url": [f"f{i}" for i in range(n)], "event_date": dates.strftime("%Y-%m-%d"), "fighter_a": "A", "actual_winner": np.where(y, "A", "B"),
                       "p_v2_a": p_v2, "market_prob_a": mkt, "p_a_wins": blend, "confidence": np.maximum(blend, 1 - blend), "tier": tier,
                       "correct": (pick(blend) == y).astype(int), "v2_correct": (pick(p_v2) == y).astype(int), "market_correct": (pick(mkt) == y).astype(int)})
    d3.to_sql("fight_picks_v3", eng, if_exists="append", index=False)
    rows = []                                                                 # yearly model-only accuracy: 2012 has too few fights to count
    for year, count, acc in [(2012, 150, 0.80), (2013, 300, 0.55), (2014, 300, 0.60), (2015, 300, 0.66), (2016, 300, 0.62)]:
        k = round(acc * count)
        rows += [(f"v2-{year}-{i}", f"{year}-06-01", "model", int(i < k)) for i in range(count)]
    rows += [("early-1", "2001-01-01", "early_rule", 1)]
    pd.DataFrame(rows, columns=["fight_url", "event_date", "method", "correct"]).to_sql("fight_picks_v2", eng, if_exists="append", index=False)
    conf2 = [0.62, 0.56, 0.52, 0.60, 0.58, 0.61, 0.59, 0.61, 0.55, 0.54, 0.81, 0.55, 0.65, 0.74]
    conf3 = [0.55, 0.55, 0.50, 0.56, 0.60, 0.61, 0.83, 0.50, 0.54, 0.81, 0.56, 0.55, 0.52, 0.82]
    mk = lambda conf, k, name, status="locked": pd.DataFrame({"bout_id": [f"{name}{i}" for i in range(len(conf))], "event_name": name, "event_date": "2026-10-24",
                                                           "status": status, "confidence": conf, "correct": [int(i < k) for i in range(len(conf))],
                                                           "graded_at": "2026-10-25 12:00:00"})
    pd.concat([mk(conf2, 8, "UFC 333: Volkanovski vs. Evloev"), mk([0.9], 0, "UFC 332: Other"), mk([0.7], 1, "UFC 333: Volkanovski vs. Evloev", "cancelled")]).to_sql("upcoming_picks_v2", eng, if_exists="append", index=False)
    mk(conf3, 8, "UFC 333: Volkanovski vs. Evloev").to_sql("upcoming_picks_v3", eng, if_exists="append", index=False)
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO fight_reasons VALUES ('f1', 'v3', :j)"), {"j": json.dumps({"market_share": 0.71})})
    return eng, d3, conf2, conf3


import json  # noqa: E402


def test_every_number_comes_from_the_database_with_the_stated_formatting(tmp_path):
    eng, d3, conf2, conf3 = _synthetic(tmp_path)
    v = readme_numbers.compute(readme_numbers.load_frames(eng), n_tests=48).values
    assert v["n_fights"] == f"{len(d3):,}" and v["first_year"] == "2016" and v["last_year"] in ("2025", "2026")
    assert v["acc_v3"] == f"{d3.correct.mean() * 100:.1f}%" and v["acc_v2"] == f"{d3.v2_correct.mean() * 100:.1f}%" and v["acc_line"] == f"{d3.market_correct.mean() * 100:.1f}%"
    assert (v["year_lo"], v["year_hi"], v["year_min_fights"]) == ("55%", "66%", "200")        # 2012 (150 fights, 80%) and the early-rule fight do not count
    assert (v["market_weight"], v["model_weight"]) == ("71%", "29%")
    assert (v["u333_n"], v["u333_v2"], v["u333_v3"]) == ("14", "8", "8")                      # the other event and the cancelled bout are ignored
    assert v["u333_expected_v2"] == f"{sum(conf2):.1f}" and v["u333_expected_v3"] == f"{sum(conf3):.1f}"
    assert v["n_tests"] == "48" and v["n_live_needed"].replace(",", "").isdigit()
    dis = (d3.p_v2_a >= .5) != (d3.p_a_wins >= .5)
    assert v["dis_share"] == f"{dis.mean() * 100:.1f}%" and v["dis_blend"] == f"{d3[dis].correct.mean() * 100:.1f}%"


def test_the_tables_are_well_formed_markdown(tmp_path):
    eng, d3, *_ = _synthetic(tmp_path)
    blocks = readme_numbers.compute(readme_numbers.load_frames(eng), n_tests=1).blocks
    assert set(blocks) == {"results_accuracy", "results_tiers", "calibration", "scoring_table", "scoring_compare"}
    for name, md in blocks.items():
        rows = md.splitlines()
        assert all(r.startswith("|") and r.endswith("|") for r in rows), name
        assert len({r.count("|") for r in rows}) == 1, f"{name}: every row needs the same number of columns"
        assert set(rows[1].replace("|", "")) == {"-"}, name                                    # the separator row
    assert sum(1 for r in blocks["results_tiers"].splitlines()[2:]) == 3 and "Coin flip | 50% |" in blocks["results_accuracy"]
    assert blocks["scoring_table"].count("**") == 4                                            # only the best predictor is bold (two cells)


def test_claims_that_hold_pass_and_claims_that_stop_holding_are_flagged(tmp_path):
    (tmp_path / "a").mkdir()
    good, *_ = _synthetic(tmp_path / "a")
    assert readme_numbers.compute(readme_numbers.load_frames(good), n_tests=1).broken_claims == []
    (tmp_path / "b").mkdir()
    bad, *_ = _synthetic(tmp_path / "b", claims_hold=False)                                    # the blend is now no better than the line
    broken = " ".join(readme_numbers.compute(readme_numbers.load_frames(bad), n_tests=1).broken_claims)
    assert "includes zero" in broken and "edge comes from fights where both fighters have a UFC history" in broken


def test_apply_rewrites_only_the_markers_keeps_the_prose_and_is_idempotent():
    text = ("Intro with a **<!--n:acc_v3-->old<!--/n-->** number and an unknown <!--n:nope-->keep<!--/n-->.\n\n"
            "<!--auto:results_accuracy-->\n\n| stale |\n\n<!--/auto:results_accuracy-->\n\nProse stays exactly as written.\n")
    nums = readme_numbers.Numbers(values={"acc_v3": "65.4%"}, blocks={"results_accuracy": "| Predictor | Right |\n|---|---|\n| x | 1% |"})
    new, changes, unfilled = readme_numbers.apply(text, nums)
    assert "**<!--n:acc_v3-->65.4%<!--/n-->**" in new and "| x | 1% |" in new and "| stale |" not in new
    assert "<!--n:nope-->keep<!--/n-->" in new and unfilled == ["nope"]                       # a marker it cannot fill is left alone, never blanked
    assert new.endswith("Prose stays exactly as written.\n") and new.startswith("Intro with a ")
    again, changes2, _ = readme_numbers.apply(new, nums)
    assert again == new and changes2 == []                                                     # running it twice changes nothing
    crlf, _, _ = readme_numbers.apply(text.replace("\n", "\r\n"), nums)
    assert "\r\n" in crlf and "\n" not in crlf.replace("\r\n", "")                             # Windows line endings survive


def test_refresh_fixes_a_stale_readme_and_a_second_run_finds_nothing_to_do(tmp_path):
    eng, d3, *_ = _synthetic(tmp_path)
    path = tmp_path / "README.md"
    path.write_text("Best: <!--n:acc_v3-->12.3%<!--/n--> of <!--n:n_fights-->1<!--/n--> fights, <!--n:n_tests-->7<!--/n--> tests.\n", encoding="utf-8")
    checked = readme_numbers.refresh(eng, path, write=False, n_tests=48)
    assert len(checked["changes"]) == 3 and "12.3%" in path.read_text(encoding="utf-8")        # check mode reports but does not write
    fixed = readme_numbers.refresh(eng, path, write=True, n_tests=48)
    assert len(fixed["changes"]) == 3 and f"{d3.correct.mean() * 100:.1f}%" in path.read_text(encoding="utf-8") and "<!--n:n_tests-->48<!--/n-->" in path.read_text(encoding="utf-8")
    assert readme_numbers.refresh(eng, path, write=True, n_tests=48)["changes"] == []


def test_every_marker_in_the_real_readme_is_known_and_closed():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    names = {m.group(1) for m in readme_numbers.TOKEN_RE.finditer(text)} | {m.group(2) for m in readme_numbers.BLOCK_RE.finditer(text)}
    assert names and names <= readme_numbers.known_names(), names - readme_numbers.known_names()
    assert text.count("<!--n:") == text.count("<!--/n-->") == len(readme_numbers.TOKEN_RE.findall(text))
    assert text.count("<!--auto:") == text.count("<!--/auto:") == len(readme_numbers.BLOCK_RE.findall(text))


def test_the_readme_step_runs_on_mondays_and_otherwise_stays_out_of_the_way(tmp_path):
    eng, *_ = _synthetic(tmp_path)

    class Rep:
        def __init__(self):
            self.lines, self.needs = [], []
        def add(self, title, *ls):
            self.lines += list(ls)
        def need(self, t):
            self.needs.append(t)
    rep = Rep()
    readme_numbers.step_readme(eng, date(2026, 10, 27), rep, dry_run=True, write=False)       # a Tuesday
    assert rep.lines == ["Only refreshed on Mondays."]


_MARKED = "Best <!--n:acc_v3-->0%<!--/n--> in <!--n:n_tests-->0<!--/n--> tests. Prose.\n"


class _Rep:
    def __init__(self):
        self.lines, self.needs = [], []
    def add(self, title, *ls):
        self.lines += list(ls)
    def need(self, t):
        self.needs.append(t)


def test_the_monday_step_writes_only_when_it_may_and_only_when_something_changed(tmp_path, monkeypatch):
    eng, d3, *_ = _synthetic(tmp_path)
    path = tmp_path / "README.md"
    monkeypatch.setattr(readme_numbers, "README", path)
    monkeypatch.setattr(readme_numbers, "count_tests", lambda *a, **k: 48)
    monday = date(2026, 10, 26)
    path.write_text(_MARKED, encoding="utf-8")
    rep = _Rep(); readme_numbers.step_readme(eng, monday, rep, dry_run=True, write=True)             # a dry run reports but never writes
    assert path.read_text(encoding="utf-8") == _MARKED and "would change 2" in rep.lines[0]
    rep = _Rep(); readme_numbers.step_readme(eng, monday, rep, dry_run=False, write=False)          # your own computer: tells you how, writes nothing
    assert path.read_text(encoding="utf-8") == _MARKED and any("only the GitHub job writes README.md" in x for x in rep.lines)
    rep = _Rep(); readme_numbers.step_readme(eng, monday, rep, dry_run=False, write=True)           # the GitHub job
    assert f"{d3.correct.mean() * 100:.1f}%" in path.read_text(encoding="utf-8") and "refreshed 2" in rep.lines[0] and "the workflow commits it" in rep.lines[0]
    rep = _Rep(); readme_numbers.step_readme(eng, monday, rep, dry_run=False, write=True)
    assert rep.lines == ["README numbers already match the database."]                              # nothing changed, so nothing to commit


def test_a_claim_that_stops_holding_asks_for_attention_in_the_daily_report(tmp_path, monkeypatch):
    eng, *_ = _synthetic(tmp_path, claims_hold=False)
    monkeypatch.setattr(readme_numbers, "README", tmp_path / "README.md")
    (tmp_path / "README.md").write_text(_MARKED, encoding="utf-8")
    monkeypatch.setattr(readme_numbers, "count_tests", lambda *a, **k: 48)
    rep = _Rep(); readme_numbers.step_readme(eng, date(2026, 10, 26), rep, dry_run=True, write=True)
    assert rep.needs and rep.needs[0].startswith("README: ")


def test_the_command_line_exit_codes(tmp_path, monkeypatch, capsys):
    import model_v2
    eng, *_ = _synthetic(tmp_path)
    path = tmp_path / "README.md"
    path.write_text(_MARKED, encoding="utf-8")
    monkeypatch.setattr(model_v2, "make_engine", lambda: eng)
    monkeypatch.setattr(readme_numbers, "README", path)
    monkeypatch.setattr(readme_numbers, "count_tests", lambda *a, **k: 48)
    assert readme_numbers.main(["check"]) == 1 and "out of date" in capsys.readouterr().out         # stale: exit 1, and nothing was written
    assert path.read_text(encoding="utf-8") == _MARKED
    assert readme_numbers.main(["update"]) == 0 and path.read_text(encoding="utf-8") != _MARKED
    assert readme_numbers.main(["check"]) == 0 and "match the database" in capsys.readouterr().out   # now in step
    assert readme_numbers.main(["nonsense"]) == 2
