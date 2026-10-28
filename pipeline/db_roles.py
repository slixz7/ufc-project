"""
The website's read-only database login: create it properly, check that it really is read-only, and retire an over-powered old one.

  python pipeline/db_roles.py reader [--rotate-password]   # create (or repair) the login `site_reader`; writes its connection string to a file
  python pipeline/db_roles.py audit                        # check the site's login (and any old one) for real read-only-ness
  python pipeline/db_roles.py retire-old                   # strip the old login `app_reader` of its access (then delete it in the Neon console)

Why this exists. A login made in Neon's web console is given extra powers: it joins Neon's own `neon_superuser` role, which can
read AND WRITE every table and create other logins. Such a login is not read-only whatever grants you add. A login made with
plain SQL (what `reader` does) has none of that. The audit checks the things that matter: no extra roles, no powerful attributes,
no write access to anything, no access to the private tables (raw odds, the game's players and picks), no creating objects.

Run with your OWNER connection string (DATABASE_URL). `reader` never prints the new password: it writes the connection string to
data/site_database_url.txt (git ignores data/). Put it in the site's secrets as DATABASE_URL, then delete the file.
"""
from __future__ import annotations

import argparse
import logging
import re
import secrets
import sys
from pathlib import Path

import sqlalchemy as sa

log = logging.getLogger("db_roles")
ROOT = Path(__file__).resolve().parent.parent
URL_FILE = ROOT / "data" / "site_database_url.txt"
READER_ROLE = "site_reader"
LEGACY_READER = "app_reader"
PRIVATE_TABLES = ("odds_snapshots", "odds_bout_summary", "game_players", "game_picks", "game_attempts")      # the site's login must never read these
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
RISKY_ATTRIBUTES = (("rolsuper", "act as a superuser"), ("rolcreaterole", "create other logins"), ("rolcreatedb", "create databases"),
                    ("rolbypassrls", "bypass row-level security"), ("rolreplication", "use replication"))


def _ident(name: str) -> str:
    """A role name made safe to put in SQL (identifiers cannot be bound as parameters): lower-case letters, digits and underscores only."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", name or ""):
        raise ValueError(f"unsafe role name: {name!r}")
    return f'"{name}"'


def _role_exists(conn, role: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar())


def _relations(conn) -> list[str]:
    """Every table, view, materialised view and foreign table in the public schema."""
    return [r[0] for r in conn.execute(sa.text("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                                  WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p', 'f') ORDER BY c.relname"""))]


