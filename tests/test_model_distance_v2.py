"""
The market-informed goes-the-distance model (v2), added alongside v1: its maths, its backtest, how it locks and grades, how the daily job and the
public pick log treat it, and how the website shows it. The behavioural tests run on a small demo built by the real pipeline on a real Postgres.
"""
import inspect
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo", "app"):
    sys.path.insert(0, str(ROOT / sub))
import auto_picks  # noqa: E402
import auto_update as au  # noqa: E402
import build_demo  # noqa: E402
import distance_scores  # noqa: E402
import distance_ui  # noqa: E402
import experiment_distance_market as ex  # noqa: E402
import model_distance  # noqa: E402
import model_distance_v2 as dv2  # noqa: E402
import model_v2  # noqa: E402
import pick_log  # noqa: E402
from auto_common import Report  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 10, 29)


# ---------------------------------------------------------------------------------------------- the maths (no database)
def _fights(n=600, seed=3):
    rng = np.random.default_rng(seed)
    p = np.clip(rng.beta(8, 8, n), 0.2, 0.8)
    open_f1 = np.clip(rng.beta(2, 2, n), 0.06, 0.94)
    return pd.DataFrame({"p_distance": p, "p_v1": p, "open_prob_f1": open_f1, "fav_prob": np.maximum(open_f1, 1 - open_f1), "went_distance": (rng.random(n) < p).astype(int)})


def test_the_second_stage_is_exactly_the_recipe_the_experiment_tested():
    df = _fights()
    assert np.allclose(dv2.stack_design(df.p_v1, df.fav_prob), ex.design(df, "quadratic"))                 # the same three columns, to the last digit
    new, old = dv2.fit_stacker(df), ex.fit(df, "quadratic")
    assert np.allclose(dv2.stack_predict(new, df.p_v1, df.fav_prob), old.predict_proba(ex.design(df, "quadratic"))[:, 1])
    assert dv2.MIN_TRAIN == ex.MIN_TRAIN and dv2.C_REG == ex.C_REG                                          # the same threshold and regularisation as the pre-declared test


def test_the_favourites_strength_does_not_depend_on_which_fighter_is_listed_first():
    p, line = np.array([0.55, 0.40]), np.array([0.70, 0.30])                                                 # fighter A the favourite, then fighter A the underdog
    fav = np.maximum(line, 1 - line)
    assert np.allclose(fav, [0.70, 0.70])
    X = dv2.stack_design(p, fav)
    assert np.allclose(X[0, 1:], X[1, 1:]) and np.allclose(X[0, 1:], [0.20, 0.04])


def test_extreme_probabilities_do_not_break_it():
    X = dv2.stack_design(np.array([0.0, 1.0, 0.5]), np.array([0.5, 0.96, 0.5]))
    assert np.isfinite(X).all()


def test_a_better_second_stage_comes_from_a_real_relationship_and_a_useless_one_changes_little():
    rng = np.random.default_rng(1)
    n = 6000
    p = np.clip(rng.beta(8, 8, n), 0.2, 0.8)
    fav = np.clip(rng.beta(2, 3, n) * 0.5 + 0.5, 0.5, 0.95)
    went = (rng.random(n) < 1 / (1 + np.exp(-(np.log(p / (1 - p)) - 3.0 * (fav - 0.5))))).astype(int)        # lopsided matchups end early, in a way p cannot see
    tr, te = slice(0, 4000), slice(4000, n)
    T = pd.DataFrame({"p_v1": p, "fav_prob": fav, "went_distance": went})
    stack = dv2.stack_predict(dv2.fit_stacker(T.iloc[tr]), p[te], fav[te])
    ll = lambda q: float(ex.losses(went[te], q).mean())
    assert ll(stack) < ll(p[te]) - 0.01                                                                      # with a real link, the stack is clearly better than v1 alone


# ---------------------------------------------------------------------------------------------- how the daily job and the log are wired (no database)
def test_the_daily_job_locks_grades_and_extends_the_new_prediction_in_the_right_steps():
    assert 'model_distance_v2.main(["predict"' in inspect.getsource(au.step_lock) and '"--within-days"' in inspect.getsource(auto_picks.step_lock)
    results = inspect.getsource(au.step_results)
    assert 'model_distance_v2.main(["grade"])' in results and 'model_distance_v2.main(["backtest"])' in results
    assert 'model_distance_v2.main(["backtest"])' in inspect.getsource(au.step_refresh_odds)                  # the Monday line reload lets more past fights be scored
    assert hasattr(au, "model_distance_v2") and "model_distance_v2" in au.__all__
    assert inspect.getsource(auto_picks.step_lock).index("model_v3.main") < inspect.getsource(auto_picks.step_lock).index("model_distance_v2.main")   # after the winner picks, never before


