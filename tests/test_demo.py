"""
The demo: invented data that must look like real data to the real pipeline, a builder that can never harm a real database, and a launcher.
The first group needs nothing but pandas; the second builds whole demos on a real Postgres (TEST_PG_ADMIN_URL) and opens every page of the website on them.
"""
import inspect
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo", "app"):
    sys.path.insert(0, str(ROOT / sub))
import build_demo  # noqa: E402
import export_history  # noqa: E402
import run_demo  # noqa: E402
import synth  # noqa: E402

try:
    __import__("streamlit")
    HAVE_STREAMLIT = True
except ImportError:
    HAVE_STREAMLIT = False
needs_streamlit = pytest.mark.skipif(not HAVE_STREAMLIT, reason="Streamlit is not installed here")
ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 10, 29)                       # a Thursday


@pytest.fixture(scope="module")
def tiny():
    return synth.generate(TODAY, "tiny", 7)


@pytest.fixture(scope="module")
def small():
    return synth.generate(TODAY, "small", 7)


# ---------------------------------------------------------------------------------------------- the invented data
def test_the_generator_is_deterministic_and_every_date_is_measured_from_today(tiny):
    again = synth.generate(TODAY, "tiny", 7)
    pd.testing.assert_frame_equal(tiny.fights, again.fights)
    pd.testing.assert_frame_equal(tiny.fighter_fights, again.fighter_fights)
    assert not tiny.fights.equals(synth.generate(TODAY, "tiny", 8).fights)                                              # another seed, another world
    d = {k: c.date for k, c in tiny.cards.items()}
    assert list(d) == ["L1", "L2", "U1", "U2", "U3"] and d["L2"] - d["L1"] == timedelta(7) == d["U2"] - d["U1"]
    assert TODAY + timedelta(2) <= d["U1"] <= TODAY + timedelta(8) and d["U1"].weekday() == 5                            # the next card: a Saturday, a few days away
    assert d["L2"] < TODAY and pd.to_datetime(tiny.fights.event_date).max().date() < d["L1"]                            # the played cards are in the past; history ends before them
    shifted = synth.generate(TODAY + timedelta(days=30), "tiny", 7)
    assert shifted.cards["U1"].date > d["U1"] + timedelta(20)                                                           # a demo built a month later has a card a month later


def test_everything_in_the_demo_is_invented(small):
    names = {f.name for f in small.fighters}
    assert len(names) == len(small.fighters) > 300                                                                      # unique, plenty of them
    real_surnames = ("McGregor", "Jones", "Silva", "Adesanya", "Nunes", "Volkanovski", "Pereira", "Topuria", "Makhachev", "Poirier", "Holloway", "Usman", "Diaz", "Rousey", "Khabib", "Nurmagomedov", "Aldo")
    assert not [n for n in names if re.search(r"\b(" + "|".join(real_surnames) + r")\b", n, flags=re.I)]                  # whole words: the invented 'Taldo' is not the real 'Aldo'
    for url in list(small.fights.fight_url) + list(small.fighter_fights.fighter_url):
        assert url.startswith("https://demo.invalid/")                                                                  # a reserved domain: never a real site
    events = set(small.fights.event_name) | {c.csv.event_name.iloc[0] for c in small.cards.values()}
    assert all(e.startswith("Demo ") for e in events)


def test_the_tables_match_the_layouts_the_real_loader_fills(tiny):
    assert set(tiny.fights.columns) == {"fight_url"} | set(export_history.FIGHT_COLS)
    assert list(tiny.fighter_fights.columns) == list(export_history.FIGHTER_COLS)
    assert set(tiny.odds.columns) == {"fight_id", "event_date", "f1_id", "f2_id", "f1_name", "f2_name", "has_odds", "f1_open_prob_novig", "f1_close_prob_novig"}
    for c in tiny.cards.values():
        assert list(c.csv.columns) == ["event_name", "event_date", "bout_order", "weight_class", "title_fight", "fighter_a", "fighter_b"]
        assert list(c.odds.columns) == ["fighter_a", "fighter_b", "odds_a", "odds_b"] and len(c.odds) == len(c.csv)


