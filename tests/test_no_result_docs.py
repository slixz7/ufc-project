"""
The rule that fights with no winner are left out, and what happens to a locked pick on one, is stated in the README, the model cards and the runbook, in the same words, and the numbers in those words
are the ones the watchdog uses. (The README is edited by hand once, below; the live repository's README also carries numbers the Monday job refreshes, so it is never replaced by an update zip.)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import watchdog as wd  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
CARDS = (ROOT / "docs" / "MODEL_CARDS.md").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
BULLET = ("- **Fights with no winner are left out.** Draws and no contests are not in the backtest or the training data (a disqualification has a winner, so it is included). "
          "A locked pick on a fight that ends that way is marked as having no result and counts neither as right nor as wrong, and the daily report lists any such pick so that a name mismatch cannot hide behind it.")


def test_the_readme_has_the_bullet_in_its_how_to_read_this_honestly_list():
    assert BULLET in README, "add the bullet to README.md by running:  python tools/add_readme_bullet.py   (do not paste text over the README by hand)"
    assert README.split("\n").count(BULLET) == 1 and README.count(BULLET) == 1, "the bullet must be on a line of its own, once; repair it with:  python tools/add_readme_bullet.py"
    start = README.index("**How to read this honestly**")
    end = README.index("\n## ", start)
    assert start < README.index(BULLET) < end and README.index("- **One card is noise.**") < README.index(BULLET)


def test_the_model_cards_say_it_as_one_of_four_rules_that_hold_for_every_model():
    assert "**Four things hold for every model.**" in CARDS and "Three things hold" not in CARDS
    block = CARDS[CARDS.index("**Four things hold for every model.**"):CARDS.index("None of these models is betting advice")]
    assert BULLET in block and len(re.findall(r"^- \*\*", block, flags=re.M)) == 4


def test_the_words_have_no_quotation_marks_so_they_survive_being_pasted_from_anywhere():
    assert not any(ch in BULLET for ch in '"\u201c\u201d\u2018\u2019')


def test_the_runbook_describes_the_report_line_and_its_threshold_in_the_codes_own_numbers():
    for needle in ("It also lists any pick that ended with no result in the last 14 days", "Three or more in 14 days fail the run", "`no_result`", "Fights with no winner are left out of the training data and the backtest", "python tools/add_readme_bullet.py"):
        assert needle in RUNBOOK, needle
    assert f"{wd.NO_RESULT_DAYS} days" in RUNBOOK and wd.NO_RESULT_ALERT_AT == 3 and "Three or more" in RUNBOOK


def test_what_the_documents_say_is_what_the_code_does():
    update = (ROOT / "pipeline" / "update_from_csv.py").read_text(encoding="utf-8")
    assert "Fights without a winner (draws, no contests) are skipped" in update                                                       # left out of the data
    grader = (ROOT / "pipeline" / "model_v2.py").read_text(encoding="utf-8")
    assert "SET status = 'no_result', graded_at = now()" in grader and "had no result (cancelled/draw/no contest)" in grader        # marked, not graded right or wrong
    digest = (ROOT / "pipeline" / "digest.py").read_text(encoding="utf-8")
    assert "FROM upcoming_picks_v2 WHERE status = 'locked'" in digest                                                                # so the results message never counts it
    health = (ROOT / "pipeline" / "model_health.py").read_text(encoding="utf-8")
    assert "status = 'locked' AND graded_at IS NOT NULL AND correct IS NOT NULL" in health                                         # nor does the health report