def test_the_daily_workflow_commits_the_whole_log_folder_so_the_new_file_is_included():
    wf = (ROOT / ".github" / "workflows" / "auto-update.yml").read_text(encoding="utf-8")
    assert "git add picks_log" in wf


def _log_engine(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'log.db'}")
    model_distance.ensure_tables(eng)
    dv2.ensure_tables(eng)
    return eng


def _bout(i, table_cols=True):
    return dict(bout_id=f"b{i}", event_name="UFC X", event_date="2026-10-31", bout_order=i, weight_class="Lightweight", fighter_a=f"A{i}", fighter_b=f"B{i}",
                scheduled_rounds=3, p_distance=0.5 + i / 20, p_base=0.48, status="locked", predicted_at="2026-10-29 14:17:00")


def _lines(rep):
    return [line for _, ls in rep.sections for line in ls]


def test_market_informed_predictions_go_to_a_log_file_of_their_own_and_never_change(tmp_path):
    eng = _log_engine(tmp_path)
    pd.DataFrame([_bout(1), _bout(2)]).to_sql("upcoming_distance_v1", eng, if_exists="append", index=False)
    pd.DataFrame([dict(_bout(i), p_v1=0.5, fav_prob=0.7) for i in (1, 2)]).to_sql("upcoming_distance_v2", eng, if_exists="append", index=False)
    picks, dist = tmp_path / "log" / "locked_picks.csv", tmp_path / "log" / "locked_distance.csv"
    market = tmp_path / "log" / "locked_distance_market.csv"
    when = pd.Timestamp("2026-10-29T14:17:00Z").to_pydatetime()

    rep = Report()
    pick_log.step_pick_log(eng, TODAY, rep, dry_run=True, write=True, path=picks, dist_path=dist)
    assert not market.exists() and any("2 newly locked market-informed goes-the-distance predictions would be added" in x for x in _lines(rep))      # a dry run writes nothing
    rep = Report()
    pick_log.step_pick_log(eng, TODAY, rep, dry_run=False, write=True, path=picks, dist_path=dist, now=when)
    assert market.exists() and dist.exists()                                                                   # its own file, beside the other distance log
    rows = market.read_text(encoding="utf-8").splitlines()
    assert rows[0].split(",") == pick_log.DIST_COLUMNS and len(rows) == 3
    assert "0.5500" in rows[1] and "fav" not in rows[0] and "odds" not in rows[0].lower() and "0.7000" not in market.read_text(encoding="utf-8")   # only the chance: never the favourite's price or the line
    assert len(dist.read_text(encoding="utf-8").splitlines()) == 3                                             # the model-only log is exactly as before
    before = market.read_text(encoding="utf-8")
    rep = Report()
    pick_log.step_pick_log(eng, TODAY, rep, dry_run=False, write=True, path=picks, dist_path=dist, now=when)
    assert market.read_text(encoding="utf-8") == before and rep.attention == []                                # a second run adds nothing and complains about nothing
    with eng.begin() as c:
        c.execute(sa.text("UPDATE upcoming_distance_v2 SET p_distance = 0.99 WHERE bout_id = 'b1'"))
    rep = Report()
    pick_log.step_pick_log(eng, TODAY, rep, dry_run=False, write=True, path=picks, dist_path=dist, now=when)
    assert rep.attention and "market-informed goes-the-distance prediction" in rep.attention[0] and "must never change" in rep.attention[0]


def test_the_log_works_before_the_new_table_exists_and_verify_reports_all_three_logs(tmp_path, capsys):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    model_distance.ensure_tables(eng)                                                                          # an older database: no v2 table
    pd.DataFrame([_bout(1)]).to_sql("upcoming_distance_v1", eng, if_exists="append", index=False)
    rep = Report()
    pick_log.step_pick_log(eng, TODAY, rep, dry_run=False, write=True, path=tmp_path / "p.csv", dist_path=tmp_path / "d.csv")
    assert not (tmp_path / "locked_distance_market.csv").exists() and (tmp_path / "d.csv").exists() and rep.attention == []
    assert pick_log.main(["verify"]) == 0
    assert "MARKET-INFORMED GOES-THE-DISTANCE PREDICTIONS" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- the website's helpers (no database)