def test_fights_are_internally_consistent(small):
    f, ff = small.fights, small.fighter_fights
    per = ff.groupby("fight_url")
    assert (per.size() == 2).all() and (per.won.sum() == 1).all() and (per.side.apply(lambda s: sorted(s)) == pd.Series([[1, 2]] * len(per), index=per.size().index)).all()
    assert set(f.num_rounds.unique()) <= {3.0, 5.0} and (f.finish_round <= f.num_rounds).all() and (f.finish_round >= 1).all()
    assert (f.groupby("event_name").num_rounds.apply(lambda s: int((s == 5).sum())) <= 1).all()                          # at most one five-round fight per card
    assert (ff.sig_land <= ff.sig_att).all() and (ff.td_land <= ff.td_att).all() and (ff[["sig_land", "td_att", "kd", "ctrl_sec", "sub_att"]] >= 0).all().all()
    one, two = ff[ff.side == 1].set_index("fight_url"), ff[ff.side == 2].set_index("fight_url")
    assert (one.sig_absorbed == two.sig_land.reindex(one.index)).all() and (two.opp_td_land == one.td_land.reindex(two.index)).all()           # each side's "absorbed" is the other's "landed"
    decided = f.result.str.startswith("Decision")
    assert (f[decided].finish_time == "5:00").all() and 0.40 < decided.mean() < 0.70                                    # how fights end looks like fighting
    assert 0.12 < f.result.isin(["KO/TKO", "TKO - Doctor's Stoppage"]).mean() < 0.35 and 0.08 < (f.result == "Submission").mean() < 0.30


def test_the_better_fighter_usually_wins_and_the_market_is_informative_but_not_a_crystal_ball(small):
    skill = {f.url: f.skill for f in small.fighters}
    ff = small.fighter_fights
    pair = ff.pivot(index="fight_url", columns="side", values=["fighter_url", "won"])
    higher = np.array([skill[a] > skill[b] for a, b in zip(pair[("fighter_url", 1)], pair[("fighter_url", 2)])])
    a_won = pair[("won", 1)].values == 1
    assert 0.58 < np.mean(higher == a_won) < 0.85                                                                       # skill matters, luck matters too
    o = small.odds.assign(a_won=small.odds.fight_id.map(pair[("won", 1)].rename(lambda u: u.rsplit("/", 1)[-1])))
    y = o.a_won.astype(int).values
    for col in ("f1_open_prob_novig", "f1_close_prob_novig"):
        p = o[col].values
        assert 0.58 < np.mean((p > .5) == (y == 1)) < 0.75 and ((p > 0.01) & (p < 0.99)).all()                         # a favourite is right most of the time, never always
    ll = lambda p: -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
    assert ll(o.f1_close_prob_novig.values) < ll(o.f1_open_prob_novig.values)                                          # the line sharpens by the close, as real ones do


def test_the_played_cards_and_the_upcoming_cards_are_set_up_like_the_real_timeline(small):
    played, coming = [small.cards["L1"], small.cards["L2"]], [small.cards[k] for k in ("U1", "U2", "U3")]
    assert all(c.results is not None and len(c.results[0]) == len(c.csv) for c in played) and all(c.results is None for c in coming)
    history = set(small.fights.fight_url)
    assert not any(u in history for c in played for u in c.results[0].fight_url)                                      # the played cards' results are NOT in the history the models first see
    booked = pd.concat([c.csv.assign(d=c.date) for c in coming])
    people = pd.concat([booked[["fighter_a", "d"]].rename(columns={"fighter_a": "n"}), booked[["fighter_b", "d"]].rename(columns={"fighter_b": "n"})])
    assert not people.duplicated("n").any()                                                                             # nobody is booked on two upcoming cards (they need rest)
    assert 1 <= int(small.fights.title_fight.sum()) and ((small.fights.title_fight) & (small.fights.num_rounds == 3)).any()   # includes tournament-style bouts flagged like title fights


