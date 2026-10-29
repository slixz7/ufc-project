"""
The distance-market experiment. It must find a real effect when one exists, never invent one, never use the future to score the past,
follow its pre-declared decision rule exactly, and never write to the database.
"""
import os
import re
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import experiment_distance_market as ex  # noqa: E402
import model_distance  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")


def planted(effect: float, seed: int = 1, per_year: int = 450, years=range(2013, 2027)) -> pd.DataFrame:
    """Fights with a stored distance prediction and an opening line. With effect > 0, how strong the favourite is really changes the chance of
    a decision, in a way the stored prediction cannot see; with effect = 0 the line is pure noise as far as the question goes."""
    rng = np.random.default_rng(seed)
    year = np.repeat(list(years), per_year)
    n = len(year)
    sig = lambda x: 1 / (1 + np.exp(-x))
    z, u = rng.normal(0, 0.5, n), rng.normal(0, 0.4, n)                  # what the stored model can see, and what it cannot
    open_p = np.clip(sig(rng.normal(0, 1.0, n)), 0.05, 0.95)
    s = np.abs(open_p - 0.5)
    went = (rng.random(n) < sig(-0.05 + z + u - effect * s)).astype(int)
    event = np.tile(np.arange(per_year), len(years)) // 15                    # 30 events a year
    return pd.DataFrame({
        "fight_url": [f"http://www.ufcstats.com/fight-details/{i:016x}" for i in range(n)], "year": year,
        "event_date": [pd.Timestamp(int(a), 1, 1) + pd.Timedelta(days=10 * int(b)) for a, b in zip(year, event)],
        "p_distance": sig(-0.05 + z), "went_distance": went, "scheduled_rounds": 3, "open_prob_f1": open_p,
        "close_prob_f1": np.clip(open_p + rng.normal(0, 0.03, n), 0.03, 0.97)})


@pytest.fixture(scope="module")
def strong():
    df = planted(3.0)
    return df, ex.walk_forward(df)


# ---------------------------------------------------------------------------------------------- it finds what is there, and only that
def test_it_finds_a_real_effect_when_one_is_planted(strong):
    v = ex.verdict(strong[1])
    assert v["adopt"] and all(v["checks"].values()), v
    assert v["gain"] > 0.005 and v["lo"] > 0 and v["years_better"] >= 8


def test_a_real_but_tiny_effect_is_dropped_because_the_rule_asks_for_a_gain_worth_having():
    v = ex.verdict(ex.walk_forward(planted(1.5)))
    assert 0 < v["gain"] < ex.RULE["min_gain"] and not v["adopt"]


def test_it_does_not_invent_an_effect_when_there_is_none():
    adopted = [ex.verdict(ex.walk_forward(planted(0.0, seed=s)))["adopt"] for s in range(1, 13)]
    assert sum(adopted) == 0, adopted                                                 # twelve different random worlds with a worthless line: none adopted


# ---------------------------------------------------------------------------------------------- it never uses the future
def test_a_year_is_never_scored_with_anything_from_that_year_or_later(strong):
    df, res = strong
    last, before = int(df.year.max()), int(df.year.max()) - 1
    pcols = [c for c in res.columns if c.startswith("p_")]

    flipped = df.copy()
    flipped.loc[flipped.year == last, "went_distance"] = 1 - flipped.loc[flipped.year == last, "went_distance"]
    r1 = ex.walk_forward(flipped)
    pd.testing.assert_frame_equal(r1[pcols], res[pcols])                              # the last year's results change no prediction at all

    scrambled = df.copy()
    rows = scrambled.year == last
    scrambled.loc[rows, "p_distance"] = 0.5
    scrambled.loc[rows, "open_prob_f1"] = 0.9
    r2 = ex.walk_forward(scrambled)
    older = res.year < last
    pd.testing.assert_frame_equal(r2.loc[older, pcols].reset_index(drop=True), res.loc[older, pcols].reset_index(drop=True))   # nothing in the last year touches an earlier year's predictions

    mid = df.copy()
    mid.loc[mid.year == before, "went_distance"] = 1 - mid.loc[mid.year == before, "went_distance"]
    r3 = ex.walk_forward(mid)
    pd.testing.assert_frame_equal(r3.loc[res.year < before, pcols].reset_index(drop=True), res.loc[res.year < before, pcols].reset_index(drop=True))
    assert not np.allclose(r3.loc[r3.year == last, "p_quadratic"], res.loc[res.year == last, "p_quadratic"])      # ...but the year before DOES feed the last year's model, as it should


