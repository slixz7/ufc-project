"""
How each announced bout actually ended: who won, by what method, and in which round. The friends' game scores method and round
picks against this; nothing else in the project records it (the pick tables only know whether the pick was right).

One row per bout in `bout_results`, written by the daily job after the results are in the dataset:
    winner_side   'a' or 'b' (the card's first or second fighter), empty when the bout has no winner
    method        'KO/TKO' (doctor's stoppages included), 'Submission', 'Decision', 'DQ', 'Other', or 'VOID'
    finish_round  the round a KO/TKO or submission happened in (empty for decisions)
    went_distance 1 for a decision, 0 for a finish, empty for a DQ or other result
A bout whose night is in the dataset but which is not in it (a fight that fell through), or that had no winner (a draw or a
no contest), is recorded as VOID so the game can ignore it.

    python pipeline/bout_results.py record [--today D]
"""
from __future__ import annotations

import argparse
import difflib
import logging
import sys
from datetime import date, datetime, timezone

import pandas as pd
import sqlalchemy as sa

import model_distance
import model_v2
import upcoming

log = logging.getLogger("bout_results")
FINISH_METHODS = ("KO/TKO", "Submission")
DDL = """CREATE TABLE IF NOT EXISTS bout_results (
    bout_id TEXT PRIMARY KEY, event_date DATE, winner_side TEXT, method TEXT, finish_round INTEGER, went_distance INTEGER,
    result_raw TEXT, recorded_at TIMESTAMPTZ)"""


def method_group(result) -> str:
    r = "" if pd.isna(result) else str(result)
    if r.startswith("Decision"):
        return "Decision"
    if r in ("KO/TKO", "TKO - Doctor's Stoppage"):
        return "KO/TKO"
    if r == "Submission":
        return "Submission"
    return "DQ" if r == "DQ" else "Other"


def ensure_table(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(DDL)


def winner_side(bout, w_url: str, w_name: str):
    """'a' or 'b': which of the card's two fighters won. By fighter id when the card has it, otherwise by name (never a coin flip)."""
    if str(w_url) == str(bout.fighter_a_url):
        return "a"
    if str(w_url) == str(bout.fighter_b_url):
        return "b"
    n = upcoming.norm(w_name)
    ra = difflib.SequenceMatcher(None, n, upcoming.norm(bout.fighter_a_name)).ratio()
    rb = difflib.SequenceMatcher(None, n, upcoming.norm(bout.fighter_b_name)).ratio()
    if max(ra, rb) >= 0.85 and abs(ra - rb) >= 0.15:
        return "a" if ra > rb else "b"
    return None


def run_record(engine, today: date) -> int:
    ensure_table(engine)
    pend = pd.read_sql(sa.text("""SELECT u.bout_id, u.event_date, u.fighter_a_name, u.fighter_b_name, u.fighter_a_url, u.fighter_b_url
                                  FROM upcoming_fights u LEFT JOIN bout_results r ON r.bout_id = u.bout_id
                                  WHERE r.bout_id IS NULL AND u.status = 'scheduled' AND u.event_date < :t"""), engine, params={"t": today})
    if pend.empty:
        log.info("No past bouts waiting to be recorded.")
        return 0
    fights = pd.read_sql(sa.text("""SELECT f.fight_url, f.event_date, f.result, f.finish_round, ff.fighter_url, ff.fighter_name, ff.won
                                    FROM fights f JOIN fighter_fights ff ON ff.fight_url = f.fight_url"""), engine)
    fights["event_date"] = pd.to_datetime(fights.event_date)
    rows, waiting = [], 0
    now = datetime.now(timezone.utc)
    for b in pend.itertuples():
        d = pd.Timestamp(b.event_date)
        win = fights[(fights.event_date >= d - pd.Timedelta(days=1)) & (fights.event_date <= d + pd.Timedelta(days=1))]
        fu = model_distance.match_fight(win, b.fighter_a_url, b.fighter_b_url, b.fighter_a_name, b.fighter_b_name)
        base = {"bout_id": b.bout_id, "event_date": d.date(), "winner_side": None, "method": "VOID", "finish_round": None, "went_distance": None,
                "result_raw": None, "recorded_at": now}
        if fu is None:
            if len(win):                       # the dataset has that night but not this bout: it did not happen
                rows.append(base)
            else:
                waiting += 1                   # the dataset does not have that night yet
            continue
        g = win[win.fight_url == fu]
        raw = g.result.iloc[0]
        base["result_raw"] = None if pd.isna(raw) else str(raw)
        won = g[g.won == 1]
        side = winner_side(b, won.fighter_url.iloc[0], won.fighter_name.iloc[0]) if len(won) else None
        if side is None:                       # a draw, a no contest, or a winner we cannot place on the card
            rows.append(base)
            continue
        m = method_group(raw)
        fr = g.finish_round.iloc[0]
        base.update(winner_side=side, method=m, finish_round=int(fr) if m in FINISH_METHODS and pd.notna(fr) else None,
                    went_distance=1 if m == "Decision" else (0 if m in FINISH_METHODS else None))
        rows.append(base)
    if rows:
        with engine.begin() as conn:
            pd.DataFrame(rows).to_sql("bout_results", conn, if_exists="append", index=False, method="multi", chunksize=200)
    log.info("Recorded %d bout results (%d VOID); %d bouts are waiting for the dataset to include their event", len(rows), sum(r["method"] == "VOID" for r in rows), waiting)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record how announced bouts ended")
    ap.add_argument("command", choices=["record"])
    ap.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run_record(model_v2.make_engine(), date.fromisoformat(args.today) if args.today else date.today())


if __name__ == "__main__":
    sys.exit(main())
