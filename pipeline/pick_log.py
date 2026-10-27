"""
The public pick log.

Every locked pick is appended to picks_log/locked_picks.csv by the daily GitHub job, which then commits the file. The
repository's history is therefore a public, time-stamped record of when each pick was made: a pick that was committed before
its event cannot have been chosen after the result.

  python pipeline/pick_log.py verify      # read the git history and report when each logged pick first appeared

Only picks are logged (who we picked, and with what probability). No betting odds are ever written here.
"""
from __future__ import annotations

import csv
import statistics
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "picks_log" / "locked_picks.csv"
LOG_REL = "picks_log/locked_picks.csv"
COLUMNS = ["pick_type", "bout_id", "event_date", "event_name", "fighter_a", "fighter_b", "predicted_winner", "p_a_wins",
           "locked_at_utc", "logged_at_utc"]
TYPES = {"model-only": "upcoming_picks_v2", "market-informed": "upcoming_picks_v3"}
PROB_TOLERANCE = 0.00051          # probabilities are written with 4 decimals


def _iso(ts) -> str:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(engine) -> pd.DataFrame:
    """Every pick in the database (both kinds), in card order. Cancelled bouts stay: a pick that was made is part of the record."""
    parts = []
    for ptype, table in TYPES.items():
        with engine.connect() as conn:
            exists = conn.execute(sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar()
        if not exists:
            continue
        df = pd.read_sql(sa.text(f"SELECT bout_id, event_date, event_name, bout_order, fighter_a, fighter_b, predicted_winner, p_a_wins, predicted_at "
                                 f"FROM {table}"), engine)
        df.insert(0, "pick_type", ptype)
        parts.append(df)
    if not parts:
        return pd.DataFrame(columns=COLUMNS + ["bout_order"])
    d = pd.concat(parts, ignore_index=True)
    d["event_date"] = pd.to_datetime(d.event_date).dt.strftime("%Y-%m-%d")
    d["p_a_wins"] = d.p_a_wins.astype(float).round(4)
    d["locked_at_utc"] = d.predicted_at.map(_iso)
    return d.sort_values(["event_date", "bout_order", "pick_type"]).reset_index(drop=True)


def read_log(path: Path = LOG_PATH) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _keys(df: pd.DataFrame) -> set:
    return set(zip(df.pick_type, df.bout_id)) if len(df) else set()


def new_rows(collected: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    have = _keys(existing)
    return collected[[(t, b) not in have for t, b in zip(collected.pick_type, collected.bout_id)]]


def changed_rows(collected: pd.DataFrame, existing: pd.DataFrame) -> list[str]:
    """Logged picks that no longer match the database. A locked pick must never change, so any hit here needs a human."""
    if existing.empty or collected.empty:
        return []
    m = existing.merge(collected, on=["pick_type", "bout_id"], suffixes=("_log", "_db"))
    bad = m[(m.predicted_winner_log != m.predicted_winner_db) | ((m.p_a_wins_log.astype(float) - m.p_a_wins_db.astype(float)).abs() > PROB_TOLERANCE)]
    return [f"{r.pick_type}: {r.fighter_a_log} vs {r.fighter_b_log} ({r.event_date_log})" for r in bad.itertuples()]


def append(rows: pd.DataFrame, path: Path = LOG_PATH, now: datetime | None = None) -> int:
    """Append rows to the log (never rewrites what is already there). Returns how many were added."""
    if rows.empty:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _iso(now or datetime.now(timezone.utc))
    fresh = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        if fresh:
            w.writerow(COLUMNS)
        for r in rows.itertuples():
            w.writerow([r.pick_type, r.bout_id, r.event_date, r.event_name, r.fighter_a, r.fighter_b, r.predicted_winner,
                        f"{float(r.p_a_wins):.4f}", r.locked_at_utc, stamp])
    return len(rows)


def should_write(args) -> bool:
    """Only the GitHub job writes the log (so your own computer never races it); --write-pick-log forces it."""
    import os
    return os.getenv("GITHUB_ACTIONS") == "true" or bool(getattr(args, "write_pick_log", False))


def step_pick_log(engine, today: date, rep, dry_run: bool, write: bool, path: Path = LOG_PATH, now: datetime | None = None) -> None:
    collected = collect(engine)
    existing = read_log(path)
    fresh = new_rows(collected, existing)
    bad = changed_rows(collected, existing)
    lines = []
    if bad:
        rep.need(f"Pick log: {len(bad)} logged pick(s) no longer match the database (a locked pick must never change): " + "; ".join(bad[:3]))
        lines.append(f"{len(bad)} logged picks differ from the database")
    if not write:
        lines.append(f"{len(fresh)} locked picks are not in the log yet; skipped here (only the GitHub job writes the log; --write-pick-log forces it)")
    elif dry_run:
        lines.append(f"{len(fresh)} newly locked picks would be added to the public pick log ({len(existing)} logged so far)")
    else:
        n = append(fresh, path, now)
        total = len(existing) + n
        lines.append(f"{n} newly locked picks added to the public pick log ({total} in total); the workflow commits the file")
    rep.add("Public pick log", *lines)


# ----------------------------------------------------------------------------------------------
# verification: read the repository history and report when each pick first appeared
# ----------------------------------------------------------------------------------------------
def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, encoding="utf-8").stdout


def first_seen(cwd: Path = ROOT, log: str = LOG_REL) -> dict:
    """{(pick_type, bout_id): (commit sha, commit time as an ISO string)} for the first commit in which each pick appears."""
    try:
        commits = [ln.split(" ", 1) for ln in _git(["log", "--reverse", "--format=%H %cI", "--", log], cwd).splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, OSError):       # not a git repository, git missing, or the folder does not exist
        return {}
    seen: dict = {}
    for sha, when in commits:
        try:
            text = _git(["show", f"{sha}:{log}"], cwd)
        except subprocess.CalledProcessError:
            continue                                     # the file was deleted in that commit
        for row in csv.DictReader(text.splitlines()):
            seen.setdefault((row["pick_type"], row["bout_id"]), (sha, when))
    return seen


def verify_report(cwd: Path = ROOT, log: str = LOG_REL) -> str:
    seen = first_seen(cwd, log)
    path = Path(cwd) / log
    if not seen or not path.exists():
        return "No picks are in the repository's history yet."
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    out, before_total = [], 0
    events: dict = {}
    for r in rows:
        key = (r["pick_type"], r["bout_id"])
        if key not in seen:
            continue                                     # in the working copy but not committed yet
        committed = datetime.fromisoformat(seen[key][1]).astimezone(timezone.utc)
        deadline = datetime.fromisoformat(r["event_date"] + "T00:00:00+00:00")
        locked = datetime.fromisoformat(r["locked_at_utc"].replace("Z", "+00:00"))
        events.setdefault((r["event_date"], r["event_name"]), []).append((committed < deadline, (committed - locked).total_seconds() / 3600))
    out.append(f"{'Event':58s} {'Picks':>5s} {'Committed before the event':>27s} {'Median hours from lock to commit':>34s}")
    for (d, name), items in sorted(events.items()):
        before = sum(1 for ok, _ in items if ok)
        before_total += before
        out.append(f"{(d + '  ' + name)[:58]:58s} {len(items):5d} {before:>27d} {statistics.median(h for _, h in items):34.1f}")
    total = sum(len(v) for v in events.values())
    out.append("")
    out.append(f"{before_total} of {total} logged picks were first committed before their event date (00:00 UTC on the day).")
    out.append("A pick committed after its event, or many hours after it was locked, was published after the fact: it was logged for the record, "
               "but the history cannot prove when it was made.")
    return "\n".join(out)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["verify"]:
        print(__doc__)
        return 2
    print(verify_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
