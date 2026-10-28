"""
Tests of the game's database permissions on a REAL Postgres server, because permissions cannot be checked on SQLite.

They run only when TEST_PG_ADMIN_URL points at a server where that user may create databases and roles (the CI job provides one):
    TEST_PG_ADMIN_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/postgres python -m pytest tests/test_game_postgres.py
Each run makes its own uniquely named database and roles and removes them afterwards.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import game_admin  # noqa: E402
import game_db  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
BOUT = {"bout_id": "b1", "status": "scheduled", "event_date": "2026-10-31", "fighter_a": "Alex One", "fighter_b": "Ben Two", "scheduled_rounds": 3}


@pytest.fixture(scope="module")
def pg():
    x = uuid.uuid4().hex[:8]
    names = {"owner": f"owner_{x}", "reader": f"reader_{x}", "game": f"game_{x}", "db": f"db_{x}"}
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {names['owner']} LOGIN PASSWORD 'ownerpw' CREATEROLE")           # like Neon's owner: can make roles, is not a superuser
        c.exec_driver_sql(f"CREATE ROLE {names['reader']} LOGIN PASSWORD 'readerpw'")
        c.exec_driver_sql(f"CREATE DATABASE {names['db']} OWNER {names['owner']}")
    base = sa.engine.make_url(ADMIN_URL).set(database=names["db"])
    owner = sa.create_engine(base.set(username=names["owner"], password="ownerpw"))
    with owner.begin() as c:                                      # a miniature of the project, with the same read-only grants as sql/readonly_role.sql
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v2 (bout_id TEXT PRIMARY KEY, predicted_winner TEXT)")
        c.exec_driver_sql("CREATE TABLE odds_snapshots (id SERIAL PRIMARY KEY, books JSONB)")
        c.exec_driver_sql("CREATE TABLE fights (fight_url TEXT PRIMARY KEY)")
        c.exec_driver_sql("INSERT INTO upcoming_picks_v2 VALUES ('b1', 'Alex One')")
        c.exec_driver_sql("CREATE VIEW v2_summary AS SELECT bout_id FROM upcoming_picks_v2")
        c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {names['reader']}")
        c.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {names['reader']}")
        c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {names['reader']}")   # later tables are readable by default: the trap
        c.exec_driver_sql(f"REVOKE ALL ON odds_snapshots FROM {names['reader']}")                         # the private odds history, as in the real project
    yield {"names": names, "base": base, "owner": owner}
    owner.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {names['db']} WITH (FORCE)")
        for r in (names["game"], names["reader"], names["owner"]):
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


def _engine(pg, role_key, password):
    return sa.create_engine(pg["base"].set(username=pg["names"][role_key], password=password))


def _refused(engine, sql):
    """True if the database refuses the statement for lack of permission."""
    try:
        with engine.begin() as c:
            c.exec_driver_sql(sql)
    except sa.exc.ProgrammingError as exc:
        return "permission denied" in str(exc.orig) or "must be owner" in str(exc.orig)
    return False


def _setup(pg, **kw):
    return game_admin.setup(pg["owner"], game_role=pg["names"]["game"], reader_role=pg["names"]["reader"], **kw)


def _audit(pg):
    return game_admin.audit(pg["owner"], game_role=pg["names"]["game"], reader_role=pg["names"]["reader"])


def _game_engine(pg, url):
    return sa.create_engine(sa.engine.make_url(url).set(drivername="postgresql+psycopg2"))


def test_setup_creates_a_game_user_that_passes_the_audit_and_prints_no_password(pg):
    url = _setup(pg)
    assert url and url.startswith("postgresql://") and pg["names"]["game"] in url and "ownerpw" not in url
    assert _audit(pg) == []
    pg["url"] = url


def test_the_game_user_can_run_the_whole_game_on_real_postgres(pg):
    g = _game_engine(pg, pg["url"])
    player, err = game_db.register(g, "Pat", "123456", True, NOW)
    assert err is None
    assert game_db.save_pick(g, player["player_id"], BOUT, "Alex One", "KO/TKO", 2, NOW) is None
    assert game_db.save_pick(g, player["player_id"], BOUT, "Ben Two", "Decision", None, NOW) is None             # the upsert on real Postgres
    assert game_db.login(g, "pat", "123456", NOW)[0]["nickname"] == "Pat" and game_db.login(g, "Pat", "000000", NOW)[1] == "Wrong nickname or PIN."
    assert game_db.all_picks(g).pick_name.tolist() == ["Ben Two"]
    with g.connect() as c:
        assert c.execute(sa.text("SHOW statement_timeout")).scalar() == "10s"


def test_the_game_user_can_reach_nothing_outside_the_game_tables(pg):
    g = _game_engine(pg, pg["url"])
    for sql in ("SELECT * FROM upcoming_picks_v2", "SELECT * FROM odds_snapshots", "SELECT * FROM fights", "SELECT * FROM v2_summary", "SELECT * FROM bout_results",
                "INSERT INTO upcoming_picks_v2 VALUES ('x', 'Hacker')", "UPDATE upcoming_picks_v2 SET predicted_winner = 'Hacker'", "DELETE FROM fights",
                "TRUNCATE game_players", "DROP TABLE game_picks", "ALTER TABLE game_picks ADD COLUMN evil TEXT", "CREATE TABLE evil (x INT)", "SELECT * FROM pg_authid",
                "CREATE ROLE rogue"):
        assert _refused(g, sql), f"the game user was allowed to run: {sql}"


def test_the_game_user_cannot_hand_out_access_to_anyone_else(pg):
    g = _game_engine(pg, pg["url"])
    with g.begin() as c:                                           # Postgres lets a non-owner RUN this but it grants nothing (only a warning), so check the effect
        c.exec_driver_sql("GRANT ALL ON game_picks TO PUBLIC")
        c.exec_driver_sql(f"GRANT SELECT ON game_players TO {pg['names']['reader']}")
    r = _engine(pg, "reader", "readerpw")
    assert _refused(r, "SELECT * FROM game_picks") and _refused(r, "SELECT * FROM game_players")             # nobody gained anything
    assert _audit(pg) == []


def test_the_public_read_only_user_still_reads_the_project_but_cannot_see_the_game_tables(pg):
    r = _engine(pg, "reader", "readerpw")
    with r.connect() as c:
        assert c.execute(sa.text("SELECT predicted_winner FROM upcoming_picks_v2")).scalar() == "Alex One"
        assert c.execute(sa.text("SELECT COUNT(*) FROM bout_results")).scalar() == 0                         # the results the game page reads are public
    for t in game_admin.GAME_TABLES:
        assert _refused(r, f"SELECT * FROM {t}"), f"the read-only user could read {t}"                       # no PIN hashes for the public site's user
    assert _refused(r, "INSERT INTO fights VALUES ('x')") and _refused(r, "INSERT INTO game_picks (player_id, bout_id) VALUES ('a', 'b')")


def test_running_setup_again_is_harmless_and_issues_no_new_password(pg):
    assert _setup(pg) is None and _audit(pg) == []
    g = _game_engine(pg, pg["url"])
    assert game_db.login(g, "Pat", "123456", NOW)[0]["nickname"] == "Pat"                                    # the existing connection string and player still work


def test_rotating_the_password_replaces_the_old_one(pg):
    new_url = _setup(pg, rotate_password=True)
    assert new_url and new_url != pg["url"]
    with pytest.raises(sa.exc.OperationalError):
        with _game_engine(pg, pg["url"]).connect():
            pass                                                                                           # the old password is dead
    assert game_db.login(_game_engine(pg, new_url), "Pat", "123456", NOW)[0]["nickname"] == "Pat"
    pg["url"] = new_url


def test_the_audit_notices_a_permission_that_should_not_be_there_and_setup_repairs_it(pg):
    names = pg["names"]
    with pg["owner"].begin() as c:
        c.exec_driver_sql(f"GRANT SELECT ON fights TO {names['game']}")                                      # a mistake: the game user can read another table
        c.exec_driver_sql(f"GRANT SELECT ON game_players TO {names['reader']}")                              # a mistake: the public user can read PIN hashes
    problems = " | ".join(_audit(pg))
    assert f"{names['game']} can SELECT on fights" in problems and f"{names['reader']} (the public site's read-only user) can read game_players" in problems
    assert _setup(pg) is None and _audit(pg) == []                                                           # setup takes both back


def test_the_audit_reports_a_missing_role(pg):
    assert game_admin.audit(pg["owner"], game_role=f"nobody_{uuid.uuid4().hex[:6]}", reader_role=pg["names"]["reader"])[0].startswith("the role ")