def test_the_first_scored_year_waits_for_enough_history_and_small_years_are_skipped():
    res = ex.walk_forward(planted(0.0, per_year=450))
    assert res.year.min() == 2015                                                      # 2013 + 2014 = 900 fights >= 800
    small = planted(0.0, per_year=60)
    assert ex.walk_forward(small).empty                                                # never 800 earlier fights, never 100 in a year


# ---------------------------------------------------------------------------------------------- the pieces
def test_calibration_error_and_the_event_bootstrap_behave():
    assert ex.ece([1, 1, 1, 0, 0, 0, 0, 0, 0, 0], [0.3] * 10) == pytest.approx(0.0)
    assert ex.ece([0] * 10, [0.9] * 10) == pytest.approx(0.9)
    assert ex.losses([1, 0], [0.5, 0.5]) == pytest.approx([np.log(2)] * 2)
    same = np.full(40, 0.7)
    g, lo, hi = ex.bootstrap_gain(same, same - 0.01, np.repeat(np.arange(10), 4))
    assert g == pytest.approx(0.01) and lo == pytest.approx(0.01) and hi == pytest.approx(0.01)        # a constant gain has no uncertainty
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.3, 400)
    g, lo, hi = ex.bootstrap_gain(np.zeros(400), noise, np.repeat(np.arange(100), 4))
    assert lo < 0 < hi                                                                 # pure noise: the interval spans zero


def test_the_decision_rule_needs_every_condition(strong):
    res = strong[1]
    assert ex.verdict(res)["adopt"]
    tighter = {**ex.RULE, "min_gain": 0.5}
    assert not ex.verdict(res, tighter)["adopt"] and not ex.verdict(res, tighter)["checks"]["gain_big_enough"]
    years = {**ex.RULE, "min_year_share": 1.01}
    assert not ex.verdict(res, years)["adopt"] and not ex.verdict(res, years)["checks"]["enough_years_better"]
    calib = {**ex.RULE, "max_ece_worse": -1.0}
    assert not ex.verdict(res, calib)["adopt"] and not ex.verdict(res, calib)["checks"]["calibration_not_worse"]
    assert ex.verdict(res, tighter)["checks"]["interval_above_zero"]                   # the conditions are independent: failing one does not change another


def test_a_gain_that_could_be_luck_is_not_adopted_even_when_everything_else_looks_fine():
    v = ex.verdict(ex.walk_forward(planted(3.0, seed=5, per_year=120)))             # a small, noisy sample of a real effect
    assert v["gain"] >= ex.RULE["min_gain"] and v["year_share"] >= ex.RULE["min_year_share"] and v["checks"]["calibration_not_worse"]     # big enough, most years agree, calibrated...
    assert v["lo"] < 0 and not v["checks"]["interval_above_zero"] and not v["adopt"]                                                      # ...but it could easily be chance: only the interval stops it


def test_the_rule_is_written_down_in_the_script_and_matches_the_code():
    doc = ex.__doc__
    assert "THE DECISION RULE, fixed before any real result was seen" in doc
    assert "at least 0.002" in doc and "95% interval" in doc and "60% of the test years" in doc and "0.005 above" in doc
    assert ex.RULE == {"min_gain": 0.002, "min_year_share": 0.60, "max_ece_worse": 0.005} and ex.PRIMARY == "quadratic"


def test_the_report_states_the_verdict_and_the_rule_and_says_it_is_not_betting_advice(strong):
    text = ex.report(strong[1], ex.verdict(strong[1]))
    assert "VERDICT: ADOPT" in text and "THE TEST" in text
    assert "nothing here is betting advice" in text.lower() and "recalibrated" in text and "favourite 50-60%" in text and "80%+" in text
    null = ex.walk_forward(planted(0.0, seed=3))
    assert "VERDICT: DROP" in ex.report(null, ex.verdict(null))
    for n in (1, 2, 3, 4):
        assert re.search(rf"\n  {n}\. ", text)                                         # all four conditions are listed, each with PASS or FAIL


