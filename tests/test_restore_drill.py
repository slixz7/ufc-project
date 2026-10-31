"""
The restore drill: rebuild the database from a backup into a throwaway database and check it. It must restore a real backup completely, notice every way a
restore can go wrong, never touch anything real, and fail when the project grows a table that the recovery recipe does not know about.
"""
import csv
import os
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo"):
    sys.path.insert(0, str(ROOT / sub))
import backup  # noqa: E402
import build_demo  # noqa: E402
import restore_drill as rd  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 10, 29)
PRODUCTION_LIKE = {"fight_reasons", "demo_meta"}          # what a real backup leaves out (fight_reasons is rebuildable), and a table only the demo has


# ---------------------------------------------------------------------------------------------- no database needed
def test_the_report_says_ok_or_exactly_what_did_not_come_back():
    good = {"ok": True, "stopped": [], "tables": [{"table": "a", "rows": 3, "restored": 3, "problems": []}, {"table": "b", "rows": 0, "restored": 0, "problems": []}]}
    text = rd.format_report(good)
    assert "RESTORE OK: all 2 tables (3 rows)" in text and "PROBLEM" not in text
    with_legacy = {"ok": True, "stopped": [], "tables": good["tables"] + [{"table": "predictions", "rows": 516, "restored": None, "problems": [], "legacy": "an early table"}]}
    text = rd.format_report(with_legacy)
    assert "RESTORE OK: all 2 tables (3 rows)" in text and "legacy: not restored" in text and "Not restored, by design: predictions (516 rows)" in text and "PROBLEM" not in text
    bad = {"ok": False, "stopped": [], "tables": [{"table": "a", "rows": 3, "restored": 1, "problems": ["1 rows came back, the backup has 3"]}, {"table": "b", "rows": 2, "restored": None, "problems": ["no setup step"]}]}
    text = rd.format_report(bad)
    assert "RESTORE FAILED" in text and "RESTORE OK" not in text and "a: 1 rows came back" in text and "b: no setup step" in text and text.count("PROBLEM") == 2
    stopped = rd.format_report({"ok": False, "stopped": ["x: the file does not match the manifest (changed or damaged)"], "tables": []})
    assert "NOT INTACT" in stopped and "nothing was restored" in stopped


def test_the_comparison_catches_missing_rows_and_missing_cells():
    expected = (3, {"a": 3, "b": 1})
    assert rd.compare(expected, (3, {"a": 3, "b": 1})) == []
    assert any("2 rows came back, the backup has 3" in p for p in rd.compare(expected, (2, {"a": 3, "b": 1})))
    assert any("column b: 0 filled-in cells came back, the backup has 1" in p for p in rd.compare(expected, (3, {"a": 3, "b": 0})))      # a blank turned up where a value was
    assert any("column a" in p for p in rd.compare(expected, (3, {"a": 2, "b": 1}))) and any("column b: None" in p for p in rd.compare(expected, (3, {"a": 3})))


