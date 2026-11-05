"""
One-command recovery: rebuild the real database from a backup into a NEW, EMPTY database, and leave it ready to use.

    python pipeline/recover.py BACKUP_DATA_FOLDER --rehearse                      # practise it, safely (a throwaway database on this computer)
    python pipeline/recover.py BACKUP_DATA_FOLDER --confirm NAME_OF_THE_DATABASE  # the real thing

BACKUP_DATA_FOLDER is the `data` folder of your private `ufc-project-backups` repository (a clone or a download). The REAL run needs a new empty database (for example a new
Neon database) and reads its connection string, the owner's, from the environment variable RECOVER_DATABASE_URL. It never reads DATABASE_URL, so the old database (or one you
forgot was still set) cannot be touched by mistake; and it refuses any database that is not empty, so it can never overwrite anything. `--confirm` must repeat the database's own
name, as a second pair of eyes (the command says what to type). Nothing it prints contains a password or a connection string.

What it does, in order (the first part is the weekly restore drill's recipe, `restore_drill.run_drill`, which is tested row for row; this adds what the drill must not do):
  1. checks every backup file against MANIFEST.csv: a damaged or incomplete backup stops everything here, before anything is created;
  2. creates every table with the project's own setup steps and restores each one, then compares every table with the backup (rows, and filled-in cells per column);
  3. moves every auto-number counter past the restored ids (the odds history's next snapshot would otherwise collide) and checks that it did;
  4. creates the two restricted logins from nothing, with new passwords: the website's read-only login and the friends' game's login, and writes each new connection string to a
     file (never to the screen);
  5. audits both logins: each can reach what it needs and nothing else;
  6. rebuilds the "Why this pick?" cache, which is not backed up (about 14 MB; `--skip-reasons` leaves it for later and says the two commands that do it).
It ends with RECOVERY OK and the exact list of secrets to change (names and where, never values), or says where it stopped. A stop before step 4 changed nothing but the new database.

What a backup cannot hold: anything created after it was made (the backup runs weekly): newer odds snapshots, locked picks and game picks. The public pick log in this repository
is still a time-stamped record of every locked pick.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa

import db_roles
import game_admin
import restore_drill

ENV_VAR = "RECOVER_DATABASE_URL"


class Refusal(Exception):
    """The run must not go ahead. The message says why and what to do instead."""


def describe(engine) -> str:
    """Where an engine points, for a person to check: host and database name, never the user or the password."""
    return f"{engine.url.host or 'this computer'}/{engine.url.database}"


def _clean(text: str, engine) -> str:
    """An error message with the password and the connection string taken out."""
    out = str(text)
    for secret in (engine.url.render_as_string(hide_password=False), engine.url.password):
        if secret:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(PASSWORD\s+)'[^']*'", r"\1'***'", out)                      # the statement that creates a login contains its new password: never show it
    return out.splitlines()[0][:240] if out else ""


def target_from_environment() -> sa.engine.Engine:
    url = os.environ.get(ENV_VAR)
    if not url:
        raise Refusal(f"Set {ENV_VAR} to the connection string of the NEW, EMPTY database (the owner's), then run this again. "
                      "This tool never reads DATABASE_URL, so it cannot touch your current database by mistake.")
    return sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1).replace("postgres://", "postgresql+psycopg2://", 1))


def assert_target(engine, confirm: str | None, rehearsal: bool) -> None:
    """The database must be empty (so nothing can be overwritten), and for the real thing the person must have typed its name."""
    try:
        tables = sa.inspect(engine).get_table_names()
    except sa.exc.SQLAlchemyError as exc:
        raise Refusal(f"Could not connect to {describe(engine)}: {_clean(exc, engine)}") from None
    if tables:
        raise Refusal(f"{describe(engine)} is not empty (it has {len(tables)} tables, for example {sorted(tables)[:3]}). Recovery only ever loads an EMPTY database, so it cannot "
                      "overwrite anything. Create a new empty database and point RECOVER_DATABASE_URL at it.")
    if not rehearsal and confirm != engine.url.database:
        raise Refusal(f"This will restore into {describe(engine)}. To go ahead, run the same command again with:  --confirm {engine.url.database}")


# ----------------------------------------------------------------------------------------------
# the steps
# ----------------------------------------------------------------------------------------------
@contextmanager
def _database_url_is(engine):
    """The models read DATABASE_URL: point it at the new database for the duration of a call, and put it back exactly as it was."""
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def rebuild_reasons(engine) -> str:
    """The stored 'Why this pick?' reasons for every past fight are not backed up (they are rebuilt from the data). Never fatal: the database is usable without them."""
    try:
        import model_v2
        import model_v3
        with _database_url_is(engine):
            for name, module in (("the winner model", model_v2), ("the market model", model_v3)):
                if module.main(["explain-past"]) not in (0, None):
                    return f"failed for {name}"
        return "rebuilt"
    except (Exception, SystemExit) as exc:                                    # a command that calls sys.exit must not end the recovery
        return f"failed ({type(exc).__name__})"


def run_recovery(directory: Path, engine, *, site_file: Path, game_file: Path, reasons: bool = True, reader_role: str = db_roles.READER_ROLE,
                 game_role: str = game_admin.DEFAULT_ROLE, log=print) -> dict:
    """Recover into `engine` (already checked to be empty). Returns what happened; `stage` is where it stopped, or 'done'."""
    out = {"ok": False, "stage": "restore", "restore": None, "files": [], "audit": [], "reasons": "skipped"}
    out["restore"] = restore_drill.run_drill(directory, engine, log)                       # steps 1 to 3: verify, create, restore, compare, counters
    if not out["restore"]["ok"]:
        return out
    out["stage"] = "logins"
    site_url = db_roles.setup_reader(engine, role=reader_role, rotate_password=True)
    game_url = game_admin.setup(engine, game_role=game_role, reader_role=(reader_role,), rotate_password=True)
    for url, path in ((site_url, site_file), (game_url, game_file)):
        db_roles.write_url_file(url, Path(path))
        out["files"].append(Path(path))
    log("  created: the website's read-only login and the game's login (new passwords, written to files)")
    out["stage"] = "audit"
    out["audit"] = db_roles.audit_reader(engine, role=reader_role) + game_admin.audit(engine, game_role=game_role, reader_role=(reader_role,))
    if out["audit"]:
        return out
    log("  audited: each login reaches what it needs and nothing else")
    if reasons:
        out["stage"] = "reasons"
        out["reasons"] = rebuild_reasons(engine)
        log(f"  the 'Why this pick?' cache: {out['reasons']}")
    out["ok"], out["stage"] = True, "done"
    return out


# ----------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------
def format_recovery(result: dict, *, target: str, rehearsal: bool, site_file: Path, game_file: Path) -> str:
    lines = [restore_drill.format_report(result["restore"])]
    if result["stage"] == "restore" and result["restore"]["stopped"]:
        lines.append("\nRECOVERY STOPPED at the first step. Nothing was created in the new database; fix the backup (download it again) and run this again.")
        return "\n".join(lines)
    if result["stage"] == "restore":
        lines.append("\nRECOVERY STOPPED after restoring: the tables above did not all come back cleanly, so no logins were created. Fix the cause and run it again "
                     "into a NEW empty database (this one is no longer empty).")
        return "\n".join(lines)
    if result["audit"]:
        lines.append("\nRECOVERY STOPPED at the audit: the new logins can reach more than they should:")
        lines += [f"  {p}" for p in result["audit"]]
        lines.append("Nothing should use the new database until this is fixed (`python pipeline/db_roles.py audit` and `python pipeline/game_admin.py audit`).")
        return "\n".join(lines)
    lines.append(f"\nRECOVERY OK{' (a rehearsal: nothing was kept)' if rehearsal else ''}: {target} now holds the backup, with the restricted logins in place and audited.")
    if result["reasons"] == "skipped":
        lines.append("The 'Why this pick?' cache was not rebuilt. When you want it: python pipeline/model_v2.py explain-past, then python pipeline/model_v3.py explain-past "
                     "(with DATABASE_URL set to the new database).")
    elif result["reasons"] != "rebuilt":
        lines.append(f"The 'Why this pick?' cache was not rebuilt ({result['reasons']}). The database is fine without it; run: python pipeline/model_v2.py explain-past, then "
                     "python pipeline/model_v3.py explain-past (with DATABASE_URL set to the new database).")
    if rehearsal:
        lines.append("\nThis was a rehearsal. The real thing is the same command without --rehearse, with RECOVER_DATABASE_URL set to a new empty database and --confirm NAME.")
        return "\n".join(lines)
    lines += ["", "WHAT TO DO NOW (nothing else has been changed; the old database, if it still exists, was never touched):",
              "  1. GitHub, this repository, Settings, Secrets and variables, Actions: set DATABASE_URL to the OWNER connection string of the new database",
              f"     (the value you put in {ENV_VAR}). The daily job, the weekly backup and the monthly lock refresh all read it.",
              f"  2. Streamlit, your app, Settings, Secrets: set DATABASE_URL to the line in {site_file} and GAME_DATABASE_URL to the line in {game_file}.",
              "     GAME_INVITE_CODE stays as it is: the players came back with the backup.",
              "  3. Delete both files once the secrets are saved (they hold the new passwords).",
              "  4. Run Actions, Auto update, Run workflow once on the new database, then open every tab of the site.",
              "",
              "What the backup could not hold: anything created after it was made (it runs weekly): the newest odds snapshots, locked picks and game picks. The public pick log in this "
              "repository still has every locked pick, time-stamped."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the database from a backup into a new empty database")
    ap.add_argument("directory", type=Path, help="the backup's data folder (the one that contains MANIFEST.csv)")
    ap.add_argument("--rehearse", action="store_true", help="practise on a throwaway database on this computer; nothing real is touched and no files are kept")
    ap.add_argument("--database-url", help="(with --rehearse) your own EMPTY local Postgres instead of the built-in one; hosted databases are refused")
    ap.add_argument("--confirm", help="the new database's name, typed again, to confirm the real run")
    ap.add_argument("--skip-reasons", action="store_true", help="do not rebuild the 'Why this pick?' cache now")
    ap.add_argument("--site-file", type=Path, default=db_roles.URL_FILE, help="where the website's new connection string is written")
    ap.add_argument("--game-file", type=Path, default=game_admin.URL_FILE, help="where the game's new connection string is written")
    ap.add_argument("--reader-role", default=db_roles.READER_ROLE, help=argparse.SUPPRESS)
    ap.add_argument("--game-role", default=game_admin.DEFAULT_ROLE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not args.directory.is_dir():
        print(f"{args.directory} is not a folder. Give the backup's data folder (the one that contains MANIFEST.csv).")
        return 2
    if args.database_url and not args.rehearse:
        print("--database-url is only for a rehearsal. The real run reads the new database from the environment variable " + ENV_VAR + ".")
        return 2
    scratch = None
    engine = None
    try:
        if args.rehearse:
            if args.database_url:
                engine = sa.create_engine(args.database_url.replace("postgresql://", "postgresql+psycopg2://", 1))
                restore_drill.assert_safe(engine)
            else:
                scratch = restore_drill.scratch_database()
                engine = scratch.__enter__()
            assert_target(engine, None, rehearsal=True)
            keep = tempfile.TemporaryDirectory(prefix="ufc-rehearsal-")
            site_file, game_file = Path(keep.name) / "site.txt", Path(keep.name) / "game.txt"
        else:
            engine = target_from_environment()
            assert_target(engine, args.confirm, rehearsal=False)
            keep, site_file, game_file = None, args.site_file, args.game_file
        print(f"{'Rehearsing into' if args.rehearse else 'Recovering into'} {describe(engine)}, from {args.directory}")
        result = run_recovery(args.directory, engine, site_file=site_file, game_file=game_file, reasons=not args.skip_reasons,
                              reader_role=args.reader_role, game_role=args.game_role)
        print(format_recovery(result, target=describe(engine), rehearsal=args.rehearse, site_file=site_file, game_file=game_file))
        if keep is not None:
            keep.cleanup()
        return 0 if result["ok"] else 1
    except SystemExit as exc:                                   # restore_drill.assert_safe refuses with a message
        print(exc)
        return 2
    except Refusal as exc:
        print(f"Refusing to run: {exc}")
        return 2
    except ImportError:
        print("The throwaway database needs the built-in Postgres package. Run: pip install -r demo/requirements.txt")
        return 2
    except Exception as exc:                                    # anything unexpected: say what, without a traceback (it could contain a statement with a password)
        print(f"Recovery stopped with an unexpected error: {type(exc).__name__}: {_clean(exc, engine) if engine is not None else ''}")
        print("The new database may now be partly filled. Create another EMPTY database and run the command again; nothing else was changed.")
        return 1
    finally:
        if scratch is not None:
            scratch.__exit__(None, None, None)
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
