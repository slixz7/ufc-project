"""Betting lines: fetch them from The Odds API, match them to announced bouts, keep the private odds history, and (Mondays) refresh the historical odds dataset."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import sqlalchemy as sa

import game_core
import model_distance_v2
import model_v3
from auto_common import Report, UA
from auto_names import _pair_sim, name_sim


ODDS_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
SNAPSHOT_GAP_HOURS = 3     # a bout gets at most one line snapshot per this many hours (so a repeated run adds nothing)
SHARED_TIME_MARGIN = timedelta(hours=5)       # when the earliest time the feed lists may be the MAIN card's (every fight shares one time, or only part of the card is listed yet), the early prelims are hours earlier
PLAUSIBLE_START = timedelta(days=2)           # a start time further than this from the event date is a feed glitch, and is ignored
EVENT_STARTS_DDL = """CREATE TABLE IF NOT EXISTS event_starts (
    event_date DATE PRIMARY KEY, first_start TIMESTAMPTZ NOT NULL, bouts INTEGER, distinct_times INTEGER, updated_at TIMESTAMPTZ)"""
ODDS_DATASET_REPO = "https://github.com/nlbair/ufc-data.git"


# ----------------------------------------------------------------------------------------------
# step: odds
# ----------------------------------------------------------------------------------------------
def fetch_odds(api_key: str, session=None, save_raw: Path | None = None):
    session = session or requests
    r = session.get(ODDS_URL, params={"regions": "us", "markets": "h2h", "oddsFormat": "american", "dateFormat": "iso", "apiKey": api_key},
                    headers={"User-Agent": UA}, timeout=30)
    if r.status_code == 401:
        raise RuntimeError("The Odds API rejected the key (401). Check the ODDS_API_KEY secret.")
    if r.status_code == 429:
        raise RuntimeError("The Odds API says the monthly quota or the rate limit is used up (429).")
    r.raise_for_status()
    if save_raw:
        save_raw.mkdir(parents=True, exist_ok=True)
        (save_raw / "odds_api_mma.json").write_text(r.text, encoding="utf-8")
    return r.json(), r.headers.get("x-requests-remaining")


def event_consensus(ev: dict):
    """(home, away, P(home) with margin removed as the median across bookmakers, number of books) or None."""
    home, away = ev.get("home_team"), ev.get("away_team")
    ps = []
    for bk in ev.get("bookmakers", []) or []:
        for m in bk.get("markets", []) or []:
            if m.get("key") != "h2h":
                continue
            prices = {o.get("name"): o.get("price") for o in m.get("outcomes", []) or [] if o.get("price") is not None}
            if home in prices and away in prices:
                try:
                    ps.append(model_v3.devig(float(prices[home]), float(prices[away])))
                except (ValueError, ZeroDivisionError):
                    continue
    if not ps or not home or not away:
        return None
    return home, away, float(np.median(ps)), len(ps)


def match_line_to_bout(home: str, away: str, commence: date, bouts: pd.DataFrame):
    """Return (bout row, home_is_a) for the best-matching announced bout, or None."""
    best, best_s = None, 0.0
    for r in bouts.itertuples():
        if abs((r.event_date - commence).days) > 1:
            continue
        s = _pair_sim(home, away, r.a, r.b)
        if s > best_s:
            best, best_s = r, s
    if best is None or best_s < 0.88:
        return None
    return best, name_sim(home, best.a) >= name_sim(home, best.b)


def classify_unmatched(home: str, away: str, commence: date, bouts: pd.DataFrame) -> str:
    """The Odds API's MMA feed also lists other promotions and rumoured or exhibition matchups, so most lines that match
    none of our bouts are simply not UFC. A line only matters when one of its fighters IS on a saved UFC bout within a day
    of it but against someone else (a late replacement opponent): 'card_changed'. Everything else is 'ignored'."""
    for r in bouts.itertuples():
        if abs((r.event_date - commence).days) > 1:
            continue
        if any(max(name_sim(x, r.a), name_sim(x, r.b)) >= 0.88 for x in (home, away)):
            return "card_changed"
    return "ignored"


SNAPSHOT_DDL = [
    """CREATE TABLE IF NOT EXISTS odds_snapshots (
        id             BIGSERIAL PRIMARY KEY,
        snapshot_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        bout_id        TEXT NOT NULL,
        event_date     DATE,
        fighter_a      TEXT,
        fighter_b      TEXT,
        commence_time  TIMESTAMPTZ,
        market_prob_a  DOUBLE PRECISION,     -- median across bookmakers, margin removed
        prob_min_a     DOUBLE PRECISION,
        prob_max_a     DOUBLE PRECISION,
        n_books        INTEGER,
        books          JSONB,                -- every bookmaker's prices, in card order: [{book, a, b, p_a}]
        source         TEXT)""",
    "CREATE INDEX IF NOT EXISTS odds_snapshots_bout_idx ON odds_snapshots (bout_id, snapshot_at)",
    """CREATE OR REPLACE VIEW odds_bout_summary AS
       WITH r AS (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY bout_id ORDER BY snapshot_at) AS rn_first,
                              ROW_NUMBER() OVER (PARTITION BY bout_id ORDER BY snapshot_at DESC) AS rn_last
                  FROM odds_snapshots s)
       SELECT bout_id, MAX(event_date) AS event_date, MAX(fighter_a) AS fighter_a, MAX(fighter_b) AS fighter_b, COUNT(*) AS snapshots,
              MIN(snapshot_at) AS first_seen, MAX(snapshot_at) AS last_seen,
              MAX(market_prob_a) FILTER (WHERE rn_first = 1) AS first_prob_a,
              MAX(market_prob_a) FILTER (WHERE rn_last = 1)  AS last_prob_a
       FROM r GROUP BY bout_id""",
    # the raw prices are kept private: the website's read-only logins (the current one and the old one) cannot see them (the app never shows odds)
    # (quote_ident and || rather than format('%I'): a % in a statement sent without parameters can upset the database driver)
    """DO $$ DECLARE r text; BEGIN
         FOREACH r IN ARRAY ARRAY['site_reader', 'app_reader'] LOOP
           IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
             EXECUTE 'REVOKE ALL ON odds_snapshots FROM ' || quote_ident(r);
             EXECUTE 'REVOKE ALL ON odds_bout_summary FROM ' || quote_ident(r);
           END IF;
         END LOOP;
       END $$""",
]


def _utc(value) -> datetime:
    """Whatever the database or the feed gives (aware, naive or text) as a timezone-aware UTC datetime."""
    t = pd.Timestamp(value)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def event_start_rows(snaps: list[dict], card_sizes: dict | None = None) -> list[dict]:
    """One row per event date: when its first fight is expected to start, from the start times the odds feed gave for that card's bouts.
    The earliest time wins. But that time may only be the main card's, in two cases: every bout has the SAME time, or the feed lists only PART of
    the card (`card_sizes` is how many bouts we have scheduled for each date; weeks out the feed shows just the top fights). Then SHARED_TIME_MARGIN
    is taken off it. For a US card the result is still later than 00:00 UTC on event day, so nothing changes there."""
    by: dict = {}
    for s in snaps:
        by.setdefault(pd.Timestamp(s["d"]).date(), []).append(_utc(s["ct"]))
    rows = []
    for d, times in sorted(by.items()):
        midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        ok = [t for t in times if abs(t - midnight) <= PLAUSIBLE_START]
        if not ok:
            continue
        first, distinct = min(ok), len(set(ok))
        size = (card_sizes or {}).get(d)
        margin = distinct == 1 or (size is not None and len(ok) < size)
        rows.append({"event_date": d, "first_start": first - SHARED_TIME_MARGIN if margin else first, "bouts": len(ok), "distinct_times": distinct, "card_size": size, "margin": margin})
    return rows


def ensure_event_starts(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(EVENT_STARTS_DDL)


def save_event_starts(engine, rows: list[dict], now: datetime) -> int:
    """Upsert the start times, returning how many were written. While a card is still open its start time follows the best current estimate, in either
    direction (a card only partly listed is estimated early, and the estimate relaxes as the rest is listed). Once a card's picks have closed it is left
    alone: a closed card must never reopen, and the early fights drop out of the feed once they start."""
    ensure_event_starts(engine)
    written = 0
    with engine.begin() as conn:
        for r in rows:
            old = conn.execute(sa.text("SELECT first_start FROM event_starts WHERE event_date = :d"), {"d": r["event_date"]}).scalar()
            if now >= game_core.closes_at(r["event_date"], None if old is None else _utc(old)):
                continue                                           # already closed (by its recorded start, or by the rule for an unknown start)
            first = r["first_start"]
            conn.execute(sa.text("""INSERT INTO event_starts (event_date, first_start, bouts, distinct_times, updated_at) VALUES (:d, :f, :b, :n, :t)
                                    ON CONFLICT (event_date) DO UPDATE SET first_start = EXCLUDED.first_start, bouts = EXCLUDED.bouts,
                                    distinct_times = EXCLUDED.distinct_times, updated_at = EXCLUDED.updated_at"""),
                         {"d": r["event_date"], "f": first, "b": r["bouts"], "n": r["distinct_times"], "t": now})
            written += 1
    return written


def ensure_snapshot_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in SNAPSHOT_DDL:
            conn.exec_driver_sql(stmt)


def event_books(ev: dict) -> list[dict]:
    """Every bookmaker's head-to-head prices for one listed fight: [{book, home, away, p_home}] (American prices)."""
    home, away = ev.get("home_team"), ev.get("away_team")
    out = []
    for bk in ev.get("bookmakers", []) or []:
        for m in bk.get("markets", []) or []:
            if m.get("key") != "h2h":
                continue
            prices = {o.get("name"): o.get("price") for o in m.get("outcomes", []) or [] if o.get("price") is not None}
            if home in prices and away in prices:
                try:
                    out.append({"book": bk.get("key"), "home": float(prices[home]), "away": float(prices[away]),
                                "p_home": model_v3.devig(float(prices[home]), float(prices[away]))})
                except (ValueError, ZeroDivisionError):
                    continue
    return out