# ----------------------------------------------------------------------------------------------
# create
# ----------------------------------------------------------------------------------------------
def setup_reader(engine, role: str = READER_ROLE, rotate_password: bool = False) -> str | None:
    """Create (or repair) the read-only login. Returns its connection string when a password was issued, otherwise None."""
    rid = _ident(role)
    password = None
    with engine.begin() as conn:
        if not _role_exists(conn, role):
            password = secrets.token_urlsafe(24)                          # letters, digits, - and _ only, so it is safe inside the statement
            conn.exec_driver_sql(f"CREATE ROLE {rid} WITH LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        elif rotate_password:
            password = secrets.token_urlsafe(24)
            conn.exec_driver_sql(f"ALTER ROLE {rid} WITH PASSWORD '{password}'")
        conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {rid}")                      # start from nothing, whatever it had
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {rid}")
        conn.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {rid}")
        conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {rid}")   # tables made later are readable too...
        private = [t for t in PRIVATE_TABLES if t in _relations(conn)]
        if private:                                                                                       # ...except the private ones
            conn.exec_driver_sql(f"REVOKE ALL ON {', '.join(private)} FROM {rid}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"ALTER ROLE {rid} SET statement_timeout = '60s'")                       # a runaway query cannot hog the database
    except sa.exc.DBAPIError:
        log.warning("could not set the statement timeout for %s", role)
    if password is None:
        return None
    return engine.url.set(drivername="postgresql", username=role, password=password).render_as_string(hide_password=False)


# ----------------------------------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------------------------------
def _power_problems(conn, role: str) -> list[str]:
    """Everything that makes an EXISTING login more than read-only: powerful attributes, extra roles, write access, private tables, creating objects."""
    problems = []
    row = conn.execute(sa.text("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = :r"), {"r": role}).fetchone()
    for col, what in RISKY_ATTRIBUTES:
        if getattr(row, col):
            problems.append(f"{role} can {what}")
    member_of = [r[0] for r in conn.execute(sa.text("""SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid JOIN pg_roles r ON r.oid = m.member
                                                       WHERE r.rolname = :r ORDER BY b.rolname"""), {"r": role})]
    if member_of:
        problems.append(f"{role} is a member of {', '.join(member_of)}, which gives it more than read access")
    has = lambda t, p: bool(conn.execute(sa.text("SELECT has_table_privilege(:w, :t, :p)"), {"w": role, "t": t, "p": p}).scalar())
    rels = _relations(conn)
    for t in rels:
        writes = [p for p in WRITE_PRIVILEGES if has(t, p)]
        if writes:
            problems.append(f"{role} can {'/'.join(writes)} on {t}")
    for t in PRIVATE_TABLES:
        if t in rels and has(t, "SELECT"):
            problems.append(f"{role} can read {t}, which is private")
    if conn.execute(sa.text("SELECT has_schema_privilege(:w, 'public', 'CREATE')"), {"w": role}).scalar():
        problems.append(f"{role} can create objects in the public schema")
    return problems


def audit_reader(engine, role: str = READER_ROLE) -> list[str]:
    """Problems that stop `role` from being a real, working read-only login for the site (an empty list means it is one)."""
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": role}).fetchone()
        if row is None:
            return [f"the login {role} does not exist"]
        problems = _power_problems(conn, role)
        if not row.rolcanlogin:
            problems.append(f"{role} cannot log in")
        if "fights" in _relations(conn) and not conn.execute(sa.text("SELECT has_table_privilege(:w, 'fights', 'SELECT')"), {"w": role}).scalar():
            problems.append(f"{role} cannot read fights, so the site would not work with it")
    return problems


def audit_legacy(engine, role: str = LEGACY_READER) -> list[str]:
    """If the old login still exists and is more than read-only, say so (a login that is clean, or absent, is fine)."""
    with engine.connect() as conn:
        if not _role_exists(conn, role):
            return []
        found = _power_problems(conn, role)
    return [f"the old login {role} still exists and is not safe: " + "; ".join(found) + f". Switch the site to {READER_ROLE}, run retire-old, then delete {role} in Neon."] if found else []


# ----------------------------------------------------------------------------------------------
# retire
# ----------------------------------------------------------------------------------------------
def retire_legacy(engine, role: str = LEGACY_READER) -> list[str]:
    """Take away everything the old login was granted and, where Neon allows it, stop it logging in. Returns a plain-language report."""
    rid = _ident(role)
    report = []
    with engine.begin() as conn:
        if not _role_exists(conn, role):
            return [f"There is no login called {role}; nothing to retire."]
        conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {rid}")
        conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {rid}")
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {rid}")
    report.append(f"Removed every table and schema grant {role} had.")
    for sql, done, failed in ((f"ALTER ROLE {rid} WITH NOLOGIN", f"{role} can no longer log in.", f"Could not stop {role} logging in from here"),
                              (f"ALTER ROLE {rid} WITH PASSWORD '{secrets.token_urlsafe(24)}'", f"Replaced {role}'s password with a random one (the old connection string is dead).",
                               f"Could not change {role}'s password from here")):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
            report.append(done)
        except sa.exc.DBAPIError:
            report.append(failed + ".")
    with engine.connect() as conn:
        still = _power_problems(conn, role)
    if still:
        report.append(f"{role} is still over-powered ({'; '.join(still)}). Delete it in the Neon console (Roles & Databases).")
    else:
        report.append(f"You can now safely delete {role} in the Neon console (Roles & Databases).")
    return report


def write_url_file(url: str, path: Path = URL_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The website's read-only database login")
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reader")
    r.add_argument("--rotate-password", action="store_true", help="issue a new password (the old one stops working)")
    sub.add_parser("audit")
    sub.add_parser("retire-old")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import model_v2
    engine = model_v2.make_engine()
    if args.command == "reader":
        url = setup_reader(engine, rotate_password=args.rotate_password)
        problems = audit_reader(engine)
        if problems:
            print("PROBLEMS with the new login:\n  " + "\n  ".join(problems))
            return 1
        print(f"The login {READER_ROLE} is ready and the audit passed: it can read the public tables and nothing else.")
        if url:
            print(f"Its connection string was written to {write_url_file(url)}\nCopy it into the site's secrets as DATABASE_URL (replacing the old line), then delete that file.")
        else:
            print("It already existed, so no new password was issued. Use --rotate-password if you need a new connection string.")
        for line in audit_legacy(engine):
            print("NOTE: " + line)
        return 0
    if args.command == "audit":
        problems = audit_reader(engine) + audit_legacy(engine)
        print("Audit passed: the site's login can read the public tables and nothing else, and no over-powered old login remains." if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    print("\n".join(retire_legacy(engine)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
