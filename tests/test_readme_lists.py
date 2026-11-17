"""
The README's "How to read this honestly" list must be well formed: every bullet is "- **Lead-in.** its sentence" on ONE line, and nothing is empty. An edit in GitHub's web editor once split the
"One card is noise." bullet in two (its bold lead-in stranded, its sentence moved to a bullet with no lead-in, and an empty bullet between), and every other test still passed, because they only asked
whether some text was present. This reads the list's structure, and it is tested against that exact damage.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADING = "**How to read this honestly**"
BULLET = re.compile(r"^- \*\*(?P<lead>.+?)\*\* (?P<body>\S.*)$")
MARKERS = re.compile(r"<!--/?n:?[^>]*-->")                                                    # the hidden number markers inside a bullet


def reading_list(text: str) -> list:
    """The bullet lines directly under the heading (after one blank line), up to the first line that is not a bullet."""
    lines = text.split("\n")
    i = lines.index(HEADING) + 1
    while lines[i].strip() == "":
        i += 1
    bullets = []
    while i < len(lines) and lines[i].startswith(("- ", "* ", "-", "*")) and lines[i].strip() not in ("**",):
        bullets.append(lines[i])
        i += 1
    return bullets


def problems(text: str) -> list:
    found = []
    if HEADING not in text.split("\n"):
        return [f"the heading {HEADING} is missing"]
    bullets = reading_list(text)
    if len(bullets) < 5:
        found.append(f"the list has only {len(bullets)} bullets")
    for n, line in enumerate(bullets, 1):
        m = BULLET.match(line)
        if not m:
            found.append(f"bullet {n} is not '- **Lead-in.** sentence' on one line: {line[:60]!r}")
            continue
        if not MARKERS.sub("", m.group("lead")).strip().endswith("."):
            found.append(f"bullet {n}'s bold lead-in does not end with a full stop: {m.group('lead')[:50]!r}")
    for n, line in enumerate(text.split("\n"), 1):
        if re.fullmatch(r"\s*[-*+]\s*", line):
            found.append(f"line {n} is an empty list item")
    leads = " | ".join(MARKERS.sub("", BULLET.match(b).group("lead")) for b in bullets if BULLET.match(b))
    for lead in ("One card is noise.", "Fights with no winner are left out."):
        if lead not in leads:
            found.append(f"there is no bullet that begins {lead!r}")
    noise = next((b for b in bullets if b.startswith("- **One card is noise.**")), "")
    if noise and not ("single event can land anywhere" in noise and "yearly accuracy" in noise):
        found.append("the 'One card is noise.' bullet is cut short: its sentence is missing from its line")
    return found


GOOD = """# T

**How to read this honestly**

- **The market is doing most of the work.** The blend puts about <!--n:market_weight-->71%<!--/n--> of its weight on the market.
- **The probabilities are honest.** The calibration table agrees.
- **The market and the model disagree on <!--n:dis_share-->25.5%<!--/n--> of fights.** In those fights the blend won more.
- **The backtest uses opening lines.** Live picks lock two days before an event.
- **One card is noise.** A card has 10 to 14 fights, so a single event can land anywhere. The model's yearly accuracy ranges widely, and the yearly accuracy is noisy.
- **Fights with no winner are left out.** Draws and no contests are not in the data.

**Calibration:** next.
"""
# the damage seen on the page: the bullet cut after its lead-in, the new bullet in the gap, an empty bullet, and the rest of the sentence in a bullet with no lead-in
SPLIT = GOOD.replace("- **One card is noise.** A card has 10 to 14 fights, so a single event can land anywhere. The model's yearly accuracy ranges widely, and the yearly accuracy is noisy.\n- **Fights with no winner are left out.** Draws and no contests are not in the data.\n",
                     "- **One card is noise.**\n- **Fights with no winner are left out.** Draws and no contests are not in the data.\n- \n- A card has 10 to 14 fights, so a single event can land anywhere. The model's yearly accuracy ranges widely, and the yearly accuracy is noisy.\n")


def test_a_well_formed_list_has_no_problems():
    assert problems(GOOD) == []


def test_the_exact_damage_seen_on_the_page_is_caught_with_all_three_symptoms():
    assert SPLIT != GOOD
    found = problems(SPLIT)
    assert any("bullet 5 is not '- **Lead-in.** sentence' on one line" in p and "One card is noise" in p for p in found)       # the lead-in alone on its line
    assert any("is an empty list item" in p for p in found) and any("bullet 7 is not" in p and "'- '" in p for p in found)      # the empty bullet
    assert any("bullet 8 is not" in p and "A card has 10 to 14 fights" in p for p in found)                                      # the stranded sentence with no lead-in
    assert any("the 'One card is noise.' bullet is cut short" in p for p in found) or any("there is no bullet that begins 'One card is noise.'" in p for p in found)


def test_each_kind_of_damage_alone_is_caught():
    assert any("empty list item" in p for p in problems(GOOD.replace("- **The probabilities are honest.**", "- \n- **The probabilities are honest.**")))
    assert any("is not '- **Lead-in.** sentence'" in p for p in problems(GOOD.replace("- **The backtest uses opening lines.**", "- The backtest uses opening lines.")))
    assert any("does not end with a full stop" in p for p in problems(GOOD.replace("**The probabilities are honest.**", "**The probabilities are honest**")))
    assert any("is not '- **Lead-in.** sentence'" in p for p in problems(GOOD.replace("- **The probabilities are honest.** The calibration table agrees.", "- **The probabilities are honest.** ")))   # a lead-in with nothing after it
    assert any("bullet that begins 'Fights with no winner are left out.'" in p for p in problems(GOOD.replace("Fights with no winner are left out.", "Draws are skipped.")))
    assert any("cut short" in p for p in problems(GOOD.replace("so a single event can land anywhere. ", "")))
    assert any("has only 4 bullets" in p for p in problems(GOOD.replace("- **The backtest uses opening lines.** Live picks lock two days before an event.\n", "").replace("- **The probabilities are honest.** The calibration table agrees.\n", "")))
    assert problems(GOOD.replace("- **The backtest uses opening lines.** Live picks lock two days before an event.\n", "")) == []                       # five bullets is still a list
    assert problems("# no heading here") == [f"the heading {HEADING} is missing"]


def test_the_real_readmes_reading_list_is_well_formed():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert problems(readme) == [], "the README's reading list is damaged: restore the file from the last good commit (git restore --source=<commit> README.md) and add the bullet with python tools/add_readme_bullet.py"


def test_the_runbook_says_why_this_test_exists_and_how_readme_edits_are_made():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("tests/test_readme_lists.py", "split", "web editor", "git restore --source="):
        assert needle in runbook, needle
