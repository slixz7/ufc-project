"""
pipeline/model_health.py: the calibration check and the retirement rules fixed in advance. Every frame is built so that the right answer is known.
"""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import auto_common  # noqa: E402
import model_health as mh  # noqa: E402


def outcomes(k, n):
    return np.array([1] * k + [0] * (n - k))


# ---------------------------------------------------------------------------------------------- calibration
@pytest.mark.parametrize("right, status, alert", [(60, "consistent with what it said", False), (52, "consistent with what it said", False), (50, "worse than it said", False),
                                                    (45, "worse than it said", True), (70, "better than it said", False), (95, "better than it said", False)])
def test_calibration_compares_the_real_count_with_what_the_models_own_probabilities_expected(right, status, alert):
    c = mh.calibration_check(np.full(100, 0.6), outcomes(right, 100))
    assert c["expected"] == pytest.approx(60) and c["sd"] == pytest.approx(np.sqrt(100 * 0.24)) and c["right"] == right
    assert c["z"] == pytest.approx((right - 60) / np.sqrt(24)) and c["status"] == status and c["alert"] is alert
    #   50 right is 2.04 standard deviations below: reported, not yet flagged for a person (that needs more than 3); 45 is 3.06 below: flagged


def test_calibration_needs_enough_fights_and_never_divides_by_zero():
    c = mh.calibration_check(np.full(99, 0.6), outcomes(0, 99))                                                         # as bad as can be, but only 99 fights
    assert c["status"] == "too few graded fights to check yet (99 so far; it needs at least 100)" and c["alert"] is False
    empty = mh.calibration_check([], [])
    assert empty["n"] == 0 and empty["z"] == 0 and empty["alert"] is False and "too few" in empty["status"]
    assert mh.calibration_check(np.ones(150), np.ones(150))["status"] == "consistent with what it said"                  # a model that said 100% and was right: no spread, no false alarm


def test_the_alert_bar_is_stricter_than_the_report_bar_because_the_check_runs_every_day():
    assert mh.Z_WORSE == 2.0 and mh.Z_ALERT == 3.0 and mh.CHECK_AFTER_FIGHTS == 100 and mh.RETIRE_AFTER_FIGHTS == 300 and mh.Z_ALERT > mh.Z_WORSE


# ---------------------------------------------------------------------------------------------- the rules
def test_a_rule_says_nothing_until_there_are_enough_fights_then_retires_only_when_clearly_worse():
    worse, better, tied = ({"verdict": v} for v in ("clearly worse", "clearly better", "cannot be told apart"))
    r = mh.rule_status(299, worse, "a coin flip", "the model-only winner pick")
    assert r == {"retire": False, "text": "keep (299 graded fights so far; the rule needs at least 300 before it can say anything)"}
    assert mh.rule_status(300, worse, "a coin flip", "the model-only winner pick") == {"retire": True, "text": "RETIRE the model-only winner pick: over 300 graded fights it is clearly worse than a coin flip"}
    assert mh.rule_status(300, better, "a coin flip", "x")["text"] == "keep (over 300 graded fights it is clearly better than a coin flip)"
    assert mh.rule_status(300, tied, "a coin flip", "x") == {"retire": False, "text": "keep (over 300 graded fights it is not clearly different from a coin flip)"}


def winners(n, wrong_every=4, market="same"):
    """n graded winner picks. The model favours the real winner except on every `wrong_every`-th fight, at 75%. `market`: 'same' (the line equals the model), 'right' (the line is
    always right at 80%) or 'wrong' (always wrong)."""
    rows = []
    for i in range(n):
        y = i % 2                                                                 # 1: fighter A wins
        pick_a = bool(y) ^ bool(wrong_every and i % wrong_every == 0)
        p_a = 0.75 if pick_a else 0.25
        actual = "A" if y else "B"
        m = {"same": p_a, "right": 0.8 if y else 0.2, "wrong": 0.2 if y else 0.8}[market]
        rows.append({"fighter_a": "A", "actual_winner": actual, "p_a_wins": p_a, "p_v2_a": p_a, "market_prob_a": m, "confidence": 0.75, "correct": int(("A" if pick_a else "B") == actual)})
    return pd.DataFrame(rows)


def test_winner_v2_is_judged_against_a_coin_flip_whichever_side_the_fighters_are_on():
    e = mh.winner_v2(winners(400))
    assert e["n"] == 400 and e["calibration"]["right"] == 300 and e["calibration"]["status"] == "consistent with what it said" and e["needs"] == []
    assert e["rule"]["text"] == "keep (over 400 graded fights it is clearly better than a coin flip)"
    bad = mh.winner_v2(winners(400, wrong_every=1))                                                                     # wrong on every single fight
    assert bad["rule"]["retire"] is True and bad["rule"]["text"].startswith("RETIRE the model-only winner pick: over 400 graded fights") and len(bad["needs"]) == 2
    flipped = winners(400)
    flipped["fighter_a"], flipped["p_a_wins"] = "B", 1 - flipped.p_a_wins                                              # the other person is now "fighter A": his chance is 1 minus; the winner's name is unchanged
    assert mh.winner_v2(flipped)["rule"]["text"] == e["rule"]["text"]                                                   # the answer does not depend on which fighter is "A"