def test_the_distance_line_is_unchanged_without_the_new_figure_and_shows_it_beside_the_old_one_with_it():
    plain = distance_ui.line_html(0.64, 3)
    assert "market-informed" not in plain and "64%" in plain and "could go either way" not in plain.replace("likely to go to a decision", "")
    both = distance_ui.line_html(0.64, 3, "full", 0.58)
    assert both.startswith(plain[:-6]) and "market-informed" in both and "<b>64%</b>" in both and "<b>58%</b>" in both
    assert both.index("64%") < both.index("58%")                                                              # the model-only figure stays the headline


def _section(cmp_hi, cmp_lo=-0.02, mean=-0.004):
    d = pd.DataFrame({"event_date": pd.date_range("2020-01-01", periods=300, freq="5D"), "p_distance": 0.5, "p_v1": 0.5, "p_base": 0.5,
                      "went_distance": np.tile([0, 1], 150), "tier": "full"})
    s2 = distance_scores.distance_scores(d)
    return distance_ui.section_v2_html(s2, s2["scores"]["model"], {"mean": mean, "lo": cmp_lo, "hi": cmp_hi, "verdict": "clearly better" if cmp_hi < 0 else "cannot be told apart"},
                                       (5, 7), {"n": 12, "rate": 0.5, "new": 0.66, "ref": 0.67})


def test_the_track_record_only_bolds_the_market_informed_row_when_it_is_clearly_better():
    clear, unclear = _section(-0.001), _section(0.004)
    assert "<b>0.6931</b>" in clear and "<b>0.6931</b>" not in unclear
    assert "Verdict: clearly better." in clear and "Verdict: cannot be told apart." in unclear
    assert "Better in 5 of 7 years" in clear and "narrow margin" in clear and "No odds are shown" in clear
    assert "The locked record:</b> 12 graded" in clear and "0.6600 with the betting market against 0.6700 model-only" in clear


def test_the_comparison_helpers_say_which_prediction_was_better_and_in_how_many_years():
    rng = np.random.default_rng(0)
    n = 900
    y = rng.integers(0, 2, n)
    good = np.where(y == 1, 0.6, 0.4)
    d = pd.DataFrame({"event_date": pd.date_range("2018-01-01", periods=n, freq="2D"), "went_distance": y, "new": good, "ref": 0.5})
    c = distance_scores.paired_vs(d, "new", "ref")
    assert c["mean"] < 0 and c["verdict"] == "clearly better"                                                  # negative: `new` has the lower loss
    yb, yt = distance_scores.years_better_vs(d, "new", "ref")
    assert yb == yt and yt >= 4
    assert distance_scores.paired_vs(d, "ref", "new")["verdict"] == "clearly worse"
    s = distance_scores.pair_summary(d, "new", "ref")
    assert s["n"] == n and s["new"] < s["ref"] and s["rate"] == pytest.approx(y.mean())


# ---------------------------------------------------------------------------------------------- the readme numbers
def _dist2_frame(edge: float, seed: int, n: int = 800):
    rng = np.random.default_rng(seed)
    y = rng.integers(0, 2, n)
    d = pd.DataFrame({"event_date": pd.date_range("2015-01-01", periods=n, freq="4D"), "p_base": 0.5, "p_v1": 0.5, "went_distance": y, "tier": "full"})
    d["p_distance"] = np.clip(0.5 + rng.normal(0, 0.03, n) + edge * (2 * y - 1), 0.05, 0.95)
    return d


