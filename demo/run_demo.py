"""
Try the whole project with no account and no setup:

    pip install -r demo/requirements.txt
    python demo/run_demo.py

It starts a private Postgres inside this folder (nothing is installed on your machine and nothing leaves it), fills it with INVENTED fighters and fights by
running the project's real pipeline, and opens the real website on them. The first run takes about a minute; after that it starts in seconds.

    --rebuild              throw the demo away and build a fresh one
    --port 8502            use another port
    --no-browser           do not open a browser tab
    --build-only           build the demo database and stop
    --size small|tiny      a smaller demo (faster to build)
    --database-url URL     use your own EMPTY Postgres database instead of the built-in one (for example a Docker container)
    --data-dir PATH        where the built-in database lives (default: your system's cache folder, never inside the project)

The demo's dates are measured from the day it was built, so a demo more than three days old is rebuilt automatically.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STALE_AFTER_DAYS = 3
for sub in ("pipeline", "demo"):
    sys.path.insert(0, str(ROOT / sub))


def default_data_dir() -> Path:
    """Where the demo's private database lives: the operating system's cache folder, NEVER inside the project. A database folder inside a synced
    folder (OneDrive, Dropbox, iCloud) gets locked and corrupted by the syncing."""
    if os.environ.get("UFC_DEMO_DIR"):
        return Path(os.environ["UFC_DEMO_DIR"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "ufc-predictor-demo"


def is_stale(meta: dict | None, today: date, build_version: str | None = None) -> bool:
    """A demo is out of date when it was built more than STALE_AFTER_DAYS ago, its 'next card' is no longer ahead of us, or (when `build_version` is given)
    it was made by an older version of the builder, for example before a model it should now contain existed."""
    if not meta or "built_on" not in meta:
        return True
    if build_version is not None and meta.get("build_version") != build_version:
        return True
    built = date.fromisoformat(meta["built_on"])
    nxt = date.fromisoformat(meta["next_card"]) if meta.get("next_card") else built
    return (today - built).days > STALE_AFTER_DAYS or (nxt - today).days < 1


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_embedded(data_dir: Path):
    """A private Postgres living in `data_dir`. Returns (server, owner connection URL)."""
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        try:
            import pgserver
        except ImportError:
            raise SystemExit("The built-in database is not installed. Run:  pip install -r demo/requirements.txt\n"
                             "(or use your own empty Postgres with  --database-url postgresql://USER:PASSWORD@HOST/DBNAME )")
    data_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    return server, server.get_uri()


def stop_embedded(server) -> None:
    """Stop the private database for good. The library stops it when this is the last process registered as using it; an earlier run that was killed (a closed
    terminal window) can leave a stale registration behind, so if the server is still running after the polite request, stop it directly. It is our own private
    database, in our own folder, so stopping it is always right."""
    try:
        server.cleanup()
    finally:
        try:
            if (Path(server.pgdata) / "postmaster.pid").exists():
                server._stop()
        except Exception:
            pass


def read_meta(engine) -> dict | None:
    import sqlalchemy as sa
    try:
        with engine.connect() as conn:
            return dict(conn.execute(sa.text("SELECT key, value FROM demo_meta")).fetchall())
    except sa.exc.SQLAlchemyError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the demo: the real project on invented data")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--size", choices=["normal", "small", "tiny"], default="normal")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--database-url", help="your own EMPTY Postgres database, instead of the built-in one")
    ap.add_argument("--data-dir", type=Path, help="where the built-in database lives (default: the system cache folder)")
    args = ap.parse_args(argv)

    import sqlalchemy as sa
    import build_demo
    import db_roles
    import game_admin

    server = None
    if args.database_url:
        owner_url = args.database_url
    else:
        data_dir = (args.data_dir or default_data_dir()) / "pg"
        print(f"The demo's private database lives in {data_dir.parent}")
        server, owner_url = start_embedded(data_dir)
    owner_url = owner_url.replace("postgresql://", "postgresql+psycopg2://", 1).replace("postgres://", "postgresql+psycopg2://", 1)
    engine = sa.create_engine(owner_url)
    try:
        today = datetime.now(timezone.utc).date()
        meta = read_meta(engine)
        if args.rebuild or is_stale(meta, today, build_demo.BUILD_VERSION):
            print("Building the demo (about a minute): inventing fighters, then running the project's real pipeline on them.\n")
            try:
                build_demo.build(owner_url, today=today, size=args.size, seed=args.seed, log=print)
            except build_demo.BuildError as exc:
                print(f"\nNot built: {exc}")
                return 1
            print()
        else:
            print(f"Using the demo built on {meta['built_on']} (next card {meta['next_card']}). Use --rebuild for a fresh one.")
        if args.build_only:
            print("Built. Run again without --build-only to open it.")
            return 0
        reader, game = build_demo.login_names(engine.url.database)
        site_url = db_roles.setup_reader(engine, role=reader, rotate_password=True)      # fresh passwords every start; nothing is stored
        game_url = game_admin.setup(engine, game_role=game, reader_role=(reader,), rotate_password=True)
        if not port_free(args.port):
            print(f"Port {args.port} is busy. Try:  python demo/run_demo.py --port {args.port + 1}")
            return 1
        env = {**os.environ, "DATABASE_URL": site_url, "GAME_DATABASE_URL": game_url, "GAME_INVITE_CODE": build_demo.INVITE_CODE, "DEMO_MODE": "1",
               "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "pipeline"), os.environ.get("PYTHONPATH", "")]))}
        url = f"http://localhost:{args.port}"
        print(f"\nThe demo is running at {url}\n"
              f"  Game page: sign in as demo_ann (or demo_bob ...) with the PIN {build_demo.PLAYER_PIN}, or join yourself with the invite code '{build_demo.INVITE_CODE}'.\n"
              "  Everything here is invented. Press Ctrl+C to stop.\n")
        proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "streamlit_app.py"), "--server.port", str(args.port),
                                 "--server.address", "127.0.0.1",                       # this computer only: by default Streamlit listens on every network interface
                                 "--server.headless", "true", "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env)
        try:
            if not args.no_browser:
                time.sleep(3)
                webbrowser.open(url)
            return proc.wait()
        except KeyboardInterrupt:
            print("\nStopping the demo.")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0
    finally:
        engine.dispose()
        if server is not None:
            stop_embedded(server)


if __name__ == "__main__":
    sys.exit(main())
