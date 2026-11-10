"""
tools/add_readme_bullet.py: add one bullet to README.md without anyone opening it, and refuse a README that is not the project's. It exists because pasting the bullet by hand once replaced the whole file
with a converted copy (no hidden number markers, escaped brackets and asterisks), which would have broken the Monday number refresh and lost the badges.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import add_readme_bullet as ab  # noqa: E402

GOOD = ("<!-- Numbers inside markers come from the database. -->\n# Title\n\n**How to read this honestly**\n\n"
        "- **The market is doing most of the work.** About <!--n:market_weight-->71%<!--/n--> of the weight.\n"
        "- **One card is noise.** A card has 10 to 14 fights.\n- **Next bullet.** Something else.\n\n## Next section\n")


def project(tmp_path, text, crlf=False):
    (tmp_path / "README.md").write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return tmp_path


def test_the_bullet_goes_on_the_line_straight_after_the_anchor_and_nothing_else_moves():
    new, changed = ab.insert_bullet(GOOD)
    assert changed
    old_lines, new_lines = GOOD.split("\n"), new.split("\n")
    assert len(new_lines) == len(old_lines) + 1
    k = old_lines.index("- **One card is noise.** A card has 10 to 14 fights.")
    assert new_lines[k + 1] == ab.BULLET and new_lines[:k + 1] == old_lines[:k + 1] and new_lines[k + 2:] == old_lines[k + 1:]


def test_running_it_again_changes_nothing():
    once, _ = ab.insert_bullet(GOOD)
    twice, changed = ab.insert_bullet(once)
    assert not changed and twice == once and once.count(ab.BULLET) == 1


def test_an_anchor_at_the_very_end_of_the_file_still_works():
    text = "<!--n:x-->1<!--/n-->\n- **One card is noise.** end"
    new, changed = ab.insert_bullet(text)
    assert changed and new == text + "\n" + ab.BULLET


@pytest.mark.parametrize("text, why", [
    ("# UFC Fight Predictor\n\n* \\*\\*One card is noise.\\*\\* converted copy\n", "hidden number markers"),                          # what a pasted copy of a web page looks like
    ("<!--n:x-->1<!--/n-->\nno such bullet here\n", "exactly once (found 0)"),
    ("<!--n:x-->1<!--/n-->\n- **One card is noise.** a\n- **One card is noise.** b\n", "exactly once (found 2)")])
def test_a_readme_that_is_not_the_projects_is_refused_with_the_reason(text, why):
    with pytest.raises(ab.NotThisReadme, match=re.escape(why) if "markers" not in why else "number markers"):
        ab.insert_bullet(text)


def test_only_the_bullet_itself_is_the_anchor_not_the_same_words_in_a_sentence_or_in_another_bullet_style():
    prose = "<!--n:x-->1<!--/n-->\nA note: One card is noise.\n- **One card is noise.** the bullet\n"
    new, changed = ab.insert_bullet(prose)
    assert changed and new == prose.rstrip("\n") + "\n" + ab.BULLET + "\n"                                                       # after the real bullet, not after the sentence
    star = "<!--n:x-->1<!--/n-->\n* **One card is noise.** a star-style list\n"
    with pytest.raises(ab.NotThisReadme, match=re.escape("exactly once (found 0)")):                                              # a different list style is not guessed at
        ab.insert_bullet(star)


def test_the_command_adds_it_once_prints_what_to_do_next_and_keeps_windows_line_endings(tmp_path):
    root = project(tmp_path, GOOD, crlf=True)
    lines = []
    assert ab.main([], root=root, out=lines.append) == 0 and lines[0].startswith("Added the bullet to README.md. Check it with: git diff README.md")
    raw = (root / "README.md").read_bytes().decode("utf-8")
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "") and raw.count(ab.BULLET) == 1
    lines.clear()
    assert ab.main([], root=root, out=lines.append) == 0 and lines == ["The bullet is already in README.md, on a line of its own; nothing changed."]


def test_check_mode_reports_without_writing(tmp_path):
    root = project(tmp_path, GOOD)
    lines = []
    assert ab.main(["--check"], root=root, out=lines.append) == 1 and "The bullet is missing from README.md" in lines[0]
    assert (root / "README.md").read_text(encoding="utf-8") == GOOD
    ab.main([], root=root, out=lambda m: None)
    assert ab.main(["--check"], root=root, out=lines.append) == 0


def test_a_pasted_over_readme_is_left_exactly_as_it_was_and_the_way_back_is_named(tmp_path):
    converted = "# UFC Fight Predictor\n\n* \\*\\*One card is noise.\\*\\* ...\n"
    root = project(tmp_path, converted)
    lines = []
    assert ab.main([], root=root, out=lines.append) == 2
    assert lines[0].startswith("README.md was NOT changed:") and "git restore README.md" in lines[0]
    assert (root / "README.md").read_text(encoding="utf-8") == converted


def test_a_missing_or_non_text_readme_is_reported_not_crashed_on(tmp_path):
    lines = []
    assert ab.main([], root=tmp_path, out=lines.append) == 2 and "was not found" in lines[0]
    (tmp_path / "README.md").write_bytes(b"\xff\xfe\x00bad")
    assert ab.main([], root=tmp_path, out=lines.append) == 2 and lines[-1] == "README.md is not UTF-8 text, so I left it alone."


def test_the_real_readme_takes_the_bullet_as_exactly_one_new_line(tmp_path):
    real = (ROOT / "README.md").read_text(encoding="utf-8")
    without = "\n".join(l for l in real.split("\n") if l != ab.BULLET)                                                             # how it is before the bullet is added
    assert without != real and "<!--n:" in without
    new, changed = ab.insert_bullet(without)
    assert changed and new == real                                                                                               # the result is exactly the committed README


def test_the_bullet_is_the_same_words_as_the_model_cards_and_the_documentation_test_and_has_no_quotation_marks():
    cards = (ROOT / "docs" / "MODEL_CARDS.md").read_text(encoding="utf-8")
    assert ab.BULLET in cards
    sys.path.insert(0, str(ROOT / "tests"))
    import test_no_result_docs as docs
    assert docs.BULLET == ab.BULLET
    assert not any(ch in ab.BULLET for ch in '"\u201c\u201d\u2018\u2019')


def test_it_needs_only_the_standard_library():
    tree = ast.parse((ROOT / "tools" / "add_readme_bullet.py").read_text(encoding="utf-8"))
    top = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert top <= {"__future__", "sys", "pathlib"}, top


# ---------------------------------------------------------------------------------------------- a bullet that is there but damaged
CLEAN = ab.insert_bullet(GOOD)[0]                                            # GOOD with the bullet on its own line after the anchor
CLEAN_LINES = CLEAN.split("\n")
K = CLEAN_LINES.index(ab.BULLET)


def glued(host_index):
    """CLEAN, but with the bullet taken off its own line and stuck to the end of line `host_index` (what a missed Enter does)."""
    lines = [l for i, l in enumerate(CLEAN_LINES) if i != K]
    lines[host_index] = lines[host_index] + " " + ab.BULLET
    return "\n".join(lines)


@pytest.mark.parametrize("text, state", [
    (GOOD, "missing"), (CLEAN, "ok"), (glued(K - 1), "damaged"), (CLEAN.replace(ab.BULLET, ab.BULLET + "  "), "damaged"), (CLEAN.replace(ab.BULLET, " " + ab.BULLET), "damaged"),
    (CLEAN + ab.BULLET + "\n", "damaged"), (CLEAN.replace(ab.BULLET, ab.BULLET + "\n" + ab.BULLET), "damaged")])
def test_the_state_of_the_bullet_is_ok_missing_or_damaged(text, state):
    assert ab.bullet_state(text) == state


@pytest.mark.parametrize("name, damaged", [
    ("glued to the end of the anchor line", glued(K - 1)), ("glued to the end of the bullet before it", glued(K - 2)), ("glued to the last bullet", glued(K)),
    ("there twice on their own lines", CLEAN.replace(ab.BULLET, ab.BULLET + "\n" + ab.BULLET)), ("with trailing spaces", CLEAN.replace(ab.BULLET, ab.BULLET + "   ")),
    ("with a leading space", CLEAN.replace(ab.BULLET, "  " + ab.BULLET)),
    ("glued with two spaces", glued(K - 1).replace(" " + ab.BULLET, "  " + ab.BULLET)), ("glued with a tab", glued(K - 1).replace(" " + ab.BULLET, "\t" + ab.BULLET)),
    ("glued with no space at all", glued(K - 1).replace(" " + ab.BULLET, ab.BULLET)), ("one proper copy and one glued on", glued(K - 1).replace("\n## Next section", "\n" + ab.BULLET + "\n## Next section"))])
def test_every_damaged_form_is_repaired_to_exactly_the_clean_text(name, damaged):
    repaired, changed = ab.insert_bullet(damaged)
    assert changed and repaired == CLEAN, name                                                                                  # the host line is restored exactly, and the bullet sits once after the anchor
    assert ab.insert_bullet(repaired) == (repaired, False)                                                                      # and a second run changes nothing


def test_the_command_says_it_repaired_and_keeps_windows_line_endings(tmp_path):
    root = project(tmp_path, glued(K - 1), crlf=True)
    lines = []
    assert ab.main([], root=root, out=lines.append) == 0 and lines[0].startswith("Repaired the damaged bullet in README.md. Check it with: git diff README.md")
    raw = (root / "README.md").read_bytes().decode("utf-8")
    assert raw == CLEAN.replace("\n", "\r\n")
    lines.clear()
    assert ab.main([], root=root, out=lines.append) == 0 and lines == ["The bullet is already in README.md, on a line of its own; nothing changed."]


def test_check_mode_calls_a_damaged_bullet_damaged_and_writes_nothing(tmp_path):
    root = project(tmp_path, glued(K - 1))
    lines = []
    assert ab.main(["--check"], root=root, out=lines.append) == 1 and "in README.md but damaged" in lines[0]
    assert (root / "README.md").read_text(encoding="utf-8") == glued(K - 1)


def test_the_real_readme_in_every_damaged_form_is_repaired_to_exactly_itself():
    real = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = real.split("\n")
    k = lines.index(ab.BULLET)
    without = [l for i, l in enumerate(lines) if i != k]
    forms = {"glued to the anchor line": without[:k - 1] + [without[k - 1] + " " + ab.BULLET] + without[k:],
             "glued to the bullet before the anchor": without[:k - 2] + [without[k - 2] + " " + ab.BULLET] + without[k - 1:]}
    for name, damaged in forms.items():
        assert ab.bullet_state("\n".join(damaged)) == "damaged", name
        assert ab.insert_bullet("\n".join(damaged))[0] == real, name
    assert ab.insert_bullet(real.replace(ab.BULLET, ab.BULLET + "   "))[0] == real
    assert ab.insert_bullet(real.replace(ab.BULLET, ab.BULLET + "\n" + ab.BULLET))[0] == real
