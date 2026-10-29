"""
The friends' game: the rules, with no database and no web page in it, so every rule can be tested on its own.

Players pick, for each fight on an open card: the winner, how it ends (KO/TKO, Submission or Decision) and, for a finish, the round.
Picks close PICK_BUFFER (6 hours) before the card's first fight starts, the same for every card. The first-fight time comes from the odds feed; when a
card's time is not known yet, picks close at 18:00 UTC the day before its event date, which is safe for any card in the world. Two scores:

  Points          winner right +1; method right (winner also right) +1; round right (winner and method also right, a finish) +1.
  Versus the model  the winner call and the goes-the-distance call (a Decision pick means "goes the distance") are each scored as
                  your result minus the model's: +1 if you are right and the model is wrong, -1 if the reverse, 0 otherwise.
                  Copying the model therefore earns exactly 0; the only way up is to disagree with it and be right.

A bout that was cancelled, never happened, or had no winner is ignored (VOID). A fight that ends in a DQ scores the winner only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

METHODS = ("KO/TKO", "Submission", "Decision")
FINISHES = ("KO/TKO", "Submission")
RESERVED_NICKNAMES = {"model", "the model", "admin", "administrator", "system", "ufc", "moderator", "owner", "bot"}
NICKNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,18}[A-Za-z0-9]$")      # 3 to 20 characters, plain ASCII
PIN_PATTERN = re.compile(r"^[0-9]{4,8}$")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1                                          # about 16 MB and a few hundredths of a second per check


class GameError(ValueError):
    """A problem to show the player in plain words."""


# ----------------------------------------------------------------------------------------------
# who you are
# ----------------------------------------------------------------------------------------------
def parse_nickname(raw) -> tuple[str, str]:
    """(the nickname to show, the lower-case key that must be unique). Plain ASCII only, so no look-alike letters and nothing that could
    be mistaken for HTML; names that could pass for the model or an administrator are refused."""
    shown = " ".join(str(raw or "").split())
    if not NICKNAME_PATTERN.match(shown):
        raise GameError("A nickname is 3 to 20 characters: letters, numbers, spaces, dots, dashes or underscores.")
    key = shown.lower()
    if key in RESERVED_NICKNAMES or key.startswith("the model"):
        raise GameError("That nickname is reserved. Please choose another.")
    return shown, key


def parse_pin(raw) -> str:
    pin = str(raw or "").strip()
    if not PIN_PATTERN.match(pin):
        raise GameError("A PIN is 4 to 8 digits (6 is a good length).")
    return pin


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    """A salted scrypt hash. The PIN itself is never stored."""
    salt = os.urandom(16) if salt is None else salt
    dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    if not isinstance(stored, str):
        return False
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(str(pin).encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(want) // 2)
        return hmac.compare_digest(got, bytes.fromhex(want))
    except (ValueError, TypeError):
        return False


_DUMMY: list[str] = []


def dummy_hash() -> str:
    """Checked when a nickname does not exist, so an unknown nickname takes as long to refuse as a wrong PIN does."""
    if not _DUMMY:
        _DUMMY.append(hash_pin("000000"))
    return _DUMMY[0]


def invite_matches(entered, expected) -> bool:
    norm = lambda s: "".join(str(s or "").split()).lower()
    return bool(norm(expected)) and hmac.compare_digest(norm(entered).encode("utf-8"), norm(expected).encode("utf-8"))


# ----------------------------------------------------------------------------------------------
# picks
# ----------------------------------------------------------------------------------------------
PICK_BUFFER = timedelta(hours=6)                                              # picks close this long before the card's first fight starts, for every card


def buffer_text() -> str:
    h = int(PICK_BUFFER.total_seconds() // 3600)
    return f"{h} hour{'s' if h != 1 else ''}"


def cutoff(event_date) -> datetime:
    """00:00 UTC on the day of the event: the reference point when a card's first-fight time is not known."""
    d = pd.Timestamp(event_date).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def closes_at(event_date, first_start=None) -> datetime:
    """When picks close: PICK_BUFFER before the card's first fight starts (`first_start`). If that is not known yet, PICK_BUFFER before 00:00 UTC on
    the event date (18:00 UTC the day before): earlier than any card in the world starts, so it is safe, and usually earlier than the real rule."""
    if first_start is None or pd.isna(first_start):
        return cutoff(event_date) - PICK_BUFFER
    t = pd.Timestamp(first_start)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.to_pydatetime() - PICK_BUFFER


def picks_open(event_date, now: datetime, first_start=None) -> bool:
    return now < closes_at(event_date, first_start)