def test_too_little_data_is_explained_not_a_crash(monkeypatch):
    monkeypatch.setattr(ex, "load", lambda engine: planted(0.0, per_year=20))
    text, v = ex.run(None)
    assert v is None and "are needed" in text and "model_distance.py backtest" in text
    monkeypatch.setattr(ex, "load", lambda engine: planted(0.0, per_year=70).assign(year=2020))        # enough fights in all, but every one in the same year: nothing earlier to learn from
    text, v = ex.run(None)
    assert v is None and "No year" in text


def test_the_script_only_ever_reads():
    src = (ROOT / "pipeline" / "experiment_distance_market.py").read_text(encoding="utf-8")
    assert re.search(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE)\b", ex.QUERY, flags=re.I) is None
    assert "SET TRANSACTION READ ONLY" in src and "to_sql" not in src and "engine.begin" not in src


# ---------------------------------------------------------------------------------------------- on a real Postgres
@pytest.fixture
def lines_db():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    name = f"exp_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    eng = sa.create_engine(sa.engine.make_url(ADMIN_URL).set(database=name))
    model_distance.ensure_tables(eng)                                                  # the real table definitions, not copies
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS fight_odds \(.*?\);", (ROOT / "sql" / "model_v3.sql").read_text(encoding="utf-8"), flags=re.S).group(0)
    with eng.begin() as c:
        c.exec_driver_sql(ddl)
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _fill(eng, df: pd.DataFrame, drop_odds_for: int = 0, bad_lines: int = 0):
    dist = pd.DataFrame({"fight_url": df.fight_url, "event_date": df.event_date.dt.date, "p_distance": df.p_distance, "went_distance": df.went_distance,
                         "scheduled_rounds": df.scheduled_rounds})
    odds = pd.DataFrame({"fight_id": df.fight_url.str.split("/").str[4], "event_date": df.event_date.dt.date, "open_prob_f1": df.open_prob_f1, "close_prob_f1": df.close_prob_f1})
    odds = odds.iloc[drop_odds_for:].copy()                                           # the first fights have a prediction but no line
    odds.iloc[:bad_lines, odds.columns.get_loc("open_prob_f1")] = 1.0                 # and a few have a nonsense line (0 or 1)
    dist.to_sql("fight_distance_v1", eng, if_exists="append", index=False, method="multi", chunksize=500)
    odds.to_sql("fight_odds", eng, if_exists="append", index=False, method="multi", chunksize=500)


@needs_postgres
def test_it_joins_the_two_tables_the_way_the_real_ones_join_and_reaches_a_verdict(lines_db):
    df = planted(3.0)
    _fill(lines_db, df, drop_odds_for=50, bad_lines=7)
    loaded = ex.load(lines_db)
    assert len(loaded) == len(df) - 50 - 7                                            # fights without a line, and fights with a nonsense line, are left out
    assert loaded.event_date.is_monotonic_increasing                                  # oldest first, which is the order a walk-forward needs
    text, v = ex.run(lines_db)
    assert v is not None and v["adopt"] and "VERDICT: ADOPT" in text


@needs_postgres
def test_the_database_itself_refuses_any_write_from_the_experiment(lines_db):
    _fill(lines_db, planted(0.0, per_year=30))
    with pytest.raises(sa.exc.DBAPIError, match="read-only"):
        with ex.read_only(lines_db) as conn:
            conn.exec_driver_sql("DELETE FROM fight_distance_v1")
    with lines_db.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM fight_distance_v1")).scalar() == 30 * 14
    ex.run(lines_db)                                                                   # and a whole run changes nothing
    with lines_db.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM fight_distance_v1")).scalar() == 30 * 14
        assert conn.execute(sa.text("SELECT COUNT(*) FROM fight_odds")).scalar() == 30 * 14