def test_the_drill_never_reads_the_real_connection_string():
    src = (ROOT / "pipeline" / "restore_drill.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]                                                                                # the code, not the docstring that explains it
    assert "getenv" not in code and "environ" not in code and "make_engine" not in code and "DATABASE_URL" not in code


def test_a_database_that_is_hosted_or_not_empty_is_refused(tmp_path):
    for host in ("ep-cool-123.us-east-2.aws.neon.tech", "db.abcd.supabase.co", "mydb.cluster-xyz.us-east-1.rds.amazonaws.com"):
        with pytest.raises(SystemExit, match="hosted"):
            rd.assert_safe(sa.create_engine(f"postgresql+psycopg2://user:secret@{host}/neondb"))
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    rd.assert_safe(eng)                                                                                          # an empty database is fine
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE somebody_elses_data (x INT)")
    with pytest.raises(SystemExit, match="not empty"):
        rd.assert_safe(eng)


def test_a_missing_folder_is_explained_not_a_crash(capsys):
    assert rd.main(["/no/such/folder"]) == 2
    assert "is not a folder" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- on a real Postgres, from a real build
def _scratch(admin):
    name = f"rd_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name)
    return name, sa.create_engine(url), url.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A real tiny demo (built by the real pipeline) and a backup of it made by the real backup script, the way a production backup is made."""
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"rdsrc_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    build_demo.build(url, today=TODAY, size="tiny", log=lambda *a: None)
    eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
    full, prod = tmp_path_factory.mktemp("full"), tmp_path_factory.mktemp("prod")
    backup.dump(eng, full, exclude={"fight_reasons"})                                                            # everything, including the demo-only table
    backup.dump(eng, prod, exclude=PRODUCTION_LIKE)                                                              # what a production backup looks like
    counts = {t: int(pd.read_sql(sa.text(f'SELECT COUNT(*) FROM "{t}"'), eng).iloc[0, 0]) for t in backup.table_names(eng, PRODUCTION_LIKE)}
    yield {"eng": eng, "full": full, "prod": prod, "counts": counts, "admin": admin}
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        for r in build_demo.login_names(name):
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


@pytest.fixture
def scratch(source):
    name, eng, url = _scratch(source["admin"])
    yield {"eng": eng, "url": url}
    eng.dispose()
    with source["admin"].connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@needs_postgres
def test_a_real_backup_restores_completely_into_an_empty_database(source, scratch):
    result = rd.run_drill(source["prod"], scratch["eng"], log=lambda *a: None)
    bad = [(e["table"], e["problems"]) for e in result["tables"] if e["problems"]]
    assert result["ok"] and not bad, bad
    got = {e["table"]: e["restored"] for e in result["tables"]}
    assert got == source["counts"]                                                                               # every table, with exactly the rows the source had
    assert got["fights"] > 300 and got["game_picks"] > 40 and got["upcoming_picks_v2"] > 10                      # and the tables that matter most are not empty ones
    with scratch["eng"].connect() as c:                                                                          # the restored data is really there and usable
        assert c.execute(sa.text("SELECT COUNT(*) FROM fights WHERE result IS NOT NULL")).scalar() > 300
        assert c.execute(sa.text("SELECT COUNT(*) FROM game_players")).scalar() == source["counts"]["game_players"]


@needs_postgres
def test_every_table_the_project_creates_has_a_step_in_the_recovery_recipe(source, scratch):
    rd.create_base_tables(scratch["eng"])
    for _, _, step in rd.SCHEMA_STEPS:
        step(scratch["eng"])
    made = set(sa.inspect(scratch["eng"]).get_table_names())
    in_project = set(sa.inspect(source["eng"]).get_table_names()) - PRODUCTION_LIKE
    assert in_project <= made, sorted(in_project - made)                                                          # a new table that the recipe cannot create fails here, until a step is added
    assert {"odds_snapshots", "event_starts", "digest_sent"} <= made                                             # the tables only a real database has (the daily job creates them)


@needs_postgres
def test_a_known_legacy_table_is_kept_in_the_backup_but_does_not_fail_the_drill(source, scratch, tmp_path):
    with source["eng"].begin() as c:
        c.exec_driver_sql("CREATE TABLE predictions (fight_url TEXT, p DOUBLE PRECISION)")
        c.exec_driver_sql("INSERT INTO predictions VALUES ('a', 0.6), ('b', NULL)")
    try:
        backup.dump(source["eng"], tmp_path / "b", exclude=PRODUCTION_LIKE)
    finally:
        with source["eng"].begin() as c:
            c.exec_driver_sql("DROP TABLE predictions")
    assert "predictions" in rd.LEGACY_TABLES and (tmp_path / "b" / "predictions.csv").exists()
    result = rd.run_drill(tmp_path / "b", scratch["eng"], log=lambda *a: None)
    assert result["ok"], [(e["table"], e["problems"]) for e in result["tables"] if e["problems"]]
    legacy = [e for e in result["tables"] if e["legacy"]]
    assert [e["table"] for e in legacy] == ["predictions"] and legacy[0]["rows"] == 2 and legacy[0]["restored"] is None
    assert "predictions" not in sa.inspect(scratch["eng"]).get_table_names()                                       # it was not recreated: nothing uses it
    assert {e["table"]: e["restored"] for e in result["tables"] if not e["legacy"]} == source["counts"]            # and every other table still came back exactly


# What the real database's two base tables look like, read from it on 2026-10-30 (information_schema.columns and pg_indexes): column names in order, with types.
REAL_COLUMNS = {
    "fights": [("fight_url", "text"), ("event_date", "date"), ("event_name", "text"), ("weight_class", "text"), ("title_fight", "boolean"), ("num_rounds", "double precision"),
               ("result", "text"), ("finish_round", "bigint"), ("finish_time", "text")],
    "fighter_fights": [("fight_url", "text"), ("fighter_url", "text"), ("fighter_name", "text"), ("side", "bigint"), ("won", "bigint"), ("sig_land", "bigint"), ("sig_att", "bigint"),
                       ("td_land", "bigint"), ("td_att", "bigint"), ("sub_att", "bigint"), ("kd", "bigint"), ("ctrl_sec", "bigint"), ("dist_att", "double precision"),
                       ("clinch_att", "double precision"), ("ground_att", "double precision"), ("sig_absorbed", "bigint"), ("opp_td_land", "bigint"), ("opp_td_att", "bigint"),
                       ("height_cm", "double precision"), ("reach_cm", "double precision"), ("stance", "text"), ("dob", "date")]}
REAL_INDEXES = {"fights": {("ix_fights_date", ("event_date",), False)},                                                # (name, columns, unique); fights_pkey is the primary key
                "fighter_fights": {("ix_ff_fighter", ("fighter_url",), False), ("ix_ff_fight", ("fight_url",), False), ("ux_ff_fight_fighter", ("fight_url", "fighter_url"), True)}}
INTEGER_COLUMNS = {t: [c for c, ty in cols if ty == "bigint"] for t, cols in REAL_COLUMNS.items()}


def _layout(eng, table):
    q = sa.text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = :t AND table_schema = 'public' ORDER BY ordinal_position")
    with eng.connect() as c:
        cols = [tuple(r) for r in c.execute(q, {"t": table})]
    insp = sa.inspect(eng)
    idx = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)}
    return cols, idx, insp.get_pk_constraint(table)["constrained_columns"]


@needs_postgres
def test_the_written_out_base_tables_are_the_real_ones_and_match_what_the_demo_builds(source, scratch):
    rd.create_base_tables(scratch["eng"])
    for t in rd.BASE_TABLES:
        cols, idx, pk = _layout(scratch["eng"], t)
        assert cols == REAL_COLUMNS[t], t                                                                            # names, order and types as in the real database
        assert idx == REAL_INDEXES[t], t                                                                             # and its indexes
        assert pk == (["fight_url"] if t == "fights" else []), t
        assert _layout(source["eng"], t) == (cols, idx, pk), t                                                       # the demo builds the very same tables, so it exercises the real types


@needs_postgres
def test_an_old_style_backup_with_whole_numbers_written_as_decimals_restores_into_the_real_bigint_columns(source, scratch, tmp_path):
    """Every backup made before the fix writes a whole number in a column that also has blanks as 12.0. The real count columns are bigint, which refuses that."""
    import pandas as pd
    shutil.copytree(source["prod"], tmp_path / "old")
    changed = 0
    for table, cols in INTEGER_COLUMNS.items():
        f = tmp_path / "old" / f"{table}.csv"
        df = pd.read_csv(f, dtype=str, keep_default_na=False)
        for c in cols:
            df[c] = df[c].map(lambda v: v + ".0" if v and "." not in v else v)
            changed += int((df[c].str.endswith(".0")).sum())
        df.to_csv(f, index=False, lineterminator="\n")
    manifest = list(csv.DictReader(open(tmp_path / "old" / backup.MANIFEST, encoding="utf-8")))
    for r in manifest:
        r["sha256"] = backup._sha256(tmp_path / "old" / f"{r['table']}.csv")                                          # the manifest is rewritten to match: this is a valid old-style backup
    with open(tmp_path / "old" / backup.MANIFEST, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(manifest[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(manifest)
    assert changed > 1000 and backup.verify(tmp_path / "old") == []                                                  # a valid backup, in the old style
    assert (tmp_path / "old" / "fighter_fights.csv").read_text(encoding="utf-8").count(".0,") > 100                   # the file really does contain decimals where integers belong
    result = rd.run_drill(tmp_path / "old", scratch["eng"], log=lambda *a: None)
    assert result["ok"], [(e["table"], e["problems"]) for e in result["tables"] if e["problems"]]
    assert {e["table"]: e["restored"] for e in result["tables"]} == source["counts"]
    with scratch["eng"].connect() as c:
        assert c.execute(sa.text("SELECT pg_typeof(sig_land)::text FROM fighter_fights LIMIT 1")).scalar() == "bigint"            # and they are bigint in the restored database


@needs_postgres
def test_a_table_the_recipe_does_not_know_is_reported_as_a_gap(source, scratch, tmp_path):
    shutil.copytree(source["full"], tmp_path / "b")                                                              # the backup that includes the demo-only table
    result = rd.run_drill(tmp_path / "b", scratch["eng"], log=lambda *a: None)
    assert not result["ok"]
    gap = [e for e in result["tables"] if e["problems"]]
    assert [e["table"] for e in gap] == ["demo_meta"] and "no setup step creates this table" in gap[0]["problems"][0]
    assert "RESTORE FAILED" in rd.format_report(result) and "demo_meta" in rd.format_report(result)
    assert all(e["restored"] is not None for e in result["tables"] if e["table"] != "demo_meta")                  # and everything else still came back


@needs_postgres
def test_a_damaged_backup_stops_before_anything_is_created(source, scratch, tmp_path):
    shutil.copytree(source["prod"], tmp_path / "b")
    f = tmp_path / "b" / "game_picks.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("\n", "\n#", 1), encoding="utf-8")                          # one stray character in one file
    result = rd.run_drill(tmp_path / "b", scratch["eng"], log=lambda *a: None)
    assert result["stopped"] and "game_picks" in result["stopped"][0] and not result["ok"]
    assert sa.inspect(scratch["eng"]).get_table_names() == []                                                    # nothing was built or loaded
    (tmp_path / "b" / backup.MANIFEST).unlink()
    assert "MANIFEST.csv is missing" in rd.run_drill(tmp_path / "b", scratch["eng"], log=lambda *a: None)["stopped"][0]


@needs_postgres
def test_a_restore_that_quietly_loses_rows_or_cells_is_noticed(source, scratch, monkeypatch):
    real = backup.restore_table

    def lossy(engine, directory, table):
        n = real(engine, directory, table)
        if table == "game_picks":
            with engine.begin() as c:
                c.execute(sa.text("DELETE FROM game_picks WHERE ctid IN (SELECT ctid FROM game_picks LIMIT 5)"))                      # five rows go missing
        if table == "bout_results":
            with engine.begin() as c:
                c.execute(sa.text("UPDATE bout_results SET method = NULL"))                                                          # a whole column comes back blank
        return n
    monkeypatch.setattr(backup, "restore_table", lossy)
    result = rd.run_drill(source["prod"], scratch["eng"], log=lambda *a: None)
    by = {e["table"]: e["problems"] for e in result["tables"]}
    assert not result["ok"] and any("rows came back" in p for p in by["game_picks"]) and any("column method" in p for p in by["bout_results"])
    assert [t for t, p in by.items() if p] == ["bout_results", "game_picks"]                                     # only the two damaged tables are named


@needs_postgres
def test_it_runs_from_the_command_line_against_an_empty_database_of_your_own(source, scratch, capsys):
    assert rd.main([str(source["prod"]), "--database-url", scratch["url"]]) == 0
    out = capsys.readouterr().out
    assert "RESTORE OK" in out and "game_picks" in out and "demo_meta" not in out
    with pytest.raises(SystemExit, match="not empty"):                                                           # and a second run on the now-full database is refused: a drill never writes over anything
        rd.main([str(source["prod"]), "--database-url", scratch["url"]])


# ---------------------------------------------------------------------------------------------- a bug the drill found in the backup itself
def _int_db(path):
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, f REAL, s TEXT)")
    return eng


def test_an_integer_column_with_blanks_is_backed_up_as_integers_not_decimals(tmp_path):
    eng = _int_db(tmp_path / "a.db")
    with eng.begin() as c:
        c.exec_driver_sql("INSERT INTO t VALUES (1, 1, 1.0, 'a'), (2, NULL, 2.5, NULL), (3, 7, NULL, 'c'), (4, 12345678901, 3.0, 'd')")
    backup.dump(eng, tmp_path / "out")
    df = pd.read_csv(tmp_path / "out" / "t.csv", dtype=str, keep_default_na=False)
    assert df.n.tolist() == ["1", "", "7", "12345678901"]                                                         # whole numbers, a blank left blank, nothing rounded
    assert df.f.tolist() == ["1.0", "2.5", "", "3.0"]                                                             # real numbers keep their decimals
    assert backup.verify(tmp_path / "out") == []


@needs_postgres
def test_a_backup_made_before_the_fix_still_restores_on_postgres(scratch, tmp_path):
    """Postgres is strict where SQLite is not: '1.0' is refused by an integer column. This is the case that broke the first restore drill."""
    out = tmp_path / "old"
    out.mkdir()
    (out / "t.csv").write_text("id,n,f,s\n1,1.0,1.0,a\n2,,2.5,\n3,7.0,,c\n", encoding="utf-8")                  # how every backup so far wrote it
    with open(out / backup.MANIFEST, "w", encoding="utf-8", newline="") as fh:
        fh.write("table,rows,sha256,columns\n" + f"t,3,{backup._sha256(out / 't.csv')},id|n|f|s\n")
    with scratch["eng"].begin() as c:
        c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, f DOUBLE PRECISION, s TEXT)")
    assert backup.restore_table(scratch["eng"], out, "t") == 3
    back = pd.read_sql(sa.text("SELECT * FROM t ORDER BY id"), scratch["eng"])
    assert back.n.tolist()[0] == 1 and pd.isna(back.n.tolist()[1]) and back.n.tolist()[2] == 7 and back.f.tolist()[:2] == [1.0, 2.5]


def test_a_backup_made_before_the_fix_still_restores_into_an_integer_column(tmp_path):
    out = tmp_path / "old"
    out.mkdir()
    (out / "t.csv").write_text("id,n,f,s\n1,1.0,1.0,a\n2,,2.5,\n3,7.0,,c\n", encoding="utf-8")                  # how every backup so far wrote it
    with open(out / backup.MANIFEST, "w", encoding="utf-8", newline="") as fh:
        fh.write("table,rows,sha256,columns\n" + f"t,3,{backup._sha256(out / 't.csv')},id|n|f|s\n")
    dst = _int_db(tmp_path / "b.db")
    assert backup.restore_table(dst, out, "t") == 3
    back = pd.read_sql(sa.text("SELECT * FROM t ORDER BY id"), dst)
    assert back.n.tolist()[0] == 1 and pd.isna(back.n.tolist()[1]) and back.n.tolist()[2] == 7                       # integers, and the blank is NULL
    assert back.f.tolist()[0] == 1.0 and back.f.tolist()[1] == 2.5 and back.s.isna().tolist() == [False, True, False]  # reals and text untouched


def test_a_dump_and_a_restore_agree_end_to_end_for_integers_with_blanks(tmp_path):
    src = _int_db(tmp_path / "a.db")
    with src.begin() as c:
        c.exec_driver_sql("INSERT INTO t VALUES (1, 1, 1.5, 'a'), (2, NULL, NULL, NULL), (3, 7, 2.5, 'c')")
    backup.dump(src, tmp_path / "out")
    dst = _int_db(tmp_path / "b.db")
    assert backup.restore_table(dst, tmp_path / "out", "t") == 3
    a, b = (pd.read_sql(sa.text("SELECT * FROM t ORDER BY id"), e) for e in (src, dst))
    pd.testing.assert_frame_equal(a, b)                                                                           # the restored table is the original, cell for cell


# ---------------------------------------------------------------------------------------------- the path you will actually use: the built-in throwaway database
try:
    __import__("pixeltable_pgserver")
    HAVE_PGSERVER = True
except ImportError:
    HAVE_PGSERVER = False


@needs_postgres
@pytest.mark.skipif(not HAVE_PGSERVER, reason="the built-in Postgres package is not installed here (pip install -r demo/requirements.txt)")
def test_the_built_in_throwaway_database_restores_a_backup_and_leaves_nothing_behind(source, capsys):
    import psutil
    import tempfile
    folders = lambda: set(Path(tempfile.gettempdir()).glob("ufc-restore-drill-*"))
    before = folders()                                                                                            # (leftovers from some earlier, unrelated run are not this test's business)
    assert rd.main([str(source["prod"])]) == 0                                                                    # no database url: it starts, uses and removes its own Postgres
    out = capsys.readouterr().out
    assert "RESTORE OK" in out and "game_picks" in out and "demo_meta" not in out
    leftovers = [p.info["pid"] for p in psutil.process_iter(["pid", "name", "cmdline"])                            # only real database processes: a shell whose command text mentions the folder is not one
                 if (p.info["name"] or "").lower().startswith("postgres") and any("ufc-restore-drill-" in part for part in (p.info["cmdline"] or []))]
    assert leftovers == []                                                                                        # the throwaway Postgres is stopped
    assert folders() - before == set()                                                                            # and no folder of its own is left behind


def test_the_drill_says_how_to_fix_a_missing_built_in_database_package(tmp_path, monkeypatch, capsys):
    (tmp_path / "MANIFEST.csv").write_text("table,rows,sha256,columns\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "pixeltable_pgserver", None)                                                 # as if the package were not installed
    assert rd.main([str(tmp_path)]) == 2
    assert "pip install -r demo/requirements.txt" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- a Windows clone: git rewrites every line ending
def _crlf(directory: Path) -> int:
    """What Git for Windows does when it clones: every \\n in every file becomes \\r\\n."""
    n = 0
    for f in Path(directory).glob("*.csv"):
        if f.name == backup.MANIFEST:
            continue
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        n += 1
    return n


def test_a_backup_whose_line_endings_git_changed_on_windows_still_verifies_and_restores_exactly(tmp_path):
    src = _int_db(tmp_path / "a.db")
    with src.begin() as c:
        c.exec_driver_sql("INSERT INTO t VALUES (1, 1, 1.5, 'one line'), (2, NULL, NULL, 'two' || char(10) || 'lines'), (3, 7, 2.5, NULL)")
    out = tmp_path / "out"
    backup.dump(src, out)
    assert backup.verify(out) == [] and backup.converted_files(out) == []                                          # as written on Linux: nothing converted
    assert _crlf(out) == 1 and b"\r\n" in (out / "t.csv").read_bytes()
    assert backup.verify(out) == [] and backup.converted_files(out) == ["t"]                                       # a Windows clone: still intact, and said to be converted
    dst = _int_db(tmp_path / "b.db")
    assert backup.restore_table(dst, out, "t") == 3
    a, b = (pd.read_sql(sa.text("SELECT * FROM t ORDER BY id"), e) for e in (src, dst))
    pd.testing.assert_frame_equal(a, b)                                                                            # cell for cell, including the value that contains a newline
    assert b.s.tolist()[1] == "two\nlines"                                                                         # not "two\r\nlines"


def test_a_converted_file_that_is_also_damaged_is_still_refused(tmp_path):
    src = _int_db(tmp_path / "a.db")
    with src.begin() as c:
        c.exec_driver_sql("INSERT INTO t VALUES (1, 1, 1.5, 'a'), (2, 2, 2.5, 'b')")
    out = tmp_path / "out"
    backup.dump(src, out)
    _crlf(out)
    f = out / "t.csv"
    f.write_bytes(f.read_bytes().replace(b",a\r\n", b",A\r\n"))                                                    # one letter changed, line endings converted too
    assert any("t:" in p and "does not match" in p for p in backup.verify(out))
    with pytest.raises(ValueError, match="refusing to restore"):
        backup.restore_table(_int_db(tmp_path / "b.db"), out, "t")
    (out / "t.csv").write_bytes(b"id,n,f,s\r\n1,1,1.5,a\r\n")                                                      # and a file with rows missing is refused as well
    assert any("t:" in p for p in backup.verify(out))


def test_the_backup_repository_tells_git_never_to_change_the_files():
    wf = (ROOT / ".github" / "workflows" / "backup.yml").read_text(encoding="utf-8")
    assert "* -text" in wf and ".gitattributes" in wf and wf.index(".gitattributes") < wf.index("git add -A")


@needs_postgres
def test_a_windows_clone_of_a_real_backup_restores_completely(source, scratch, tmp_path):
    shutil.copytree(source["prod"], tmp_path / "clone")
    assert _crlf(tmp_path / "clone") > 15                                                                          # every table's file converted, as on your computer
    assert backup.verify(tmp_path / "clone") == [] and len(backup.converted_files(tmp_path / "clone")) > 15
    lines = []
    result = rd.run_drill(tmp_path / "clone", scratch["eng"], log=lines.append)
    assert result["ok"] and any("line endings" in x for x in lines)
    assert {e["table"]: e["restored"] for e in result["tables"]} == source["counts"]
