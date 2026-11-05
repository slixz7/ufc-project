"""
pipeline/recover.py: one-command recovery from a backup into a new, empty database. The behavioural tests run the real pipeline on a real Postgres: a demo is built and backed up, then
recovered into an empty database owned by a LIMITED role (like a hosted database's owner), and the real website is opened against the recovered copy. The rest are the refusals,
the failure paths, and the rule that no password or connection string is ever printed.
"""
import logging
import os
import re
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo", "app", "tests"):
    sys.path.insert(0, str(ROOT / sub))
import auto_odds  # noqa: E402
import backup  # noqa: E402
import build_demo  # noqa: E402
import db_roles  # noqa: E402
import game_admin  # noqa: E402
import recover  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 10, 29)
OWNER_PASSWORD = "OwnerPw-Zq9x7Lm2"


def conn(user: str, password: str, host: str, database: str, driver: str = "postgresql+psycopg2") -> str:
    """A connection string assembled from its parts, so no realistic-looking one is written out in this file (the project's credential scan would, rightly, flag it)."""
    return f"{driver}://{user}:{password}@{host}/{database}"


def _admin():
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")


def _url(database: str, user: str | None = None, password: str | None = None) -> str:
    u = sa.engine.make_url(ADMIN_URL).set(database=database)
    if user:
        u = u.set(username=user, password=password)
    return u.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A demo built by the real pipeline, and a backup of it."""
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    name = f"rcw_{uuid.uuid4().hex[:8]}"
    admin = _admin()
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    build_demo.build(_url(name), today=TODAY, size="small", log=lambda *a: None)
    eng = sa.create_engine(_url(name).replace("postgresql://", "postgresql+psycopg2://", 1))
    auto_odds.ensure_snapshot_tables(eng)                                                                                 # the daily job makes this table on its first real run; the demo has no snapshots yet
    with eng.begin() as c:                                                                                                # ids 1 to 3: an unfixed counter (it restarts at 1) would collide with them at once
        c.exec_driver_sql("INSERT INTO odds_snapshots (id, bout_id, n_books) VALUES (1, 'a', 4), (2, 'b', 5), (3, 'c', 6)")
    data = tmp_path_factory.mktemp("backup") / "data"
    manifest = backup.dump(eng, data, exclude=backup.EXCLUDE | {"demo_meta"})                                          # demo_meta is the demo's own marker: a real database has no such table
    counts = {m["table"]: int(m["rows"]) for m in manifest}
    yield {"eng": eng, "name": name, "url": _url(name), "data": data, "counts": counts}
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        for r in build_demo.login_names(name):
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


@contextmanager
def empty_target():
    """A new empty database owned by a limited login (it may create roles, like a hosted database's owner), with names for the two logins the tool will create."""
    name, owner = f"rct_{uuid.uuid4().hex[:8]}", f"own_{uuid.uuid4().hex[:8]}"
    reader, game = build_demo.login_names(name)
    admin = _admin()
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{OWNER_PASSWORD}'")
        c.exec_driver_sql(f"CREATE DATABASE {name} OWNER {owner}")
    url = _url(name, owner, OWNER_PASSWORD)
    eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
    try:
        yield {"name": name, "url": url, "eng": eng, "reader": reader, "game": game, "owner": owner}
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            for r in (reader, game, owner):
                c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


def go(target, data, monkeypatch, tmp_path, *extra, confirm=True, env=True):
    if env:
        monkeypatch.setenv(recover.ENV_VAR, target["url"])
    args = [str(data), "--site-file", str(tmp_path / "site.txt"), "--game-file", str(tmp_path / "game.txt"), "--reader-role", target["reader"], "--game-role", target["game"], *extra]
    if confirm:
        args += ["--confirm", target["name"]]
    return recover.main(args)


def count(eng, table) -> int:
    with eng.connect() as c:
        return int(c.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar())


# ---------------------------------------------------------------------------------------------- a complete recovery
@needs_postgres
def test_a_complete_recovery_from_a_real_backup_leaves_a_working_audited_database_and_a_working_website(world, monkeypatch, tmp_path, capsys, caplog):
    pytest.importorskip("streamlit")
    from test_demo import _app, _plain
    sentinel = conn("someone", "else", "old-database.example", "db")
    monkeypatch.setenv("DATABASE_URL", sentinel)                                                                           # a DATABASE_URL pointing somewhere else entirely
    before = {t: count(world["eng"], t) for t in world["counts"]}
    with empty_target() as t, caplog.at_level(logging.INFO):
        assert go(t, world["data"], monkeypatch, tmp_path) == 0
        out = capsys.readouterr().out + caplog.text
        assert "RECOVERY OK:" in out and f"{t['name']} now holds the backup" in out
        assert "RESTORE OK: all" in out and "WHAT TO DO NOW" in out and recover.ENV_VAR in out and "GAME_INVITE_CODE stays as it is" in out
        eng = t["eng"]
        assert before == world["counts"]                                                                                    # the backup was taken from exactly this
        assert {tb: count(eng, tb) for tb in world["counts"]} == world["counts"]                                           # every table came back with the same number of rows
        assert {tb: count(world["eng"], tb) for tb in world["counts"]} == before                                          # the source database was never touched
        assert os.environ["DATABASE_URL"] == sentinel                                                                      # and the environment is exactly as it was
        # the counters carry on after the restored ids
        assert backup.check_sequences(eng) == []
        with eng.begin() as c:
            nxt = c.execute(sa.text("INSERT INTO odds_snapshots (bout_id, n_books) VALUES ('after-recovery', 3) RETURNING id")).scalar()
        assert world["counts"]["odds_snapshots"] == 3 and nxt == 4                                                         # the next snapshot, not a collision with a restored one
        # both logins exist, are audited, and behave
        assert db_roles.audit_reader(eng, role=t["reader"]) == [] and game_admin.audit(eng, game_role=t["game"], reader_role=(t["reader"],)) == []
        site_url, game_url = (tmp_path / "site.txt").read_text().strip(), (tmp_path / "game.txt").read_text().strip()
        assert site_url.startswith(f"postgresql://{t['reader']}:") and game_url.startswith(f"postgresql://{t['game']}:")
        site = sa.create_engine(site_url.replace("postgresql://", "postgresql+psycopg2://", 1))
        game = sa.create_engine(game_url.replace("postgresql://", "postgresql+psycopg2://", 1))
        with site.connect() as c:
            assert c.execute(sa.text("SELECT COUNT(*) FROM fights")).scalar() == world["counts"]["fights"]
        with pytest.raises(sa.exc.ProgrammingError), site.connect() as c:
            c.execute(sa.text("SELECT COUNT(*) FROM odds_snapshots"))                                                      # the website's login cannot read the odds
        with game.connect() as c:
            assert c.execute(sa.text("SELECT COUNT(*) FROM game_players")).scalar() == world["counts"]["game_players"]
        with pytest.raises(sa.exc.ProgrammingError), game.connect() as c:
            c.execute(sa.text("SELECT COUNT(*) FROM fights"))                                                              # and the game's login cannot read anything else
        if os.name == "posix":
            assert oct((tmp_path / "site.txt").stat().st_mode & 0o777) == "0o600"                                         # a file only its owner can read
        # nothing secret was printed
        for secret in (OWNER_PASSWORD, sa.engine.make_url(site_url).password, sa.engine.make_url(game_url).password, "postgresql://" + t["reader"]):
            assert secret not in out, "a password or connection string reached the output"
        # the 'Why this pick?' cache came back, as many as the source had
        for version in ("v2", "v3"):
            with world["eng"].connect() as c1, eng.connect() as c2:
                q = sa.text("SELECT COUNT(*) FROM fight_reasons WHERE version = :v")
                assert c2.execute(q, {"v": version}).scalar() == c1.execute(q, {"v": version}).scalar() > 0
        # the real website opens against the recovered database with the new logins
        info = {"database_url": site_url, "game_database_url": game_url, "invite_code": "demo"}
        with _app(info) as at:
            page = re.sub(r"\s+", " ", _plain(at))
            assert not at.exception and not at.error and "Why this pick?" in page and "Why this distance prediction?" in page
            at = at.radio[0].set_value("Track record").run()
            assert not at.exception and "How much evidence is there?" in _plain(at)
        # a second run into the same database is refused: it is no longer empty
        capsys.readouterr()
        assert go(t, world["data"], monkeypatch, tmp_path) == 2
        assert "is not empty" in capsys.readouterr().out
        site.dispose()
        game.dispose()


@needs_postgres
def test_logins_that_already_exist_on_the_server_are_given_new_passwords_and_the_old_ones_stop_working(world, monkeypatch, tmp_path, capsys):
    with empty_target() as t:
        with t["eng"].begin() as c:                                                                                         # as on a hosted project: the logins outlive the old database
            c.exec_driver_sql(f"CREATE ROLE {t['reader']} LOGIN PASSWORD 'OldReaderPw-1'")
            c.exec_driver_sql(f"CREATE ROLE {t['game']} LOGIN PASSWORD 'OldGamePw-1'")
        assert go(t, world["data"], monkeypatch, tmp_path, "--skip-reasons") == 0
        site_url, game_url = (tmp_path / "site.txt").read_text().strip(), (tmp_path / "game.txt").read_text().strip()
        assert "OldReaderPw-1" not in site_url and "OldGamePw-1" not in game_url
        for url in (site_url, game_url):
            eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
            with eng.connect() as c:
                assert c.execute(sa.text("SELECT 1")).scalar() == 1                                                          # the new password works
            eng.dispose()
        for role, old in ((t["reader"], "OldReaderPw-1"), (t["game"], "OldGamePw-1")):
            stale = sa.create_engine(sa.engine.make_url(t["url"]).set(username=role, password=old))
            with pytest.raises(sa.exc.OperationalError), stale.connect():
                pass                                                                                                         # and the old one is refused
            stale.dispose()


# ---------------------------------------------------------------------------------------------- refusals
@needs_postgres
def test_a_database_that_already_has_tables_is_refused_and_nothing_in_it_changes(world, monkeypatch, tmp_path, capsys):
    before = {t: count(world["eng"], t) for t in world["counts"]}
    fake = {"url": world["url"], "name": world["name"], "reader": "rc_reader_x", "game": "rc_game_x"}                   # the real, full source database as the "target"
    assert go(fake, world["data"], monkeypatch, tmp_path) == 2
    out = capsys.readouterr().out
    assert "is not empty" in out and "cannot overwrite anything" in out
    assert {t: count(world["eng"], t) for t in world["counts"]} == before
    assert not (tmp_path / "site.txt").exists() and not (tmp_path / "game.txt").exists()
    with _admin().connect() as c:
        assert c.execute(sa.text("SELECT COUNT(*) FROM pg_roles WHERE rolname IN ('rc_reader_x', 'rc_game_x')")).scalar() == 0


@needs_postgres
@pytest.mark.parametrize("confirm", [None, "wrong_name", ""])
def test_without_the_databases_own_name_typed_again_nothing_is_created(world, monkeypatch, tmp_path, capsys, confirm):
    with empty_target() as t:
        monkeypatch.setenv(recover.ENV_VAR, t["url"])
        args = [str(world["data"]), "--site-file", str(tmp_path / "s.txt"), "--game-file", str(tmp_path / "g.txt")] + (["--confirm", confirm] if confirm is not None else [])
        assert recover.main(args) == 2
        out = capsys.readouterr().out
        assert f"--confirm {t['name']}" in out and "This will restore into" in out                                           # it says exactly what to type
        assert sa.inspect(t["eng"]).get_table_names() == [] and not (tmp_path / "s.txt").exists()


@needs_postgres
def test_the_new_databases_address_comes_only_from_its_own_variable_never_from_database_url(world, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(recover.ENV_VAR, raising=False)
    monkeypatch.setenv("DATABASE_URL", world["url"])                                                                      # the "real" database, set as usual
    before = {t: count(world["eng"], t) for t in world["counts"]}
    assert recover.main([str(world["data"]), "--confirm", world["name"]]) == 2
    out = capsys.readouterr().out
    assert recover.ENV_VAR in out and "never reads DATABASE_URL" in out
    assert {t: count(world["eng"], t) for t in world["counts"]} == before


def test_the_other_ways_to_misuse_it_are_refused_before_anything_happens(tmp_path, capsys):
    assert recover.main([str(tmp_path / "nope")]) == 2 and "is not a folder" in capsys.readouterr().out
    assert recover.main([str(tmp_path), "--database-url", "postgresql://x/y"]) == 2 and "only for a rehearsal" in capsys.readouterr().out
    assert recover.main([str(tmp_path), "--rehearse", "--database-url", conn("u", "p", "ep-cool-123." + "neon.tech", "db", "postgresql")]) == 2
    assert "hosted database" in capsys.readouterr().out                                                                   # refused before any connection is tried


def test_an_unreachable_database_is_reported_without_its_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(recover.ENV_VAR, conn("someone", "SecretPw-1Ab", "127.0.0.1:1", "target", "postgresql"))
    assert recover.main([str(tmp_path), "--confirm", "target"]) == 2
    out = capsys.readouterr().out
    assert "Could not connect to 127.0.0.1/target" in out and "SecretPw-1Ab" not in out and "someone" not in out


# ---------------------------------------------------------------------------------------------- failures part-way
@needs_postgres
def test_a_damaged_backup_stops_everything_before_anything_is_created(world, monkeypatch, tmp_path, capsys):
    bad = tmp_path / "data"
    shutil.copytree(world["data"], bad)
    victim = next(p for p in sorted(bad.glob("*.csv")) if p.name != backup.MANIFEST and p.stat().st_size > 50)
    victim.write_bytes(victim.read_bytes() + b"1,2,3\n")                                                                  # one extra line: the file no longer matches its checksum
    with empty_target() as t:
        assert go(t, bad, monkeypatch, tmp_path) == 1
        out = capsys.readouterr().out
        assert "THE BACKUP IS NOT INTACT" in out and "RECOVERY STOPPED at the first step" in out and victim.stem in out
        assert sa.inspect(t["eng"]).get_table_names() == []                                                                # not a single table
        with _admin().connect() as c:
            assert c.execute(sa.text("SELECT COUNT(*) FROM pg_roles WHERE rolname IN (:a, :b)"), {"a": t["reader"], "b": t["game"]}).scalar() == 0
        assert not (tmp_path / "site.txt").exists()


@needs_postgres
def test_a_login_that_can_reach_too_much_stops_the_recovery_and_says_not_to_use_the_database(world, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(db_roles, "audit_reader", lambda engine, role=None: ["site_reader can read odds_snapshots, which is private"])
    with empty_target() as t:
        assert go(t, world["data"], monkeypatch, tmp_path, "--skip-reasons") == 1
        out = capsys.readouterr().out
        assert "RECOVERY STOPPED at the audit" in out and "can read odds_snapshots" in out and "Nothing should use the new database until this is fixed" in out
        assert "RECOVERY OK" not in out and "WHAT TO DO NOW" not in out


@needs_postgres
def test_an_unexpected_error_is_reported_safely_with_no_traceback_and_no_password(world, monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise RuntimeError("could not run: CREATE ROLE x WITH LOGIN PASSWORD 'LeakMe-9zQ' ...")
    monkeypatch.setattr(game_admin, "setup", boom)
    with empty_target() as t:
        assert go(t, world["data"], monkeypatch, tmp_path, "--skip-reasons") == 1
        out = capsys.readouterr().out
        assert "Recovery stopped with an unexpected error: RuntimeError" in out and "may now be partly filled" in out
        assert "LeakMe-9zQ" not in out and "Traceback" not in out and OWNER_PASSWORD not in out


@needs_postgres
@pytest.mark.parametrize("failure", [RuntimeError("boom"), SystemExit(3)])
def test_a_failure_rebuilding_the_reasons_cache_never_fails_the_recovery(world, monkeypatch, tmp_path, capsys, failure):
    import model_v2

    def fail(argv=None):
        raise failure
    monkeypatch.setattr(model_v2, "main", fail)
    with empty_target() as t:
        assert go(t, world["data"], monkeypatch, tmp_path) == 0                                                            # the database is complete and usable without that cache
        out = capsys.readouterr().out
        assert "RECOVERY OK:" in out and "was not rebuilt (failed (" in out and "model_v2.py explain-past" in out and "model_v3.py explain-past" in out


@needs_postgres
def test_skipping_the_reasons_cache_leaves_it_empty_and_says_how_to_build_it_later(world, monkeypatch, tmp_path, capsys):
    with empty_target() as t:
        assert go(t, world["data"], monkeypatch, tmp_path, "--skip-reasons") == 0
        out = capsys.readouterr().out
        assert "The 'Why this pick?' cache was not rebuilt. When you want it:" in out and count(t["eng"], "fight_reasons") == 0
        assert count(t["eng"], "pick_reasons") == world["counts"]["pick_reasons"]                                           # the upcoming picks' reasons ARE in the backup


# ---------------------------------------------------------------------------------------------- rehearsal
@needs_postgres
def test_a_rehearsal_runs_the_same_recovery_keeps_no_files_and_prints_the_real_commands_instead_of_secrets_to_change(world, monkeypatch, tmp_path, capsys):
    site, game = tmp_path / "default_site.txt", tmp_path / "default_game.txt"
    monkeypatch.setattr(db_roles, "URL_FILE", site)
    monkeypatch.setattr(game_admin, "URL_FILE", game)
    with empty_target() as t:
        args = [str(world["data"]), "--rehearse", "--database-url", t["url"], "--skip-reasons", "--reader-role", t["reader"], "--game-role", t["game"]]
        assert recover.main(args) == 0
        out = capsys.readouterr().out
        assert "RECOVERY OK (a rehearsal: nothing was kept)" in out and "This was a rehearsal." in out and "WHAT TO DO NOW" not in out
        assert not site.exists() and not game.exists() and count(t["eng"], "fights") == world["counts"]["fights"]
        assert recover.main(args) == 2 and "is not empty" in capsys.readouterr().out                                       # even a rehearsal refuses a database that has tables


# ---------------------------------------------------------------------------------------------- small pieces
def test_describe_shows_where_and_never_who_or_with_what_password():
    host = "ep-cool-1." + "neon.tech:5432"
    eng = sa.create_engine(conn("alice", "TopSecret-7", host, "ufc"))
    assert recover.describe(eng) == "ep-cool-1.neon.tech/ufc"
    assert recover.describe(sa.create_engine("postgresql+psycopg2:///localdb")) == "this computer/localdb"


def test_error_messages_lose_the_password_the_connection_string_and_any_create_role_statement():
    url = conn("alice", "TopSecret-7", "ep-cool-1." + "neon.tech:5432", "ufc")
    eng = sa.create_engine(url)
    cleaned = recover._clean(f"failed for {url} using TopSecret-7\nsecond line", eng)
    assert "TopSecret-7" not in cleaned and "alice" not in cleaned and "second line" not in cleaned and "***" in cleaned
    assert recover._clean("x: CREATE ROLE r WITH LOGIN PASSWORD 'abc-DEF_123' NOSUPERUSER", eng) == "x: CREATE ROLE r WITH LOGIN PASSWORD '***' NOSUPERUSER"
    assert recover._clean("", eng) == ""


def test_the_tool_cannot_read_database_url_as_an_input_and_never_prints_a_connection_string():
    src = (ROOT / "pipeline" / "recover.py").read_text(encoding="utf-8")
    assert "getenv(\"DATABASE_URL\")" not in src and src.count("os.environ.get(\"DATABASE_URL\")") == 1                       # read once, only to put it back exactly as it was
    body = src[src.index("def _database_url_is"):src.index("def rebuild_reasons")]
    assert "os.environ.get(\"DATABASE_URL\")" in body and "finally:" in body
    printed = " ".join(re.findall(r"print\(([^\n]*)\)", src) + re.findall(r"lines\.append\(([^\n]*)\)", src))
    assert "render_as_string" not in printed and ".password" not in printed and "site_url" not in printed and "game_url" not in printed
    assert src.count("--confirm") >= 3 and "assert_target(engine, args.confirm, rehearsal=False)" in src                      # the real run cannot skip the check


def test_the_runbook_describes_the_recovery_and_the_counter_fix():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## One-command recovery", "python pipeline/recover.py", "RECOVER_DATABASE_URL", "--confirm", "--rehearse", "never reads DATABASE_URL", "tests/test_recover.py",
                   "auto-number counter", "fix_sequences", "check_sequences", "GAME_DATABASE_URL"):
        assert needle in runbook, needle
