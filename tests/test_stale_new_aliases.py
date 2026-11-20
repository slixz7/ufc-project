"""
The watchdog's check for a debut alias that went stale: a line `Name,NEW` in upcoming/aliases.csv says the fighter has no UFC history, which stops being true the day his fight reaches the data.
The rules are tested on plain tables of fighters; the query runs on a real Postgres with hand-built rows; the step runs through the daily job's own command.
"""
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_update  # noqa: E402
import upcoming  # noqa: E402
import watchdog as wd  # noqa: E402
from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 11, 25)


def fighters(*rows):
    """rows: (name, n_fights, last_fight, weight_class). The same table upcoming.fighter_index returns."""
    df = pd.DataFrame([{"fighter_url": f"u{i}", "fighter_name": n, "n_fights": k, "last_fight": d, "weight_class": w} for i, (n, k, d, w) in enumerate(rows)])
    df["norm"] = df.fighter_name.map(upcoming.norm)
    return df


IDX = fighters(("Carlos Calderon", 1, date(2026, 11, 7), "Featherweight"), ("Bruno Silva", 11, date(2026, 6, 6), "Flyweight"), ("Bruno Silva", 11, date(2025, 3, 1), "Middleweight"),
               ("Lorenz Larkin", 20, date(2022, 1, 1), "Welterweight"))


def aliases(**pairs):
    return {upcoming.norm(k.replace("_", " ")): v for k, v in pairs.items()}


# ---------------------------------------------------------------------------------------------- the rule
def test_a_new_line_whose_name_is_now_one_fighter_in_the_data_is_stale():
    total, stale = wd.stale_new_aliases(aliases(Carlos_Calderon="NEW"), IDX)
    assert total == 1 and stale == [{"name": "Carlos Calderon", "fights": 1, "last_fight": date(2026, 11, 7), "weight_class": "Featherweight"}]


def test_a_new_line_for_a_name_not_in_the_data_is_still_correct():
    assert wd.stale_new_aliases(aliases(Alik_Lorenz="NEW"), IDX) == (1, [])


def test_a_similar_name_is_not_the_same_fighter():
    assert wd.stale_new_aliases(aliases(Alik_Lorenz="NEW", Lorenz_Larkin_Jr="NEW"), IDX) == (2, [])


def test_a_new_line_for_a_name_two_fighters_share_is_a_deliberate_choice_and_is_left_alone():
    assert wd.stale_new_aliases(aliases(Bruno_Silva="NEW"), IDX) == (1, [])


def test_only_new_lines_are_checked_never_a_real_alias_or_a_url():
    got = wd.stale_new_aliases({"carlos calderon": "Carlos Calderon", "bruno silva": "http://ufcstats.com/fighter-details/294aa73dbf37d281"}, IDX)
    assert got == (0, [])


@pytest.mark.parametrize("target", ["NEW", "new", " New ", "NEW\t"])
def test_the_marker_is_found_whatever_its_case_or_spacing(target):
    assert wd.stale_new_aliases({"carlos calderon": target}, IDX)[0] == 1


def test_names_are_compared_the_way_the_daily_job_compares_them():
    idx = fighters(("Uroš Medić", 9, date(2026, 11, 14), "Welterweight"), ("Jose Souza", 5, date(2026, 5, 2), "Lightweight"))
    total, stale = wd.stale_new_aliases({upcoming.norm("Uros Medic"): "NEW"}, idx)                # accents and case do not matter
    assert total == 1 and [s["name"] for s in stale] == ["Uroš Medić"]


def test_stale_ones_come_back_in_a_steady_order():
    idx = fighters(("Zed Last", 1, date(2026, 1, 1), "Lightweight"), ("Amy First", 2, date(2026, 2, 1), "Flyweight"))
    _, stale = wd.stale_new_aliases({"zed last": "NEW", "amy first": "NEW"}, idx)
    assert [s["name"] for s in stale] == ["Amy First", "Zed Last"]


