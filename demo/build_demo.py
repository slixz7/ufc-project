"""
Build the demo database: invented fighters and fights, run through the project's REAL pipeline.

    python demo/build_demo.py --database-url postgresql://USER:PASSWORD@HOST/DBNAME [--size normal|small|tiny] [--seed 7]

Nothing in the data is real (see synth.py). Everything built from it is: the feature SQL, the winner model, the betting-market model, the
goes-the-distance model, the locking and grading of picks, the results recorder, the game tables, and the two restricted database logins.
The timeline is replayed with the pipeline's own "pretend today" options:

  history ...... the invented base tables (the layout the real loader fills), then the real model backtests
  L1, L2 ....... two cards that have already happened: loaded, picked and LOCKED about nine days before (market lines two days before), then their
                 results arrive in the data and everything is graded. Their lock times are backdated so the record reads like a real timeline.
  U1, U2, U3 ... three cards still to come, with locked picks (U1 also has its market-informed picks)
  the game ..... fictional players with picks on the played cards (so there is a leaderboard), a few on the next card, and start times

It refuses to touch any database that already holds something else: it only ever builds into an EMPTY database, or rebuilds one it built before.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import random
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo"):
    sys.path.insert(0, str(ROOT / sub))

import auto_odds  # noqa: E402
import bout_results  # noqa: E402
import db_roles  # noqa: E402
import game_admin  # noqa: E402
import game_db  # noqa: E402
import model_distance  # noqa: E402
import model_distance_v2  # noqa: E402
import model_v2  # noqa: E402
import model_v3  # noqa: E402
import synth  # noqa: E402
import upcoming  # noqa: E402

INVITE_CODE = "demo"
BUILD_VERSION = "2"             # bump when a change means a demo built by older code should be rebuilt (the launcher compares it); 2 = the market-informed distance model
# Database logins belong to the whole Postgres SERVER, not to one database. The demo therefore uses logins of its own, named after its database, so that
# building a demo on a server that also holds a real project can never change the real `site_reader` or `game_app`, and two demos cannot break each other.
READER_PREFIX, GAME_PREFIX = "demo_site_", "demo_game_"


def login_names(database: str | None) -> tuple[str, str]:
    """(the read-only login, the game login) for the demo in this database."""
    tag = re.sub(r"[^a-z0-9_]", "_", (database or "db").lower())[:30] or "db"
    return READER_PREFIX + tag, GAME_PREFIX + tag


PLAYER_PIN = "123456"
PLAYERS = [("demo_ann", .62), ("demo_bob", .58), ("demo_cyd", .55), ("demo_dee", .52), ("demo_eli", .50), ("demo_fay", .47)]
DDL = """
-- The two base tables, with the column types and indexes of the real database (read from it on 2026-10-30): the counts are bigint, the three attempt-type
-- columns and the heights and reaches are decimals.
CREATE TABLE fights (fight_url TEXT PRIMARY KEY, event_date DATE NOT NULL, event_name TEXT, weight_class TEXT, title_fight BOOLEAN, num_rounds DOUBLE PRECISION,
    result TEXT, finish_round BIGINT, finish_time TEXT);
CREATE TABLE fighter_fights (fight_url TEXT NOT NULL, fighter_url TEXT NOT NULL, fighter_name TEXT, side BIGINT, won BIGINT, sig_land BIGINT,
    sig_att BIGINT, td_land BIGINT, td_att BIGINT, sub_att BIGINT, kd BIGINT, ctrl_sec BIGINT,
    dist_att DOUBLE PRECISION, clinch_att DOUBLE PRECISION, ground_att DOUBLE PRECISION, sig_absorbed BIGINT, opp_td_land BIGINT,
    opp_td_att BIGINT, height_cm DOUBLE PRECISION, reach_cm DOUBLE PRECISION, stance TEXT, dob DATE);
