"""
Back up the database to plain CSV files, check a backup, and restore from it.

  python pipeline/backup.py dump [--out DIR] [--exclude a,b]    # every table to DIR/<table>.csv plus DIR/MANIFEST.csv
  python pipeline/backup.py verify DIR                          # does every file match the manifest? (needs no database)
  python pipeline/backup.py restore DIR --table NAME            # load DIR/NAME.csv into an EXISTING, EMPTY table

Why plain CSV in a stable order: a table that only ever gains rows (the odds history, the locked picks) then changes only at its
end from one backup to the next, so a version-controlled backup stores just the new rows. Nothing in the files depends on when
the backup was taken, so a week with no new data produces a byte-identical backup and no new commit.

Everything is backed up except tables that are large and can be rebuilt from the others (EXCLUDE). The odds history
(odds_snapshots) cannot be re-downloaded, which is the main reason this exists. Reading it needs the OWNER connection string.

`restore` never deletes anything: it refuses a table that already has rows, and it checks the file against the manifest first.
To recover from scratch, run the project's setup commands to create the empty tables, then restore the ones you need.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "backup"
EXCLUDE = {"fight_reasons"}          # ~14 MB of stored reasons; rebuilt by `model_v2.py explain-past` and `model_v3.py explain-past`
MANIFEST = "MANIFEST.csv"
MANIFEST_COLUMNS = ["table", "rows", "sha256", "columns"]
ORDER_FALLBACK = 4                   # a table without a primary key is ordered by its first few columns


def table_names(engine, exclude=EXCLUDE) -> list[str]:
    return sorted(t for t in sa.inspect(engine).get_table_names() if t not in set(exclude))


def _order_by(engine, table: str, columns: list[str]) -> str:
    pk = sa.inspect(engine).get_pk_constraint(table).get("constrained_columns") or []
    return ", ".join(f'"{c}"' for c in (pk or columns[:ORDER_FALLBACK]))


def _cell(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":")) if isinstance(v, (dict, list)) else v


def format_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Make a table's rows write out the same way every time: JSON cells with sorted keys, everything else as the database gave it."""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(_cell)
    return df


def _integer_columns(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table) if isinstance(c["type"], sa.Integer)}


def read_table(engine, table: str) -> pd.DataFrame:
    columns = [c["name"] for c in sa.inspect(engine).get_columns(table)]
    df = pd.read_sql(sa.text(f'SELECT * FROM "{table}" ORDER BY {_order_by(engine, table, columns)}'), engine)
    for c in _integer_columns(engine, table) & set(df.columns):
        df[c] = df[c].astype("Int64")            # an integer column with a blank in it is read back as decimals (1.0); write 1, and leave the blank blank
    return format_frame(df)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_bytes(path: Path, expected_sha256: str | None) -> bytes:
    """The file's bytes as the backup wrote them. Git for Windows rewrites every line ending as \\r\\n when it clones, which changes every checksum although
    nothing is damaged; if the file only matches its checksum once those are put back, that is what it is, and the original bytes are returned."""
    raw = Path(path).read_bytes()
    if expected_sha256 is None or b"\r\n" not in raw or hashlib.sha256(raw).hexdigest() == expected_sha256:
        return raw
    fixed = raw.replace(b"\r\n", b"\n")
    return fixed if hashlib.sha256(fixed).hexdigest() == expected_sha256 else raw


def _manifest_sha(directory: Path, table: str) -> str | None:
    f = Path(directory) / MANIFEST
    if not f.exists():
        return None
    for row in csv.DictReader(open(f, encoding="utf-8", newline="")):
        if row["table"] == table:
            return row["sha256"]
    return None


def read_backup_csv(directory: Path, table: str, **kwargs) -> pd.DataFrame:
    """A backed-up table as a frame, reading it the way it was written even if git has changed its line endings."""
    raw = _file_bytes(Path(directory) / f"{table}.csv", _manifest_sha(directory, table))
    return pd.read_csv(io.BytesIO(raw), **kwargs)


def converted_files(directory: Path) -> list[str]:
    """Tables whose files have had their line endings changed by git (they are accepted: only the line endings differ)."""
    d = Path(directory)
    out = []
    for row in csv.DictReader(open(d / MANIFEST, encoding="utf-8", newline="")) if (d / MANIFEST).exists() else []:
        f = d / f"{row['table']}.csv"
        if f.exists() and _sha256(f) != row["sha256"] and _file_bytes(f, row["sha256"]) != f.read_bytes():
            out.append(row["table"])
    return out


