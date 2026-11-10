"""
The watchdog: what if something stops WITHOUT failing loudly?

Everything else alerts when a job FAILS. These checks look for the quiet failures, and report through the daily job's own "Needs your attention" list (so a problem fails the run
and reaches the private alert channel, like any other):

  * STALE RESULTS. An event finished days ago and its locked picks are still not graded, so the results have not reached the database. The results come from one Kaggle dataset;
    if it stops updating, the live record quietly stops growing. (A card that was cancelled has its picks marked cancelled by the cards step, so it is not counted.)
  * A FULL DATABASE. The hosting plan has a storage cap and the odds history grows every day. This reports the size every run and raises a problem at 80% of the limit, which is the
    repository variable DB_SIZE_LIMIT_MB (default 500; check your plan's real limit on the host's dashboard).

  * PICKS WITH NO RESULT. A locked pick whose fight the grader could not match to a result is marked 'no_result': neither right nor wrong, and out of every record. That is correct for a draw, a
    no contest or a fight that fell through, but the same status is what a name mismatch would produce, and then a pick would vanish from the record unseen. So every run lists the picks that
    ended that way in the last 14 days (the event and the fighters, so that a person can check the real result), and three or more in that time fail the run: a draw or a no contest is rare, so
    that many points at results not being matched to picks.

Two more quiet failures are covered outside the database: heartbeat.py (the job did not run at all) and token_expiry.py (the backup token is about to expire).
"""
from __future__ import annotations

import logging
import os
from datetime import date

import sqlalchemy as sa

log = logging.getLogger("watchdog")
STALE_AFTER_DAYS = 5                 # results usually arrive within a day or two; five days with none is a real problem
DEFAULT_LIMIT_MB = 500
WARN_PERCENT = 80
NO_RESULT_DAYS = 14                  # how far back the report looks for picks that ended with no result
NO_RESULT_ALERT_AT = 3               # this many in that time is more likely a matching problem than rare outcomes, so it needs attention
NO_RESULT_SHOWN = 5                  # how many to name in the report


def stale_events(engine, today: date, days: int = STALE_AFTER_DAYS) -> list[dict]:
    """Past events (at least `days` ago) that still have locked, ungraded picks: [{'event_date', 'event_name', 'picks'}], oldest first."""
    if not sa.inspect(engine).has_table("upcoming_picks_v2"):
        return []
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""SELECT event_date, MIN(event_name) AS event_name, COUNT(*) AS picks FROM upcoming_picks_v2
                                       WHERE status = 'locked' AND graded_at IS NULL AND event_date <= :cutoff
                                       GROUP BY event_date ORDER BY event_date"""), {"cutoff": date.fromordinal(today.toordinal() - days)}).fetchall()
    return [{"event_date": r[0], "event_name": r[1], "picks": int(r[2])} for r in rows]


def stale_message(e: dict, today: date) -> str:
    when = e["event_date"]
    return (f"Results for the {e['event_name'] or 'card'} on {when:%b} {when.day} ({e['picks']} locked pick{'s' if e['picks'] != 1 else ''}) are still not in the database, "
            f"{(today - when).days} days after the event. The Kaggle dataset may have stopped updating (kaggle.com/datasets/scarekrow/ufc-data); the live record stops growing until it does.")


def no_result_picks(engine, today: date, days: int = NO_RESULT_DAYS) -> list[dict]:
    """Model-only picks (every bout has one) that ended as 'no_result' for an event in the last `days` days, oldest first: [{'event_name', 'event_date', 'fighter_a', 'fighter_b'}]."""
    if not sa.inspect(engine).has_table("upcoming_picks_v2"):
        return []
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""SELECT event_name, event_date, fighter_a, fighter_b FROM upcoming_picks_v2
                                       WHERE status = 'no_result' AND event_date >= :since AND event_date <= :today
                                       ORDER BY event_date, event_name, fighter_a, fighter_b"""),
                            {"since": date.fromordinal(today.toordinal() - days), "today": today}).fetchall()
    return [{"event_name": r[0], "event_date": r[1], "fighter_a": r[2], "fighter_b": r[3]} for r in rows]


def no_result_lines(rows: list[dict], days: int = NO_RESULT_DAYS) -> list[str]:
    """The report lines for those picks: a heading, then up to NO_RESULT_SHOWN of them by name."""
    if not rows:
        return [f"Picks with no result (last {days} days): none"]
    lines = [f"Picks with no result (last {days} days): {len(rows)}. A draw, a no contest or a fight that fell through is the usual reason; if the fight did have a winner, "
             "its names were not matched, so check them against the real result."]
    for r in rows[:NO_RESULT_SHOWN]:
        when = r["event_date"]
        lines.append(f"  {r['event_name'] or 'a card'} ({when:%Y-%m-%d}): {r['fighter_a']} vs. {r['fighter_b']}")
    if len(rows) > NO_RESULT_SHOWN:
        lines.append(f"  and {len(rows) - NO_RESULT_SHOWN} more")
    return lines


