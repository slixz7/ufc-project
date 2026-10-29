"""
Scoring the friends' game from the database. The website and the Discord digest both use this, so the two can never disagree about a score.

`q(sql, params)` reads the project's public tables and returns a DataFrame (the website passes its cached read-only query function, the daily job a plain
one on the owner connection). `engine` is the connection that can read the game's own tables. A table that does not exist yet reads as empty.
"""
from __future__ import annotations

import pandas as pd
import sqlalchemy as sa

import game_core as core
import game_db

RESULTS_SQL = """SELECT u.bout_id, u.event_name, u.event_date, u.fighter_a_name AS fighter_a, u.fighter_b_name AS fighter_b, r.winner_side,
                        r.method AS actual_method, r.finish_round, r.went_distance
                 FROM upcoming_fights u JOIN bout_results r ON r.bout_id = u.bout_id"""
STARTS_SQL = "SELECT event_date, first_start FROM event_starts"
MODEL_SQL = "SELECT bout_id, predicted_winner AS model_winner, confidence FROM {table} WHERE status = 'locked'"
DIST_SQL = "SELECT bout_id, p_distance, scheduled_rounds FROM upcoming_distance_v1 WHERE status = 'locked'"
RESULT_COLUMNS = ["bout_id", "event_name", "event_date", "fighter_a", "fighter_b", "winner_side", "actual_method", "finish_round", "went_distance"]


def safe(q, sql: str, params: dict | None = None, columns=()) -> pd.DataFrame:
    """A read that returns an empty table instead of an error when a table does not exist yet."""
    try:
        return q(sql, params or {})
    except (sa.exc.SQLAlchemyError, pd.errors.DatabaseError, RuntimeError):      # pandas wraps a missing table in its own DatabaseError
        return pd.DataFrame(columns=list(columns))


def starts(q) -> dict:
    """{event date: when its first fight is expected to start}, for the cards the odds feed has given start times for."""
    df = safe(q, STARTS_SQL, columns=["event_date", "first_start"])
    return {pd.Timestamp(r.event_date).date(): pd.Timestamp(r.first_start) for r in df.itertuples() if pd.notna(r.first_start)}


def model_winners(q) -> pd.DataFrame:
    v2 = safe(q, MODEL_SQL.format(table="upcoming_picks_v2"), columns=["bout_id", "model_winner", "confidence"])
    v3 = safe(q, MODEL_SQL.format(table="upcoming_picks_v3"), columns=["bout_id", "model_winner", "confidence"])
    both = pd.concat([v3.assign(rank=0), v2.assign(rank=1)], ignore_index=True)                    # the pick the site shows: market-informed when there is one
    return both.sort_values("rank").drop_duplicates("bout_id")[["bout_id", "model_winner", "confidence"]].reset_index(drop=True)


def score_all(q, engine) -> pd.DataFrame:
    """Every player's scored picks on every bout that has a result (the columns of core.SCORED_COLUMNS)."""
    res = safe(q, RESULTS_SQL, columns=RESULT_COLUMNS)
    picks = game_db.all_picks(engine)
    if res.empty or picks.empty:
        return pd.DataFrame(columns=core.SCORED_COLUMNS)
    dist = safe(q, DIST_SQL, columns=["bout_id", "p_distance", "scheduled_rounds"])
    return core.score_picks(picks, res[["bout_id", "event_name", "event_date", "fighter_a", "fighter_b"]],
                            res[["bout_id", "winner_side", "actual_method", "finish_round", "went_distance"]], model_winners(q)[["bout_id", "model_winner"]], dist)