# ---------------------------------------------------------------------------------------------- the builder's safeguards and the launcher
def test_the_builder_refuses_hosted_databases_whatever_they_contain():
    for host in ("ep-cool-123.us-east-2.aws.neon.tech", "db.abcd.supabase.co", "mydb.cluster-xyz.us-east-1.rds.amazonaws.com"):
        eng = sa.create_engine(f"postgresql+psycopg2://user:secret@{host}/neondb")
        with pytest.raises(build_demo.BuildError, match="hosted"):
            build_demo.assert_safe_to_build(eng)


def test_the_demo_uses_logins_of_its_own_so_it_can_never_change_a_real_one_or_another_demo():
    assert build_demo.login_names("postgres") == ("demo_site_postgres", "demo_game_postgres")
    for db in ("neondb", "site_reader", "game_app", "My-DB!", "", None, "x" * 80):
        reader, game = build_demo.login_names(db)
        assert reader.startswith("demo_site_") and game.startswith("demo_game_") and reader not in ("site_reader", "app_reader") and game != "game_app"
        assert len(reader) < 63 and len(game) < 63 and reader.replace("_", "").isalnum() and game.replace("_", "").isalnum()
    assert build_demo.login_names("one") != build_demo.login_names("two")                                              # two demos on one server do not share logins


def test_the_launcher_knows_when_a_demo_has_gone_stale():
    meta = lambda built, nxt: {"built_on": str(built), "next_card": str(nxt)}
    assert run_demo.is_stale(None, TODAY) and run_demo.is_stale({}, TODAY)
    assert not run_demo.is_stale(meta(TODAY, TODAY + timedelta(2)), TODAY)
    assert not run_demo.is_stale(meta(TODAY - timedelta(2), TODAY + timedelta(1)), TODAY)
    assert run_demo.is_stale(meta(TODAY - timedelta(4), TODAY + timedelta(9)), TODAY)                                  # built too long ago
    current = lambda **kw: {**meta(TODAY, TODAY + timedelta(2)), "build_version": "7", **kw}
    assert not run_demo.is_stale(current(), TODAY, "7")
    assert run_demo.is_stale(current(build_version="6"), TODAY, "7") and run_demo.is_stale(meta(TODAY, TODAY + timedelta(2)), TODAY, "7")      # built by older code, or before versions were recorded: rebuilt
    assert not run_demo.is_stale(meta(TODAY, TODAY + timedelta(2)), TODAY)                                              # (without a version to compare, the old rules apply)
    assert build_demo.BUILD_VERSION and "build_version" in inspect.getsource(build_demo.build)                          # the builder really records it
    assert run_demo.is_stale(meta(TODAY - timedelta(1), TODAY), TODAY)                                                  # its "next card" is today: no longer ahead of us


