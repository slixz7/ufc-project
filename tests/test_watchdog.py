"""
pipeline/watchdog.py: the checks for quiet failures that live in the database: results that never arrived, and a database that is filling up. The maths and wording are tested directly;
the queries run on a real Postgres with hand-built rows, and the step runs through the daily job's own command.
"""
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_update  # noqa: E402
import watchdog as wd  # noqa: E402
from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 11, 5)
MB = 1024 * 1024


@contextmanager
def fresh_db(monkeypatch=None):
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"wd_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE fights (fight_url TEXT PRIMARY KEY, event_date DATE)")
        c.exec_driver_sql("CREATE TABLE upcoming_picks_v2 (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, status TEXT, graded_at TIMESTAMPTZ, fighter_a TEXT, fighter_b TEXT)")
    try:
        yield eng, url.render_as_string(hide_password=False)
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def picks(eng, rows):
    with eng.begin() as c:
        for i, (event, when, status, graded) in enumerate(rows):
            c.execute(sa.text("INSERT INTO upcoming_picks_v2 VALUES (:b, :e, :d, :s, :g)"), {"b": f"b{i}", "e": event, "d": when, "s": status, "g": "2026-10-01 00:00+00" if graded else None})


# ---------------------------------------------------------------------------------------------- the size maths
@pytest.mark.parametrize("raw, expected, note", [(None, 500.0, False), ("", 500.0, False), ("   ", 500.0, False), ("250", 250.0, False), (" 1024 ", 1024.0, False), ("0.5", 0.5, False),
                                                 ("0", 500.0, True), ("-5", 500.0, True), ("abc", 500.0, True), ("nan", 500.0, True), ("inf", 500.0, True), ("5e2", 500.0, False)])
def test_the_size_limit_comes_from_the_variable_or_falls_back_to_500_with_a_note_when_it_was_unusable(raw, expected, note):
    value, text = wd.limit_mb(raw)
    assert value == expected and (text is not None) == note
    if note:
        assert "is not a positive number, so 500 MB was used" in text


def test_sizes_are_shown_in_the_unit_a_person_reads():
    assert wd._mb(0) == "0.0" and wd._mb(1.5 * MB) == "1.5" and wd._mb(9.9 * MB) == "9.9" and wd._mb(10 * MB) == "10" and wd._mb(87.4 * MB) == "87" and wd._mb(1536 * MB) == "1,536"


def test_the_report_states_the_size_and_raises_a_problem_from_eighty_percent():
    lines, problem = wd.size_report(87 * MB, 500, [("odds_snapshots", 30 * MB), ("fight_odds", 5 * MB)])
    assert lines == ["Database size: 87 MB of 500 MB (17%)", "Largest tables: odds_snapshots 30 MB, fight_odds 5.0 MB"] and problem is None
    assert wd.size_report(399 * MB, 500, [])[1] is None                                                                          # 79.8%: still fine
    lines, problem = wd.size_report(400 * MB, 500, [("odds_snapshots", 300 * MB)])
    assert lines[0] == "Database size: 400 MB of 500 MB (80%)" and problem.startswith("The database is 80% full (400 of 500 MB).")
    assert "The biggest tables are odds_snapshots." in problem and "DB_SIZE_LIMIT_MB" in problem and "stops writes" in problem
    assert wd.size_report(10 * MB, 500, [])[0] == ["Database size: 10 MB of 500 MB (2%)"]                                        # no table line when there is no list


def test_the_stale_results_message_names_the_card_the_picks_and_the_days():
    e = {"event_date": date(2026, 10, 25), "event_name": "UFC 321: Someone vs. Other", "picks": 12}
    assert wd.stale_message(e, TODAY) == ("Results for the UFC 321: Someone vs. Other on Oct 25 (12 locked picks) are still not in the database, 11 days after the event. "
                                          "The Kaggle dataset may have stopped updating (kaggle.com/datasets/scarekrow/ufc-data); the live record stops growing until it does.")
    one = wd.stale_message({"event_date": date(2026, 11, 1), "event_name": None, "picks": 1}, TODAY)
    assert one.startswith("Results for the card on Nov 1 (1 locked pick) are") and "4 days after" in one