def test_the_readme_generator_fills_the_new_numbers_and_guards_each_claim_the_readme_makes():
    import readme_numbers as rn
    keys = ("dist2_n", "dist2_gain", "dist2_gain_lo", "dist2_gain_hi", "dist2_years_better", "dist2_years_total", "dist2_first_year", "dist2_last_year")
    assert set(keys) <= rn.known_names()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in keys:
        assert f"<!--n:{key}-->" in readme
    assert "cannot be told apart" in readme                                                                    # the claims the README makes, which the checks below protect

    # what the README says today: better on average, better in most years, but the range includes zero. Nothing is flagged, and the range is signed.
    d = _dist2_frame(0.001, 3)
    c = distance_scores.paired_vs(d, "p_distance", "p_v1")
    assert c["mean"] < 0 and c["verdict"] == "cannot be told apart"                                            # the premise of this case
    nums = rn.compute({"dist2": d}, 1)
    assert nums.broken_claims == [] and float(nums.values["dist2_gain"]) > 0
    assert nums.values["dist2_gain_lo"].startswith("-") and nums.values["dist2_gain_hi"].startswith("+")      # the range is shown with its signs: it spans zero

    # it becomes clearly better: "cannot be told apart" is no longer true
    clear = rn.compute({"dist2": _dist2_frame(0.01, 3)}, 1)
    assert any("cannot be told apart, but now the market-informed one is clearly better" in x for x in clear.broken_claims)
    assert clear.values["dist2_gain_lo"].startswith("+")

    # it is no better on average: "better on average" is no longer true
    worse = _dist2_frame(0.0, 3)
    worse["p_distance"] = 0.5 + (worse.p_distance - 0.5) * 0                                                   # the same as the model-only prediction exactly
    assert any("better than the model-only one on average" in x for x in rn.compute({"dist2": worse}, 1).broken_claims)

    # better on average but in too few years: "better in most years" is no longer true
    few = _dist2_frame(0.001, 3)
    few["p_distance"] = np.where(pd.to_datetime(few.event_date).dt.year.isin([2015, 2016, 2017, 2018]), 0.5, np.where(few.went_distance == 1, 0.49, 0.51))
    assert any("better in most years" in x for x in rn.compute({"dist2": few}, 1).broken_claims)

    assert any("fight_distance_v2" in x for x in rn.compute({}, 1).skipped)                                    # a missing backtest is skipped, not a crash


# ---------------------------------------------------------------------------------------------- on a real Postgres, built by the real pipeline
def _q(eng, sql, **params):
    return pd.read_sql(sa.text(sql), eng, params=params)


def _n(eng, sql, **params):
    return int(_q(eng, sql, **params).iloc[0, 0])


@contextmanager
def restoring(eng, *tables):
    """Run a test that changes tables, then put every row back exactly as it was."""
    saved = {t: _q(eng, f"SELECT * FROM {t}") for t in tables}
    try:
        yield
    finally:
        with eng.begin() as c:
            for t in tables:
                c.execute(sa.text(f"DELETE FROM {t}"))
        for t, df in saved.items():
            if len(df):
                df.to_sql(t, eng, if_exists="append", index=False, method="multi", chunksize=200)


@pytest.fixture(scope="module")
def world():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"dv2_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    info = build_demo.build(url, today=TODAY, size="small", log=lambda *a: None)
    eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
    card = date.fromisoformat(info["next_card"])
    yield {"eng": eng, "card": card, "pretend": card - timedelta(days=2), "info": info}
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        for r in build_demo.login_names(name):
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


@pytest.fixture
def db(world, monkeypatch):
    monkeypatch.setattr(model_v2, "make_engine", lambda: world["eng"])               # the commands open "the" database: make that the demo
    return world


def _next_card_ids(eng, card):
    return list(_q(eng, "SELECT bout_id FROM upcoming_fights WHERE event_date = :d", d=card).bout_id)


@needs_postgres
def test_the_demo_ran_the_new_model_through_the_real_pipeline(world):
    eng = world["eng"]
    assert world["info"]["market_model"] is True
    assert _n(eng, "SELECT COUNT(*) FROM fight_distance_v2") > 300
    assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE status = 'locked'") >= 25
    assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE graded_at IS NOT NULL AND went_distance IS NOT NULL") >= 15


@needs_postgres
def test_the_backtest_uses_only_fights_that_have_a_line_and_waits_for_enough_history(world):
    eng = world["eng"]
    b = _q(eng, "SELECT b.*, v.p_distance AS v1_p, o.open_prob_f1 FROM fight_distance_v2 b JOIN fight_distance_v1 v USING (fight_url) "
                "JOIN fight_odds o ON o.fight_id = split_part(b.fight_url, '/', 5)")
    assert len(b) == _n(eng, "SELECT COUNT(*) FROM fight_distance_v2")                                           # every v2 row has a v1 row and a line
    assert (b.trained_on_fights >= dv2.MIN_TRAIN).all() and np.allclose(b.p_v1, b.v1_p)                         # it improves v1's own stored number, never recomputes it
    assert np.allclose(b.fav_prob, np.maximum(b.open_prob_f1, 1 - b.open_prob_f1))
    T = dv2.training_table(eng)
    earlier = np.searchsorted(T.event_date.values, pd.to_datetime(b.event_date).values, side="left")
    assert (b.trained_on_fights.values == earlier).all()                                                          # each fight is scored by a model trained on strictly EARLIER fights only
    assert ((b.p_distance > 0) & (b.p_distance < 1)).all()