def test_the_demo_database_lives_outside_the_project_and_outside_synced_folders_by_default(monkeypatch):
    # Compared as paths, not as text: a path is written with / on one system and \\ on another, and this test runs on both.
    monkeypatch.delenv("UFC_DEMO_DIR", raising=False)
    monkeypatch.setattr(run_demo.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Sheld\AppData\Local")
    win = run_demo.default_data_dir()
    assert win.name == "ufc-predictor-demo" and win.parent == Path(r"C:\Users\Sheld\AppData\Local") and "OneDrive" not in str(win)
    monkeypatch.setattr(run_demo.sys, "platform", "darwin")
    mac = run_demo.default_data_dir()
    assert mac.name == "ufc-predictor-demo" and mac.parent.name == "Caches" and mac.parent.parent.name == "Library"
    monkeypatch.setattr(run_demo.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/x/.cache")
    assert run_demo.default_data_dir() == Path("/home/x/.cache") / "ufc-predictor-demo"
    monkeypatch.setenv("UFC_DEMO_DIR", "/somewhere/else")
    assert run_demo.default_data_dir() == Path("/somewhere/else")
    assert ROOT not in run_demo.default_data_dir().parents and run_demo.default_data_dir() != ROOT


def test_the_demo_website_listens_on_this_computer_only():
    launcher = (ROOT / "demo" / "run_demo.py").read_text(encoding="utf-8")
    assert '"--server.address", "127.0.0.1"' in launcher                                   # Streamlit's default is every network interface, which would put the demo on the whole Wi-Fi
    assert "0.0.0.0" not in launcher


def test_the_demo_banner_and_the_sign_in_hint_exist_only_for_demo_mode():
    app = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    game = (ROOT / "app" / "game_ui.py").read_text(encoding="utf-8")
    assert 'if os.getenv("DEMO_MODE"):' in app and "demo-banner" in app and "invented" in app
    assert 'if os.getenv("DEMO_MODE"):' in game and "demo_ann" in game and build_demo.PLAYER_PIN in game
    assert "DEMO_MODE" not in (ROOT / ".github" / "workflows" / "auto-update.yml").read_text(encoding="utf-8")        # never set on the real site's job
    assert (ROOT / "demo" / "requirements.txt").read_text(encoding="utf-8").count("pixeltable-pgserver") == 1


# ---------------------------------------------------------------------------------------------- whole demos, on a real Postgres
def _admin():
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")


def _fresh_db(admin):
    name = f"demo_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    return name, sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


def _drop(admin, *names):
    with admin.connect() as c:
        for n in names:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {n} WITH (FORCE)")
            for r in build_demo.login_names(n):                                                                  # the demo's own logins (they own nothing once the database is gone)
                c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


@pytest.fixture(scope="module")
def tiny_demo():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    admin = _admin()
    name, url = _fresh_db(admin)
    info = build_demo.build(url, today=TODAY, size="tiny", log=lambda *a: None)
    info["owner_url"] = url
    yield info
    _drop(admin, name)


@pytest.fixture(scope="module")
def small_demo():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    admin = _admin()
    name, url = _fresh_db(admin)
    info = build_demo.build(url, today=TODAY, size="small", log=lambda *a: None)
    info["owner_url"] = url
    yield info
    _drop(admin, name)


@contextmanager
def _app(info):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_data.clear()                                  # the site caches query results by their SQL text, not by database: clear it so one demo's pages never show another's data
    st.cache_resource.clear()
    keep = {k: os.environ.get(k) for k in ("DATABASE_URL", "GAME_DATABASE_URL", "GAME_INVITE_CODE", "DEMO_MODE")}
    os.environ.update(DATABASE_URL=info["database_url"], GAME_DATABASE_URL=info["game_database_url"], GAME_INVITE_CODE=info["invite_code"], DEMO_MODE="1")
    try:
        yield AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=180).run()
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _text(at):
    return " ".join(str(h.value) for h in at.html) + " " + " ".join(str(m.value) for m in at.markdown)


def _plain(at) -> str:
    """What a visitor reads: the page's text with the styles and tags stripped, plus its info boxes."""
    t = re.sub(r"<style.*?</style>", " ", _text(at) + " " + " ".join(str(i.value) for i in at.info), flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))


@needs_postgres
def test_the_builder_only_ever_fills_an_empty_database(tiny_demo):
    admin = _admin()
    name, url = _fresh_db(admin)
    try:
        eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
        with eng.begin() as c:
            c.exec_driver_sql("CREATE TABLE somebody_elses_data (x INT)")
            c.exec_driver_sql("INSERT INTO somebody_elses_data VALUES (42)")
        with pytest.raises(build_demo.BuildError, match="EMPTY"):
            build_demo.build(url, today=TODAY, size="tiny", log=lambda *a: None)
        with eng.connect() as c:
            assert c.execute(sa.text("SELECT x FROM somebody_elses_data")).scalar() == 42                              # untouched
    finally:
        _drop(admin, name)