CREATE UNIQUE INDEX ux_ff_fight_fighter ON fighter_fights (fight_url, fighter_url);
CREATE INDEX ix_ff_fighter ON fighter_fights (fighter_url);
CREATE INDEX ix_ff_fight ON fighter_fights (fight_url);
CREATE INDEX ix_fights_date ON fights (event_date);
CREATE TABLE demo_meta (key TEXT PRIMARY KEY, value TEXT);
"""


class BuildError(RuntimeError):
    pass


def assert_safe_to_build(engine) -> None:
    """Only ever build into an empty database, or one this builder made before. Never into a real project database."""
    host = (engine.url.host or "").lower()
    if "neon.tech" in host or "amazonaws.com" in host or "supabase" in host:
        raise BuildError("That looks like a hosted database, not a scratch one. The demo builder will not touch it.")
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names()) | set(insp.get_view_names())
    if tables and "demo_meta" not in tables:
        raise BuildError(f"That database already contains tables ({', '.join(sorted(tables)[:4])}...) and is not a demo database. The builder only fills an EMPTY database, "
                         "so it cannot damage anything. Point it at a new empty one.")


def _run(label: str, fn, argv: list[str], log) -> None:
    """Run one pipeline command quietly (it prints its own detail); show that detail only if it fails."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = fn(argv)
    except SystemExit as exc:
        rc = f"stopped: {exc}"
    if rc not in (0, None):
        raise BuildError(f"{label} failed ({rc})\n{out.getvalue()[-1500:]}")
    log(f"  {label}")


def _bouts(engine, event_name: str) -> pd.DataFrame:
    return pd.read_sql(sa.text("SELECT bout_id, event_date, bout_order, fighter_a_name AS a, fighter_b_name AS b FROM upcoming_fights WHERE event_name = :e AND status = 'scheduled'"),
                       engine, params={"e": event_name})


def _backdate(engine, bout_ids: list[str], locked: datetime, graded: datetime | None, lines: datetime) -> None:
    """The picks were locked when the pipeline ran (today); for a card that has already been played, say they were locked back then."""
    ids = list(bout_ids)
    with engine.begin() as conn:
        for table, col, when in (("upcoming_fights", "loaded_at", locked - timedelta(days=1)), ("upcoming_picks_v2", "predicted_at", locked), ("upcoming_predictions", "predicted_at", locked),
                                 ("upcoming_distance_v1", "predicted_at", locked), ("upcoming_picks_v3", "predicted_at", lines), ("upcoming_distance_v2", "predicted_at", lines), ("upcoming_odds", "entered_at", lines - timedelta(hours=3)),
                                 ("upcoming_picks_v2", "graded_at", graded), ("upcoming_picks_v3", "graded_at", graded), ("upcoming_predictions", "graded_at", graded),
                                 ("upcoming_distance_v1", "graded_at", graded), ("upcoming_distance_v2", "graded_at", graded)):
            if when is None:
                continue
            try:
                conn.execute(sa.text(f"UPDATE {table} SET {col} = :w WHERE bout_id = ANY(:ids) AND {col} IS NOT NULL"), {"w": when, "ids": ids})
            except sa.exc.ProgrammingError:
                conn.rollback()
                raise


def _result_lookup(card) -> dict:
    """{frozenset of the two names: (winner, method, round)} from a played card's invented results."""
    fights, ffs = card.results
    if fights is None or fights.empty:
        return {}
    names = ffs.groupby("fight_url").fighter_name.apply(frozenset)
    winners = ffs[ffs.won == 1].set_index("fight_url").fighter_name
    out = {}
    for r in fights.itertuples():
        method = "Decision" if str(r.result).startswith("Decision") else "KO/TKO" if r.result in ("KO/TKO", "TKO - Doctor's Stoppage") else "Submission" if r.result == "Submission" else "Other"
        out[names[r.fight_url]] = (winners[r.fight_url], method, None if method == "Decision" else int(r.finish_round))
    return out


