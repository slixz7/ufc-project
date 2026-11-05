"""
Auto-number counters after a restore. A restore loads rows with their OLD ids, but an auto-number counter (today only odds_snapshots.id) starts again at 1, so the first new
row would collide with a restored one: the table matches the backup row for row and the daily job's next insert still fails. These tests show the failure, the fix, and that the
weekly restore drill now checks for it.
"""
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_odds  # noqa: E402
import backup  # noqa: E402
import restore_drill as rd  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")


@contextmanager
def fresh_db():
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"seq_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def run(eng, sql, **p):
    with eng.begin() as c:
        result = c.execute(sa.text(sql), p)
        return result.scalar() if result.returns_rows else None


@needs_postgres
def test_a_restored_table_whose_counter_was_not_moved_fails_on_the_next_insert_and_the_check_says_so():
    with fresh_db() as eng:
        run(eng, "CREATE TABLE t (id BIGSERIAL PRIMARY KEY, v TEXT)")
        run(eng, "INSERT INTO t (id, v) VALUES (1, 'a'), (5, 'b'), (9, 'c')")                                               # what a restore does: rows with their old ids
        assert backup.check_sequences(eng) == ["t.id: the next auto-number would be 1 but the table already holds id 9, so the next insert would fail with a duplicate key"]
        with pytest.raises(sa.exc.IntegrityError):
            run(eng, "INSERT INTO t (v) VALUES ('new')")                                                                    # the failure the daily job would have hit


@needs_postgres
def test_fixing_moves_the_counter_past_the_largest_id_and_the_next_insert_works():
    with fresh_db() as eng:
        run(eng, "CREATE TABLE t (id BIGSERIAL PRIMARY KEY, v TEXT)")
        run(eng, "INSERT INTO t (id, v) VALUES (1, 'a'), (5, 'b'), (9, 'c')")
        assert backup.fix_sequences(eng) == ["t.id: the next id is 10"]
        assert backup.check_sequences(eng) == []
        assert run(eng, "INSERT INTO t (v) VALUES ('new') RETURNING id") == 10


@needs_postgres
def test_fixing_twice_changes_nothing_and_an_empty_table_starts_at_one():
    with fresh_db() as eng:
        run(eng, "CREATE TABLE t (id BIGSERIAL PRIMARY KEY, v TEXT)")
        assert backup.fix_sequences(eng) == ["t.id: the next id is 1"] and backup.check_sequences(eng) == []
        assert run(eng, "INSERT INTO t (v) VALUES ('first') RETURNING id") == 1
        assert backup.fix_sequences(eng) == ["t.id: the next id is 2"] and backup.fix_sequences(eng) == ["t.id: the next id is 2"]   # repeating it moves nothing
        assert run(eng, "INSERT INTO t (v) VALUES ('second') RETURNING id") == 2


@needs_postgres
def test_a_very_large_id_is_handled():
    with fresh_db() as eng:
        run(eng, "CREATE TABLE t (id BIGSERIAL PRIMARY KEY, v TEXT)")
        run(eng, "INSERT INTO t (id, v) VALUES (1099511627776, 'big')")                                                     # 2 to the 40th
        backup.fix_sequences(eng)
        assert run(eng, "INSERT INTO t (v) VALUES ('new') RETURNING id") == 1099511627777


@needs_postgres
def test_naming_a_table_fixes_only_that_table_and_tables_without_counters_are_left_alone():
    with fresh_db() as eng:
        for t in ("t1", "t2"):
            run(eng, f"CREATE TABLE {t} (id BIGSERIAL PRIMARY KEY, v TEXT)")
            run(eng, f"INSERT INTO {t} (id, v) VALUES (7, 'x')")
        run(eng, "CREATE TABLE plain (id BIGINT PRIMARY KEY, v TEXT)")
        run(eng, "INSERT INTO plain (id, v) VALUES (3, 'y')")
        assert backup.fix_sequences(eng, "t1") == ["t1.id: the next id is 8"] and backup.fix_sequences(eng, "plain") == []
        assert [p.split(":")[0] for p in backup.check_sequences(eng)] == ["t2.id"]                                          # t2 is still behind
        assert backup.fix_sequences(eng) == ["t1.id: the next id is 8", "t2.id: the next id is 8"] and backup.check_sequences(eng) == []