@needs_postgres
def test_a_tiny_demo_is_built_by_the_real_pipeline_graded_and_audited(tiny_demo):
    eng = sa.create_engine(tiny_demo["owner_url"].replace("postgresql://", "postgresql+psycopg2://", 1))
    q = lambda sql: pd.read_sql(sa.text(sql), eng)
    n = lambda sql: int(q(sql).iloc[0, 0])
    assert n("SELECT COUNT(*) FROM fights") > 350 and n("SELECT COUNT(*) FROM fight_picks_v2") == n("SELECT COUNT(*) FROM fights")        # a walk-forward pick for every fight
    assert n("SELECT COUNT(*) FROM upcoming_picks_v2 WHERE status = 'locked' AND correct IS NOT NULL") >= 10                          # the two played cards: locked, then graded
    assert n("SELECT COUNT(*) FROM upcoming_picks_v2 WHERE status = 'locked' AND correct IS NULL") >= 15                              # the three coming cards: locked, waiting
    assert n("SELECT COUNT(*) FROM upcoming_distance_v1") >= 25 and n("SELECT COUNT(*) FROM fight_distance_v1") > 80 and n("SELECT COUNT(*) FROM bout_results") >= 10
    old = q("SELECT MAX(predicted_at) AS p, MIN(a.event_date) AS e FROM upcoming_picks_v2 a WHERE correct IS NOT NULL").iloc[0]
    assert pd.Timestamp(old.p).tz_localize(None) < pd.Timestamp(old.e)                                                              # a played card's picks were locked BEFORE it: the timeline reads right
    assert n("SELECT COUNT(*) FROM game_players") == 6 and n("SELECT COUNT(*) FROM game_picks") > 40 and n("SELECT COUNT(*) FROM event_starts") == 3
    assert n("SELECT COUNT(*) FROM upcoming_distance_v2") == 0 and n("SELECT COUNT(*) FROM fight_distance_v2") == 0                    # the market-informed distance model: tables exist, nothing faked
    assert n("SELECT COUNT(*) FROM upcoming_picks_v3") == 0 and n("SELECT COUNT(*) FROM fight_picks_v3") == 0 and tiny_demo["market_model"] is False   # too little history for the market model: skipped, not faked
    assert q("SELECT value FROM demo_meta WHERE key = 'built_on'").iloc[0, 0] == str(TODAY)


@needs_postgres
def test_the_demos_two_logins_are_the_restricted_ones(tiny_demo):
    site = sa.create_engine(tiny_demo["database_url"].replace("postgresql://", "postgresql+psycopg2://", 1))
    game = sa.create_engine(tiny_demo["game_database_url"].replace("postgresql://", "postgresql+psycopg2://", 1))
    with site.connect() as c:
        assert c.execute(sa.text("SELECT COUNT(*) FROM fights")).scalar() > 0
    for engine, sql in ((site, "INSERT INTO fights (fight_url, event_date) VALUES ('x', '2026-01-01')"), (site, "SELECT * FROM game_players"), (game, "SELECT * FROM fights"),
                        (game, "DELETE FROM upcoming_picks_v2"), (site, "CREATE TABLE evil (x INT)")):
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            with engine.begin() as c:
                c.exec_driver_sql(sql)
    with game.begin() as c:
        assert c.execute(sa.text("SELECT COUNT(*) FROM game_players")).scalar() == 6                                   # the game's own tables, and only those


@needs_postgres
def test_rebuilding_gives_a_fresh_demo_with_working_logins_even_though_logins_already_exist(tiny_demo):
    admin = _admin()
    name, url = _fresh_db(admin)                                          # a second database on the same server: the demo logins already exist
    try:
        info = build_demo.build(url, today=TODAY, size="tiny", log=lambda *a: None)
        info2 = build_demo.build(url, today=TODAY + timedelta(days=1), size="tiny", log=lambda *a: None)   # and a REBUILD into the same database
        assert info["database_url"] and info2["database_url"] and info["database_url"] != info2["database_url"]      # fresh passwords every time
        assert info2["built_on"] == str(TODAY + timedelta(days=1))
        sa.create_engine(info2["database_url"].replace("postgresql://", "postgresql+psycopg2://", 1)).connect().close()
    finally:
        _drop(admin, name)