def _seed_players(engine, cards: dict, today: date, seed: int, log) -> None:
    rnd = random.Random(seed)
    players = {}
    for nick, _ in PLAYERS:
        p, err = game_db.register(engine, nick, PLAYER_PIN, True, datetime.combine(today - timedelta(days=60), datetime.min.time(), tzinfo=timezone.utc))
        if err:
            raise BuildError(f"could not add the demo player {nick}: {err}")
        players[nick] = p
    for key in ("L1", "L2"):
        card = cards[key]
        truth = _result_lookup(card)
        bouts = _bouts(engine, card.csv.event_name.iloc[0])
        when = datetime.combine(card.date - timedelta(days=4), datetime.min.time(), tzinfo=timezone.utc)
        for b in bouts.itertuples():
            winner, method, fround = truth.get(frozenset([b.a, b.b]), (None, None, None))
            if winner is None:
                continue
            rounds = 5 if int(b.bout_order) == 1 else 3
            for nick, skill in PLAYERS:
                if rnd.random() < 0.10:
                    continue                                            # nobody picks every fight
                right = rnd.random() < skill
                pick = winner if right else (b.b if winner == b.a else b.a)
                m = method if (right and rnd.random() < 0.45) else rnd.choice(["KO/TKO", "Submission", "Decision", "Decision"])
                r = None if m == "Decision" else (fround if (m == method and rnd.random() < 0.40 and fround) else rnd.randint(1, rounds))
                err = game_db.save_pick(engine, players[nick]["player_id"], {"bout_id": b.bout_id, "status": "scheduled", "event_date": b.event_date, "fighter_a": b.a, "fighter_b": b.b,
                                                                              "scheduled_rounds": rounds}, pick, m, r, when)
                if err:
                    raise BuildError(f"demo pick refused: {err}")
    u1 = cards["U1"]
    now = pretend_now(u1.date)                                            # the build runs on its own pretend clock, never the real one: its result must not depend on when it is run
    for nick, _ in PLAYERS[:3]:                                           # a few picks on the next card, so the page shows saved and unsaved fights
        for b in _bouts(engine, u1.csv.event_name.iloc[0]).itertuples():
            if rnd.random() < 0.55:
                rounds = 5 if int(b.bout_order) == 1 else 3
                m = rnd.choice(["KO/TKO", "Submission", "Decision"])
                game_db.save_pick(engine, players[nick]["player_id"], {"bout_id": b.bout_id, "status": "scheduled", "event_date": b.event_date, "fighter_a": b.a, "fighter_b": b.b,
                                                                       "scheduled_rounds": rounds}, rnd.choice([b.a, b.b]), m, None if m == "Decision" else rnd.randint(1, rounds), now)
    log(f"  {len(PLAYERS)} fictional players with picks (nickname + PIN {PLAYER_PIN}); the invite code is '{INVITE_CODE}'")


def pretend_now(next_card: date) -> datetime:
    """The moment the demo pretends it is: noon UTC two days before its next card, so that card is open for picks and its start time can be recorded."""
    return datetime.combine(next_card - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)