def orient_books(books: list[dict], home_is_a: bool) -> list[dict]:
    """The same prices in card order (a = the card's fighter A), with each book's margin-free P(A)."""
    return [{"book": b["book"], "a": b["home"] if home_is_a else b["away"], "b": b["away"] if home_is_a else b["home"],
             "p_a": round(b["p_home"] if home_is_a else 1 - b["p_home"], 4)} for b in books]


def step_odds(engine, today: date, rep: Report, dry_run: bool, save_raw: Path | None, session=None, show: bool = False) -> None:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        rep.add("Betting lines (The Odds API)", "No ODDS_API_KEY set, skipped. Bouts stay model-only until you add one.")
        return
    events, remaining = fetch_odds(key, session, save_raw)
    now = datetime.now(timezone.utc)
    bouts = pd.read_sql(sa.text("""SELECT u.bout_id, u.event_date, u.fighter_a_name AS a, u.fighter_b_name AS b,
                                           (SELECT COUNT(*) FROM upcoming_picks_v3 p WHERE p.bout_id = u.bout_id) AS locked
                                    FROM upcoming_fights u WHERE u.status = 'scheduled' AND u.event_date >= :t"""), engine, params={"t": today})
    matched, ignored, changed, lopsided, skipped_locked, rows, snaps, shown = 0, [], [], [], 0, [], [], []
    for ev in events:
        cons = event_consensus(ev)
        if cons is None:
            continue
        home, away, p_home, n_books = cons
        try:
            commence_dt = datetime.fromisoformat(str(ev["commence_time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if commence_dt <= now:
            continue                                  # already started: a line is no longer "before the fight"
        commence = commence_dt.date()
        hit = match_line_to_bout(home, away, commence, bouts)
        if hit is None:
            if classify_unmatched(home, away, commence, bouts) == "card_changed":
                changed.append((home, away, commence))
            else:
                ignored.append((home, away, commence))
            continue
        bout, home_is_a = hit
        p_a = p_home if home_is_a else 1 - p_home
        books = orient_books(event_books(ev), home_is_a)
        snaps.append({"b": bout.bout_id, "d": bout.event_date, "fa": bout.a, "fb": bout.b, "ct": commence_dt, "p": p_a,
                      "pmin": min(x["p_a"] for x in books), "pmax": max(x["p_a"] for x in books), "n": len(books), "books": json.dumps(books),
                      "src": f"the-odds-api ({now:%Y-%m-%d %H:%M} UTC)"})
        if bout.locked:
            skipped_locked += 1                       # its pick is locked; the line is still recorded in the history above
            continue
        if not 0.02 <= p_a <= 0.98:
            lopsided.append((bout.a, bout.b, p_a))
            continue
        if bout.event_date <= today:
            continue                                  # event day: nothing left to lock, but the snapshot above is kept
        matched += 1
        shown.append(f"   {bout.event_date}  {bout.a} vs {bout.b}: {p_a:.0%} for {bout.a} (median of {n_books} book(s))")
        rows.append({"bout_id": bout.bout_id, "p": p_a, "src": f"the-odds-api median of {n_books} book(s), {now:%Y-%m-%d %H:%M} UTC"})
    sizes = {pd.Timestamp(k).date(): int(v) for k, v in bouts.groupby("event_date").size().items()}
    stored, starts = 0, event_start_rows(snaps, sizes)
    if not dry_run:
        ensure_snapshot_tables(engine)
        save_event_starts(engine, starts, now)
        with engine.begin() as conn:
            for r in rows:
                conn.execute(sa.text("""INSERT INTO upcoming_odds (bout_id, odds_a, odds_b, market_prob_a, source) VALUES (:b, NULL, NULL, :p, :s)
                                        ON CONFLICT (bout_id) DO UPDATE SET market_prob_a = EXCLUDED.market_prob_a, source = EXCLUDED.source,
                                        entered_at = now()"""), {"b": r["bout_id"], "p": r["p"], "s": r["src"]})
            for sn in snaps:
                res = conn.execute(sa.text("""INSERT INTO odds_snapshots (bout_id, event_date, fighter_a, fighter_b, commence_time, market_prob_a, prob_min_a,
                                                                           prob_max_a, n_books, books, source)
                                              SELECT :b, :d, :fa, :fb, :ct, :p, :pmin, :pmax, :n, CAST(:books AS jsonb), :src
                                              WHERE NOT EXISTS (SELECT 1 FROM odds_snapshots s WHERE s.bout_id = :b
                                                                AND s.snapshot_at > now() - make_interval(hours => :gap))"""), {**sn, "gap": SNAPSHOT_GAP_HOURS})
                stored += res.rowcount
    lines = [f"{len(events)} fights listed by the API; {matched} matched to announced UFC bouts" + (" (dry run, not saved)" if dry_run else " and saved"),
             f"{skipped_locked} already have a locked market-informed pick (their lines are final)",
             f"API credits left this month: {remaining if remaining is not None else 'unknown'}"]
    if dry_run:
        lines.append(f"{len(snaps)} line snapshots would be added to the odds history")
    else:
        total = pd.read_sql(sa.text("SELECT COUNT(*) AS n, COUNT(DISTINCT bout_id) AS b, MIN(snapshot_at)::date AS d FROM odds_snapshots"), engine).iloc[0]
        lines.append(f"{stored} new line snapshots stored; the history now holds {int(total.n)} snapshots of {int(total.b)} bouts since {total.d}")
    if starts:
        lines.append(f"first-fight start times {'would be ' if dry_run else ''}recorded for {len(starts)} card(s) (the game closes picks {game_core.buffer_text()} before them): "
                     + "; ".join(f"{r['event_date']} {r['first_start']:%H:%M} UTC ({r['bouts']} of {r['card_size']} fights listed{', with a 5-hour margin' if r['margin'] else ''})"
                                 if r.get("card_size") else f"{r['event_date']} {r['first_start']:%H:%M} UTC" for r in starts[:6]))
    for r in starts:                                               # a card close to its date that the feed lists only in part: its close time is a cautious estimate
        if r.get("card_size") and r["bouts"] < r["card_size"] and today < r["event_date"] <= today + timedelta(days=3):
            lines.append(f"only {r['bouts']} of {r['card_size']} fights are listed by the feed for the card on {r['event_date']}: its pick deadline is a cautious estimate until the rest are listed")
    covered = {r["event_date"] for r in starts}
    unknown = sorted({d for d in bouts.event_date if today < d <= today + timedelta(days=3) and d not in covered})
    if unknown:                                                    # the case where the game's midnight-UTC rule could close a card's picks too late
        lines.append("no first-fight start time from the odds feed yet for the card(s) on " + ", ".join(str(d) for d in unknown)
                     + ": the game closes their picks at 18:00 UTC the day before the event until a start time is known")
    if show:
        lines += shown
    for a, b, p in lopsided:
        lines.append(f"not used for a pick: the line for {a} vs {b} is too lopsided ({p:.0%}); it is still recorded in the history")
    if ignored:
        lines.append(f"{len(ignored)} other lines ignored (other promotions, rumoured or not-yet-announced matchups)")
        if show:
            lines += [f"   ignored: {h} vs {a} ({c})" for h, a, c in ignored[:15]]
    for h, a, c in changed:
        lines.append(f"CHECK THE CARD: a line lists {h} vs {a} on {c}, but one of them is on our card against someone else")
        if (c - today).days <= 5:
            rep.need(f"Odds: a line lists {h} vs {a} on {c}, but one of them is saved against a different opponent. "
                     "The card may need a replacement opponent (re-run the cards step).")
    rep.add("Betting lines (The Odds API)", *lines)


def odds_history(engine) -> int:
    """A plain report on the odds history collected so far, and (once results are in) how the last line did against our picks."""
    ok = pd.read_sql(sa.text("SELECT to_regclass('public.odds_snapshots') IS NOT NULL AS ok"), engine).ok.iloc[0]
    if not ok:
        print("No odds history yet. It starts with the first real run of the odds step.")
        return 0
    t = pd.read_sql(sa.text("SELECT COUNT(*) AS n, COUNT(DISTINCT bout_id) AS b, COUNT(DISTINCT snapshot_at::date) AS days, MIN(snapshot_at)::date AS d0, "
                            "MAX(snapshot_at)::date AS d1 FROM odds_snapshots"), engine).iloc[0]
    print(f"Odds history: {int(t.n)} snapshots of {int(t.b)} bouts over {int(t.days)} day(s), {t.d0} to {t.d1}.")
    s = pd.read_sql(sa.text("SELECT * FROM odds_bout_summary"), engine)
    multi = s[s.snapshots > 1]
    if len(multi):
        move = (multi.last_prob_a - multi.first_prob_a).abs() * 100
        print(f"Line movement between the first and last snapshot of a bout: average {move.mean():.1f} points, biggest {move.max():.1f} "
              f"({len(multi)} bouts with 2+ snapshots).")
    g = pd.read_sql(sa.text("""SELECT s.fighter_a, s.fighter_b, s.last_prob_a, v2.actual_winner, v2.correct AS v2c, v3.correct AS v3c
                               FROM odds_bout_summary s JOIN upcoming_picks_v2 v2 USING (bout_id) LEFT JOIN upcoming_picks_v3 v3 USING (bout_id)
                               WHERE v2.actual_winner IS NOT NULL"""), engine)
    if g.empty:
        print("No bout with a recorded line has a result yet. After an event is graded, this report compares the last line with our picks.")
        return 0
    fav_right = ((g.last_prob_a >= 0.5) == (g.actual_winner == g.fighter_a))
    print(f"Graded bouts with a recorded line: {len(g)}. The last line's favourite won {int(fav_right.sum())} ({fav_right.mean():.0%}); "
          f"our model-only picks {int(g.v2c.sum())} ({g.v2c.mean():.0%})" + (f"; market-informed picks {int(g.v3c.dropna().sum())} of {int(g.v3c.notna().sum())}." if g.v3c.notna().any() else "."))
    return 0


def step_refresh_odds(engine, today: date, rep: Report, dry_run: bool, workdir: Path, force: bool) -> None:
    if today.weekday() != 0 and not force:
        rep.add("Historical odds refresh", "Only runs on Mondays.")
        return
    if dry_run:
        rep.add("Historical odds refresh", "Would clone the odds dataset, reload it, and extend the v3 and market-informed goes-the-distance backtests.")
        return
    dest = workdir / "nlbair"
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], check=True, capture_output=True)
    else:
        subprocess.run(["git", "clone", "--depth", "1", ODDS_DATASET_REPO, str(dest)], check=True, capture_output=True)
    csv = dest / "data" / "ufc_all_fights_wide.csv"
    for argv in (["load-odds", "--csv", str(csv)], ["backtest"]):
        rc = model_v3.main(argv)
        if rc not in (0, None):
            raise RuntimeError(f"model_v3 {argv[0]} returned {rc}")
    rc = model_distance_v2.main(["backtest"])                 # the new lines let more past fights be scored with the market-informed goes-the-distance model
    if rc not in (0, None):
        raise RuntimeError(f"model_distance_v2 backtest returned {rc}")
    rep.add("Historical odds refresh", "reloaded the odds dataset and extended the v3 and market-informed goes-the-distance backtests")
