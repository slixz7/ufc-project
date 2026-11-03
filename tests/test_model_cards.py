"""
docs/MODEL_CARDS.md: one card per model, and the retirement rules fixed in advance. The cards must not drift from the code: their claims about the models are checked against
the code, and the rules they state are the rules pipeline/model_health.py applies.
"""
import inspect
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import model_distance  # noqa: E402
import model_distance_v2 as mdv2  # noqa: E402
import model_health as mh  # noqa: E402
import model_v2  # noqa: E402
import model_v3  # noqa: E402

CARDS = (ROOT / "docs" / "MODEL_CARDS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def card(name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", CARDS, flags=re.S | re.M)
    assert m, f"no card called {name!r}"
    return m.group(1)


# ---------------------------------------------------------------------------------------------- the cards exist and answer the same questions
def test_there_is_one_card_for_each_model_the_health_check_watches_in_the_same_order():
    heads = re.findall(r"^## (.+)$", CARDS, flags=re.M)
    assert heads == list(mh.MODELS) + ["The rules at a glance"]


@pytest.mark.parametrize("name", list(mh.MODELS))
def test_every_card_says_what_it_predicts_where_it_is_weak_and_when_it_would_be_retired(name):
    c = card(name)
    for part in ("**What it predicts.**", "**Known limits.**", "**Monitoring and retirement.**"):
        assert part in c, (name, part)
    assert "Retire it if" in c and "at least 300 graded" in c


def test_every_file_the_cards_name_exists():
    paths = set(re.findall(r"`((?:pipeline|sql|docs|picks_log|app|tests)/[A-Za-z0-9_./-]*)`", CARDS))
    assert len(paths) >= 12, paths
    assert not [p for p in paths if not (ROOT / p).exists()], [p for p in paths if not (ROOT / p).exists()]


def test_the_cards_quote_no_live_numbers_only_where_to_find_them():
    assert set(re.findall(r"\d[\d,.]*%", CARDS)) == {"95%"}                                                           # the confidence level, never a result
    assert not re.search(r"\b\d{1,3},\d{3}\b", CARDS)                                                                # no fight counts either
    assert "never shows betting odds" in CARDS and CARDS.lower().count("betting advice") >= 2


# ---------------------------------------------------------------------------------------------- the rules are the rules the code applies
def test_the_thresholds_in_the_cards_are_the_ones_in_the_code():
    assert mh.RETIRE_AFTER_FIGHTS == mdv2.RETIRE_AFTER_FIGHTS == 300                                                  # one number, in both modules that apply a rule
    assert f"at least {mh.CHECK_AFTER_FIGHTS} graded" in CARDS and f"at least {mh.RETIRE_AFTER_FIGHTS} graded" in CARDS
    assert f"more than {int(mh.Z_ALERT)} standard deviations" in CARDS and mh.Z_ALERT == 3.0 and mh.CHECK_AFTER_FIGHTS == 100


def _worse(n=400):
    y = np.array([i % 2 for i in range(n)])
    return y, np.where(y == 1, 0.2, 0.8)                                                                              # always wrong


def test_each_cards_baseline_is_the_baseline_the_code_compares_against():
    y, anti = _worse()
    winners = pd.DataFrame({"fighter_a": "A", "actual_winner": np.where(y == 1, "A", "B"), "p_a_wins": anti, "p_v2_a": anti, "market_prob_a": np.where(y == 1, 0.8, 0.2),
                            "confidence": 0.8, "correct": 0})
    dist = pd.DataFrame({"p_distance": anti, "p_v2": anti, "p_v1": 0.5, "p_base": 0.5, "went_distance": y})
    baseline = lambda e: re.search(r"clearly worse than (.+)$", e["rule"]["text"]).group(1)
    for entry, name in ((mh.winner_v2(winners), mh.MODELS[0]), (mh.winner_v3(winners), mh.MODELS[1]), (mh.distance_v1(dist), mh.MODELS[2])):
        assert entry["rule"]["retire"] is True
        assert baseline(entry) in card(name) or baseline(entry) in CARDS, (name, baseline(entry))                        # "a coin flip", "the bookmakers' opening line alone", "the plain decision rate of the previous three years"
    d2 = mh.distance_v2(dist)
    assert d2["rule"]["retire"] is True and "clearly worse than the model-only one" in d2["rule"]["text"] and "clearly worse than v1" in card(mh.MODELS[3])
    table = card("The rules at a glance")
    for needle in ("A coin flip", "The bookmakers' opening line alone", "The plain decision rate of the previous three years", "Goes the distance v1"):
        assert needle in table, needle