def test_winner_v3_is_judged_against_the_line_alone_and_reports_all_three_log_losses():
    assert mh.winner_v3(winners(400, market="same"))["rule"]["text"] == "keep (over 400 graded fights it is not clearly different from the bookmakers' opening line alone)"
    worse = mh.winner_v3(winners(400, wrong_every=1, market="right"))                                                   # the blend is always wrong, the line always right
    assert worse["rule"]["retire"] is True and "RETIRE the market-informed winner pick: over 400 graded fights it is clearly worse than the bookmakers' opening line alone" in worse["rule"]["text"]
    better = mh.winner_v3(winners(400, wrong_every=0, market="wrong"))
    assert better["rule"]["retire"] is False and "clearly better than the bookmakers' opening line alone" in better["rule"]["text"]
    tiny = pd.DataFrame({"fighter_a": ["A", "A"], "actual_winner": ["A", "B"], "p_a_wins": [0.8, 0.3], "market_prob_a": [0.7, 0.4], "p_v2_a": [0.6, 0.5], "confidence": [0.8, 0.7], "correct": [1, 1]})
    assert mh.winner_v3(tiny)["extra"] == "log loss on these 2 fights: blend 0.2899, the line alone 0.4338, model-only 0.6020 (lower is better; a coin flip is 0.6931)"


def distance(n, informative=True, base=0.5):
    y = np.array([i % 2 for i in range(n)])
    p = np.where(y == 1, 0.7, 0.3) if informative else np.where(y == 1, 0.3, 0.7)
    return pd.DataFrame({"p_distance": p, "p_v2": p, "p_v1": base, "p_base": base, "went_distance": y})


def test_the_distance_models_are_judged_against_the_plain_decision_rate_and_against_v1():
    good = mh.distance_v1(distance(400))
    assert good["rule"]["text"] == "keep (over 400 graded fights it is clearly better than the plain decision rate of the previous three years)" and good["calibration"]["status"] == "consistent with what it said"
    bad = mh.distance_v1(distance(400, informative=False))
    assert bad["rule"]["retire"] is True and "RETIRE the goes-the-distance prediction" in bad["rule"]["text"]
    assert mh.distance_v2(distance(400, informative=False))["rule"]["retire"] is True                                   # worse than v1 (which says 50% every time here)
    assert mh.distance_v2(distance(400))["rule"]["retire"] is False
    assert "so far; the rule needs at least 300" in mh.distance_v2(distance(12))["rule"]["text"]                         # the same text the distance report prints (one rule, one place)


def test_every_model_always_gets_an_entry_in_the_same_order_even_with_no_data():
    r = mh.health_report({})
    assert [e["model"] for e in r] == list(mh.MODELS) and all(e["n"] == 0 and e["needs"] == [] for e in r)
    assert mh.lines(r) == [f"{m}: no graded locked predictions yet." for m in mh.MODELS]


def test_what_needs_a_person_is_only_a_stricter_calibration_alarm_or_a_retirement_rule():
    ok = mh.winner_v2(winners(400))
    assert ok["needs"] == []
    f = winners(200)
    f["correct"] = [1] * 120 + [0] * 80                                                                                   # 120 right where 75% confidence expected 150 (6.1 standard deviations short)
    e = mh.winner_v2(f)
    assert e["calibration"]["alert"] is True and any("standard deviations worse than its own stated probabilities (120 against 150.0 expected, over 200 graded fights)" in t for t in e["needs"])
    mild = winners(200)
    mild["correct"] = [1] * 139 + [0] * 61                                                                                # 139 against 150: 1.8 standard deviations short: not even reported
    assert mh.winner_v2(mild)["calibration"]["status"] == "consistent with what it said"
    retire = [t for t in mh.winner_v2(winners(400, wrong_every=1))["needs"] if t.startswith("Winner model v2 (model-only): RETIRE")]
    assert len(retire) == 1 and "(a rule fixed in advance, see docs/MODEL_CARDS.md)" in retire[0]


def test_the_report_lines_say_right_for_winners_and_went_the_distance_for_the_distance_models():
    out = mh.lines(mh.health_report({"v2": winners(400), "d1": distance(400)}))
    assert out[0].startswith("Winner model v2 (model-only): 300 of 400 right, against 300.0 expected from its own probabilities (+0.0 standard deviations): consistent with what it said. Retirement rule: keep")
    assert out[1] == "Winner model v3 (market-informed): no graded locked predictions yet."
    assert out[2].startswith("Goes the distance v1 (model-only): 200 of 400 went the distance, against 200.0 expected")


