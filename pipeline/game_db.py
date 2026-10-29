"""
The friends' game's database layer: players, picks and login throttling. Plain parameterised SQL that runs on Postgres and SQLite.

It is used with a database user that can touch ONLY these three tables (see game_admin.py): the public site's read-only user cannot
read them at all, and this user cannot read or change anything else in the project. Everything the page is asked to do is checked
again here (the right fighters, an open card, a valid round), so a tampered request can do no more than a normal one.

Throttling is kept in the database, not in the page, so closing the browser or opening a new tab does not reset it:
  * a nickname is locked after 5 wrong PINs in 15 minutes, or 20 in a day;
  * registration pauses after 10 wrong invite codes in an hour;
  * at most MAX_PLAYERS players can join.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

import pandas as pd
import sqlalchemy as sa

import game_core as core

GAME_TABLES = ("game_players", "game_picks", "game_attempts")
MAX_PLAYERS = 100
LOGIN_LIMITS = ((5, timedelta(minutes=15)), (20, timedelta(hours=24)))
INVITE_LIMIT = (10, timedelta(hours=1))
DDL = [
    """CREATE TABLE IF NOT EXISTS game_players (
        player_id TEXT PRIMARY KEY, nickname TEXT NOT NULL, nickname_key TEXT NOT NULL UNIQUE, pin_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, last_login_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS game_picks (
        player_id TEXT NOT NULL, bout_id TEXT NOT NULL, pick_name TEXT NOT NULL, pick_method TEXT NOT NULL, pick_round INTEGER,
        submitted_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (player_id, bout_id))""",
    "CREATE TABLE IF NOT EXISTS game_attempts (kind TEXT NOT NULL, key TEXT NOT NULL, attempted_at TIMESTAMPTZ NOT NULL, ok BOOLEAN NOT NULL)",
    "CREATE INDEX IF NOT EXISTS game_attempts_idx ON game_attempts (kind, key, attempted_at)",
]


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.exec_driver_sql(stmt)


# ----------------------------------------------------------------------------------------------
# throttling
# ----------------------------------------------------------------------------------------------
def _utc(value) -> datetime:
    """Whatever the database hands back (a datetime, a naive datetime, or text) as a timezone-aware UTC datetime."""
    t = pd.Timestamp(value)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def _minutes_locked(conn, kind: str, key: str, now: datetime, limits) -> int:
    """0 if allowed; otherwise about how many minutes until the next attempt is allowed. Only failures since the last success count."""
    last_ok = conn.execute(sa.text("SELECT MAX(attempted_at) FROM game_attempts WHERE kind = :k AND key = :key AND ok = :ok"), {"k": kind, "key": key, "ok": True}).scalar()
    last_ok = _utc(last_ok) if last_ok is not None else None
    worst = 0.0
    for limit, window in limits:
        since = now - window
        if last_ok is not None and last_ok > since:
            since = last_ok
        failures = conn.execute(sa.text("SELECT attempted_at FROM game_attempts WHERE kind = :k AND key = :key AND ok = :ok AND attempted_at > :since ORDER BY attempted_at"),
                                {"k": kind, "key": key, "ok": False, "since": since}).fetchall()
        if len(failures) >= limit:               # locked until enough of these failures have aged out of the window
            frees_at = _utc(failures[len(failures) - limit][0]) + window
            worst = max(worst, (frees_at - now).total_seconds() / 60)
    return max(1, int(worst + 0.99)) if worst > 0 else 0


def _record(conn, kind: str, key: str, now: datetime, ok: bool) -> None:
    conn.execute(sa.text("INSERT INTO game_attempts (kind, key, attempted_at, ok) VALUES (:k, :key, :t, :ok)"), {"k": kind, "key": key, "t": now, "ok": ok})


# ----------------------------------------------------------------------------------------------
# joining and signing in
# ----------------------------------------------------------------------------------------------
def register(engine, nickname, pin, invite_ok: bool, now: datetime, max_players: int = MAX_PLAYERS):
    """(player, error). `invite_ok` is the result of checking the invite code against the secret; it is never stored."""
    with engine.begin() as conn:
        wait = _minutes_locked(conn, "invite", "global", now, (INVITE_LIMIT,))
        if wait:
            return None, f"Too many wrong invite codes. Joining is paused for about {wait} minutes."
        _record(conn, "invite", "global", now, bool(invite_ok))
        if not invite_ok:
            return None, "That invite code is not right."
        if conn.execute(sa.text("SELECT COUNT(*) FROM game_players")).scalar() >= max_players:
            return None, "The game is full."
    try:
        shown, key = core.parse_nickname(nickname)
        pin = core.parse_pin(pin)
    except core.GameError as exc:
        return None, str(exc)
    player = {"player_id": uuid.uuid4().hex, "nickname": shown}
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO game_players (player_id, nickname, nickname_key, pin_hash, created_at) VALUES (:id, :n, :k, :h, :t)"),
                         {"id": player["player_id"], "n": shown, "k": key, "h": core.hash_pin(pin), "t": now})
    except sa.exc.IntegrityError:
        return None, "That nickname is taken."
    return player, None


def login(engine, nickname, pin, now: datetime):
    """(player, error). The same message for an unknown nickname and a wrong PIN, and the same effort for both."""
    wrong = "Wrong nickname or PIN."
    try:
        _, key = core.parse_nickname(nickname)
        pin = core.parse_pin(pin)
    except core.GameError:
        return None, wrong
    with engine.begin() as conn:
        wait = _minutes_locked(conn, "login", key, now, LOGIN_LIMITS)
        if wait:
            return None, f"Too many wrong attempts. Try again in about {wait} minutes."
        row = conn.execute(sa.text("SELECT player_id, nickname, pin_hash FROM game_players WHERE nickname_key = :k"), {"k": key}).fetchone()
        ok = core.verify_pin(pin, row.pin_hash if row else core.dummy_hash()) and row is not None
        _record(conn, "login", key, now, ok)
        if not ok:
            return None, wrong
        conn.execute(sa.text("UPDATE game_players SET last_login_at = :t WHERE player_id = :id"), {"t": now, "id": row.player_id})
        return {"player_id": row.player_id, "nickname": row.nickname}, None


# ----------------------------------------------------------------------------------------------
# picks
# ----------------------------------------------------------------------------------------------
def save_pick(engine, player_id: str, bout: dict, winner, method, rnd, now: datetime) -> str | None:
    """Save or change a pick; returns an error to show, or None when it was saved."""
    err, stored_round = core.validate_pick(bout, winner, method, rnd, now)
    if err:
        return err
    with engine.begin() as conn:
        if not conn.execute(sa.text("SELECT 1 FROM game_players WHERE player_id = :p"), {"p": player_id}).fetchone():
            return "Please sign in again."
        conn.execute(sa.text("""INSERT INTO game_picks (player_id, bout_id, pick_name, pick_method, pick_round, submitted_at, updated_at)
                                VALUES (:p, :b, :w, :m, :r, :t, :t)
                                ON CONFLICT (player_id, bout_id) DO UPDATE SET pick_name = EXCLUDED.pick_name, pick_method = EXCLUDED.pick_method,
                                    pick_round = EXCLUDED.pick_round, updated_at = EXCLUDED.updated_at"""),
                     {"p": player_id, "b": bout["bout_id"], "w": winner, "m": method, "r": stored_round, "t": now})
    return None


def delete_pick(engine, player_id: str, bout: dict, now: datetime) -> str | None:
    """Remove a pick (what you see is what is saved: clearing a fight on the page clears it here). Only while the card is still open."""
    if bout.get("status") != "scheduled":
        return "That bout is no longer on the card."
    if not core.picks_open(bout["event_date"], now, bout.get("first_start")):
        return "Picks for this card have closed."
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM game_picks WHERE player_id = :p AND bout_id = :b"), {"p": player_id, "b": bout["bout_id"]})
    return None


def my_picks(engine, player_id: str) -> pd.DataFrame:
    return pd.read_sql(sa.text("SELECT bout_id, pick_name, pick_method, pick_round, updated_at FROM game_picks WHERE player_id = :p"), engine, params={"p": player_id})


def all_picks(engine) -> pd.DataFrame:
    return pd.read_sql(sa.text("""SELECT p.player_id, pl.nickname, p.bout_id, p.pick_name, p.pick_method, p.pick_round
                                  FROM game_picks p JOIN game_players pl ON pl.player_id = p.player_id"""), engine)


def delete_player(engine, player_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM game_picks WHERE player_id = :p"), {"p": player_id})
        conn.execute(sa.text("DELETE FROM game_players WHERE player_id = :p"), {"p": player_id})


# ----------------------------------------------------------------------------------------------
# for the owner (game_admin.py)
# ----------------------------------------------------------------------------------------------
def list_players(engine) -> pd.DataFrame:
    return pd.read_sql(sa.text("""SELECT pl.nickname, pl.created_at, pl.last_login_at, COUNT(p.bout_id) AS picks
                                  FROM game_players pl LEFT JOIN game_picks p ON p.player_id = pl.player_id
                                  GROUP BY pl.nickname, pl.created_at, pl.last_login_at ORDER BY pl.created_at"""), engine)


def reset_pin(engine, nickname: str, now: datetime, new_pin: str | None = None) -> str:
    """Set a new PIN (a random 6 digits unless given), clear any lock, and return it so the owner can pass it on."""
    _, key = core.parse_nickname(nickname)
    pin = core.parse_pin(new_pin) if new_pin else f"{secrets.randbelow(10 ** 6):06d}"
    with engine.begin() as conn:
        if not conn.execute(sa.text("UPDATE game_players SET pin_hash = :h WHERE nickname_key = :k"), {"h": core.hash_pin(pin), "k": key}).rowcount:
            raise core.GameError(f"There is no player called {nickname}.")
        conn.execute(sa.text("DELETE FROM game_attempts WHERE kind = 'login' AND key = :k"), {"k": key})
    return pin


def delete_by_nickname(engine, nickname: str) -> bool:
    _, key = core.parse_nickname(nickname)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT player_id FROM game_players WHERE nickname_key = :k"), {"k": key}).fetchone()
    if not row:
        return False
    delete_player(engine, row.player_id)
    return True