def validate_pick(bout: dict, winner, method, rnd, now: datetime) -> tuple[str | None, int | None]:
    """(an error to show, or None) and the round to store. `bout` needs status, event_date, fighter_a, fighter_b and scheduled_rounds."""
    if bout.get("status") != "scheduled":
        return "That bout is no longer on the card.", None
    if not picks_open(bout["event_date"], now, bout.get("first_start")):
        return "Picks for this card have closed.", None
    if winner not in (bout["fighter_a"], bout["fighter_b"]):
        return "Pick one of the two fighters.", None
    if method not in METHODS:
        return "Pick how the fight ends: KO/TKO, Submission or Decision.", None
    if method == "Decision":
        return None, None                                                    # a decision has no finishing round
    try:
        r = int(rnd)
    except (TypeError, ValueError):
        return "Pick the round the finish happens in.", None
    if not 1 <= r <= int(bout["scheduled_rounds"]):
        return f"The round must be between 1 and {int(bout['scheduled_rounds'])}.", None
    return None, r


# ----------------------------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------------------------
SCORED_COLUMNS = ["player_id", "nickname", "bout_id", "event_name", "event_date", "fighter_a", "fighter_b", "pick_name", "pick_method", "pick_round", "winner", "actual_method",
                  "finish_round", "winner_ok", "method_ok", "round_ok", "points", "model_winner", "vs_winner", "vs_distance", "vs_model"]


def score_picks(picks: pd.DataFrame, bouts: pd.DataFrame, results: pd.DataFrame, model_winners: pd.DataFrame, distance: pd.DataFrame) -> pd.DataFrame:
    """One row per pick on a bout that has a result.
    picks:         player_id, nickname, bout_id, pick_name, pick_method, pick_round
    bouts:         bout_id, event_name, event_date, fighter_a, fighter_b
    results:       bout_id, winner_side, actual_method, finish_round, went_distance
    model_winners: bout_id, model_winner (the pick the site shows: market-informed when there is one, otherwise model-only)
    distance:      bout_id, p_distance (the goes-the-distance model's chance)"""
    if picks.empty:
        return pd.DataFrame(columns=SCORED_COLUMNS)
    d = picks.merge(bouts, on="bout_id").merge(results, on="bout_id")
    d = d[d.winner_side.isin(["a", "b"]) & (d.actual_method != "VOID")].copy()
    if d.empty:
        return pd.DataFrame(columns=SCORED_COLUMNS)
    d["winner"] = np.where(d.winner_side == "a", d.fighter_a, d.fighter_b)
    d["winner_ok"] = d.pick_name == d.winner
    d["method_ok"] = d.winner_ok & (d.pick_method == d.actual_method)
    d["round_ok"] = d.method_ok & d.pick_method.isin(FINISHES) & (pd.to_numeric(d.pick_round, errors="coerce") == pd.to_numeric(d.finish_round, errors="coerce"))
    d["points"] = d.winner_ok.astype(int) + d.method_ok.astype(int) + d.round_ok.astype(int)
    d = d.merge(model_winners, on="bout_id", how="left").merge(distance[["bout_id", "p_distance"]], on="bout_id", how="left")
    has_model = d.model_winner.notna()
    model_ok = (d.model_winner == d.winner) & has_model
    d["vs_winner"] = np.where(has_model, d.winner_ok.astype(int) - model_ok.astype(int), 0)
    decided = d.actual_method.isin(METHODS) & d.p_distance.notna()                   # a DQ or other result is not a distance question
    went = d.actual_method == "Decision"
    you_ok = (d.pick_method == "Decision") == went
    model_dist_ok = (d.p_distance.fillna(0.0) >= 0.5) == went
    d["vs_distance"] = np.where(decided, you_ok.astype(int) - model_dist_ok.astype(int), 0)
    d["vs_model"] = d.vs_winner + d.vs_distance
    return d[SCORED_COLUMNS].reset_index(drop=True)


def leaderboard(scored: pd.DataFrame, year: int | None = None) -> pd.DataFrame:
    """One row per player, best first. Ties are broken by the versus-the-model score, then by nickname."""
    cols = ["nickname", "picks", "winners_right", "points", "vs_model"]
    if scored.empty:
        return pd.DataFrame(columns=cols)
    d = scored if year is None else scored[pd.to_datetime(scored.event_date).dt.year == year]
    if d.empty:
        return pd.DataFrame(columns=cols)
    g = d.groupby(["player_id", "nickname"]).agg(picks=("bout_id", "size"), winners_right=("winner_ok", "sum"), points=("points", "sum"), vs_model=("vs_model", "sum")).reset_index()
    return g.sort_values(["points", "vs_model", "nickname"], ascending=[False, False, True])[cols].reset_index(drop=True)


def versus_model_board(scored: pd.DataFrame, year: int | None = None) -> pd.DataFrame:
    lb = leaderboard(scored, year)
    return lb.sort_values(["vs_model", "points", "nickname"], ascending=[False, False, True]).reset_index(drop=True) if not lb.empty else lb
