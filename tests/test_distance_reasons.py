"""
"Why this distance prediction?": the breakdown of each goes-the-distance prediction (pipeline/distance_reasons.py), how it is stored beside the locked prediction, and the rule that
decides whether it may exist at all: it must reproduce the locked number. The maths is checked on hand-built models; the storage on a demo built by the real pipeline on a real Postgres.
"""
import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
for sub in ("pipeline", "demo", "app"):
    sys.path.insert(0, str(ROOT / sub))
import build_demo  # noqa: E402
import distance_reasons as dr  # noqa: E402
import model_distance as md  # noqa: E402
import model_distance_v2 as dv2  # noqa: E402

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="no test Postgres server (set TEST_PG_ADMIN_URL)")
TODAY = date(2026, 10, 29)
FEATURES = md.FEATURES


# ---------------------------------------------------------------------------------------------- a model whose numbers are chosen by hand
class _Scaler:
    def __init__(self, mean, scale):
        self.mean, self.scale = np.asarray(mean, float), np.asarray(scale, float)

    def transform(self, X):
        return (np.asarray(X, float) - self.mean) / self.scale


class _Logit:
    def __init__(self, coef, intercept):
        self.coef_, self.intercept_ = np.array([coef], float), np.array([intercept], float)


class Hand:
    """Quacks like make_pipeline(StandardScaler(), LogisticRegression()): model[0] transforms, model[1] holds coef_ and intercept_."""
    def __init__(self, coef: dict, intercept: float = 0.0, mean=None, scale=None):
        n = len(FEATURES)
        self.parts = (_Scaler(mean if mean is not None else [0.0] * n, scale if scale is not None else [1.0] * n), _Logit([coef.get(f, 0.0) for f in FEATURES], intercept))

    def __getitem__(self, i):
        return self.parts[i]


def X_of(**values) -> pd.DataFrame:
    return pd.DataFrame([{f: values.get(f, 0.0) for f in FEATURES}])


def bout(**kw) -> pd.Series:
    base = {"c_elig_a": 9, "c_dec_a": 3, "c_fin_win_a": 4, "c_fin_loss_a": 2, "c_dur_frac_a": 5.4, "c_elig_b": 8, "c_dec_b": 5, "c_fin_win_b": 2, "c_fin_loss_b": 1, "c_dur_frac_b": 5.2, "five": 0, "title": 0}
    return pd.Series({**base, **kw})


# ---------------------------------------------------------------------------------------------- the maths
def test_the_contributions_are_coefficient_times_standardised_input_and_add_up_to_the_log_odds():
    model = Hand({"dec_mean": 2.0, "finwin_mean": -1.5, "five": -0.5}, intercept=-0.25, mean=[0.5] + [0.0] * 9, scale=[0.1] + [1.0] * 9)
    X = X_of(dec_mean=0.7, finwin_mean=0.4, five=1)
    C, intercept, Z = dr.contributions(model, X)
    assert Z.loc[0, "dec_mean"] == pytest.approx((0.7 - 0.5) / 0.1) and Z.loc[0, "finwin_mean"] == 0.4
    assert C.loc[0, "dec_mean"] == pytest.approx(2.0 * 2.0) == pytest.approx(4.0)                                           # 2.0 x (0.2 / 0.1)
    assert C.loc[0, "finwin_mean"] == pytest.approx(-1.5 * 0.4) and C.loc[0, "five"] == pytest.approx(-0.5)
    assert intercept == -0.25
    total = intercept + C.loc[0].sum()
    assert total == pytest.approx(-0.25 + 4.0 - 0.6 - 0.5) == pytest.approx(2.65)
    assert dr.sigmoid(total) == pytest.approx(1 / (1 + np.exp(-2.65)))


