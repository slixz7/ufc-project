"""
Practise a restore, safely: rebuild the whole database from a backup into a THROWAWAY database on this computer, and check it.

    python pipeline/restore_drill.py PATH_TO_THE_BACKUP_DATA_FOLDER

PATH is the `data` folder of your private `ufc-project-backups` repository (clone or download it first), or a folder made by
`python pipeline/backup.py dump`. This never reads DATABASE_URL and never touches your real database: it starts its own empty
Postgres in a temporary folder (the same built-in one the demo uses: pip install -r demo/requirements.txt), does the work there,
and deletes it afterwards. Nothing is written outside that temporary folder.

What it does, in order, which is also the recovery recipe if the real database were ever lost:
  1. checks every file against MANIFEST.csv (a damaged or incomplete backup stops here);
  2. creates the base tables (fights, fighter_fights) from a written-out layout, as a stand-in for the loader (update_from_csv.py), which fills
     them from the Kaggle data in a real recovery;
  3. creates every other table with the project's own setup steps (upcoming.py setup, model_v2.py setup, model_v3.py setup, model_distance.py
     setup, model_distance_v2.py setup, and the tables the daily job and the friends' game create for themselves);
  4. restores each table with `backup.py restore` (which only loads an empty table and never deletes);
  5. compares what came back with what was backed up: the number of rows, and for every column the number of filled-in cells.
It ends with RESTORE OK, or with the list of everything that did not come back.

A table that is in the backup but that no setup step creates is reported as a gap. That means the recipe above is out of date for a
table that has been added to the project: add its setup step to SCHEMA_STEPS below (a test fails until you do).
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa

import backup

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
# Tables that are in the real database and therefore in the backup, but that nothing in the project reads or writes any more. They stay in the backup (they are
# small and harmless), they are not restored, and they do not fail the drill. Anything else without a setup step DOES fail it. Check the reason before adding one.
LEGACY_TABLES = {"predictions": "an early table that no code, SQL or page uses any more (the project's tables are fight_predictions and upcoming_predictions)"}
BASE_TABLES = ("fights", "fighter_fights")          # created by the loader from the Kaggle data; here, from the backup
HOSTED = ("neon.tech", "supabase", "amazonaws.com", "render.com", "elephantsql", "azure.com", "googleapis.com")


# ----------------------------------------------------------------------------------------------
# the recovery recipe
# ----------------------------------------------------------------------------------------------
def _sql(name: str):
    def run(engine):
        with engine.begin() as conn:
            conn.exec_driver_sql((SQL_DIR / name).read_text(encoding="utf-8"))
    return run


def _call(module: str, func: str):
    def run(engine):
        import importlib
        getattr(importlib.import_module(module), func)(engine)
    return run


def _reasons(engine):
    import model_v2
    with engine.begin() as conn:
        conn.exec_driver_sql(model_v2.REASONS_DDL)
        conn.exec_driver_sql(model_v2.FIGHT_REASONS_DDL)


# (what it is, the command it stands for, what to run). The order matters: the views in features.sql read the base tables.
SCHEMA_STEPS = [
    ("features and predictions views and tables", "upcoming.py setup", lambda e: [_sql("features.sql")(e), _sql("predictions.sql")(e)]),
    ("winner model v2 tables", "model_v2.py setup", _sql("model_v2.sql")),
    ("market model v3 tables", "model_v3.py setup", _sql("model_v3.sql")),
    ("goes-the-distance tables", "model_distance.py setup", _call("model_distance", "ensure_tables")),
    ("market-informed goes-the-distance tables", "model_distance_v2.py setup", _call("model_distance_v2", "ensure_tables")),
    ("the reasons tables", "created by model_v2.py explain", _reasons),
    ("the friends' game tables", "game_admin.py setup", _call("game_db", "ensure_tables")),
    ("how played bouts ended", "bout_results.py", _call("bout_results", "ensure_table")),
    ("the Discord posting log", "the daily job", _call("digest", "ensure")),
    ("the odds history and event start times", "the daily job's first real run", lambda e: [_call("auto_odds", "ensure_snapshot_tables")(e), _call("auto_odds", "ensure_event_starts")(e)]),
]


# The two base tables, which no setup command creates (the loader, update_from_csv.py, fills them from the Kaggle data). Their layout is
# written out here so a restore does not have to guess column types from text; a test checks it against the tables a real build produces.
BASE_TABLES_DDL = """
CREATE TABLE fights (fight_url TEXT PRIMARY KEY, event_date DATE NOT NULL, event_name TEXT, weight_class TEXT, title_fight BOOLEAN, num_rounds DOUBLE PRECISION,
    result TEXT, finish_round DOUBLE PRECISION, finish_time TEXT);
