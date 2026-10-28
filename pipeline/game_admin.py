"""
Owner tools for the friends' game (use your OWNER connection string, DATABASE_URL):

  python pipeline/game_admin.py setup [--rotate-password]    # create the tables and the game's own database user
  python pipeline/game_admin.py audit                        # check that user can touch the game tables and NOTHING else
  python pipeline/game_admin.py players                      # who has joined, and how many picks each has made
  python pipeline/game_admin.py reset-pin NICKNAME [--pin N] # a forgotten PIN: sets a new one (random unless --pin) and prints it once
  python pipeline/game_admin.py delete NICKNAME              # remove a player and their picks

Why a separate database user: the public site's own user is read-only, which is a deliberate safety. The game must write, so it gets a
second user that can read and write only game_players, game_picks and game_attempts. If its password ever leaked, nobody could read
your picks, odds or backups, or change anything outside the game. The site's read-only user is also STOPPED from reading the game
tables (it would otherwise inherit read access to every new table), so it cannot see PIN hashes.

`setup` never prints the new password. It writes the full connection string to data/game_database_url.txt (git ignores data/):
copy it into the site's secrets as GAME_DATABASE_URL, then delete the file.
"""
from __future__ import annotations

import argparse
import logging
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

import bout_results
import game_core
import game_db

log = logging.getLogger("game_admin")
ROOT = Path(__file__).resolve().parent.parent
URL_FILE = ROOT / "data" / "game_database_url.txt"
DEFAULT_ROLE = "game_app"
DEFAULT_READERS = ("site_reader", "app_reader")             # the site's current read-only login, and the old one it replaces (checked while it exists)
GAME_TABLES = game_db.GAME_TABLES
ALL_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"


def _ident(name: str) -> str:
    """A role name made safe to put in SQL (identifiers cannot be bound as parameters): lower-case letters, digits and underscores only."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", name or ""):
        raise ValueError(f"unsafe role name: {name!r}")
    return f'"{name}"'


def _readers(reader_role) -> tuple:
    return (reader_role,) if isinstance(reader_role, str) else tuple(reader_role)


def _role_exists(conn, role: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar())


def setup(engine, game_role: str = DEFAULT_ROLE, reader_role=DEFAULT_READERS, rotate_password: bool = False) -> str | None:
    """Create the tables and the locked-down role. Returns the new connection string when a password was issued, otherwise None."""
    game_db.ensure_tables(engine)
    bout_results.ensure_table(engine)
    gr = _ident(game_role)
    password = None
    with engine.begin() as conn:
        if not _role_exists(conn, game_role):
            password = secrets.token_urlsafe(24)                          # letters, digits, - and _ only, so it is safe inside the statement
            conn.exec_driver_sql(f"CREATE ROLE {gr} WITH LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
        elif rotate_password:
            password = secrets.token_urlsafe(24)
            conn.exec_driver_sql(f"ALTER ROLE {gr} WITH PASSWORD '{password}'")
        conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {gr}")                      # start from nothing, whatever it had before
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {gr}")
        conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(GAME_TABLES)} TO {gr}")
        for reader in _readers(reader_role):                                                             # the read-only user inherits SELECT on new tables: take it away
            if _role_exists(conn, reader):
                conn.exec_driver_sql(f"REVOKE ALL ON {', '.join(GAME_TABLES)} FROM {_ident(reader)}")
    for setting in (f"ALTER ROLE {gr} CONNECTION LIMIT 10", f"ALTER ROLE {gr} SET statement_timeout = '10s'"):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(setting)
        except sa.exc.DBAPIError as exc:                                  # a convenience limit; if the host does not allow it, say so and carry on
            log.warning("could not apply %s (%s)", setting.split(" ROLE ")[1].split(" ", 1)[1], type(exc).__name__)
    if password is None:
        return None
    return engine.url.set(drivername="postgresql", username=game_role, password=password).render_as_string(hide_password=False)


def audit(engine, game_role: str = DEFAULT_ROLE, reader_role=DEFAULT_READERS) -> list[str]:
    """Problems with the permissions (an empty list means the game user can reach the game tables and nothing else)."""
    problems = []
    insp = sa.inspect(engine)
    with engine.connect() as conn:
        role = conn.execute(sa.text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": game_role}).fetchone()
        if role is None:
            return [f"the role {game_role} does not exist; run setup"]
        if role.rolsuper or role.rolcreatedb or role.rolcreaterole:
            problems.append(f"{game_role} has powers it should not have (superuser, create database or create role)")
        if not role.rolcanlogin:
            problems.append(f"{game_role} cannot log in")
        has = lambda who, table, priv: bool(conn.execute(sa.text("SELECT has_table_privilege(:w, :t, :p)"), {"w": who, "t": table, "p": priv}).scalar())
        for t in GAME_TABLES:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                if not has(game_role, t, priv):
                    problems.append(f"{game_role} cannot {priv} on {t}")
        others = [t for t in insp.get_table_names() + insp.get_view_names() if t not in GAME_TABLES]
        for t in others:
            leaked = [p.strip() for p in ALL_PRIVILEGES.split(",") if has(game_role, t, p.strip())]
            if leaked:
                problems.append(f"{game_role} can {'/'.join(leaked)} on {t}, which it must not touch")
        if conn.execute(sa.text("SELECT has_schema_privilege(:w, 'public', 'CREATE')"), {"w": game_role}).scalar():
            problems.append(f"{game_role} can create objects in the public schema")
        for reader in _readers(reader_role):
            if conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": reader}).scalar():
                for t in GAME_TABLES:
                    if has(reader, t, "SELECT"):
                        problems.append(f"{reader} (the public site's read-only user) can read {t}")
    return problems


def write_url_file(url: str, path: Path = URL_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)                                                 # readable by you only (a no-op on Windows)
    except OSError:
        pass
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Owner tools for the friends' game")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--rotate-password", action="store_true", help="issue a new password (the old one stops working)")
    sub.add_parser("audit")
    sub.add_parser("players")
    r = sub.add_parser("reset-pin")
    r.add_argument("nickname")
    r.add_argument("--pin", help="choose the new PIN yourself (4 to 8 digits); otherwise a random 6 digits")
    d = sub.add_parser("delete")
    d.add_argument("nickname")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import model_v2
    engine = model_v2.make_engine()
    if args.command == "setup":
        url = setup(engine, rotate_password=args.rotate_password)
        problems = audit(engine)
        if problems:
            print("PROBLEMS with the permissions:\n  " + "\n  ".join(problems))
            return 1
        print("Tables and the game's database user are ready; the audit passed.")
        if url:
            print(f"Its connection string was written to {write_url_file(url)}\nCopy it into the site's secrets as GAME_DATABASE_URL, then delete that file.")
        else:
            print("The user already existed, so no new password was issued. Use --rotate-password if you need a new connection string.")
        return 0
    if args.command == "audit":
        problems = audit(engine)
        print("Audit passed: the game user can read and write the game tables and nothing else; the public read-only user cannot read them."
              if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    if args.command == "players":
        df = game_db.list_players(engine)
        print(df.to_string(index=False) if len(df) else "No players yet.")
        return 0
    try:
        if args.command == "reset-pin":
            print(f"New PIN for {args.nickname}: {game_db.reset_pin(engine, args.nickname, datetime.now(timezone.utc), args.pin)}\nTell them privately; they can use it straight away.")
        else:
            print("Deleted." if game_db.delete_by_nickname(engine, args.nickname) else "There is no player with that nickname.")
    except game_core.GameError as exc:
        print(f"Not done: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
