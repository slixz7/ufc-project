#!/usr/bin/env python3
"""
Add the "fights with no winner are left out" bullet to README.md, once, on a line of its own, without opening the file; or repair it if it is already there but damaged.

    python tools/add_readme_bullet.py            # add it (does nothing if it is already there)
    python tools/add_readme_bullet.py --check    # only say whether it is there (exit 1 if not)

It puts the bullet on the line straight after the bullet that begins "- **One card is noise.**", in the list under "How to read this honestly", and leaves every other byte of the file
as it was (line endings included). If the bullet is already there but DAMAGED (glued to the end of another line, there twice, or with stray spaces), it takes every copy out and puts one clean
copy back in the right place, which is what a missed Enter when pasting by hand produces: the page then shows the bullet's dash and asterisks as text inside the bullet above it. It REFUSES to touch a README that does not look like this project's: the real one carries hidden number markers (<!--n:...-->) that the Monday job refreshes, and
a copy pasted over it from somewhere else (a rendered page, a document editor) loses them, along with the badges. In that case it says so and tells you how to get the real file back.
The bullet is also in docs/MODEL_CARDS.md, in the same words, and a test keeps the two equal. Uses only the standard library.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = "- **One card is noise.**"
BULLET = ("- **Fights with no winner are left out.** Draws and no contests are not in the backtest or the training data (a disqualification has a winner, so it is included). "
          "A locked pick on a fight that ends that way is marked as having no result and counts neither as right nor as wrong, and the daily report lists any such pick so that a name mismatch cannot hide behind it.")
MARKER = "<!--n:"                       # the hidden number markers that only the project's own README has


class NotThisReadme(ValueError):
    pass


def bullet_state(text: str, bullet: str = BULLET) -> str:
    """'ok' (exactly one copy, on a line of its own, nothing after it), 'missing' (no copy) or 'damaged' (anything else: glued to another line, more than once, stray spaces)."""
    on_its_own_line = sum(1 for line in text.split("\n") if line == bullet)
    anywhere = text.count(bullet)
    if on_its_own_line == 1 and anywhere == 1:
        return "ok"
    return "missing" if anywhere == 0 else "damaged"


def insert_bullet(text: str, bullet: str = BULLET, anchor: str = ANCHOR) -> tuple[str, bool]:
    """(the new text, whether it changed). The text uses \\n line endings. Idempotent. The README must carry the generated markers, and the anchor bullet must appear exactly once.
    Every existing copy of the bullet is taken out first (wherever it sits), then one clean copy goes on the line after the anchor, so a damaged bullet is repaired by the same call."""
    if bullet_state(text, bullet) == "ok":
        return text, False
    if MARKER not in text:
        raise NotThisReadme("this does not look like the project's README: its hidden number markers (<!--n:...-->) are missing, which is what happens when a copy from a web page or a "
                            "document editor is pasted over it. Get the real file back with:  git restore README.md")
    kept = []
    for line in text.split("\n"):
        if bullet in line:
            line = line.replace(" " + bullet, "").replace(bullet, "").rstrip()
            if not line.strip():
                continue                                      # the line held nothing but the bullet: drop it
        kept.append(line)
    cleaned = "\n".join(kept)
    if cleaned.count(anchor) != 1:
        raise NotThisReadme(f"could not find the bullet that begins {anchor!r} exactly once (found {cleaned.count(anchor)}), so I do not know where to put the new one.")
    i = cleaned.index(anchor)
    j = cleaned.find("\n", i)
    j = len(cleaned) if j == -1 else j
    return cleaned[:j] + "\n" + bullet + cleaned[j:], True


def main(argv=None, root: Path = ROOT, out=print) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    path = Path(root) / "README.md"
    if not path.is_file():
        out(f"README.md was not found in {Path(root)}. Run this from the project folder.")
        return 2
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        out("README.md is not UTF-8 text, so I left it alone.")
        return 2
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    state = bullet_state(text)
    try:
        new, changed = insert_bullet(text)
    except NotThisReadme as exc:
        out(f"README.md was NOT changed: {exc}")
        return 2
    if not changed:
        out("The bullet is already in README.md, on a line of its own; nothing changed.")
        return 0
    if check:
        out("The bullet is missing from README.md. Run: python tools/add_readme_bullet.py" if state == "missing"
            else "The bullet is in README.md but damaged (not on a line of its own, or there more than once). Run: python tools/add_readme_bullet.py")
        return 1
    path.write_bytes((new.replace("\n", "\r\n") if crlf else new).encode("utf-8"))
    what = "Added the bullet to" if state == "missing" else "Repaired the damaged bullet in"
    out(f"{what} README.md. Check it with: git diff README.md  (the bullet on a line of its own), then: python -m pytest -q tests/test_no_result_docs.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