# ---------------------------------------------------------------------------------------------- the queries, on a real database
@needs_postgres
def test_only_past_events_with_locked_ungraded_picks_are_stale_and_five_days_is_the_boundary():
    with fresh_db() as (eng, url):
        picks(eng, [("old card", date(2026, 10, 25), "locked", False), ("old card", date(2026, 10, 25), "locked", False), ("old card", date(2026, 10, 25), "locked", False),
                    ("edge card", date(2026, 10, 31), "locked", False),                                                         # exactly five days ago: stale
                    ("recent card", date(2026, 11, 2), "locked", False),                                                        # three days: results may simply not be out yet
                    ("graded card", date(2026, 10, 10), "locked", True),                                                        # graded: fine
                    ("cancelled card", date(2026, 10, 12), "cancelled", False),                                                 # cancelled: fine
                    ("future card", date(2026, 11, 20), "locked", False)])
        assert wd.stale_events(eng, TODAY) == [{"event_date": date(2026, 10, 25), "event_name": "old card", "picks": 3}, {"event_date": date(2026, 10, 31), "event_name": "edge card", "picks": 1}]
        assert wd.stale_events(eng, TODAY, days=3)[-1]["event_name"] == "recent card" and wd.stale_events(eng, date(2026, 10, 26)) == []


@needs_postgres
def test_a_database_without_the_picks_table_has_nothing_stale_rather_than_an_error():
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"wd_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=name))
    try:
        assert wd.stale_events(eng, TODAY) == []
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@needs_postgres
def test_the_size_query_reads_the_real_database_and_lists_its_biggest_public_tables_largest_first():
    with fresh_db() as (eng, url):
        with eng.begin() as c:
            c.exec_driver_sql("CREATE TABLE padding (id SERIAL PRIMARY KEY, junk TEXT)")
            c.exec_driver_sql("INSERT INTO padding (junk) SELECT repeat('x', 500) FROM generate_series(1, 20000)")
        total, biggest = wd.database_size(eng)
        assert total > 8 * MB and biggest[0][0] == "padding" and biggest[0][1] > 10 * MB and len(biggest) == 3
        assert [b for _, b in biggest] == sorted((b for _, b in biggest), reverse=True) and {n for n, _ in biggest} <= {"padding", "fights", "upcoming_picks_v2"}


@needs_postgres
def test_the_step_reports_freshness_stale_events_and_size_and_raises_each_problem():
    with fresh_db() as (eng, url):
        with eng.begin() as c:
            c.exec_driver_sql("INSERT INTO fights VALUES ('f1', '2026-10-31'), ('f2', '2026-10-24')")
        picks(eng, [("old card", date(2026, 10, 20), "locked", False)])
        rep = Report()
        os.environ["DB_SIZE_LIMIT_MB"] = "1"                                                                                    # a limit this database is far beyond
        try:
            wd.step_watchdog(eng, TODAY, rep)
        finally:
            del os.environ["DB_SIZE_LIMIT_MB"]
        title, lines = rep.sections[0]
        assert title == "Watchdog" and lines[0] == "Newest results in the database: Oct 31, 2026 (5 days ago)" and lines[1] == "Results: 1 past event(s) still waiting for results"
        assert lines[2].startswith("Database size:") and "of 1 MB" in lines[2]
        assert len(rep.attention) == 2 and rep.attention[0].startswith("Results for the old card on Oct 20 (1 locked pick)") and rep.attention[1].startswith("The database is ")