def test_the_cards_say_the_same_things_as_the_health_report_about_what_is_flagged():
    assert "Needs your attention" in CARDS and "python pipeline/auto_update.py health" in CARDS and "python pipeline/model_health.py" in CARDS
    assert "Change this page and `pipeline/model_health.py` in the same commit" in CARDS


# ---------------------------------------------------------------------------------------------- what the cards say about the models is true of the code
def test_the_winner_cards_describe_the_models_the_code_builds():
    v2, v3 = card(mh.MODELS[0]), card(mh.MODELS[1])
    assert model_v2.MIN_TRAIN == 300 and "first 300 fights" in v2 and "early_rule" in v2 and "early_rule" in inspect.getsource(model_v2)
    for tier in ("full", "limited", "debut"):
        assert f"**{tier}**" in v2 and f'"{tier}"' in inspect.getsource(model_v2)
    assert "fit_intercept=False" in inspect.getsource(model_v2.fit) and "no intercept" in v2                            # "a logistic regression with no intercept"
    assert "LogisticRegression" in inspect.getsource(model_v2.fit)
    assert "fit_intercept=False" in inspect.getsource(model_v3.fit_stacker) and "no intercept" in v3 and "LogisticRegression" in inspect.getsource(model_v3.fit_stacker)
    imp = lambda a: 100 / (a + 100) if a > 0 else -a / (-a + 100)
    assert model_v3.devig(-250, 200) == pytest.approx(imp(-250) / (imp(-250) + imp(200))) and "divided out evenly" in v3


def test_the_distance_cards_describe_the_models_the_code_builds():
    d1, d2 = card(mh.MODELS[2]), card(mh.MODELS[3])
    assert model_distance.ERA_START.year == 2002 and "since 2002" in d1 and "disqualifications left out" in d1
    assert model_distance.K_SHRINK > 0 and "pulled toward the average" in d1 and "LogisticRegression" in inspect.getsource(model_distance.fit) and "regularised" in d1 and 0 < model_distance.C_REG < 1
    design = mdv2.stack_design(np.array([0.5, 0.6]), np.array([0.7, 0.6]))
    assert design.shape == (2, 3) and design[0, 1] == pytest.approx(0.2) and design[0, 2] == pytest.approx(0.04)          # v1's prediction, the favourite's strength, and its square
    assert "its square" in d2 and "alongside" in d2 and "never instead of it" in d2 and "LogisticRegression" in inspect.getsource(mdv2.fit_stacker) and "stack_design" in inspect.getsource(mdv2.fit_stacker)


# ---------------------------------------------------------------------------------------------- the rest of the documentation points here
def test_the_readme_and_runbook_point_to_the_cards_and_the_health_check():
    row = [l for l in README.splitlines() if l.startswith("| **Model cards**")]
    assert len(row) == 1 and "(docs/MODEL_CARDS.md)" in row[0] and "retiring it" in row[0] and not re.search(r"\d+%", row[0])
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "## Model health and the model cards" in runbook and "pipeline/model_health.py" in runbook and "tests/test_model_cards.py" in runbook
    assert "python pipeline/auto_update.py health --dry-run" in runbook


def test_the_readme_shows_exactly_three_demo_screenshots():
    shots = re.findall(r"!\[[^\]]*\]\(docs/images/((?:demo-[a-z-]+|upcoming|trackrecord|leaderboard)\.png)\)", README)
    assert sorted(shots) == ["leaderboard.png", "trackrecord.png", "upcoming.png"]                                      # the owner chose three: a fourth is a decision, not an accident
