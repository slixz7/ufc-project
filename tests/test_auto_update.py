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