@needs_postgres
def test_a_healthy_database_raises_nothing_and_says_so(monkeypatch):
    with fresh_db() as (eng, url):
        with eng.begin() as c:
            c.exec_driver_sql("INSERT INTO fights VALUES ('f1', '2026-11-01')")
        picks(eng, [("graded", date(2026, 10, 20), "locked", True), ("soon", date(2026, 11, 14), "locked", False)])
        monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
        assert rep.attention == [] and rep.sections[0][1][:2] == ["Newest results in the database: Nov 1, 2026 (4 days ago)", "Results: every past event's picks are graded"]
        monkeypatch.setenv("DB_SIZE_LIMIT_MB", "lots")
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
        assert rep.attention == [] and any("is not a positive number, so 500 MB was used" in l for l in rep.sections[0][1])      # an unusable variable never breaks the run


@needs_postgres
def test_the_daily_jobs_own_command_runs_the_watchdog_and_a_problem_fails_the_run_like_any_other(monkeypatch, capsys):
    with fresh_db() as (eng, url):
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        assert auto_update.main(["watchdog"]) == 0
        assert "## Watchdog" in capsys.readouterr().out
        picks(eng, [("old card", date(2026, 10, 1), "locked", False)])
        assert auto_update.main(["watchdog", "--today", "2026-11-05"]) == 3                                                    # "needs attention" is exit code 3, which fails the job
        out = capsys.readouterr().out
        assert "## Needs your attention\n- Results for the old card on Oct 1" in out


def test_the_watchdog_is_a_step_of_the_daily_job_between_the_health_check_and_the_readme():
    src = (ROOT / "pipeline" / "auto_update.py").read_text(encoding="utf-8")
    steps = src[src.index("steps = {"):src.index("chosen = ")]
    assert steps.index('"health"') < steps.index('"watchdog"') < steps.index('"readme"') and "watchdog.step_watchdog(engine, today, rep)" in steps
    assert '"health", "watchdog", "readme"' in src and "import watchdog" in src


# ---------------------------------------------------------------------------------------------- picks that ended with no result
def no_result(eng, rows):
    """rows: (event_name, event_date, status, fighter_a, fighter_b)"""
    with eng.begin() as c:
        for i, (event, when, status, a, b) in enumerate(rows):
            c.execute(sa.text("INSERT INTO upcoming_picks_v2 (bout_id, event_name, event_date, status, fighter_a, fighter_b) VALUES (:i, :e, :d, :s, :a, :b)"),
                      {"i": f"nr{uuid.uuid4().hex[:10]}", "e": event, "d": when, "s": status, "a": a, "b": b})


def nr(day, n=0):
    return {"event_name": "UFC X", "event_date": date(2026, 10, day), "fighter_a": f"A{n}", "fighter_b": f"B{n}"}


@needs_postgres
def test_only_no_result_picks_inside_the_window_are_found_oldest_first_and_the_edges_are_exact():
    with fresh_db() as (eng, url):
        no_result(eng, [("later card", date(2026, 11, 1), "no_result", "Late A", "Late B"), ("edge card", date(2026, 10, 22), "no_result", "Edge A", "Edge B"),         # exactly 14 days ago: in
                        ("old card", date(2026, 10, 21), "no_result", "Old A", "Old B"),                                                                              # 15 days ago: out
                        ("locked card", date(2026, 11, 2), "locked", "L A", "L B"), ("called off", date(2026, 11, 2), "cancelled", "C A", "C B"),                    # not no_result: out
                        ("future card", date(2026, 11, 20), "no_result", "F A", "F B")])                                                                              # not happened yet: out
        rows = wd.no_result_picks(eng, TODAY)
        assert [(r["event_name"], r["fighter_a"]) for r in rows] == [("edge card", "Edge A"), ("later card", "Late A")]
        assert wd.no_result_picks(eng, TODAY, days=3) == [] and len(wd.no_result_picks(eng, TODAY, days=15)) == 3