def build(url: str, today: date | None = None, size: str = "normal", seed: int = 7, log=print) -> dict:
    """Fill the (empty) database at `url` and return what the demo needs to run: the two restricted connection strings and the invite code."""
    today = today or datetime.now(timezone.utc).date()
    owner_url = url.replace("postgresql://", "postgresql+psycopg2://", 1).replace("postgres://", "postgresql+psycopg2://", 1)
    engine = sa.create_engine(owner_url)
    assert_safe_to_build(engine)
    saved_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = owner_url                                      # the pipeline modules read it
    logging.disable(logging.INFO)
    try:
        log(f"Inventing fighters and fights ({size}, seed {seed}) ...")
        data = synth.generate(today, size, seed)
        cards = data.cards
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
            for stmt in DDL.strip().split(";\n"):
                if stmt.strip():
                    conn.exec_driver_sql(stmt)
            data.fights.to_sql("fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
            data.fighter_fights.to_sql("fighter_fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
        log(f"  {len(data.fights)} invented fights loaded, through {data.fights.event_date.max()}")
        log("Running the project's real pipeline on them ...")
        _run("feature views (features.sql, predictions.sql)", upcoming.main, ["setup"], log)
        _run("winner model v2: tables and views", model_v2.main, ["setup"], log)
        _run("market model v3: tables and views", model_v3.main, ["setup"], log)
        _run("distance model: tables", model_distance.main, ["setup"], log)
        _run("market-informed distance model: tables", model_distance_v2.main, ["setup"], log)
        tmp = Path(tempfile.mkdtemp(prefix="demo_"))
        data.odds.to_csv(tmp / "odds.csv", index=False)
        _run("winner model v2: a walk-forward pick for every past fight", model_v2.main, ["backtest"], log)
        market = len(data.fights) >= synth.MARKET_MIN_FIGHTS
        if market:
            _run("market model v3: load the historical lines", model_v3.main, ["load-odds", "--csv", str(tmp / "odds.csv")], log)
            _run("market model v3: walk-forward backtest", model_v3.main, ["backtest"], log)
        else:
            log("  market model v3: skipped (too little history for it to learn from; a safeguard in the real model)")
        _run("distance model: walk-forward backtest", model_distance.main, ["backtest"], log)
        if market:
            _run("market-informed distance model: walk-forward backtest", model_distance_v2.main, ["backtest"], log)
        nowhere = str(tmp / "no_aliases.csv")                                    # never read the real project's name aliases
        for key in ("L1", "L2"):
            card = cards[key]
            name = card.csv.event_name.iloc[0]
            pick_day, lines_day = card.date - timedelta(days=9), card.date - timedelta(days=2)
            log(f"Replaying {name}: announced, picked and locked, then played and graded")
            card.csv.to_csv(tmp / f"{key}.csv", index=False)
            card.odds.to_csv(tmp / f"{key}_odds.csv", index=False)
            _run("load the card", upcoming.main, ["load", "--csv", str(tmp / f"{key}.csv"), "--allow-new", "--aliases", nowhere, "--today", str(pick_day)], log)
            _run("lock the winner picks (v2)", model_v2.main, ["predict", "--today", str(pick_day)], log)
            _run("lock the distance predictions", model_distance.main, ["predict", "--today", str(pick_day)], log)
            if market:
                _run("enter the market lines", model_v3.main, ["odds", "--csv", str(tmp / f"{key}_odds.csv"), "--today", str(lines_day)], log)
                _run("lock the market-informed picks (v3)", model_v3.main, ["predict", "--today", str(lines_day)], log)
                _run("lock the market-informed distance predictions", model_distance_v2.main, ["predict", "--today", str(lines_day)], log)
            ids = list(_bouts(engine, name).bout_id)
            _backdate(engine, ids, datetime.combine(pick_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15), None,
                      datetime.combine(lines_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15))
            fights, ffs = card.results
            with engine.begin() as conn:                                         # the event happens, and the data source publishes its results
                fights.to_sql("fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
                ffs.to_sql("fighter_fights", conn, if_exists="append", index=False, method="multi", chunksize=500)
            for label, fn in (("grade the card", upcoming.main), ("grade v2", model_v2.main), ("grade v3", model_v3.main), ("grade the distance picks", model_distance.main),
                              ("grade the market-informed distance picks", model_distance_v2.main)):
                if fn in (model_v3.main, model_distance_v2.main) and not market:
                    continue
                _run(label, fn, ["grade"], log)
            _backdate(engine, ids, datetime.combine(pick_day, datetime.min.time(), tzinfo=timezone.utc), datetime.combine(card.date + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
                      + timedelta(hours=14), datetime.combine(lines_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15))
            for label, fn in (("score the new fights (v2)", model_v2.main), ("score the new fights (v3)", model_v3.main), ("score the new fights (distance)", model_distance.main),
                              ("score the new fights (market-informed distance)", model_distance_v2.main)):
                if fn in (model_v3.main, model_distance_v2.main) and not market:
                    continue
                _run(label, fn, ["backtest"], log)
        for key in ("U1", "U2", "U3"):
            card = cards[key]
            log(f"Announcing {card.csv.event_name.iloc[0]} (in {(card.date - today).days} days)")
            card.csv.to_csv(tmp / f"{key}.csv", index=False)
            _run("load the card", upcoming.main, ["load", "--csv", str(tmp / f"{key}.csv"), "--allow-new", "--aliases", nowhere, "--today", str(today)], log)
            _run("lock the winner picks (v2)", model_v2.main, ["predict", "--today", str(today)], log)
            _run("lock the distance predictions", model_distance.main, ["predict", "--today", str(today)], log)
        card = cards["U1"]                                                        # the next card is close enough for the market-informed picks
        if market:
            card.odds.to_csv(tmp / "U1_odds.csv", index=False)
            pretend = card.date - timedelta(days=2)
            _run("enter the market lines for the next card", model_v3.main, ["odds", "--csv", str(tmp / "U1_odds.csv"), "--today", str(pretend)], log)
            _run("lock its market-informed picks (v3)", model_v3.main, ["predict", "--today", str(pretend)], log)
            _run("lock its market-informed distance predictions", model_distance_v2.main, ["predict", "--today", str(pretend)], log)
        log("Writing the plain-language reasons behind the picks ...")
        for label, fn in (("reasons for the locked picks (v2)", model_v2.main), ("reasons for the locked picks (v3)", model_v3.main)):
            if fn is model_v3.main and not market:
                continue
            _run(label, fn, ["explain"], log)
        for label, fn in (("reasons for the past picks (v2)", model_v2.main), ("reasons for the past picks (v3)", model_v3.main)):
            if fn is model_v3.main and not market:
                continue
            _run(label, fn, ["explain-past"], log)
        _run("record how the played bouts ended", bout_results.main, ["record", "--today", str(today)], log)
        starts = [{"event_date": cards["U1"].date, "first_start": datetime.combine(cards["U1"].date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=19), "bouts": 10, "distinct_times": 6},
                  {"event_date": cards["U2"].date, "first_start": datetime.combine(cards["U2"].date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=18), "bouts": 9, "distinct_times": 5},
                  {"event_date": cards["U3"].date, "first_start": datetime.combine(cards["U3"].date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=22), "bouts": 10, "distinct_times": 6}]
        auto_odds.save_event_starts(engine, starts, pretend_now(cards["U1"].date))
        log("Setting up the game and the two restricted database logins ...")
        reader, game = login_names(engine.url.database)
        site_url = db_roles.setup_reader(engine, role=reader, rotate_password=True)      # on a rebuild the logins exist already: always issue fresh passwords
        game_url = game_admin.setup(engine, game_role=game, reader_role=(reader,), rotate_password=True)
        problems = db_roles.audit_reader(engine, role=reader) + game_admin.audit(engine, game_role=game, reader_role=(reader,))
        if problems:
            raise BuildError("the security audit failed on the demo database: " + "; ".join(problems[:3]))
        log("  both audits pass: the site's login can only read, the game's login can only touch the game tables")
        _seed_players(engine, cards, today, seed, log)
        _run("record how the played bouts ended (again, for the game)", bout_results.main, ["record", "--today", str(today)], log)
        with engine.begin() as conn:
            for k, v in (("built_on", str(today)), ("size", size), ("seed", str(seed)), ("next_card", str(cards["U1"].date)), ("market_model", str(market)), ("build_version", BUILD_VERSION)):
                conn.execute(sa.text("INSERT INTO demo_meta (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": k, "v": v})
        return {"database_url": site_url, "game_database_url": game_url, "invite_code": INVITE_CODE, "player_pin": PLAYER_PIN, "players": [n for n, _ in PLAYERS],
                "built_on": str(today), "next_card": str(cards["U1"].date), "market_model": market, "logins": [reader, game]}
    finally:
        logging.disable(logging.NOTSET)
        if saved_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_env


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the demo database from invented data, with the project's real pipeline")
    ap.add_argument("--database-url", required=True, help="an EMPTY Postgres database (or a demo database to rebuild)")
    ap.add_argument("--size", choices=sorted(synth.SIZES), default="normal")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    try:
        out = build(args.database_url, size=args.size, seed=args.seed)
    except BuildError as exc:
        print(f"Not built: {exc}")
        return 1
    print("\nDemo database ready. The site would connect with:\n  " + "\n  ".join(f"{k} = {'(hidden)' if 'url' in k else v}" for k, v in out.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