# ---------------------------------------------------------------------------------------------- the words
def test_nothing_to_check_says_nothing_and_a_healthy_list_says_one_line():
    assert wd.stale_new_lines(0, []) == []
    assert wd.stale_new_lines(2, []) == ["Debut aliases (NEW) in upcoming/aliases.csv: 2, none of them in the data yet (still correct)"]
    assert wd.stale_new_problem([]) is None


def test_a_stale_list_names_each_fighter_and_what_the_data_holds():
    _, stale = wd.stale_new_aliases(aliases(Carlos_Calderon="NEW", Alik_Lorenz="NEW"), IDX)
    lines = wd.stale_new_lines(2, stale)
    assert lines[0] == "Debut aliases (NEW) in upcoming/aliases.csv: 2, of which 1 now match a fighter in the data and should be removed"
    assert lines[1] == "  Carlos Calderon: 1 UFC fight in the data, last fought Nov 7, 2026" and len(lines) == 2


def test_the_problem_says_what_to_do_and_names_the_exception_for_a_different_person_with_the_same_name():
    _, stale = wd.stale_new_aliases(aliases(Carlos_Calderon="NEW"), IDX)
    text = wd.stale_new_problem(stale)
    assert text.startswith("upcoming/aliases.csv still marks Carlos Calderon as a UFC debut ('NEW')")
    assert "delete that line from upcoming/aliases.csv and push" in text and "If it is a different person who only shares the name, keep the line" in text


def test_the_problem_names_only_the_first_three_and_uses_the_plural():
    idx = fighters(*[(f"Fighter {c}", 1, date(2026, 11, 7), "Lightweight") for c in "ABCDE"])
    _, stale = wd.stale_new_aliases({upcoming.norm(f"Fighter {c}"): "NEW" for c in "ABCDE"}, idx)
    text = wd.stale_new_problem(stale)
    assert "Fighter A, Fighter B, Fighter C..." in text and "Fighter D" not in text and "as UFC debuts" in text and "those lines" in text
    assert "of which 5 now match a fighter" in wd.stale_new_lines(5, stale)[0]


def test_a_long_list_is_capped_in_the_report_like_the_other_lists():
    idx = fighters(*[(f"Fighter {c}", 1, date(2026, 11, 7), "Lightweight") for c in "ABCDEFG"])
    _, stale = wd.stale_new_aliases({upcoming.norm(f"Fighter {c}"): "NEW" for c in "ABCDEFG"}, idx)
    lines = wd.stale_new_lines(7, stale)
    assert len(lines) == 1 + wd.NO_RESULT_SHOWN + 1 and lines[-1] == "  and 2 more"


# ---------------------------------------------------------------------------------------------- the real query
@contextmanager
def fresh_db(with_tables=True):
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"sn_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name)
    eng = sa.create_engine(url)
    if with_tables:
        with eng.begin() as c:
            c.exec_driver_sql("CREATE TABLE fights (fight_url TEXT PRIMARY KEY, event_date DATE, weight_class TEXT)")
            c.exec_driver_sql("CREATE TABLE fighter_fights (fight_url TEXT, fighter_url TEXT, fighter_name TEXT)")
            c.exec_driver_sql("CREATE TABLE upcoming_picks_v2 (bout_id TEXT PRIMARY KEY, event_name TEXT, event_date DATE, status TEXT, graded_at TIMESTAMPTZ, fighter_a TEXT, fighter_b TEXT)")
            c.exec_driver_sql("INSERT INTO fights VALUES ('f1', '2026-11-07', 'Featherweight'), ('f2', '2026-10-24', 'Welterweight')")
            c.exec_driver_sql("INSERT INTO fighter_fights VALUES ('f1', 'u-cal', 'Carlos Calderon'), ('f1', 'u-bas', 'Austin Bashi'), ('f2', 'u-cho', 'Someone Else')")
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def write_aliases(tmp_path, *lines):
    p = tmp_path / "aliases.csv"
    p.write_text("card_name,dataset_name\n" + "".join(l + "\n" for l in lines), encoding="utf-8")
    return p


