"""
docs/writeup.md: the project's case study. It is a dated snapshot, so these tests never compare its figures with the README's (those refresh themselves, and a test that
tied the two together could one day stop the daily job). They check what must stay true however the numbers move: it says what it can and cannot claim, it makes none of
the claims this project has been careful not to make, and everything it says about the code is true of the code.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import model_health as mh  # noqa: E402

TEXT = (ROOT / "docs" / "writeup.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_it_is_a_finished_dated_snapshot_not_a_draft_with_notes_to_the_author():
    for marker in ("[Screenshot", "(draft)", "TODO", "Check every number", "XXX", "Lorem", "swap in newer"):
        assert marker not in TEXT, marker
    assert "snapshot from early November 2026" in TEXT and "README's numbers refresh themselves" in TEXT
    assert 1000 < len(TEXT.split()) < 2200                                                                               # a case study, not a stub and not a book


def test_it_says_what_the_project_can_and_cannot_claim():
    for sentence in ("does most of the work", "Twenty-six fights prove nothing", "I do not claim an edge over the betting market", "I do not claim that 26 live fights show skill",
                     "not betting advice", "never shows betting odds", "not affiliated with or endorsed by the UFC", "cannot be told apart"):
        assert sentence in TEXT, sentence
    assert "I claim an honest evaluation" in TEXT and "## What I would and would not claim" in TEXT


def test_it_makes_none_of_the_claims_this_project_is_careful_not_to_make():
    assert not re.search(r"\b(beat|beats|beating|outperform\w*)\s+(the\s+)?(bookmakers?|market|odds)\b", TEXT, flags=re.I)         # "beats the opening line", a measured comparison, is allowed; beating the market is not claimed
    for phrase in ("guarantee", "profit", "sure bet", "easy money", "winning strategy", "always wins", "can't lose"):
        assert phrase not in TEXT.lower(), phrase
    assert "never shows betting odds" in TEXT and not re.search(r"[+-]\d{3}\b", TEXT)                                       # no odds quoted, as on the site


def test_its_links_work_and_it_points_to_the_live_app_the_code_and_the_demo():
    for target in re.findall(r"\]\(([^)\s]+)\)", TEXT):
        if not target.startswith("http"):
            assert (ROOT / "docs" / target).exists(), target
    assert "<https://ufc-predictor.streamlit.app/>" in TEXT and "<https://github.com/slixz7/ufc-project>" in TEXT and "python demo/run_demo.py" in TEXT
    assert (ROOT / "demo" / "run_demo.py").exists()


def test_what_it_says_about_the_code_is_true_of_the_code():
    cards = (ROOT / "pipeline" / "auto_cards.py").read_text(encoding="utf-8")
    assert "refuses a card that suddenly shrinks" in TEXT
    assert re.search(r"^MAX_SHRINK\s*=\s*\d+", cards, flags=re.M) and cards.count("MAX_SHRINK") >= 2                       # the guard is defined and actually used
    assert "never guesses a fighter's name" in TEXT and "never guesses a name" in README                                    # and the README says the same
    assert "at least 300 graded fights" in TEXT and mh.RETIRE_AFTER_FIGHTS == 300                                           # the retirement rule it quotes is the one the code applies
    assert "Retire it if" in (ROOT / "docs" / "MODEL_CARDS.md").read_text(encoding="utf-8") and "(MODEL_CARDS.md)" in TEXT
    assert (ROOT / "pipeline" / "restore_drill.py").exists() and (ROOT / "pipeline" / "notify_failure.py").exists() and (ROOT / "constraints-ci.txt").exists()
    assert "--history" in (ROOT / "tools" / "scan_secrets.py").read_text(encoding="utf-8") and "scans for credentials in the files and the whole git history" in TEXT
    n_tests = sum(len(re.findall(r"^def test_", p.read_text(encoding="utf-8"), flags=re.M)) for p in (ROOT / "tests").glob("test_*.py"))
    assert "hundreds of automated tests" in TEXT and n_tests >= 200, n_tests                                              # "hundreds" is true, whatever the exact count


def test_it_is_not_the_old_draft_and_the_readme_and_docs_point_to_it():
    for stale in ("48 tests", "UFC 333 was the first card", "Retrain the blend", "*[Screenshot", "## LinkedIn post"):
        assert stale not in TEXT, stale                                                                                  # the old draft's out-of-date lines
    row = [l for l in README.splitlines() if l.startswith("| **Write-up**")]
    assert len(row) == 1 and "(docs/writeup.md)" in row[0] and not re.search(r"\d+%", row[0])
    assert "[write-up](docs/writeup.md)" in README


def test_the_live_record_it_quotes_is_described_as_too_small_to_conclude_anything():
    live = TEXT[TEXT.index("## The live record"):TEXT.index("## Running it unattended")]
    assert "prove nothing" in live and "its own stated probabilities expected" in live and "17 of 26" in live
    assert "locks every pick before the event" in live and "cannot change" in live