# ---------------------------------------------------------------------------------------------- the database and the daily job
def make_db(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'h.db'}")
    ts = "2026-11-01 12:00:00"

    def rows(base, n_good=3):
        out = [dict(base, status="locked", graded_at=ts) for _ in range(n_good)]
        out.append(dict(base, status="cancelled", graded_at=ts))                       # a cancelled bout: not counted
        out.append(dict(base, status="locked", graded_at=None))                        # not played yet: not counted
        return out
    v2 = dict(fighter_a="A", p_a_wins=0.7, confidence=0.7, actual_winner="A", correct=1)
    v3 = dict(v2, p_v2_a=0.6, market_prob_a=0.72)
    pd.DataFrame(rows(v2) + [dict(v2, status="locked", graded_at=ts, correct=None)]).assign(bout_id=lambda d: [f"b{i}" for i in range(len(d))]).to_sql("upcoming_picks_v2", eng, index=False)   # a draw: correct unknown
    pd.DataFrame(rows(v3)).assign(bout_id=lambda d: [f"b{i}" for i in range(len(d))]).to_sql("upcoming_picks_v3", eng, index=False)
    d1 = pd.DataFrame(rows(dict(p_distance=0.5, p_base=0.49, went_distance=1))).assign(bout_id=lambda d: [f"b{i}" for i in range(5)])
    d1.to_sql("upcoming_distance_v1", eng, index=False)
    pd.DataFrame(rows(dict(p_distance=0.55, p_base=0.49, went_distance=1))).assign(bout_id=lambda d: [f"b{i}" for i in range(5)]).to_sql("upcoming_distance_v2", eng, index=False)
    return eng


def test_the_loader_returns_none_for_tables_that_do_not_exist_yet_and_only_graded_locked_rows_for_those_that_do(tmp_path):
    empty = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert mh.load_frames(empty) == {"v2": None, "v3": None, "d1": None, "d2": None}
    f = mh.load_frames(make_db(tmp_path))
    assert {k: len(v) for k, v in f.items()} == {"v2": 3, "v3": 3, "d1": 3, "d2": 3}                                    # cancelled, ungraded and no-result rows are left out
    assert set(f["d2"].columns) == {"p_v2", "p_v1", "p_base", "went_distance"} and (f["d2"].p_v1 == 0.5).all() and (f["d2"].p_v2 == 0.55).all()      # v2 joined to v1 on the same bout


def test_the_daily_step_adds_one_section_and_flags_only_what_needs_a_person():
    rep = auto_common.Report()
    mh.step_health(None, date(2026, 11, 2), rep, loader=lambda e: {"v2": winners(400)})
    assert [t for t, _ in rep.sections] == ["Model health"] and len(rep.sections[0][1]) == 4 and rep.attention == []
    rep2 = auto_common.Report()
    mh.step_health(None, date(2026, 11, 2), rep2, loader=lambda e: {"v2": winners(400, wrong_every=1)})
    assert len(rep2.attention) == 2 and "Model health" in rep2.render(date(2026, 11, 2), False)


def test_the_health_command_runs_end_to_end_through_the_daily_jobs_entry_point(tmp_path, monkeypatch, capsys):
    import auto_update as au
    import model_v2
    monkeypatch.setattr(model_v2, "make_engine", lambda: make_db(tmp_path))
    assert au.main(["health", "--dry-run", "--today", "2026-11-02"]) == 0
    out = capsys.readouterr().out
    assert "## Model health" in out and "Winner model v2 (model-only): 3 of 3 right" in out and "too few graded fights to check yet (3 so far" in out and "Nothing." in out


def test_the_standalone_command_accepts_a_postgresql_address_the_way_every_other_command_does(monkeypatch):
    import model_v2
    seen = []
    monkeypatch.setattr(sa, "create_engine", lambda url, *a, **k: seen.append(url) or "ENGINE")
    monkeypatch.setattr(mh, "load_frames", lambda e: {})
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example/neondb")
    assert mh.main() == 0 and seen == ["postgresql+psycopg2://user:pw@db.example/neondb"]                               # the plain address would ask for a driver this project does not use
    seen.clear()
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db.example/neondb")
    mh.main()
    assert seen == ["postgresql+psycopg2://user:pw@db.example/neondb"] and model_v2.make_engine is not None


def test_the_standalone_command_prints_the_report_and_exits_nonzero_only_when_something_needs_a_person(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert mh.main() == 2 and "DATABASE_URL is not set" in capsys.readouterr().out
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    assert mh.main() == 0
    out = capsys.readouterr().out
    assert "Model health" in out and "no graded locked predictions yet" in out and "Nothing needs a person." in out
    monkeypatch.setattr(mh, "load_frames", lambda e: {"v2": winners(400, wrong_every=1)})
    assert mh.main() == 1 and "Needs a person:" in capsys.readouterr().out


def test_every_rule_says_keep_in_the_same_words_when_nothing_has_fired():
    for text in (mh.winner_v2(winners(40))["rule"]["text"], mh.winner_v3(winners(40))["rule"]["text"], mh.distance_v1(distance(40))["rule"]["text"],
                 mh.distance_v2(distance(40))["rule"]["text"], mh.winner_v2(winners(400))["rule"]["text"], mh.distance_v2(distance(400))["rule"]["text"]):
        assert text.startswith("keep ("), text                                                                            # one phrase for "no rule applies", wherever the rule lives
    assert "keep both running" not in (ROOT / "pipeline" / "model_distance_v2.py").read_text(encoding="utf-8")                # the older wording that made one line read differently from the others