@needs_postgres
def test_a_database_without_counters_has_nothing_to_fix():
    with fresh_db() as eng:
        run(eng, "CREATE TABLE plain (id BIGINT PRIMARY KEY)")
        assert backup.serial_columns(eng) == [] and backup.fix_sequences(eng) == [] and backup.check_sequences(eng) == []


@needs_postgres
def test_restoring_the_real_odds_history_table_leaves_it_ready_for_the_daily_jobs_next_snapshot(tmp_path):
    with fresh_db() as source, fresh_db() as target:
        auto_odds.ensure_snapshot_tables(source)
        run(source, "INSERT INTO odds_snapshots (id, bout_id, n_books) VALUES (11, 'a', 4), (12, 'b', 5), (40, 'c', 6)")
        backup.dump(source, tmp_path / "data")
        auto_odds.ensure_snapshot_tables(target)
        assert backup.restore_table(target, tmp_path / "data", "odds_snapshots") == 3
        assert backup.check_sequences(target) == []
        assert run(target, "INSERT INTO odds_snapshots (bout_id, n_books) VALUES ('new', 3) RETURNING id") == 41                # the next snapshot, not a collision


@needs_postgres
def test_the_weekly_drill_now_notices_a_restore_that_leaves_the_counters_behind(tmp_path, monkeypatch):
    with fresh_db() as source, fresh_db() as scratch:
        auto_odds.ensure_snapshot_tables(source)
        run(source, "INSERT INTO odds_snapshots (id, bout_id, n_books) VALUES (21, 'a', 4), (22, 'b', 5)")
        backup.dump(source, tmp_path / "data")
        good = rd.run_drill(tmp_path / "data", scratch, log=lambda *a: None)
        assert good["ok"] is True and good["sequence_problems"] == [] and "RESTORE OK" in rd.format_report(good)
    with fresh_db() as scratch2:
        monkeypatch.setattr(backup, "fix_sequences", lambda *a, **k: [])                                                    # the old behaviour: rows restored, counters left alone
        bad = rd.run_drill(tmp_path / "data", scratch2, log=lambda *a: None)
        assert bad["ok"] is False and len(bad["sequence_problems"]) == 1 and all(not e["problems"] for e in bad["tables"])   # every table matches the backup, and it is still a failure
        report = rd.format_report(bad)
        assert "WHAT WOULD BREAK THE NEXT INSERT:" in report and "odds_snapshots.id" in report and "the next new row would be refused" in report and "RESTORE OK" not in report


def test_the_report_text_for_a_counter_problem_and_the_older_result_shape():
    table = {"table": "t", "rows": 2, "restored": 2, "problems": [], "legacy": None}
    broken = {"ok": False, "stopped": [], "tables": [table], "sequence_problems": ["t.id: the next auto-number would be 1 but the table already holds id 9, so the next insert would fail with a duplicate key"]}
    text = rd.format_report(broken)
    assert "WHAT WOULD BREAK THE NEXT INSERT:\n  t.id:" in text and text.endswith("so the next new row would be refused.")
    old_shape = {"ok": True, "stopped": [], "tables": [table]}                                                              # a result made without the new key still formats
    assert "RESTORE OK: all 1 tables" in rd.format_report(old_shape)


def test_a_database_that_is_not_postgres_has_no_counters_and_every_function_is_a_quiet_no_op():
    eng = sa.create_engine("sqlite://")
    assert backup.serial_columns(eng) == [] and backup.fix_sequences(eng) == [] and backup.check_sequences(eng) == []


def test_the_documentation_describes_the_check():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "auto-number counter" in runbook and "fix_sequences" in runbook and "check_sequences" in runbook
