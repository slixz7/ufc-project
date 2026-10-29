"""
Tests of the website's read-only login on a REAL Postgres server (permissions cannot be checked on SQLite). They run only when
TEST_PG_ADMIN_URL points at a server where that user may create databases and roles; each run uses its own uniquely named database
and roles and removes them afterwards.

The fixture reproduces what a real Neon project turned out to have: an old "read-only" login made in the web console, which has four
powerful attributes and belongs to a Neon-style role that can read AND write every table.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import auto_odds  # noqa: E402
import db_roles  # noqa: E402
import game_admin  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")


@pytest.fixture(scope="module")
def pg():
    x = uuid.uuid4().hex[:8]
    n = {k: f"{k}_{x}" for k in ("owner", "ns", "legacy", "legacy2", "site", "game", "db")}
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {n['owner']} LOGIN PASSWORD 'ownerpw' CREATEROLE")                       # like Neon's owner: makes roles, is not a superuser
        c.exec_driver_sql(f"CREATE ROLE {n['ns']} NOLOGIN CREATEROLE CREATEDB BYPASSRLS REPLICATION")            # a stand-in for Neon's neon_superuser
        c.exec_driver_sql(f"GRANT pg_read_all_data, pg_write_all_data TO {n['ns']}")
        c.exec_driver_sql(f"CREATE ROLE {n['legacy']} LOGIN PASSWORD 'legacypw' CREATEROLE CREATEDB BYPASSRLS REPLICATION IN ROLE {n['ns']}")     # the console-made login
        c.exec_driver_sql(f"CREATE DATABASE {n['db']} OWNER {n['owner']}")
    base = sa.engine.make_url(ADMIN_URL).set(database=n["db"])
    owner = sa.create_engine(base.set(username=n["owner"], password="ownerpw"))
    with owner.begin() as c:
        c.exec_driver_sql("CREATE TABLE fights (fight_url TEXT PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v2 (bout_id TEXT PRIMARY KEY, predicted_winner TEXT)")
    auto_odds.ensure_snapshot_tables(owner)                                                                         # the real private odds table and view
    with owner.begin() as c:                                                                                        # the grants sql/readonly_role.sql used to give the old login
        c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {n['legacy']}")
        c.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {n['legacy']}")
        c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {n['legacy']}")
        c.exec_driver_sql(f"REVOKE ALL ON odds_snapshots, odds_bout_summary FROM {n['legacy']}")
    made_real_names = []
    yield {"n": n, "base": base, "owner": owner, "admin": admin, "made_real_names": made_real_names}
    made_real_names = bool(made_real_names)
    owner.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {n['db']} WITH (FORCE)")
        for r in (n["game"], n["site"], n["legacy2"], n["legacy"], n["ns"], n["owner"], "site_reader", "app_reader"):
            if r in ("site_reader", "app_reader") and not made_real_names:
                continue                                                                                           # only drop the real-named logins if THIS run made them
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


def _as(pg, role, password):
    return sa.create_engine(pg["base"].set(username=role, password=password))


def _refused(engine, sql):
    try:
        with engine.begin() as c:
            c.exec_driver_sql(sql)
    except sa.exc.ProgrammingError as exc:
        return "permission denied" in str(exc.orig) or "must be owner" in str(exc.orig)
    return False


def _can_login(pg, role, password):
    try:
        with _as(pg, role, password).connect() as c:
            c.exec_driver_sql("SELECT 1")
        return True
    except sa.exc.OperationalError:
        return False


def test_a_console_style_login_is_condemned_for_every_reason(pg):
    n = pg["n"]
    problems = " | ".join(db_roles.audit_reader(pg["owner"], role=n["legacy"]))
    for expected in ("can create other logins", "can create databases", "can bypass row-level security", "can use replication", f"is a member of {n['ns']}",
                     "INSERT/UPDATE/DELETE on fights", "can read odds_snapshots, which is private", "can read odds_bout_summary, which is private"):
        assert expected in problems, expected
    legacy = db_roles.audit_legacy(pg["owner"], role=n["legacy"])
    assert len(legacy) == 1 and "still exists and is not safe" in legacy[0] and "retire-old" in legacy[0]
    assert db_roles.audit_legacy(pg["owner"], role=f"nobody_{uuid.uuid4().hex[:6]}") == []                           # no old login at all is fine
    assert "does not exist" in db_roles.audit_reader(pg["owner"], role=f"nobody_{uuid.uuid4().hex[:6]}")[0]


def test_a_properly_made_login_passes_the_audit_and_is_genuinely_read_only(pg):
    url = db_roles.setup_reader(pg["owner"], role=pg["n"]["site"])
    assert url and url.startswith("postgresql://") and pg["n"]["site"] in url and "ownerpw" not in url
    assert db_roles.audit_reader(pg["owner"], role=pg["n"]["site"]) == []
    pg["site_url"] = url
    site = sa.create_engine(sa.engine.make_url(url).set(drivername="postgresql+psycopg2"))
    with site.connect() as c:
        c.exec_driver_sql("SELECT * FROM fights"); c.exec_driver_sql("SELECT * FROM upcoming_picks_v2")           # the public tables are readable
        assert c.execute(sa.text("SHOW statement_timeout")).scalar() in ("1min", "60s")
    for sql in ("INSERT INTO fights VALUES ('x')", "UPDATE fights SET fight_url = 'y'", "DELETE FROM fights", "TRUNCATE fights", "DROP TABLE fights",
                "ALTER TABLE fights ADD COLUMN evil TEXT", "CREATE TABLE evil (x INT)", "CREATE ROLE rogue", "SELECT * FROM pg_authid",
                "SELECT * FROM odds_snapshots", "SELECT * FROM odds_bout_summary"):
        assert _refused(site, sql), f"the new login was allowed to run: {sql}"
    with pg["owner"].connect() as c:
        row = c.execute(sa.text("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = :r"), {"r": pg["n"]["site"]}).fetchone()
        assert not any(row)
        assert c.execute(sa.text("SELECT COUNT(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = :r"), {"r": pg["n"]["site"]}).scalar() == 0


def test_a_table_made_later_is_readable_but_a_private_one_made_later_is_caught_and_repaired(pg):
    site = sa.create_engine(sa.engine.make_url(pg["site_url"]).set(drivername="postgresql+psycopg2"))
    with pg["owner"].begin() as c:
        c.exec_driver_sql("CREATE TABLE later_public (x INT)")
    with site.connect() as c:
        c.exec_driver_sql("SELECT * FROM later_public")                                                              # new tables are readable without any further step
    with pg["owner"].begin() as c:
        c.exec_driver_sql("CREATE TABLE game_picks (player_id TEXT)")                                               # but a private table made later inherits that default...
    assert any("can read game_picks, which is private" in p for p in db_roles.audit_reader(pg["owner"], role=pg["n"]["site"]))   # ...which the audit catches
    assert db_roles.setup_reader(pg["owner"], role=pg["n"]["site"]) is None                                          # and running setup again repairs
    assert db_roles.audit_reader(pg["owner"], role=pg["n"]["site"]) == []
    with pg["owner"].begin() as c:
        c.exec_driver_sql("DROP TABLE game_picks")


def test_setup_is_repeatable_and_a_new_password_kills_the_old_one(pg):
    site = pg["n"]["site"]
    password = sa.engine.make_url(pg["site_url"]).password
    assert _can_login(pg, site, password)
    assert db_roles.setup_reader(pg["owner"], role=site) is None and _can_login(pg, site, password)               # no new password unless asked for
    new_url = db_roles.setup_reader(pg["owner"], role=site, rotate_password=True)
    assert new_url and sa.engine.make_url(new_url).password != password
    assert not _can_login(pg, site, password) and _can_login(pg, site, sa.engine.make_url(new_url).password)
    assert db_roles.audit_reader(pg["owner"], role=site) == []


def test_the_daily_jobs_odds_hiding_covers_both_the_new_and_the_old_login_by_name(pg):
    admin = pg["admin"]
    with admin.connect() as c:
        taken = c.exec_driver_sql("SELECT rolname FROM pg_roles WHERE rolname IN ('site_reader', 'app_reader')").fetchall()
    if taken:
        pytest.skip("this server already has a login with one of those real names")
    pg["made_real_names"].append(True)                                                                              # the fixture's teardown drops them even if this test fails half way
    with admin.connect() as c:
        c.exec_driver_sql("CREATE ROLE site_reader LOGIN PASSWORD 'x'")
        c.exec_driver_sql("CREATE ROLE app_reader LOGIN PASSWORD 'x'")
    if True:
        with pg["owner"].begin() as c:
            c.exec_driver_sql("GRANT SELECT ON odds_snapshots, odds_bout_summary TO site_reader, app_reader")        # as the default privileges would have
            assert c.execute(sa.text("SELECT has_table_privilege('site_reader', 'odds_snapshots', 'SELECT')")).scalar()
        auto_odds.ensure_snapshot_tables(pg["owner"])                                                              # what every daily run does
        auto_odds.ensure_snapshot_tables(pg["owner"])                                                              # and it is safe to repeat
        with pg["owner"].connect() as c:
            for role in ("site_reader", "app_reader"):
                for t in ("odds_snapshots", "odds_bout_summary"):
                    assert not c.execute(sa.text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": role, "t": t}).scalar(), (role, t)


def test_the_game_audit_catches_the_old_login_reading_the_game_tables_just_as_it_did_on_the_real_database(pg):
    n = pg["n"]
    both = (n["site"], n["legacy"])
    game_admin.setup(pg["owner"], game_role=n["game"], reader_role=both)
    problems = game_admin.audit(pg["owner"], game_role=n["game"], reader_role=both)
    assert any(n["legacy"] in p and "can read game_players" in p for p in problems)                                  # through its membership, whatever was revoked by name
    assert not any(n["site"] in p for p in problems)                                                                 # the new login cannot
    assert game_admin.audit(pg["owner"], game_role=n["game"], reader_role=(n["site"],)) == []


def test_retiring_a_login_you_cannot_alter_strips_what_it_can_and_says_to_delete_it(pg):
    n = pg["n"]
    report = " | ".join(db_roles.retire_legacy(pg["owner"], role=n["legacy"]))
    assert "Removed every table and schema grant" in report and "Could not stop" in report                           # the owner cannot alter a role it did not create (as on Neon)
    assert "still over-powered" in report and "Delete it in the Neon console" in report                              # it still can, through its roles: say so, never claim success
    with pg["owner"].connect() as c:
        assert n["legacy"] not in c.execute(sa.text("SELECT relacl::text FROM pg_class WHERE relname = 'fights'")).scalar()      # its direct grants are gone
    assert "There is no login called" in db_roles.retire_legacy(pg["owner"], role=f"nobody_{uuid.uuid4().hex[:6]}")[0]


def test_retiring_a_login_the_owner_made_locks_it_and_kills_its_password(pg):
    n = pg["n"]
    with pg["owner"].begin() as c:
        c.exec_driver_sql(f"CREATE ROLE {n['legacy2']} LOGIN PASSWORD 'oldpw'")
        c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {n['legacy2']}")
        c.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {n['legacy2']}")
    assert _can_login(pg, n["legacy2"], "oldpw")
    report = " | ".join(db_roles.retire_legacy(pg["owner"], role=n["legacy2"]))
    assert "can no longer log in" in report and "old connection string is dead" in report and "You can now safely delete" in report
    assert not _can_login(pg, n["legacy2"], "oldpw")


def test_the_start_times_table_is_readable_by_the_site_login_but_not_writable_and_the_audit_still_passes(pg):
    from datetime import datetime, timezone
    site_url = db_roles.setup_reader(pg["owner"], role=pg["n"]["site"], rotate_password=True)                      # a current password (the earlier test rotated it)
    auto_odds.save_event_starts(pg["owner"], [{"event_date": datetime(2026, 1, 31).date(), "first_start": datetime(2026, 1, 30, 22, 0, tzinfo=timezone.utc), "bouts": 3, "distinct_times": 3}],
                                datetime(2026, 1, 28, 14, 17, tzinfo=timezone.utc))                                 # created later by the owner: readable through the default grants
    site = sa.create_engine(sa.engine.make_url(site_url).set(drivername="postgresql+psycopg2"))
    with site.connect() as c:
        got = c.execute(sa.text("SELECT first_start FROM event_starts")).scalar()
    assert got == datetime(2026, 1, 30, 22, 0, tzinfo=timezone.utc)                                                 # real timestamptz round trip
    assert _refused(site, "INSERT INTO event_starts (event_date, first_start) VALUES ('2026-02-07', now())")
    assert _refused(site, "UPDATE event_starts SET first_start = now()") and _refused(site, "DELETE FROM event_starts")
    assert db_roles.audit_reader(pg["owner"], role=pg["n"]["site"]) == []                                           # a public table, not a private one