CREATE TABLE fighter_fights (fight_url TEXT NOT NULL, fighter_url TEXT NOT NULL, fighter_name TEXT, side INTEGER, won INTEGER, sig_land DOUBLE PRECISION,
    sig_att DOUBLE PRECISION, td_land DOUBLE PRECISION, td_att DOUBLE PRECISION, sub_att DOUBLE PRECISION, kd DOUBLE PRECISION, ctrl_sec DOUBLE PRECISION,
    dist_att DOUBLE PRECISION, clinch_att DOUBLE PRECISION, ground_att DOUBLE PRECISION, sig_absorbed DOUBLE PRECISION, opp_td_land DOUBLE PRECISION,
    opp_td_att DOUBLE PRECISION, height_cm DOUBLE PRECISION, reach_cm DOUBLE PRECISION, stance TEXT, dob DATE);
CREATE UNIQUE INDEX ux_ff_fight_fighter ON fighter_fights (fight_url, fighter_url);
"""


def create_base_tables(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(BASE_TABLES_DDL)


# ----------------------------------------------------------------------------------------------
# checking what came back
# ----------------------------------------------------------------------------------------------
def csv_profile(directory: Path, table: str) -> tuple[int, dict[str, int]]:
    """(rows, filled-in cells per column) of a backed-up table, reading every cell as text; a blank cell was NULL."""
    df = backup.read_backup_csv(directory, table, dtype=str, keep_default_na=False)
    return len(df), {c: int((df[c] != "").sum()) for c in df.columns}


def db_profile(engine, table: str, columns: list[str]) -> tuple[int, dict[str, int]]:
    with engine.connect() as conn:
        rows = int(conn.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar())
        filled = {c: int(conn.execute(sa.text(f'SELECT COUNT("{c}") FROM "{table}"')).scalar()) for c in columns}
    return rows, filled


def compare(expected: tuple[int, dict[str, int]], got: tuple[int, dict[str, int]]) -> list[str]:
    problems = []
    if expected[0] != got[0]:
        problems.append(f"{got[0]} rows came back, the backup has {expected[0]}")
    for col, n in expected[1].items():
        if got[1].get(col) != n:
            problems.append(f"column {col}: {got[1].get(col)} filled-in cells came back, the backup has {n}")
    return problems


def run_drill(directory: Path, engine, log=print) -> dict:
    """Rebuild the database in `engine` (which must be empty) from the backup in `directory`, and check every table. Returns a dict with `ok`, `stopped`, `tables`."""
    directory = Path(directory)
    problems = backup.verify(directory)
    if problems:
        return {"ok": False, "stopped": problems, "tables": []}
    manifest = list(csv.DictReader(open(directory / backup.MANIFEST, encoding="utf-8")))
    log("Backup folder checked against its manifest: every file matches.")
    converted = backup.converted_files(directory)
    if converted:
        log(f"  note: git changed the line endings of {len(converted)} file(s) when this backup was downloaded (normal on Windows); they were read as written.")
    create_base_tables(engine)
    log("Base tables created (fights, fighter_fights): the loader normally creates these from the Kaggle data.")
    for what, command, step in SCHEMA_STEPS:
        step(engine)
        log(f"  created: {what}  ({command})")
    existing = set(sa.inspect(engine).get_table_names())
    out = []
    for row in manifest:
        t = row["table"]
        entry = {"table": t, "rows": int(row["rows"]), "restored": None, "problems": [], "legacy": None}
        if t not in existing and t in LEGACY_TABLES:
            entry["legacy"] = LEGACY_TABLES[t]
        elif t not in existing:
            entry["problems"].append("no setup step creates this table, so there is nowhere to restore it (add one to SCHEMA_STEPS)")
        else:
            try:
                backup.restore_table(engine, directory, t)
                expected = csv_profile(directory, t)
                got = db_profile(engine, t, list(expected[1]))
                entry["restored"] = got[0]
                entry["problems"] += compare(expected, got)
            except (ValueError, sa.exc.SQLAlchemyError) as exc:
                entry["problems"].append(str(exc).splitlines()[0][:200])
        out.append(entry)
    return {"ok": all(not e["problems"] for e in out), "stopped": [], "tables": out}


def format_report(result: dict) -> str:
    if result["stopped"]:
        return "THE BACKUP IS NOT INTACT, so nothing was restored:\n" + "\n".join(f"  - {p}" for p in result["stopped"])
    lines = [f"\n{'table':28s} {'in the backup':>13s} {'restored':>9s}   result"]
    for e in result["tables"]:
        lines.append(f"{e['table']:28s} {e['rows']:13,d} {('-' if e['restored'] is None else format(e['restored'], ',d')):>9s}   "
                     + ("PROBLEM" if e["problems"] else "legacy: not restored" if e.get("legacy") else "ok"))
    bad = [e for e in result["tables"] if e["problems"]]
    if bad:
        lines.append("\nWHAT DID NOT COME BACK CLEANLY:")
        for e in bad:
            lines += [f"  {e['table']}: {p}" for p in e["problems"]]
        lines.append("\nRESTORE FAILED for the tables above. Everything else came back; fix the cause, then run the drill again.")
    else:
        legacy = [e for e in result["tables"] if e.get("legacy")]
        kept = [e for e in result["tables"] if not e.get("legacy")]
        rows = sum(e["rows"] for e in kept)
        lines.append(f"\nRESTORE OK: all {len(kept)} tables ({rows:,} rows) came back, and each one matches the backup row for row and column for column.")
        for e in legacy:
            lines.append(f"Not restored, by design: {e['table']} ({e['rows']:,} rows), {e['legacy']}. It stays in the backup.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# the throwaway database
# ----------------------------------------------------------------------------------------------
def _stop(server) -> None:
    """Stop the private database for good, even if an earlier killed run left a stale registration (same approach as the demo launcher)."""
    try:
        server.cleanup()
    finally:
        try:
            if (Path(server.pgdata) / "postmaster.pid").exists():
                server._stop()
        except Exception:
            pass


@contextmanager
def scratch_database():
    """An empty Postgres in a temporary folder, removed afterwards."""
    import pixeltable_pgserver
    with tempfile.TemporaryDirectory(prefix="ufc-restore-drill-", ignore_cleanup_errors=True) as tmp:
        server = pixeltable_pgserver.get_server(Path(tmp) / "pg", cleanup_mode="stop")
        engine = sa.create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg2://", 1))
        try:
            yield engine
        finally:
            engine.dispose()
            _stop(server)


def assert_safe(engine) -> None:
    """Whatever database the drill is given, it must be empty and must not be a hosted one: a drill must not be able to touch anything real."""
    host = (engine.url.host or "").lower()
    if any(h in host for h in HOSTED):
        raise SystemExit(f"Refusing to run: {host} looks like a hosted database. The drill only runs on a throwaway database on this computer.")
    tables = sa.inspect(engine).get_table_names()
    if tables:
        raise SystemExit(f"Refusing to run: the database is not empty (it has {len(tables)} tables, for example {sorted(tables)[:3]}). The drill needs an empty one.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Practise a restore into a throwaway database and check it")
    ap.add_argument("directory", type=Path, help="the backup's data folder")
    ap.add_argument("--database-url", help="use your own EMPTY local Postgres instead of the built-in one (hosted databases and non-empty ones are refused)")
    args = ap.parse_args(argv)
    if not args.directory.is_dir():
        print(f"{args.directory} is not a folder. Give the backup's data folder (the one that contains MANIFEST.csv).")
        return 2
    try:
        if args.database_url:
            engine = sa.create_engine(args.database_url.replace("postgresql://", "postgresql+psycopg2://", 1))
            assert_safe(engine)
            result = run_drill(args.directory, engine)
        else:
            with scratch_database() as engine:
                result = run_drill(args.directory, engine)
    except ImportError:
        print("The throwaway database needs the built-in Postgres package. Run: pip install -r demo/requirements.txt")
        return 2
    print(format_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
