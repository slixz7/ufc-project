#!/usr/bin/env python3
"""
Rebuild the dependency lock (constraints-ci.txt) from a fresh `pip freeze`, say what changed, and refuse anything that a passing test run did not prove.

It is the brain of .github/workflows/refresh-lock.yml (monthly). Standard library only, because the job that opens the pull request installs nothing.

    python tools/refresh_lock.py plan --freeze freeze.txt --current constraints-ci.txt --out constraints-ci.txt --date 2026-12-01
        Job 1 (read-only, no secrets). Compares the fresh freeze with the current lock. If the pins are the same it says changed=false and writes nothing. Otherwise it
        writes the new lock (so the tests that follow check the NEW versions and the NEW file) and says changed=true.

    python tools/refresh_lock.py summarize-log pytest.log
        Reads the last pytest line. Prints "553 passed, 2 skipped", or exits 2 if anything failed, errored or nothing passed.

    python tools/refresh_lock.py publish --new proposed/constraints-ci.txt --current constraints-ci.txt --tests "553 passed, 2 skipped" --date 2026-12-01 \
        --out-lock constraints-ci.txt --out-summary pr-body.md
        Job 2 (write access, runs only code from main). The proposed lock came from a job that had just run brand-new, unreviewed packages, so it is NOT trusted as a
        file: only its exact pins are read, the header and the pull request text are written here, and the result must still look like this project's lock.

Do not edit constraints-ci.txt by hand, and never regenerate it on another Python version or operating system.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9._+!-]*)")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TESTS_OK = re.compile(r"[1-9]\d* passed(, [1-9]\d* skipped)?")
CORE = ("pandas", "numpy", "scikit-learn", "sqlalchemy", "psycopg2-binary", "streamlit", "pytest", "pyflakes", "requests", "beautifulsoup4", "kaggle")   # what the tests depend on
MIN_PINS = 40                                                                                                  # the same floor as tests/test_pinned_dependencies.py
MAX_ROWS = 120                                                                                                 # rows in the pull request's table
KINDS = ("major", "downgrade", "minor", "patch", "calendar", "other")                                          # the order a reader should look at them
PENDING = "being confirmed by the test run that follows this file"


class LockError(ValueError):
    """Something about the lock or the inputs is not acceptable. The message says what and where."""


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str, what: str = "the lock") -> dict[str, tuple[str, str]]:
    """{normalised name: (name, version)} from lines of `name==version`. Comments and blank lines are ignored; anything else is an error, so nothing can hide in the file."""
    pins: dict[str, tuple[str, str]] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN.fullmatch(line)
        if not m:
            raise LockError(f"{what}, line {n}: not an exact pin (name==version); editable and direct-URL installs cannot be locked: {line[:60]!r}")
        name, version = m.groups()
        if norm(name) in pins:
            raise LockError(f"{what}, line {n}: {name} appears twice")
        pins[norm(name)] = (name, version)
    return pins


def same_pins(a: dict, b: dict) -> bool:
    return {k: v[1] for k, v in a.items()} == {k: v[1] for k, v in b.items()}


def check_valid_date(date: str) -> str:
    if not DATE.fullmatch(date or ""):
        raise LockError(f"the date must look like 2026-12-01, not {date!r}")
    return date


def header(date: str, tests: str) -> str:
    return (
        "# Exact package versions for the GitHub workflows (Python 3.12 on Linux). The workflows install with: pip install -r <requirements file> -c constraints-ci.txt\n"
        "#\n"
        f"# PROVEN: on {date} a brand-new environment installed pipeline/requirements.txt + app/requirements.txt + pyflakes, resolved to exactly these versions,\n"
        f"# and the whole test suite passed against them ({tests}). The requirements files keep only the minimum each package needs; this file says\n"
        "# which versions are actually used, so a new release of any package cannot change the daily job, the backup or the tests until this lock is refreshed.\n"
        "#\n"
        "# It is refreshed on the 1st of every month by .github/workflows/refresh-lock.yml: a brand-new Python 3.12 environment installs the newest versions the requirements\n"
        "# allow, the whole suite runs against them, and only if everything passes is a pull request opened for you to review and merge. Do not edit it by hand, and do not\n"
        "# regenerate it on another Python version or operating system.\n"
    )


def render(pins: dict[str, tuple[str, str]], date: str, tests: str) -> str:
    body = "".join(f"{name}=={version}\n" for name, version in sorted(pins.values(), key=lambda p: norm(p[0])))
    return header(check_valid_date(date), tests) + body


# ---------------------------------------------------------------------------------------------- what changed
def _numbers(version: str) -> list[int]:
    """The leading release numbers of a version: 2.3.1 -> [2, 3, 1]; 1.0rc1 -> [1, 0]; 2025.10.5 -> [2025, 10, 5]."""
    out: list[int] = []
    for part in version.split("+")[0].split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group()))
        if m.end() != len(part):
            break
    return out


def classify(old: str, new: str) -> str:
    a, b = _numbers(old), _numbers(new)
    size = max(len(a), len(b), 1)
    a, b = a + [0] * (size - len(a)), b + [0] * (size - len(b))
    if a == b:
        return "other"                                                                                         # the same release numbers with a different pre-release, post-release or local label
    if b < a:
        return "downgrade"
    if a[0] >= 1000 and b[0] >= 1000:
        return "calendar"                                                                                      # dated releases (certifi 2025.10.5), where the first number is a year, not a breaking change
    first = next(i for i in range(size) if a[i] != b[i])
    return "major" if first == 0 else "minor" if first == 1 else "patch"


def diff_pins(old: dict, new: dict) -> dict:
    changed = [(new[k][0], old[k][1], new[k][1], classify(old[k][1], new[k][1])) for k in new if k in old and old[k][1] != new[k][1]]
    changed.sort(key=lambda row: (KINDS.index(row[3]), norm(row[0])))
    return {"changed": changed, "added": sorted((new[k] for k in new if k not in old), key=lambda p: norm(p[0])),
            "removed": sorted((old[k] for k in old if k not in new), key=lambda p: norm(p[0]))}


def summary_markdown(d: dict, tests: str, date: str) -> str:
    c, a, r = d["changed"], d["added"], d["removed"]
    lines = [f"## Monthly dependency refresh ({date})", "",
             f"{len(c)} package version(s) changed, {len(a)} added, {len(r)} removed. The whole suite passed against the new versions ({tests}).", ""]
    if c:
        flagged = sum(1 for row in c if row[3] in ("major", "downgrade"))
        lines += ["### What changed", "", "| Package | Was | Now | Change |", "|---|---|---|---|"]
        for name, was, now, kind in c[:MAX_ROWS]:
            lines.append(f"| {name} | {was} | {now} | {'**MAJOR**' if kind == 'major' else '**DOWNGRADE**' if kind == 'downgrade' else kind} |")
        if len(c) > MAX_ROWS:
            lines.append(f"| ... | | | {len(c) - MAX_ROWS} more rows (see the file diff) |")
        lines += ["", f"{flagged} row(s) are major bumps or downgrades: read those first." if flagged else "No major bumps or downgrades.", ""]
    if a:
        lines += ["### Added (new in the dependency tree)", ""] + [f"- {name} {version}" for name, version in a] + [""]
    if r:
        lines += ["### Removed (no longer needed)", ""] + [f"- {name} {version}" for name, version in r] + [""]
    lines += ["### What was checked (in the refresh run)", "",
              "- A brand-new Python 3.12 environment installed `pipeline/requirements.txt`, `app/requirements.txt` and pyflakes with no lock, and the versions it chose became this lock.",
              f"- The static check, both credential scans and the whole test suite ran against exactly these versions ({tests}), including the check that the installed versions equal the lock.",
              "- The new packages ran in a job with read-only access and no secrets. This pull request was written by a separate job that installs nothing and re-checked the file.", "",
              "### Before you merge", "",
              "- GitHub does not run the Tests workflow on a pull request opened by another workflow. The same checks already ran above, and Tests runs again on `main` when you merge. "
              "Closing and reopening this pull request makes GitHub run it now.",
              "- Merging changes only `constraints-ci.txt`. The daily job and the backup pick up the new versions on their next runs.",
              "- To hold a package back, add an upper bound in the requirements file (for example `pandas<3`) and run the Refresh lock workflow again."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- the test result
def summarize_log(text: str) -> str:
    """'553 passed, 2 skipped' from pytest's last line, or LockError when anything failed, errored or nothing passed."""
    last = None
    for line in text.splitlines():
        m = re.fullmatch(r"=*\s*(.*?\d+ (?:passed|failed|errors?|skipped|deselected|warnings?|xfailed|xpassed|no tests ran).*?)\s+in\s+[\d.]+s(?:\s+\(.*\))?\s*=*", line.strip())
        if m:
            last = m.group(1)
    if last is None:
        raise LockError("no pytest summary line found: the tests did not finish")
    if not re.fullmatch(r"\d+ [a-z]+(?:, \d+ [a-z]+)*", last):
        raise LockError(f"the pytest summary line has words this tool does not recognise: {last!r}")
    counts = {word: int(n) for n, word in re.findall(r"(\d+) ([a-z]+)", last)}
    bad = set(counts) - {"passed", "skipped", "warning", "warnings", "deselected", "xfailed", "xpassed"}
    if bad or counts.get("passed", 0) < 1:
        raise LockError(f"the tests did not all pass: {last!r}")
    return f"{counts['passed']} passed" + (f", {counts['skipped']} skipped" if counts.get("skipped") else "")