def dump(engine, out: Path = OUT, exclude=EXCLUDE) -> list[dict]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for t in table_names(engine, exclude):
        df = read_table(engine, t)
        path = out / f"{t}.csv"
        df.to_csv(path, index=False, lineterminator="\n", na_rep="")
        manifest.append({"table": t, "rows": len(df), "sha256": _sha256(path), "columns": "|".join(df.columns)})
    with open(out / MANIFEST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(manifest)
    return manifest


def verify(directory: Path) -> list[str]:
    """Problems found in a backup folder (an empty list means every file matches the manifest)."""
    d = Path(directory)
    if not (d / MANIFEST).exists():
        return [f"{MANIFEST} is missing"]
    problems = []
    listed = set()
    for row in csv.DictReader(open(d / MANIFEST, encoding="utf-8")):
        t = row["table"]
        listed.add(f"{t}.csv")
        f = d / f"{t}.csv"
        if not f.exists():
            problems.append(f"{t}: the file is missing")
        elif hashlib.sha256(_file_bytes(f, row["sha256"])).hexdigest() != row["sha256"]:
            problems.append(f"{t}: the file does not match the manifest (changed or damaged)")
        elif len(pd.read_csv(io.BytesIO(_file_bytes(f, row["sha256"])), dtype=str, keep_default_na=False)) != int(row["rows"]):
            problems.append(f"{t}: the row count differs from the manifest")
    problems += [f"{f.name}: not listed in the manifest" for f in sorted(d.glob("*.csv")) if f.name != MANIFEST and f.name not in listed]
    return problems


def restore_table(engine, directory: Path, table: str) -> int:
    """Load a backed-up table into an existing table that has no rows. Never deletes. Returns the number of rows loaded."""
    d = Path(directory)
    problems = [p for p in verify(d) if p.startswith(f"{table}:") or p.startswith(MANIFEST)]
    if problems:
        raise ValueError("refusing to restore: " + "; ".join(problems))
    if not (d / f"{table}.csv").exists():
        raise ValueError(f"{table}: there is no backup file for this table")
    insp = sa.inspect(engine)
    if not insp.has_table(table):
        raise ValueError(f"{table}: the table does not exist; create it first with the project's setup command")
    with engine.connect() as conn:
        if conn.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar():
            raise ValueError(f"{table}: the table already has rows; restore only loads into an empty table")
    df = read_backup_csv(d, table, dtype=str, keep_default_na=False).replace({"": None})              # a blank cell was NULL
    for c in _integer_columns(engine, table) & set(df.columns):                                    # backups made before the fix wrote an integer with a blank in its column as 1.0
        df[c] = df[c].map(lambda v: v if v is None else re.sub(r"\.0+$", "", v))
    unknown = set(df.columns) - {c["name"] for c in insp.get_columns(table)}
    if unknown:
        raise ValueError(f"{table}: the backup has columns the table does not: {sorted(unknown)}")
    with engine.begin() as conn:
        df.to_sql(table, conn, if_exists="append", index=False, method="multi", chunksize=200)
    return len(df)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Back up, check and restore the database")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", default=str(OUT))
    d.add_argument("--exclude", default=",".join(sorted(EXCLUDE)), help="comma-separated tables to leave out")
    v = sub.add_parser("verify")
    v.add_argument("directory")
    r = sub.add_parser("restore")
    r.add_argument("directory")
    r.add_argument("--table", required=True)
    args = ap.parse_args(argv)
    if args.command == "verify":
        problems = verify(Path(args.directory))
        print("Backup is intact." if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    import model_v2
    engine = model_v2.make_engine()
    if args.command == "dump":
        manifest = dump(engine, Path(args.out), {x for x in args.exclude.split(",") if x})
        for m in manifest:
            print(f"  {m['table']:28s} {m['rows']:>9,} rows")
        print(f"Saved {len(manifest)} tables ({sum(m['rows'] for m in manifest):,} rows) to {args.out}")
        return 0
    try:
        n = restore_table(engine, Path(args.directory), args.table)
    except ValueError as exc:
        print(f"Not restored: {exc}")
        return 1
    print(f"Restored {n:,} rows into {args.table}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
