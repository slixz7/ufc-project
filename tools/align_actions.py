#!/usr/bin/env python3
"""
Keep every GitHub Action in the workflow files on the SAME major version as the rest of the repository.

    python tools/align_actions.py                  # make every workflow file agree (the most common major wins; a tie goes to the highest)
    python tools/align_actions.py --check          # only say whether they agree (exit 1 if not); write nothing
    python tools/align_actions.py FILE [FILE...]   # fix just these files, to match the OTHER workflow files

Why it exists: Dependabot moves an action (checkout, upload-artifact, ...) in every workflow file at once, and a test insists that each action has one version everywhere. An update zip
that carries a whole workflow file brings the versions it was written with, and applying it would quietly undo what Dependabot did to that file. `tools/apply_update.py` therefore calls
this for every workflow file it places, so a placed workflow adopts the versions your repository already uses. Run it by hand if the test "each action has one version everywhere" fails.

Only `uses: owner/repo@vN` lines are touched (subfolder actions such as github/codeql-action/init count as their repository). Pins to a branch, a commit, a local action or a docker image are
left alone, and so are comments, indentation and line endings. It uses only Python's standard library.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USES = re.compile(r"^(?P<lead>[ \t]*(?:-[ \t]+)?uses:[ \t]*)(?P<action>[\w.-]+/[\w.-]+)(?P<sub>(?:/[\w./-]+)?)@v(?P<major>\d+)(?P<tail>[^\n]*)$", re.M)
WORKFLOWS = ".github/workflows"


def majors(text: str) -> list[tuple[str, int]]:
    """[(owner/repo, major)] for every `uses: owner/repo[/folder]@vN` line, in order."""
    return [(m.group("action"), int(m.group("major"))) for m in USES.finditer(text)]


def consensus(texts) -> dict[str, int]:
    """For each action, the major most of its uses are on; on a tie, the highest (Dependabot only ever moves up)."""
    seen: dict[str, Counter] = {}
    for text in texts:
        for action, major in majors(text):
            seen.setdefault(action, Counter())[major] += 1
    return {a: max(c, key=lambda v: (c[v], v)) for a, c in seen.items()}


def align_text(text: str, wanted: dict[str, int]) -> tuple[str, list[tuple[str, int, int]]]:
    """(the text with each use moved to the wanted major, [(action, old, new)]). Anything not in `wanted` is left as it is."""
    changes: list[tuple[str, int, int]] = []

    def fix(m):
        action, old = m.group("action"), int(m.group("major"))
        new = wanted.get(action, old)
        if new != old:
            changes.append((action, old, new))
        return f"{m.group('lead')}{action}{m.group('sub')}@v{new}{m.group('tail')}"
    return USES.sub(fix, text), changes


def align_bytes(data: bytes, wanted: dict[str, int]) -> tuple[bytes, list[tuple[str, int, int]]]:
    """The same for a file's bytes; a file that is not UTF-8 text is returned unchanged."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, []
    new, changes = align_text(text, wanted)
    return (new.encode("utf-8") if changes else data), changes


def workflow_files(root: Path) -> list[Path]:
    folder = Path(root) / WORKFLOWS
    return sorted(p for p in folder.glob("*.y*ml") if p.is_file() and p.suffix in (".yml", ".yaml")) if folder.is_dir() else []


def reference_majors(root: Path, exclude=()) -> dict[str, int]:
    """The majors in use across the repository's workflow files, leaving out the files (as relative paths) in `exclude`: what an incoming workflow should match."""
    skip = {str(e).replace("\\", "/") for e in exclude}
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in workflow_files(root) if f"{WORKFLOWS}/{p.name}" not in skip]
    return consensus(texts)


def main(argv=None, root: Path = ROOT, out=print) -> int:
    ap = argparse.ArgumentParser(description="Make the GitHub Actions in the workflow files agree on one major version each")
    ap.add_argument("files", nargs="*", type=Path, help="only these workflow files (default: all of them)")
    ap.add_argument("--check", action="store_true", help="say whether they agree; change nothing; exit 1 if they do not")
    args = ap.parse_args(argv)
    root = Path(root)
    everything = workflow_files(root)
    if not everything:
        out(f"No workflow files found in {WORKFLOWS}/.")
        return 0
    targets = [Path(f) if Path(f).is_absolute() else root / f for f in args.files] if args.files else everything
    missing = [t for t in targets if not t.is_file()]
    if missing:
        out("Not found: " + ", ".join(str(m) for m in missing))
        return 2
    rel = lambda p: p.resolve().relative_to(root.resolve()).as_posix()           # noqa: E731
    wanted = consensus(p.read_text(encoding="utf-8", errors="replace") for p in everything) if not args.files else \
        reference_majors(root, exclude=[rel(t) for t in targets])
    pending = []
    for t in targets:
        data = t.read_bytes()
        new, changes = align_bytes(data, wanted)
        if changes:
            pending.append((t, new, changes))
    for t, new, changes in pending:
        for action, old, now in changes:
            out(f"{'DIFFERS ' if args.check else 'ALIGNED '} {rel(t)}: {action} v{old} -> v{now}")
        if not args.check:
            t.write_bytes(new)
    if not pending:
        out("The workflow files already agree on one version of each action." if not args.files else "Those workflow files already match the rest.")
        return 0
    if args.check:
        out("They do not agree. Run: python tools/align_actions.py")
        return 1
    out("Done. Now run: python -m pytest -q tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
