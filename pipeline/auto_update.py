#!/usr/bin/env python3
"""
auto_update.py - hands-off upkeep for the UFC Fight Predictor.

One command runs everything in the right order (each step is separate, so one failure never blocks the others):
    cards         read the schedule and announced fight cards from Wikipedia and save them (upcoming_fights)
    v2            lock a model-only pick for every newly announced bout (model_v2.py predict)
    odds          fetch current betting lines from The Odds API and store a consensus for each announced bout
    lock          lock market-informed (v3) picks once an event is inside the lock window (default: 2 days before)
    results       download the latest results, add them, grade locked picks, score new fights
    refresh-odds  (Mondays) refresh the historical odds dataset and the v3 backtest
    odds-history  (a report, not part of `all`) what the daily line snapshots have collected so far
    pick-log      append newly locked picks to picks_log/locked_picks.csv (the GitHub job commits it; locally only with --write-pick-log)
    readme        (Mondays) refresh the generated numbers and tables in README.md from the database; the GitHub job commits the change
    digest        post newly locked picks and event results to Discord (needs DISCORD_WEBHOOK_URL), each exactly once
    discord-test  (not part of `all`) send one test message to check the webhook

    python pipeline/auto_update.py all --dry-run        # look, change nothing (do this first)
    python pipeline/auto_update.py all                  # the real thing (this is what the scheduled job runs)
    python pipeline/auto_update.py cards --dry-run --show              # lists every bout it read and how each name was matched
    python pipeline/auto_update.py cards --dry-run --save-raw raw      # also saves the downloaded pages for inspection
    python pipeline/auto_update.py odds  --dry-run --save-raw raw

Safety rules (this script writes to your database on its own, so it is deliberately careful)
  * Locked picks are never changed; every pick is made before its event.
  * A fighter name that is not an exact match is NEVER guessed. Near-misses are held for you to review
    (add a line to upcoming/aliases.csv); only a name with no similar fighter is treated as a UFC debut.
  * A card that suddenly has far fewer bouts than we already saved is skipped, not applied.
  * A bout is only marked cancelled when its whole card was read cleanly (no held names).
  * Anything that needs a human is listed under "Needs your attention" and makes the run end with a non-zero
    exit code, so GitHub emails you.

Environment: DATABASE_URL (owner string), ODDS_API_KEY (optional), KAGGLE_API_TOKEN (optional; the older KAGGLE_USERNAME + KAGGLE_KEY pair also works).
Sources: Wikipedia (text under CC BY-SA, https://en.wikipedia.org), The Odds API (https://the-odds-api.com).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import digest
import model_distance
import model_v2
import model_v3
import pick_log
import readme_numbers
import update_from_csv
import upcoming

from auto_cards import (
    clean,
    HORIZON_DAYS,
    MAX_SHRINK,
    parse_card,
    parse_date,
    parse_scheduled_events,
    SEPARATORS,
    step_cards,
    URGENT_DAYS,
    WIKI_API,
    wiki_html,
)
from auto_common import (
    log,
    Report,
    ROOT,
    UA,
)
from auto_names import (
    _pair_sim,
    ACTIVE_DAYS,
    letters_key,
    name_sim,
    resolve_name,
)
from auto_odds import (
    classify_unmatched,
    ensure_snapshot_tables,
    event_books,
    event_consensus,
    fetch_odds,
    match_line_to_bout,
    ODDS_DATASET_REPO,
    odds_history,
    ODDS_URL,
    orient_books,
    SNAPSHOT_DDL,
    SNAPSHOT_GAP_HOURS,
    step_odds,
    step_refresh_odds,
)
from auto_picks import (
    LOCK_DAYS,
    step_lock,
    step_v2,
)
from auto_results import (
    kaggle_api,
    kaggle_configured,
    KAGGLE_DATASET,
    step_results,
)

# Everything this file used to define now lives in the auto_* modules; it is re-exported here so the command line, the tests and anything
# that imports auto_update see exactly the names they always did.
__all__ = [
    "_pair_sim",
    "ACTIVE_DAYS",
    "classify_unmatched",
    "clean",
    "digest",
    "ensure_snapshot_tables",
    "event_books",
    "event_consensus",
    "fetch_odds",
    "HORIZON_DAYS",
    "kaggle_api",
    "kaggle_configured",
    "KAGGLE_DATASET",
    "letters_key",
    "LOCK_DAYS",
    "log",
    "main",
    "match_line_to_bout",
    "MAX_SHRINK",
    "model_distance",
    "model_v2",
    "model_v3",
    "name_sim",
    "ODDS_DATASET_REPO",
    "odds_history",
    "ODDS_URL",
    "orient_books",
    "parse_card",
    "parse_date",
    "parse_scheduled_events",
    "pick_log",
    "readme_numbers",
    "Report",
    "resolve_name",
    "ROOT",
    "run",
    "SEPARATORS",
    "SNAPSHOT_DDL",
    "SNAPSHOT_GAP_HOURS",
    "step_cards",
    "step_lock",
    "step_odds",
    "step_refresh_odds",
    "step_results",
    "step_v2",
    "UA",
    "upcoming",
    "update_from_csv",
    "URGENT_DAYS",
    "WIKI_API",
    "wiki_html",
]


# ----------------------------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------------------------
def run(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    engine = model_v2.make_engine()
    rep = Report()
    save_raw = Path(args.save_raw) if args.save_raw else None
    workdir = Path(args.workdir)
    steps = {
        "cards": lambda: step_cards(engine, today, rep, args.dry_run, args.horizon, save_raw, show=args.show),
        "v2": lambda: step_v2(engine, today, rep, args.dry_run),
        "odds": lambda: step_odds(engine, today, rep, args.dry_run, save_raw, show=args.show),
        "lock": lambda: step_lock(engine, today, rep, args.dry_run, args.lock_days),
        "pick-log": lambda: pick_log.step_pick_log(engine, today, rep, args.dry_run, write=pick_log.should_write(args)),
        "results": lambda: step_results(engine, today, rep, args.dry_run, workdir),
        "refresh-odds": lambda: step_refresh_odds(engine, today, rep, args.dry_run, workdir, args.command == "refresh-odds"),
        "readme": lambda: readme_numbers.step_readme(engine, today, rep, args.dry_run, write=os.getenv("GITHUB_ACTIONS") == "true", forced=args.command == "readme"),
        "digest": lambda: digest.run_digest(engine, today, rep, args.dry_run),
    }
    chosen = list(steps) if args.command == "all" else [args.command]
    for name in chosen:
        t0 = time.time()
        try:
            steps[name]()
            log.info("step %-13s ok (%.1fs)", name, time.time() - t0)
        except (Exception, SystemExit) as exc:               # one step failing must not stop the others (even one that calls exit())
            log.exception("step %s failed", name)
            rep.failed.append(name)
            rep.add(f"FAILED: {name}", f"{type(exc).__name__}: {str(exc)[:300]}")
            rep.need(f"The '{name}' step failed: {type(exc).__name__}: {str(exc)[:160]}")
    text = rep.render(today, args.dry_run)
    print("\n" + text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    if rep.failed:
        return 1
    return 3 if rep.attention else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hands-off upkeep for the UFC Fight Predictor")
    ap.add_argument("command", choices=["all", "cards", "v2", "odds", "lock", "pick-log", "results", "refresh-odds", "readme", "digest", "odds-history", "discord-test"])
    ap.add_argument("--dry-run", action="store_true", help="look and report; write nothing")
    ap.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    ap.add_argument("--write-pick-log", action="store_true", help="(pick-log) write the public pick log from this computer too; normally only the GitHub job does")
    ap.add_argument("--horizon", type=int, default=HORIZON_DAYS, help="how many days ahead to look for announced events")
    ap.add_argument("--lock-days", type=int, default=LOCK_DAYS, help="lock market-informed picks this many days before an event")
    ap.add_argument("--show", action="store_true", help="(cards) list every bout it read and how each name was matched; (odds) list each matched line and the ignored ones")
    ap.add_argument("--save-raw", help="folder to save the downloaded Wikipedia pages / odds response (to inspect or send to me)")
    ap.add_argument("--workdir", default=str(ROOT / "data" / "auto"), help="scratch folder for downloads")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "odds-history":
        return odds_history(model_v2.make_engine())
    if args.command == "discord-test":
        return digest.discord_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