@needs_postgres
def test_a_database_without_the_picks_table_has_no_no_result_picks_rather_than_an_error():
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"wd_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=name))
    try:
        assert wd.no_result_picks(eng, TODAY) == []
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_the_report_lines_say_none_or_name_the_fights_and_cap_the_list():
    assert wd.no_result_lines([]) == ["Picks with no result (last 14 days): none"]
    one = wd.no_result_lines([nr(30, 1)])
    assert one[0].startswith("Picks with no result (last 14 days): 1. A draw, a no contest or a fight that fell through is the usual reason;") and "check them against the real result." in one[0]
    assert one[1:] == ["  UFC X (2026-10-30): A1 vs. B1"]
    many = wd.no_result_lines([nr(20 + i, i) for i in range(7)])
    assert many[0].split(":")[1].strip().startswith("7.") and len(many) == 1 + wd.NO_RESULT_SHOWN + 1 and many[-1] == "  and 2 more"
    assert wd.no_result_lines([{"event_name": None, "event_date": date(2026, 10, 30), "fighter_a": "X", "fighter_b": "Y"}])[1] == "  a card (2026-10-30): X vs. Y"


@pytest.mark.parametrize("n, raised", [(0, False), (1, False), (2, False), (3, True), (4, True), (9, True)])
def test_one_or_two_are_only_reported_and_three_or_more_need_attention(n, raised):
    rows = [nr(10 + i, i) for i in range(n)]
    problem = wd.no_result_problem(rows)
    assert (problem is not None) == raised
    if raised:
        assert problem.startswith(f"{n} picks ended with no result in the last 14 days (A0 vs. B0 (2026-10-10); A1 vs. B1 (2026-10-11); A2 vs. B2 (2026-10-12)")
        assert ("..." in problem) == (n > 3) and "this looks like results not being matched to picks" in problem and "A draw or a no contest is rare" in problem


def test_the_thresholds_are_the_ones_the_documents_state():
    assert (wd.NO_RESULT_DAYS, wd.NO_RESULT_ALERT_AT, wd.NO_RESULT_SHOWN) == (14, 3, 5)


@needs_postgres
def test_the_step_reports_the_picks_after_the_size_lines_and_raises_a_problem_only_from_three(monkeypatch):
    with fresh_db() as (eng, url):
        with eng.begin() as c:
            c.exec_driver_sql("INSERT INTO fights VALUES ('f1', '2026-11-02')")
        monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
        no_result(eng, [("UFC 1", date(2026, 11, 1), "no_result", "Ann", "Bea"), ("UFC 1", date(2026, 11, 1), "no_result", "Cy", "Dee")])
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
        lines = rep.sections[0][1]
        assert rep.attention == [] and lines[2].startswith("Database size:") and lines[4].startswith("Picks with no result (last 14 days): 2.")                      # after the size lines
        assert lines[5:] == ["  UFC 1 (2026-11-01): Ann vs. Bea", "  UFC 1 (2026-11-01): Cy vs. Dee"]
        no_result(eng, [("UFC 1", date(2026, 11, 1), "no_result", "Eve", "Fay")])
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
        assert len(rep.attention) == 1 and rep.attention[0].startswith("3 picks ended with no result in the last 14 days (Ann vs. Bea (2026-11-01); Cy vs. Dee (2026-11-01); Eve vs. Fay (2026-11-01))")


@needs_postgres
def test_the_daily_jobs_own_command_passes_with_two_and_fails_the_run_with_three(monkeypatch, capsys):
    with fresh_db() as (eng, url):
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        no_result(eng, [("UFC 1", date(2026, 11, 1), "no_result", "Ann", "Bea"), ("UFC 1", date(2026, 11, 1), "no_result", "Cy", "Dee")])
        assert auto_update.main(["watchdog", "--today", "2026-11-05"]) == 0
        assert "Picks with no result (last 14 days): 2." in capsys.readouterr().out
        no_result(eng, [("UFC 1", date(2026, 11, 1), "no_result", "Eve", "Fay")])
        assert auto_update.main(["watchdog", "--today", "2026-11-05"]) == 3                                                                                          # "needs attention" is exit code 3
        assert "## Needs your attention\n- 3 picks ended with no result" in capsys.readouterr().out
