"""Locking picks: model-only picks for newly announced bouts, and market-informed picks once an event is inside the lock window."""
from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa

import model_distance
import model_v2
import model_v3
from auto_common import Report


LOCK_DAYS = 2              # lock market-informed picks when the event is this close


# ----------------------------------------------------------------------------------------------
# step: v2 picks for new bouts
# ----------------------------------------------------------------------------------------------
def step_v2(engine, today: date, rep: Report, dry_run: bool) -> None:
    with engine.connect() as conn:
        n = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_fights u LEFT JOIN upcoming_picks_v2 p USING (bout_id)
                                    WHERE u.status = 'scheduled' AND u.event_date > :t AND p.bout_id IS NULL"""), {"t": today}).scalar()
    with engine.connect() as conn:                                      # bouts still without a goes-the-distance prediction
        if sa.inspect(engine).has_table("upcoming_distance_v1"):
            nd = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_fights u LEFT JOIN upcoming_distance_v1 d ON d.bout_id = u.bout_id
                                         WHERE u.status = 'scheduled' AND u.event_date > :t AND d.bout_id IS NULL"""), {"t": today}).scalar()
        else:
            nd = conn.execute(sa.text("SELECT COUNT(*) FROM upcoming_fights WHERE status = 'scheduled' AND event_date > :t"), {"t": today}).scalar()
    if dry_run:
        rep.add("Model-only picks (v2)", f"{n} newly announced bouts would get a locked model-only pick")
        rep.add("Goes-the-distance predictions", f"{nd} bouts would get a locked prediction")
        return
    rc = model_v2.main(["predict", "--today", today.isoformat()])
    rep.add("Model-only picks (v2)", f"{n} bouts needed a pick; model_v2 predict returned {rc}")
    if rc != 0:
        raise RuntimeError(f"model_v2 predict returned {rc}")
    model_v2.main(["explain"])               # fills in any missing "Why this pick?" reasons (never blocks a pick)
    rc2 = model_distance.main(["predict", "--today", today.isoformat()])
    rep.add("Goes-the-distance predictions", f"{nd} bouts needed a prediction; model_distance predict returned {rc2}")
    if rc2 != 0:
        raise RuntimeError(f"model_distance predict returned {rc2}")


# ----------------------------------------------------------------------------------------------
# step: lock market-informed picks
# ----------------------------------------------------------------------------------------------
def step_lock(engine, today: date, rep: Report, dry_run: bool, lock_days: int) -> None:
    argv = ["predict", "--today", today.isoformat(), "--within-days", str(lock_days)] + (["--dry-run"] if dry_run else [])
    rc = model_v3.main(argv)
    rep.add("Market-informed picks (v3)", f"events within {lock_days} days are locked with the latest line; model_v3 predict returned {rc}")
    if rc not in (0, None):
        raise RuntimeError(f"model_v3 predict returned {rc}")
    if not dry_run:
        model_v3.main(["explain"])           # fills in any missing "Why this pick?" reasons (never blocks a pick)
    with engine.connect() as conn:
        soon = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_fights u LEFT JOIN upcoming_odds o USING (bout_id)
                                       WHERE u.status = 'scheduled' AND u.event_date > :t AND u.event_date <= :lim AND o.bout_id IS NULL"""),
                            {"t": today, "lim": today + timedelta(days=lock_days)}).scalar()
    if soon:
        rep.add("Bouts about to start with no betting line", f"{soon} bout(s) in the next {lock_days} days have no line and will stay model-only")