@needs_postgres
def test_the_check_reads_the_real_tables_and_finds_the_fighter_whose_fight_has_arrived(tmp_path):
    path = write_aliases(tmp_path, "Carlos Calderon,NEW", "Alik Lorenz,NEW", "Cam Rowston X,Cam Rowston")
    with fresh_db() as eng:
        total, stale = wd.new_alias_check(eng, path)
    assert total == 2 and [(s["name"], s["fights"], s["last_fight"], s["weight_class"]) for s in stale] == [("Carlos Calderon", 1, date(2026, 11, 7), "Featherweight")]


@needs_postgres
def test_a_database_without_the_fight_tables_or_a_missing_file_means_nothing_to_check_not_an_error(tmp_path):
    path = write_aliases(tmp_path, "Carlos Calderon,NEW")
    with fresh_db(with_tables=False) as eng:
        assert wd.new_alias_check(eng, path) == (0, [])
    with fresh_db() as eng:
        assert wd.new_alias_check(eng, tmp_path / "nope.csv") == (0, [])


@needs_postgres
def test_the_step_reports_a_stale_alias_and_raises_a_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "ALIASES_PATH", write_aliases(tmp_path, "Carlos Calderon,NEW", "Alik Lorenz,NEW"))
    monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
    with fresh_db() as eng:
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
    lines = rep.sections[0][1]
    assert "Debut aliases (NEW) in upcoming/aliases.csv: 2, of which 1 now match a fighter in the data and should be removed" in lines
    assert "  Carlos Calderon: 1 UFC fight in the data, last fought Nov 7, 2026" in lines
    assert len(rep.attention) == 1 and rep.attention[0].startswith("upcoming/aliases.csv still marks Carlos Calderon as a UFC debut")


@needs_postgres
def test_the_step_with_only_correct_debut_aliases_raises_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "ALIASES_PATH", write_aliases(tmp_path, "Alik Lorenz,NEW"))
    monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
    with fresh_db() as eng:
        rep = Report()
        wd.step_watchdog(eng, TODAY, rep)
    assert rep.attention == [] and "Debut aliases (NEW) in upcoming/aliases.csv: 1, none of them in the data yet (still correct)" in rep.sections[0][1]


def test_a_check_that_cannot_run_is_reported_and_never_fails_the_run(monkeypatch):
    class Boom(Exception):
        pass

    def broken(*_a, **_k):
        raise Boom("the table is locked")

    monkeypatch.setattr(wd, "newest_result", lambda e: None)
    monkeypatch.setattr(wd, "stale_events", lambda e, t: [])
    monkeypatch.setattr(wd, "database_size", lambda e: (1024, []))
    monkeypatch.setattr(wd, "no_result_picks", lambda e, t: [])
    monkeypatch.setattr(wd, "new_alias_check", broken)
    rep = Report()
    wd.step_watchdog(None, TODAY, rep)
    assert rep.attention == [] and "The debut-alias check could not run (Boom)." in rep.sections[0][1]
    assert "locked" not in " ".join(rep.sections[0][1])                       # the text of an error is never echoed, only its class


@needs_postgres
def test_the_daily_jobs_own_command_fails_the_run_for_a_stale_alias(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wd, "ALIASES_PATH", write_aliases(tmp_path, "Carlos Calderon,NEW"))
    monkeypatch.setenv("DB_SIZE_LIMIT_MB", "100000")
    with fresh_db() as eng:
        url = eng.url.render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL", url)
        code = auto_update.main(["watchdog"])
    out = capsys.readouterr().out
    assert code != 0 and "Needs your attention" in out and "still marks Carlos Calderon as a UFC debut" in out


# ---------------------------------------------------------------------------------------------- the repository's own list and the documents
def test_every_alias_line_in_the_repository_is_well_formed_and_the_marker_is_exactly_new():
    df = pd.read_csv(ROOT / "upcoming" / "aliases.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == ["card_name", "dataset_name"]
    for r in df.itertuples():
        assert r.card_name.strip() and r.dataset_name.strip()
        if r.dataset_name.strip().lower() == "new":
            assert r.dataset_name == "NEW"                                      # the one spelling the documents use


def test_the_runbook_describes_the_check_and_what_to_do_about_it():
    text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "A debut alias that went stale" in text and "delete that line from `upcoming/aliases.csv`" in text