@needs_postgres
@needs_streamlit
def test_every_page_of_the_real_website_opens_on_the_demo_and_says_it_is_a_demo(tiny_demo):
    with _app(tiny_demo) as at:
        assert not at.exception and not at.error and "DEMO" in _plain(at) and "invented" in _plain(at), _plain(at)[:300]
        pages = list(at.radio[0].options)
        assert pages == ["Upcoming", "Past events", "Fighters", "Track record", "Game"]
        next_card = pd.read_sql(sa.text("SELECT event_name FROM upcoming_fights WHERE event_date = :d AND bout_order = 1"), sa.create_engine(tiny_demo["owner_url"].replace("postgresql://", "postgresql+psycopg2://", 1)),
                                params={"d": date.fromisoformat(tiny_demo["next_card"])}).iloc[0, 0]
        assert next_card in _plain(at), (next_card, _plain(at)[:300])                                                  # the Upcoming page shows the invented next card
        for page in pages:
            at = at.radio[0].set_value(page).run()
            assert not at.exception and not at.error and len(_plain(at)) > (600 if page == "Game" else 1500), (page, _plain(at)[:200])
            assert "DEMO" in _plain(at), page
        at = at.radio[0].set_value("Track record").run()
        assert "graded" in _plain(at).lower(), _plain(at)[:600]
        assert "waiting for results" in _plain(at).lower() and "locked pre-fight predictions" in _plain(at).lower(), _plain(at)[:600]


@needs_postgres
@needs_streamlit
def test_a_visitor_can_sign_in_as_a_demo_player_and_see_the_leaderboard(tiny_demo):
    with _app(tiny_demo) as at:
        at = at.radio[0].set_value("Game").run()
        assert "demo_ann" in _plain(at) and tiny_demo["player_pin"] in _plain(at), _plain(at)[:400]                      # the page tells a visitor how to sign in
        at.text_input(key="g_si_nick").input("demo_ann")
        at.text_input(key="g_si_pin").input(tiny_demo["player_pin"])
        at = [b for b in at.button if b.label == "Sign in"][0].click().run()
        assert not at.exception and at.session_state["g_player"]["nickname"] == "demo_ann" and "Signed in as <b>demo_ann</b>" in _text(at)
        assert "beating the model" in _text(at) and "Beat the model" in _text(at)                                      # scored picks on the two played cards: a real leaderboard


@needs_postgres
@needs_streamlit
def test_the_small_demo_adds_the_market_model_and_its_picks_appear_on_the_website(small_demo):
    eng = sa.create_engine(small_demo["owner_url"].replace("postgresql://", "postgresql+psycopg2://", 1))
    n = lambda sql: int(pd.read_sql(sa.text(sql), eng).iloc[0, 0])
    assert small_demo["market_model"] is True and n("SELECT COUNT(*) FROM fight_picks_v3") > 150 and n("SELECT COUNT(*) FROM upcoming_picks_v3") >= 8
    assert n("SELECT COUNT(*) FROM upcoming_picks_v3 WHERE correct IS NOT NULL") >= 10                                       # the two played cards' market-informed picks were graded
    assert n("SELECT COUNT(*) FROM fight_distance_v2") > 200 and n("SELECT COUNT(*) FROM upcoming_distance_v2 WHERE status = 'locked'") >= 15        # the market-informed distance model ran too
    assert n("SELECT COUNT(*) FROM upcoming_distance_v2 WHERE graded_at IS NOT NULL AND went_distance IS NOT NULL") >= 10
    with _app(small_demo) as at:
        assert not at.exception and "market-informed" in _plain(at).lower(), _plain(at)[:400]                         # the next card shows its market-informed picks
        assert re.search(r"Goes the distance.{0,220}market-informed \d+%", _plain(at)), _plain(at)[:600]              # ...and its market-informed goes-the-distance figure, beside the model-only one
        at = at.radio[0].set_value("Track record").run()
        assert not at.exception and "market-informed picks" in _plain(at).lower(), _plain(at)[:600]
        assert "The same question, with the betting market" in _plain(at) and "Verdict:" in _plain(at), _plain(at)[:600]