def check_tests(tests: str) -> str:
    if not TESTS_OK.fullmatch(tests or ""):
        raise LockError(f"the test result must look like '553 passed, 2 skipped', not {tests!r}")
    return tests


# ---------------------------------------------------------------------------------------------- the commands
def _output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def plan(freeze: str, current: str, out: str, date: str) -> int:
    new, old = parse_pins(open(freeze, encoding="utf-8").read(), "the fresh freeze"), parse_pins(open(current, encoding="utf-8").read())
    if len(new) < MIN_PINS:
        raise LockError(f"the fresh freeze has only {len(new)} packages; something went wrong with the install")
    if same_pins(new, old):
        print("No changes: the newest allowed versions are exactly the locked ones.")
        _output("changed", "false")
        return 0
    d = diff_pins(old, new)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(new, date, PENDING))
    print(f"Changes: {len(d['changed'])} changed, {len(d['added'])} added, {len(d['removed'])} removed.")
    _output("changed", "true")
    return 0


def publish(new_path: str, current: str, tests: str, date: str, out_lock: str, out_summary: str) -> int:
    check_tests(tests)
    check_valid_date(date)
    new, old = parse_pins(open(new_path, encoding="utf-8").read(), "the proposed lock"), parse_pins(open(current, encoding="utf-8").read())
    missing = [name for name in CORE if name not in new]
    if len(new) < MIN_PINS or missing:
        raise LockError(f"the proposed lock is not credible: {len(new)} packages, missing {missing}")
    if not 0.5 * len(old) <= len(new) <= 1.5 * len(old):
        raise LockError(f"the proposed lock has {len(new)} packages where the current one has {len(old)}: too different to trust")
    if same_pins(new, old):
        print("No changes.")
        _output("changed", "false")
        return 0
    with open(out_lock, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(new, date, tests))
    with open(out_summary, "w", encoding="utf-8", newline="\n") as f:
        f.write(summary_markdown(diff_pins(old, new), tests, date))
    print("Wrote the new lock and the pull request text.")
    _output("changed", "true")
    return 0