def _training_frame(n=3000, seed=11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    def side():
        fights = rng.poisson(7, n)
        k = lambda p: rng.binomial(fights, np.clip(p, 0, 1)).astype(float)
        return {"c_elig": fights.astype(float), "c_dec": k(rng.beta(4, 5, n)), "c_fin_win": k(0.35), "c_fin_loss": k(0.25), "c_dur_frac": fights * np.clip(rng.normal(0.62, 0.1, n), 0.05, 1)}
    a, b = side(), side()
    df = pd.DataFrame({**{f"{k}_a": v for k, v in a.items()}, **{f"{k}_b": v for k, v in b.items()}})
    df["five"], df["title"] = (rng.random(n) < 0.08).astype(int), (rng.random(n) < 0.07).astype(int)
    rate = (df.c_dec_a + df.c_dec_b) / (df.c_elig_a + df.c_elig_b).clip(lower=1)
    df["dec"] = (rng.random(n) < 1 / (1 + np.exp(-(-0.3 + 2.0 * (rate - 0.45) - 0.4 * df.five)))).astype(int)
    return df


def test_on_the_real_fitted_model_the_breakdown_reproduces_every_prediction_exactly():
    Tr = _training_frame()
    model, mu = md.fit(Tr)
    X = md.design(Tr.iloc[:500], mu)
    C, intercept, Z = dr.contributions(model, X)
    assert np.allclose(1 / (1 + np.exp(-(intercept + C.sum(axis=1).to_numpy()))), model.predict_proba(X)[:, 1], atol=1e-12, rtol=0)
    assert abs(dr.sigmoid(intercept) - Tr.dec.mean()) < 0.03                                                                # the intercept is an average fight: close to the plain decision rate
    for i in (0, 7, 123):
        r = dr.build_v1(Tr.iloc[i], C.iloc[i], Z.iloc[i], intercept, "Silva Santos", "Jon Jones")
        assert r["p"] == pytest.approx(model.predict_proba(X.iloc[[i]])[0, 1], abs=1e-4) and r["base"] == pytest.approx(dr.sigmoid(intercept), abs=1e-4)
        assert r["intercept"] + sum(f["c"] for f in r["factors"]) + r["rest"] == pytest.approx(r["total"], abs=1e-3)           # what is shown adds up to the model's log-odds


def test_the_six_factors_use_every_input_exactly_once():
    flat = [f for feats in dr.GROUPS.values() for f in feats]
    assert sorted(flat) == sorted(FEATURES) and len(flat) == len(set(flat))
    assert set(dr.LABELS) == set(dr.GROUPS) and set(dr.HEADLINE) <= set(dr.GROUPS)


def test_each_factor_is_the_sum_of_its_inputs_sorted_by_size_with_small_ones_folded_into_the_rest():
    model = Hand({"dec_mean": 0.6, "dec_min": -0.2, "dec_max": 0.1, "finwin_mean": -0.8, "finwin_max": 0.1, "finloss_mean": 0.01, "five": -0.5, "title": 0.015}, intercept=0.2)
    X = X_of(dec_mean=1.0, dec_min=1.0, dec_max=1.0, finwin_mean=1.0, finwin_max=1.0, finloss_mean=1.0, five=1, title=1)
    C, icpt, Z = dr.contributions(model, X)
    r = dr.build_v1(bout(five=1), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")
    assert [(f["key"], f["c"]) for f in r["factors"]] == [("finishing", -0.7), ("decisions", 0.5), ("rounds", -0.5)]            # largest push first; a tie (0.5 and 0.5) keeps the factors' own order
    assert r["rest"] == pytest.approx(0.01 + 0.015)                                                                         # durability (0.01) and title (0.015) are below 0.02: folded together
    assert [f["toward"] for f in r["factors"]] == ["finish", "decision", "finish"]
    assert r["total"] == pytest.approx(0.2 + 0.5 - 0.7 + 0.01 - 0.5 + 0.015, abs=1e-3) == pytest.approx(-0.475, abs=1e-3) and r["rounds"] == 5


@pytest.mark.parametrize("coef, z, expected_net", [
    (+0.4, +1.0, False),     # a high decision rate, and the bar pushes toward a decision: they agree
    (-0.4, +1.0, True),      # a high decision rate, yet the bar pushes toward a finish: the bar opposes the headline number
    (-0.4, -1.0, True),      # a LOW decision rate, yet the bar pushes toward a decision (a negative coefficient times a negative input): opposes
    (+0.4, -1.0, False),     # a low decision rate and the bar pushes toward a finish: they agree
    (-4.0, +0.04, False),    # the headline number is within a whisker of average (and the bar is big enough to be shown): nothing to contradict
])
def test_a_bar_that_opposes_its_headline_number_is_flagged_as_a_net_effect(coef, z, expected_net):
    model = Hand({"dec_mean": coef})
    C, icpt, Z = dr.contributions(model, X_of(dec_mean=z))
    f = next(f for f in dr.build_v1(bout(), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")["factors"] if f["key"] == "decisions")
    assert f["net"] is expected_net and f["toward"] == ("decision" if coef * z > 0 else "finish")


@pytest.mark.parametrize("group, feature, z, coef, expected_net", [
    ("finishing", "finwin_mean", +1.0, -0.4, False),    # fighters who win by finishing often: toward a finish, as the number says
    ("finishing", "finwin_mean", +1.0, +0.4, True),
    ("durability", "finloss_mean", +1.0, -0.4, False),  # fighters who get finished often: toward a finish
    ("durability", "finloss_mean", +1.0, +0.4, True),
    ("pace", "dur_mean", +1.0, +0.4, False),            # fights that use the whole clock: toward a decision
    ("pace", "dur_mean", +1.0, -0.4, True),
])
def test_the_other_factors_know_which_way_a_high_number_should_push(group, feature, z, coef, expected_net):
    C, icpt, Z = dr.contributions(Hand({feature: coef}), X_of(**{feature: z}))
    f = next(f for f in dr.build_v1(bout(), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")["factors"] if f["key"] == group)
    assert f["net"] is expected_net


def test_binary_factors_state_the_fact_and_can_never_be_flagged():
    C, icpt, Z = dr.contributions(Hand({"five": -0.5, "title": -0.3}), X_of(five=1, title=1))
    r = dr.build_v1(bout(five=1, title=1), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")
    by = {f["key"]: f for f in r["factors"]}
    assert by["rounds"]["detail"] == "Scheduled for five rounds (assumed: a title fight or main event)" and by["title"]["detail"] == "A title fight"
    assert not by["rounds"]["net"] and not by["title"]["net"]
    C, icpt, Z = dr.contributions(Hand({"five": -0.5, "title": -0.3}), X_of())
    r = dr.build_v1(bout(), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")
    assert r["rounds"] == 3
    assert dr._detail("rounds", bout(), "One", "Two") == "Scheduled for three rounds (assumed: only a title fight or main event has five)" and dr._detail("title", bout(), "One", "Two") == "Not a title fight"


def test_the_words_are_the_fighters_own_counts_with_last_names_and_a_plain_line_for_a_debut():
    r = bout()
    assert dr._detail("decisions", r, "Santos", "Jones") == "Earlier fights that went to a decision: Santos 3 of 9, Jones 5 of 8"
    assert dr._detail("finishing", r, "Santos", "Jones") == "Earlier fights won by a finish: Santos 4 of 9, Jones 2 of 8"
    assert dr._detail("durability", r, "Santos", "Jones") == "Earlier fights lost by a finish: Santos 2 of 9, Jones 1 of 8"
    assert dr._detail("pace", r, "Santos", "Jones") == "Average share of the scheduled time their earlier fights used: Santos 60%, Jones 65%"
    debut = bout(c_elig_a=0, c_dec_a=0, c_fin_win_a=0, c_fin_loss_a=0, c_dur_frac_a=0)
    assert dr._detail("decisions", debut, "Newman", "Jones") == "Earlier fights that went to a decision: Newman has no UFC fights yet, Jones 5 of 8"
    assert dr._detail("pace", debut, "Newman", "Jones").endswith("Newman has no UFC fights yet, Jones 65%")                    # never a division by zero
    both = bout(c_elig_a=0, c_elig_b=0, c_dec_a=0, c_dec_b=0)
    assert dr._detail("decisions", both, "Newman", "Young") == "Earlier fights that went to a decision: Newman has no UFC fights yet, Young has no UFC fights yet"
    names = dr.build_v1(bout(), pd.Series({f: 0.1 for f in FEATURES}), pd.Series({f: 0.5 for f in FEATURES}), 0.0, "Silva Santos", "Max Smith Jr.")
    assert "Santos" in names["factors"][0]["detail"] and "Smith Jr." in names["factors"][0]["detail"] and names["a"] == "Silva Santos"


def test_the_stored_breakdown_is_plain_json_with_a_schema_and_rounded_numbers():
    C, icpt, Z = dr.contributions(Hand({"dec_mean": 0.3333333, "five": -0.4444444}, intercept=-0.123456789), X_of(dec_mean=1.0, five=1))
    r = dr.build_v1(bout(five=1), C.iloc[0], Z.iloc[0], icpt, "A One", "B Two")
    assert json.loads(json.dumps(r)) == r and r["schema"] == dr.SCHEMA == 1 and r["kind"] == "d1"
    assert r["intercept"] == -0.1235 and r["base"] == round(dr.sigmoid(-0.123456789), 4) and all(f["c"] == round(f["c"], 4) for f in r["factors"])
    assert set(r) == {"schema", "kind", "a", "b", "rounds", "intercept", "base", "total", "p", "factors", "rest"}


def test_the_market_informed_comparison_is_the_arithmetic_it_says_it_is():
    r = dr.build_v2("A One", "B Two", p_v1=0.45, fav_prob=0.73, p_v2=0.41, typical=0.66, p_typical=0.43)
    assert r == {"schema": 1, "kind": "d2", "a": "A One", "b": "B Two", "p_v1": 0.45, "fav_prob": 0.73, "typical_fav": 0.66, "p_typical": 0.43, "p_v2": 0.41,
                 "line_effect": -0.02, "rest_effect": -0.02}
    assert r["line_effect"] + r["rest_effect"] == pytest.approx(r["p_v2"] - r["p_v1"])                                      # the two parts make up the whole change


def test_the_typical_favourite_is_the_second_stages_own_training_average():
    rng = np.random.default_rng(4)
    n = 1500
    p = np.clip(rng.beta(8, 8, n), 0.2, 0.8)
    fav = np.clip(rng.beta(2, 3, n) * 0.5 + 0.5, 0.5, 0.95)
    T = pd.DataFrame({"p_v1": p, "fav_prob": fav, "went_distance": (rng.random(n) < p).astype(int)})
    stacker = dv2.fit_stacker(T)
    typical = 0.5 + float(stacker[0].mean_[1])
    assert typical == pytest.approx(T.fav_prob.mean(), abs=1e-12)                                                           # the average favourite strength in the training data
    here = dv2.stack_predict(stacker, [0.5], [0.9])[0]
    typ = dv2.stack_predict(stacker, [0.5], [typical])[0]
    assert here != typ and dr.build_v2("A", "B", 0.5, 0.9, here, typical, typ)["line_effect"] == pytest.approx(here - typ, abs=1e-4)


# ---------------------------------------------------------------------------------------------- the real pipeline on a real Postgres
@pytest.fixture(scope="module")
def world():
    if not ADMIN_URL:
        pytest.skip("no test Postgres server (set TEST_PG_ADMIN_URL)")
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"dr_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    url = sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    build_demo.build(url, today=TODAY, size="small", log=lambda *a: None)
    eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        for r in build_demo.login_names(name):
            c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


def q(eng, sql, **params) -> pd.DataFrame:
    return pd.read_sql(sa.text(sql), eng, params=params)


def stored(eng, version, only_open=True) -> dict:
    tbl = "upcoming_distance_v1" if version == "d1" else "upcoming_distance_v2"
    extra = "AND u.status = 'locked' AND u.graded_at IS NULL" if only_open else ""
    df = q(eng, f"SELECT r.bout_id, r.reasons FROM pick_reasons r JOIN {tbl} u USING (bout_id) WHERE r.version = :v {extra}", v=version)
    return {r.bout_id: (r.reasons if isinstance(r.reasons, dict) else json.loads(r.reasons)) for r in df.itertuples()}


def open_bouts(eng, version) -> pd.DataFrame:
    tbl = "upcoming_distance_v1" if version == "d1" else "upcoming_distance_v2"
    return q(eng, f"SELECT * FROM {tbl} WHERE status = 'locked' AND graded_at IS NULL")


@contextmanager
def restored(eng):
    """Whatever a test breaks, the module's demo is put back: reasons rebuilt, statuses and numbers restored."""
    snap1 = q(eng, "SELECT bout_id, p_distance, status, trained_on_fights FROM upcoming_distance_v1")
    snap2 = q(eng, "SELECT bout_id, p_distance, status, trained_on_fights FROM upcoming_distance_v2")
    try:
        yield
    finally:
        with eng.begin() as c:
            for tbl, snap in (("upcoming_distance_v1", snap1), ("upcoming_distance_v2", snap2)):
                for r in snap.itertuples():
                    c.execute(sa.text(f"UPDATE {tbl} SET p_distance = :p, status = :s, trained_on_fights = :n WHERE bout_id = :b"), {"p": r.p_distance, "s": r.status, "n": r.trained_on_fights, "b": r.bout_id})
        md.explain_missing(eng)
        dv2.explain_missing(eng)


@needs_postgres
def test_every_locked_prediction_got_its_breakdown_from_the_real_pipeline_and_each_agrees_with_the_locked_number(world):
    for version, key, col in (("d1", "p", "p_distance"), ("d2", "p_v2", "p_distance")):
        locked = open_bouts(world, version).set_index("bout_id")
        got = stored(world, version)
        assert len(locked) >= 10 and set(got) == set(locked.index), (version, len(locked), len(got))
        for bout_id, d in got.items():
            assert abs(d[key] - locked.p_distance[bout_id]) <= 5e-5, (version, bout_id)                                  # never a number the card does not show
            assert d["schema"] == 1 and d["kind"] == version and d["a"] == locked.fighter_a[bout_id] and d["b"] == locked.fighter_b[bout_id]
    for d in stored(world, "d1").values():
        assert d["intercept"] + sum(f["c"] for f in d["factors"]) + d["rest"] == pytest.approx(d["total"], abs=1e-3)
        assert all(abs(f["c"]) >= dr.MIN_SHOWN for f in d["factors"]) and [abs(f["c"]) for f in d["factors"]] == sorted((abs(f["c"]) for f in d["factors"]), reverse=True)
    T = dv2.training_table(world)
    d2_open = open_bouts(world, "d2").set_index("bout_id")
    for bout_id, d in stored(world, "d2").items():
        assert d["p_v1"] == pytest.approx(float(open_bouts(world, "d1").set_index("bout_id").p_distance[bout_id]), abs=5e-5)    # d2 starts from the very d1 number on the card
        assert d["line_effect"] + d["rest_effect"] == pytest.approx(d["p_v2"] - d["p_v1"], abs=2e-4)
        tr = T.iloc[: int(d2_open.trained_on_fights[bout_id])]                                                              # the fights the second stage learned from, worked out independently here
        stacker = dv2.fit_stacker(tr)
        assert d["typical_fav"] == pytest.approx(tr.fav_prob.mean(), abs=1e-4)                                              # "a typical fight" is the training average, not some other column
        assert d["p_typical"] == pytest.approx(float(dv2.stack_predict(stacker, [d["p_v1"]], [0.5 + float(stacker[0].mean_[1])])[0]), abs=2e-4)


@needs_postgres
def test_rebuilding_the_breakdowns_from_scratch_gives_exactly_the_ones_made_at_lock_time(world):
    before = {v: stored(world, v) for v in ("d1", "d2")}
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql("""DELETE FROM pick_reasons WHERE version IN ('d1', 'd2') AND bout_id IN
                                 (SELECT bout_id FROM upcoming_distance_v1 WHERE status = 'locked' AND graded_at IS NULL
                                  UNION SELECT bout_id FROM upcoming_distance_v2 WHERE status = 'locked' AND graded_at IS NULL)""")
        assert stored(world, "d1") == {} and stored(world, "d2") == {}
        n1, n2 = md.explain_missing(world), dv2.explain_missing(world)
        assert n1 == (len(before["d1"]), 0) and n2 == (len(before["d2"]), 0)
        assert stored(world, "d1") == before["d1"] and stored(world, "d2") == before["d2"]                             # identical to what was stored when the picks were locked


@needs_postgres
def test_asking_again_changes_nothing(world):
    created = q(world, "SELECT bout_id, version, created_at FROM pick_reasons WHERE version IN ('d1', 'd2') ORDER BY 1, 2")
    assert md.explain_missing(world) == (0, 0) and dv2.explain_missing(world) == (0, 0)
    assert q(world, "SELECT bout_id, version, created_at FROM pick_reasons WHERE version IN ('d1', 'd2') ORDER BY 1, 2").equals(created)       # nothing rewritten


@needs_postgres
@pytest.mark.parametrize("version, module, table", [("d1", md, "upcoming_distance_v1"), ("d2", dv2, "upcoming_distance_v2")])
def test_a_breakdown_that_does_not_reproduce_the_locked_number_is_refused_never_stored(world, caplog, version, module, table):
    victim = open_bouts(world, version).bout_id.iloc[0]
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql(f"UPDATE {table} SET p_distance = p_distance + 0.01 WHERE bout_id = '{victim}'")              # the locked number no longer matches the rebuilt model
            c.exec_driver_sql(f"DELETE FROM pick_reasons WHERE bout_id = '{victim}' AND version = '{version}'")
        with caplog.at_level(logging.WARNING):
            assert module.explain_missing(world) == (0, 1)
        assert victim not in stored(world, version) and f"{victim}" in caplog.text and "no " in caplog.text


@needs_postgres
@pytest.mark.parametrize("version, module, table", [("d1", md, "upcoming_distance_v1"), ("d2", dv2, "upcoming_distance_v2")])
def test_a_model_that_cannot_be_rebuilt_is_skipped_with_a_warning_not_guessed(world, caplog, version, module, table):
    victim = open_bouts(world, version).bout_id.iloc[0]
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql(f"UPDATE {table} SET trained_on_fights = 10 WHERE bout_id = '{victim}'")                      # far too little history to be a real model
            c.exec_driver_sql(f"DELETE FROM pick_reasons WHERE bout_id = '{victim}' AND version = '{version}'")
        with caplog.at_level(logging.WARNING):
            assert module.explain_missing(world) == (0, 1)
        assert victim not in stored(world, version) and "Cannot rebuild" in caplog.text


@needs_postgres
def test_only_open_locked_bouts_are_explained_not_cancelled_or_graded_ones(world):
    cancelled = open_bouts(world, "d1").bout_id.iloc[0]
    graded = q(world, "SELECT bout_id FROM upcoming_distance_v1 WHERE graded_at IS NOT NULL LIMIT 1").bout_id.iloc[0]
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql(f"UPDATE upcoming_distance_v1 SET status = 'cancelled' WHERE bout_id = '{cancelled}'")
            c.exec_driver_sql(f"DELETE FROM pick_reasons WHERE version = 'd1' AND bout_id IN ('{cancelled}', '{graded}')")
        assert md.explain_missing(world) == (0, 0)
        assert q(world, f"SELECT COUNT(*) AS n FROM pick_reasons WHERE version = 'd1' AND bout_id IN ('{cancelled}', '{graded}')").n[0] == 0


@needs_postgres
def test_the_market_informed_comparison_also_covers_only_open_locked_bouts(world):
    cancelled = open_bouts(world, "d2").bout_id.iloc[0]
    graded = q(world, "SELECT bout_id FROM upcoming_distance_v2 WHERE graded_at IS NOT NULL LIMIT 1").bout_id[0]
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql(f"UPDATE upcoming_distance_v2 SET status = 'cancelled' WHERE bout_id = '{cancelled}'")
            c.exec_driver_sql(f"DELETE FROM pick_reasons WHERE version = 'd2' AND bout_id IN ('{cancelled}', '{graded}')")
        assert dv2.explain_missing(world) == (0, 0)
        assert q(world, f"SELECT COUNT(*) AS n FROM pick_reasons WHERE version = 'd2' AND bout_id IN ('{cancelled}', '{graded}')").n[0] == 0


@needs_postgres
def test_storing_breakdowns_leaves_the_winner_reasons_alone(world):
    winner = q(world, "SELECT version, COUNT(*) AS n FROM pick_reasons WHERE version IN ('v2', 'v3') GROUP BY 1 ORDER BY 1")
    assert list(winner.version) == ["v2", "v3"] and (winner.n > 10).all()
    md.explain_missing(world)
    dv2.explain_missing(world)
    assert q(world, "SELECT version, COUNT(*) AS n FROM pick_reasons WHERE version IN ('v2', 'v3') GROUP BY 1 ORDER BY 1").equals(winner)


@needs_postgres
def test_predict_stores_breakdowns_a_dry_run_stores_nothing_and_a_failure_in_them_never_fails_the_run(world, monkeypatch, caplog):
    victims = open_bouts(world, "d2").bout_id.iloc[:2].tolist()
    with restored(world):
        with world.begin() as c:
            c.exec_driver_sql("DELETE FROM pick_reasons WHERE version = 'd2' AND bout_id = ANY(:b)".replace(":b", "ARRAY['" + "','".join(victims) + "']"))
        assert dv2.run_predict(world, TODAY, dry_run=True) == 0
        assert not set(victims) & set(stored(world, "d2"))                                                                    # a dry run writes nothing, breakdowns included
        assert dv2.run_predict(world, TODAY) == 0
        assert set(victims) <= set(stored(world, "d2"))                                                                       # a real run backfills them
        with world.begin() as c:
            c.exec_driver_sql("DELETE FROM pick_reasons WHERE version = 'd1' AND bout_id = ANY(ARRAY['" + "','".join(open_bouts(world, "d1").bout_id.iloc[:2].tolist()) + "'])")
        with monkeypatch.context() as m:                                                                                       # the fake lives only for this one call, so the clean-up below is real
            m.setattr(md, "explain_missing", lambda engine: (_ for _ in ()).throw(RuntimeError("boom")))
            with caplog.at_level(logging.WARNING):
                assert md.run_predict(world, TODAY) == 0                                                                       # the predictions stand
        assert "Could not store the why-this-prediction breakdowns: RuntimeError: boom" in caplog.text


@needs_postgres
def test_the_commands_exist_and_say_how_many_they_stored(world, monkeypatch, caplog):
    import model_v2
    monkeypatch.setattr(model_v2, "make_engine", lambda: world)
    for module in (md, dv2):
        with caplog.at_level(logging.INFO):
            assert module.main(["explain-upcoming"]) == 0
        assert "Stored 0" in caplog.text and "0 skipped" in caplog.text
        caplog.clear()