@needs_postgres
def test_the_backtest_is_incremental_and_a_rebuild_reproduces_it(db):
    eng = db["eng"]
    before = _q(eng, "SELECT fight_url, p_distance FROM fight_distance_v2 ORDER BY fight_url")
    assert dv2.run_backtest(eng) == 0 and _n(eng, "SELECT COUNT(*) FROM fight_distance_v2") == len(before)         # nothing new to add
    with restoring(eng, "fight_distance_v2"):
        assert dv2.run_backtest(eng, rebuild=True) == 0
        after = _q(eng, "SELECT fight_url, p_distance FROM fight_distance_v2 ORDER BY fight_url")
        assert list(after.fight_url) == list(before.fight_url) and np.allclose(after.p_distance, before.p_distance)


@needs_postgres
def test_the_backtest_never_uses_a_later_fight_to_score_an_earlier_one(db):
    eng = db["eng"]
    original = _q(eng, "SELECT fight_url, event_date, p_distance FROM fight_distance_v2 ORDER BY fight_url")
    cutoff = pd.to_datetime(original.event_date).sort_values().iloc[len(original) // 2].date()
    with restoring(eng, "fight_distance_v1", "fight_distance_v2"):
        with eng.begin() as c:                                                                                      # rewrite history after the cutoff: flip every result and every prediction
            c.execute(sa.text("UPDATE fight_distance_v1 SET went_distance = 1 - went_distance, p_distance = 1 - p_distance WHERE event_date > :c"), {"c": cutoff})
        dv2.run_backtest(eng, rebuild=True)
        changed = _q(eng, "SELECT fight_url, event_date, p_distance FROM fight_distance_v2 ORDER BY fight_url")
    m = original.merge(changed, on="fight_url", suffixes=("_old", "_new"))
    early = pd.to_datetime(m.event_date_old) <= pd.Timestamp(cutoff)
    assert early.sum() > 100 and np.allclose(m.p_distance_old[early], m.p_distance_new[early])                      # nothing before the cutoff noticed
    assert not np.allclose(m.p_distance_old[~early], m.p_distance_new[~early])                                      # (and the later ones did: the test can tell the difference)


@needs_postgres
def test_a_prediction_is_locked_once_from_earlier_fights_and_never_changes(db):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    ids = _next_card_ids(eng, card)
    with restoring(eng, "upcoming_distance_v2", "upcoming_odds"):
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        assert dv2.run_predict(eng, pretend) == 0
        rows = _q(eng, "SELECT * FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids) ORDER BY bout_id", ids=ids)
        have_both = set(_q(eng, "SELECT o.bout_id FROM upcoming_odds o JOIN upcoming_distance_v1 v ON v.bout_id = o.bout_id AND v.status = 'locked' WHERE o.bout_id = ANY(:ids)", ids=ids).bout_id)
        assert set(rows.bout_id) == have_both and len(rows) >= 8 and (rows.status == "locked").all() and rows.graded_at.isna().all()
        T = dv2.training_table(eng)
        train = T[T.event_date < pd.Timestamp(card)]
        assert (rows.trained_on_fights == len(train)).all()                                                         # learned from earlier fights only
        line = _q(eng, "SELECT bout_id, market_prob_a FROM upcoming_odds WHERE bout_id = ANY(:ids)", ids=ids).set_index("bout_id").market_prob_a
        fav = np.maximum(line.loc[rows.bout_id], 1 - line.loc[rows.bout_id]).to_numpy()
        v1 = _q(eng, "SELECT bout_id, p_distance FROM upcoming_distance_v1 WHERE bout_id = ANY(:ids)", ids=ids).set_index("bout_id").p_distance
        assert np.allclose(rows.fav_prob, fav) and np.allclose(rows.p_v1, v1.loc[rows.bout_id])
        assert np.allclose(rows.p_distance, dv2.stack_predict(dv2.fit_stacker(train), rows.p_v1.to_numpy(), fav))        # the same maths, done by hand
        before = rows.set_index("bout_id")[["p_distance", "predicted_at", "fav_prob", "p_v1"]]
        with eng.begin() as c:                                                                                      # the line moves...
            c.execute(sa.text("UPDATE upcoming_odds SET market_prob_a = 0.93 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        assert dv2.run_predict(eng, pretend) == 0
        after = _q(eng, "SELECT * FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids) ORDER BY bout_id", ids=ids).set_index("bout_id")[["p_distance", "predicted_at", "fav_prob", "p_v1"]]
        pd.testing.assert_frame_equal(before, after)                                                                # ...and a locked prediction does not


@needs_postgres
def test_a_dry_run_and_the_lock_window_write_nothing_until_the_event_is_close_enough(db):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    ids = _next_card_ids(eng, card)
    count = lambda: _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)", ids=ids)
    with restoring(eng, "upcoming_distance_v2"):
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        assert dv2.run_predict(eng, pretend, dry_run=True) == 0 and count() == 0                                    # a dry run writes nothing
        assert dv2.run_predict(eng, pretend, within_days=1) == 0 and count() == 0                                   # the event is two days away: not inside a one-day window
        assert dv2.run_predict(eng, pretend, within_days=0) == 0 and count() == 0
        assert dv2.run_predict(eng, card, within_days=2) == 0 and count() == 0                                      # on the day of the event, nothing is "upcoming" any more
        assert dv2.run_predict(eng, pretend, within_days=2) == 0 and count() >= 8                                   # inside the two-day window: locked


@needs_postgres
def test_a_bout_with_no_line_or_no_model_only_prediction_is_skipped_never_guessed(db, caplog):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    ids = _next_card_ids(eng, card)
    with restoring(eng, "upcoming_distance_v2", "upcoming_distance_v1", "upcoming_odds"):
        have = list(_q(eng, "SELECT o.bout_id FROM upcoming_odds o JOIN upcoming_distance_v1 v ON v.bout_id = o.bout_id WHERE o.bout_id = ANY(:ids) ORDER BY o.bout_id", ids=ids).bout_id)
        no_line, no_v1 = have[0], have[1]
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
            c.execute(sa.text("DELETE FROM upcoming_odds WHERE bout_id = :b"), {"b": no_line})
            c.execute(sa.text("DELETE FROM upcoming_distance_v1 WHERE bout_id = :b"), {"b": no_v1})
        with caplog.at_level("WARNING"):
            assert dv2.run_predict(eng, pretend) == 0                                                                 # skipping is not a failure
        done = set(_q(eng, "SELECT bout_id FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)", ids=ids).bout_id)
        assert no_line not in done and no_v1 not in done and set(have) - {no_line, no_v1} == done
        assert any(no_v1 in r.getMessage() for r in caplog.records)                                                    # and the skipped bout is named in the log, so it is noticed


@needs_postgres
def test_cancelled_bouts_follow_the_card_and_come_back_if_reinstated(db):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    with restoring(eng, "upcoming_fights", "upcoming_distance_v2"):
        bout = _q(eng, "SELECT bout_id FROM upcoming_distance_v2 WHERE event_date = :d ORDER BY bout_id LIMIT 1", d=card).bout_id.iloc[0]
        with eng.begin() as c:
            c.execute(sa.text("UPDATE upcoming_fights SET status = 'cancelled' WHERE bout_id = :b"), {"b": bout})
        dv2.run_predict(eng, pretend)
        assert _q(eng, "SELECT status FROM upcoming_distance_v2 WHERE bout_id = :b", b=bout).status.iloc[0] == "cancelled"
        with eng.begin() as c:
            c.execute(sa.text("UPDATE upcoming_fights SET status = 'scheduled' WHERE bout_id = :b"), {"b": bout})
        dv2.run_predict(eng, pretend)
        assert _q(eng, "SELECT status FROM upcoming_distance_v2 WHERE bout_id = :b", b=bout).status.iloc[0] == "locked"


@needs_postgres
def test_grading_uses_the_same_rules_as_the_model_only_prediction_and_agrees_with_it(db):
    eng = db["eng"]
    with restoring(eng, "upcoming_distance_v2"):
        with eng.begin() as c:                                                                                      # un-grade everything, so grading has to earn it back
            c.execute(sa.text("UPDATE upcoming_distance_v2 SET graded_at = NULL, went_distance = NULL"))
        assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE graded_at IS NOT NULL") == 0
        assert dv2.run_grade(eng) == 0
        both = _q(eng, "SELECT m.bout_id, m.went_distance AS w2, v.went_distance AS w1 FROM upcoming_distance_v2 m JOIN upcoming_distance_v1 v USING (bout_id) WHERE m.graded_at IS NOT NULL")
        assert len(both) >= 15 and (both.w2.fillna(-1) == both.w1.fillna(-1)).all()                                # the same fight, the same result, for both predictions
        assert both.w2.isin([0, 1]).all()
        assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE graded_at IS NULL AND status = 'locked' AND event_date < :t", t=TODAY) == 0       # and nothing in the past is left waiting
        assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE graded_at IS NOT NULL AND event_date >= :t", t=TODAY) == 0                       # while the card still to come is not graded early


@needs_postgres
def test_too_little_history_is_an_error_not_a_guess(db, monkeypatch):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    ids = _next_card_ids(eng, card)
    monkeypatch.setattr(dv2, "MIN_TRAIN", 10 ** 6)
    with restoring(eng, "upcoming_distance_v2"):
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        assert dv2.run_predict(eng, pretend) == 2                                                                   # the daily job turns this into a visible failure
        assert _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)", ids=ids) == 0


@needs_postgres
def test_with_nothing_to_do_or_no_lines_at_all_nothing_breaks(db):
    eng = db["eng"]
    assert dv2.run_predict(eng, date(2031, 1, 1)) == 0                                                              # no bout is upcoming by then
    with restoring(eng, "fight_odds", "fight_distance_v2"):
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM fight_odds"))
            c.execute(sa.text("DELETE FROM fight_distance_v2"))
        assert dv2.run_backtest(eng) == 0                                                                           # no lines yet: nothing to score, and not an error (the daily job calls this)
        assert _n(eng, "SELECT COUNT(*) FROM fight_distance_v2") == 0
        assert dv2.cmd_scoring(eng) == 2                                                                            # the report says to run the backtest first


@needs_postgres
def test_the_report_compares_the_two_predictions_on_the_same_fights(db, capsys):
    assert dv2.main(["scoring"]) == 0
    out = capsys.readouterr().out
    for needle in ("Market-informed (v2)", "Model-only (v1), same fights", "Last-3-years decision rate", "Coin flip", "Paired log-loss difference, market-informed minus model-only",
                   "Better than model-only in", "Calibration of the market-informed prediction", "The locked record"):
        assert needle in out, needle


@needs_postgres
def test_the_daily_jobs_lock_step_locks_the_market_informed_distance_prediction_beside_the_winner_picks(db):
    eng, card, pretend = db["eng"], db["card"], db["pretend"]
    ids = _next_card_ids(eng, card)
    count = lambda: _n(eng, "SELECT COUNT(*) FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)", ids=ids)
    with restoring(eng, "upcoming_distance_v2"):
        locked_before = count()
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        rep = Report()
        auto_picks.step_lock(eng, pretend, rep, dry_run=True, lock_days=2)
        assert count() == 0 and any("Market-informed goes-the-distance predictions" == t for t, _ in rep.sections)     # a dry run reports and writes nothing
        rep = Report()
        auto_picks.step_lock(eng, pretend, rep, dry_run=False, lock_days=2)
        assert count() == locked_before > 0 and "returned 0" in " ".join(_lines(rep))
        again = Report()
        auto_picks.step_lock(eng, pretend, again, dry_run=False, lock_days=2)
        assert count() == locked_before                                                                              # the next day's run adds nothing
        far = Report()
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2 WHERE bout_id = ANY(:ids)"), {"ids": ids})
        auto_picks.step_lock(eng, pretend - timedelta(days=3), far, dry_run=False, lock_days=2)                      # five days before the event: outside the two-day window
        assert count() == 0


@needs_postgres
def test_a_failing_second_stage_makes_the_daily_job_say_so(db, monkeypatch):
    eng, pretend = db["eng"], db["pretend"]
    monkeypatch.setattr(dv2, "MIN_TRAIN", 10 ** 6)
    with restoring(eng, "upcoming_distance_v2"):
        with eng.begin() as c:
            c.execute(sa.text("DELETE FROM upcoming_distance_v2"))
        with pytest.raises(RuntimeError, match="model_distance_v2 predict returned 2"):
            auto_picks.step_lock(eng, pretend, Report(), dry_run=False, lock_days=2)