def pin_one(lock_text: str, spec: str, date: str, why: str) -> tuple[str, str]:
    """(the new lock text, a sentence saying what was done). Adds or moves ONE exact pin and keeps the file sorted and well-formed; a line is added to the header saying what was added and why,
    because the header otherwise claims a fully proven fresh environment. The next monthly refresh regenerates the whole file (and includes the package, once it is in a requirements file)."""
    m = PIN.fullmatch((spec or "").strip())
    if not m:
        raise LockError(f"give an exact pin such as six==1.17.0, not {(spec or '')[:40]!r}")
    name, version = m.groups()
    check_valid_date(date)
    note = " ".join(str(why or "").split())[:120]
    if not note:
        raise LockError("say why (--why): the header records the reason")
    pins = parse_pins(lock_text)
    key = norm(name)
    if key in pins and pins[key][1] == version:
        return lock_text, f"{pins[key][0]}=={version} is already pinned; nothing changed"
    verb = "moved to" if key in pins else "added as"
    pins[key] = (pins[key][0] if key in pins else name, version)
    crlf = "\r\n" in lock_text
    lines = lock_text.replace("\r\n", "\n").split("\n")
    top = []
    for line in lines:
        if line.startswith("#") or (not line.strip() and top):
            top.append(line)
        else:
            break
    while top and not top[-1].strip():
        top.pop()
    top.append(f"# Pinned by hand on {date}: {pins[key][0]}=={version} ({note}). The next monthly refresh regenerates this file, including it.")
    body = [f"{n}=={v}" for n, v in sorted(pins.values(), key=lambda p: norm(p[0]))]
    out = "\n".join(top + body) + "\n"
    return (out.replace("\n", "\r\n") if crlf else out), f"{pins[key][0]}=={version} {verb} a pin in the lock"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rebuild constraints-ci.txt safely.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--freeze", required=True), a.add_argument("--current", required=True), a.add_argument("--out", required=True), a.add_argument("--date", required=True)
    b = sub.add_parser("summarize-log")
    b.add_argument("log")
    b.add_argument("--github-output", action="store_true")
    c = sub.add_parser("publish")
    for opt in ("--new", "--current", "--tests", "--date", "--out-lock", "--out-summary"):
        c.add_argument(opt, required=True)
    d = sub.add_parser("pin", help="add or move ONE exact pin in the lock, recording why in its header")
    d.add_argument("spec", help="name==version, for example six==1.17.0")
    d.add_argument("--date", required=True), d.add_argument("--why", required=True), d.add_argument("--lock", default="constraints-ci.txt")
    args = p.parse_args(argv)
    try:
        if args.cmd == "pin":
            text = open(args.lock, encoding="utf-8", newline="").read()
            new, said = pin_one(text, args.spec, args.date, args.why)
            if new != text:
                open(args.lock, "w", encoding="utf-8", newline="").write(new)
            print(said)
            return 0
        if args.cmd == "plan":
            return plan(args.freeze, args.current, args.out, args.date)
        if args.cmd == "summarize-log":
            result = summarize_log(open(args.log, encoding="utf-8", errors="replace").read())
            print(result)
            if args.github_output:
                _output("tests", result)
            return 0
        return publish(args.new, args.current, args.tests, args.date, args.out_lock, args.out_summary)
    except (LockError, OSError) as exc:
        print(f"refresh_lock: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