def no_result_problem(rows: list[dict], days: int = NO_RESULT_DAYS) -> str | None:
    """A problem to raise when there are NO_RESULT_ALERT_AT or more, naming the first few; one or two are only reported."""
    if len(rows) < NO_RESULT_ALERT_AT:
        return None
    names = "; ".join(f"{r['fighter_a']} vs. {r['fighter_b']} ({r['event_date']:%Y-%m-%d})" for r in rows[:3])
    return (f"{len(rows)} picks ended with no result in the last {days} days ({names}{'...' if len(rows) > 3 else ''}). A draw or a no contest is rare, so this looks like results "
            "not being matched to picks: check those fights' real results and the fighters' names.")


def newest_result(engine) -> date | None:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT MAX(event_date) FROM fights")).scalar()


def limit_mb(raw: str | None) -> tuple[float, str | None]:
    """The size limit in MB from the variable DB_SIZE_LIMIT_MB; a missing, empty or unusable value gives the default (and a note when it was unusable)."""
    if raw is None or not str(raw).strip():
        return float(DEFAULT_LIMIT_MB), None
    try:
        v = float(str(raw).strip())
        if v > 0 and v == v and v != float("inf"):
            return v, None
    except ValueError:
        pass
    return float(DEFAULT_LIMIT_MB), f"DB_SIZE_LIMIT_MB is {str(raw)[:20]!r}, which is not a positive number, so {DEFAULT_LIMIT_MB} MB was used."


def database_size(engine) -> tuple[int, list[tuple[str, int]]]:
    """(bytes in the whole database, the three biggest tables as (name, bytes)). Postgres only."""
    with engine.connect() as conn:
        total = int(conn.execute(sa.text("SELECT pg_database_size(current_database())")).scalar())
        big = conn.execute(sa.text("""SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                      WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 2 DESC LIMIT 3""")).fetchall()
    return total, [(r[0], int(r[1])) for r in big]


def _mb(nbytes: float) -> str:
    return f"{nbytes / 1024 / 1024:,.0f}" if nbytes >= 10 * 1024 * 1024 else f"{nbytes / 1024 / 1024:,.1f}"


def size_report(total: int, limit: float, biggest: list[tuple[str, int]]) -> tuple[list[str], str | None]:
    """(lines for the report, a problem to raise or None). The problem starts at WARN_PERCENT of the limit."""
    pct = 100.0 * total / (limit * 1024 * 1024)
    lines = [f"Database size: {_mb(total)} MB of {limit:,.0f} MB ({pct:.0f}%)"]
    if biggest:
        lines.append("Largest tables: " + ", ".join(f"{n} {_mb(b)} MB" for n, b in biggest))
    problem = None
    if pct >= WARN_PERCENT:
        problem = (f"The database is {pct:.0f}% full ({_mb(total)} of {limit:,.0f} MB). A hosting plan's storage cap stops writes, and the daily job would start failing. "
                   f"The biggest tables are {', '.join(n for n, _ in biggest) or 'unknown'}. Trim what is not needed or move to a bigger plan; if your plan's real limit "
                   "differs from this figure, set the repository variable DB_SIZE_LIMIT_MB.")
    return lines, problem


def step_watchdog(engine, today: date, rep) -> None:
    lines = []
    newest = newest_result(engine)
    if newest:
        lines.append(f"Newest results in the database: {newest:%b} {newest.day}, {newest:%Y} ({(today - newest).days} days ago)")
    stale = stale_events(engine, today)
    for e in stale:
        rep.need(stale_message(e, today))
    lines.append("Results: every past event's picks are graded" if not stale else f"Results: {len(stale)} past event(s) still waiting for results")
    limit, note = limit_mb(os.getenv("DB_SIZE_LIMIT_MB"))
    if note:
        lines.append(note)
    total, biggest = database_size(engine)
    size_lines, problem = size_report(total, limit, biggest)
    lines += size_lines
    if problem:
        rep.need(problem)
    no_result = no_result_picks(engine, today)
    lines += no_result_lines(no_result)
    problem = no_result_problem(no_result)
    if problem:
        rep.need(problem)
    rep.add("Watchdog", *lines)
