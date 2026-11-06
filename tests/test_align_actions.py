"""
tools/align_actions.py and its use by tools/apply_update.py. The bug it prevents is real: an update zip carried a workflow file with the action versions it was written with, and applying it
undid what Dependabot had done to that file (upload-artifact v7 in one workflow, v4 in another), which a test then refused.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import align_actions as aa  # noqa: E402
import apply_update as au  # noqa: E402

NOW = None


def wf(*uses, extra=""):
    steps = "".join(f"      - uses: {u}\n" for u in uses)
    return f"name: X\non: [push]\njobs:\n  j:\n    runs-on: ubuntu-24.04\n    steps:\n{steps}{extra}"


def repo(tmp_path, files):
    root = tmp_path / "proj"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return root


def read(root, rel):
    return (root / rel).read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------------------------- reading versions
def test_it_reads_only_pins_to_a_major_version_including_actions_in_a_subfolder():
    text = ("steps:\n  - uses: actions/checkout@v4\n  - uses: actions/setup-python@v5   # a comment\n    uses: github/codeql-action/init@v3\n"
            "  - uses: some/branch-pinned@main\n  - uses: some/sha-pinned@0123456789abcdef0123456789abcdef01234567\n  - uses: ./.github/actions/local\n  - uses: docker://alpine:3\n"
            "  # uses: actions/commented@v9\n  - run: echo uses actions/not-a-use@v8\n")
    assert aa.majors(text) == [("actions/checkout", 4), ("actions/setup-python", 5), ("github/codeql-action", 3)]


def test_the_most_common_major_wins_a_tie_goes_to_the_highest_and_each_action_is_decided_alone():
    assert aa.consensus([wf("actions/checkout@v4", "actions/setup-python@v5"), wf("actions/checkout@v4"), wf("actions/checkout@v7")]) == {"actions/checkout": 4, "actions/setup-python": 5}
    assert aa.consensus([wf("actions/upload-artifact@v4"), wf("actions/upload-artifact@v7")]) == {"actions/upload-artifact": 7}              # tie: Dependabot only moves up
    assert aa.consensus([wf("a/b/c@v1"), wf("a/b/d@v2"), wf("a/b/e@v2")]) == {"a/b": 2}                                                       # folders of one repository count as that repository
    assert aa.consensus([]) == {} and aa.consensus(["no workflow here"]) == {}


# ---------------------------------------------------------------------------------------------- changing them
def test_only_the_version_changes_comments_indentation_and_line_endings_stay():
    text = "steps:\r\n  - uses: actions/checkout@v4   # keep this\r\n    with:\r\n      fetch-depth: 0\r\n  - uses: actions/upload-artifact@v4\r\n  - uses: x/branch@main\r\n"
    new, changes = aa.align_text(text, {"actions/checkout": 7, "actions/upload-artifact": 4, "x/branch": 9})
    assert new == "steps:\r\n  - uses: actions/checkout@v7   # keep this\r\n    with:\r\n      fetch-depth: 0\r\n  - uses: actions/upload-artifact@v4\r\n  - uses: x/branch@main\r\n"
    assert changes == [("actions/checkout", 4, 7)]
    assert aa.align_text(new, {"actions/checkout": 7}) == (new, [])                                                                              # idempotent


def test_bytes_that_are_not_text_are_returned_untouched():
    blob = b"\xff\xfe\x00uses: actions/checkout@v4"
    assert aa.align_bytes(blob, {"actions/checkout": 7}) == (blob, [])
    same = wf("actions/checkout@v7").encode()
    assert aa.align_bytes(same, {"actions/checkout": 7}) == (same, [])


def test_the_reference_is_the_other_workflow_files_not_the_ones_being_replaced(tmp_path):
    root = repo(tmp_path, {".github/workflows/a.yml": wf("actions/checkout@v7"), ".github/workflows/b.yml": wf("actions/checkout@v7"), ".github/workflows/old.yml": wf("actions/checkout@v3"),
                           ".github/workflows/readme.txt": "uses: actions/checkout@v1\n"})
    assert aa.reference_majors(root, exclude=[".github/workflows/old.yml"]) == {"actions/checkout": 7}
    assert aa.reference_majors(root) == {"actions/checkout": 7}                                                                                  # 2 on v7 beat 1 on v3; the .txt is not a workflow
    assert aa.reference_majors(root, exclude=[".github/workflows/a.yml", ".github/workflows/b.yml", ".github/workflows/old.yml"]) == {}
    assert aa.reference_majors(tmp_path / "nowhere") == {}


# ---------------------------------------------------------------------------------------------- the command, on the state that really happened
def todays_mess(tmp_path):
    return repo(tmp_path, {
        ".github/workflows/tests.yml": wf("actions/checkout@v4", "actions/setup-python@v5"),
        ".github/workflows/refresh-lock.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v7", "actions/download-artifact@v8"),
        ".github/workflows/smoke-test.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v4"),                   # overwritten by an update zip
    })


def test_the_command_repairs_the_exact_state_the_failed_test_reported(tmp_path):
    root = todays_mess(tmp_path)
    lines = []
    assert aa.main([], root=root, out=lines.append) == 0
    assert lines[0] == "ALIGNED  .github/workflows/smoke-test.yml: actions/upload-artifact v4 -> v7" and lines[-1] == "Done. Now run: python -m pytest -q tests" and len(lines) == 2
    assert "upload-artifact@v7" in read(root, ".github/workflows/smoke-test.yml") and "checkout@v4" in read(root, ".github/workflows/smoke-test.yml")
    assert "download-artifact@v8" in read(root, ".github/workflows/refresh-lock.yml") and "upload-artifact@v7" in read(root, ".github/workflows/refresh-lock.yml")        # nothing else moved
    lines.clear()
    assert aa.main([], root=root, out=lines.append) == 0 and lines == ["The workflow files already agree on one version of each action."]


def test_check_mode_reports_and_changes_nothing_and_says_what_to_run(tmp_path):
    root = todays_mess(tmp_path)
    before = read(root, ".github/workflows/smoke-test.yml")
    lines = []
    assert aa.main(["--check"], root=root, out=lines.append) == 1
    assert lines == ["DIFFERS  .github/workflows/smoke-test.yml: actions/upload-artifact v4 -> v7", "They do not agree. Run: python tools/align_actions.py"]
    assert read(root, ".github/workflows/smoke-test.yml") == before
    aa.main([], root=root, out=lambda m: None)
    assert aa.main(["--check"], root=root, out=lambda m: None) == 0


def test_naming_files_fixes_only_those_to_match_the_others_and_unknown_files_are_refused(tmp_path):
    root = todays_mess(tmp_path)
    lines = []
    assert aa.main([".github/workflows/smoke-test.yml"], root=root, out=lines.append) == 0
    assert lines[0].startswith("ALIGNED  .github/workflows/smoke-test.yml: actions/upload-artifact v4 -> v7")
    assert aa.main([".github/workflows/smoke-test.yml"], root=root, out=lines.append) == 0 and lines[-1] == "Those workflow files already match the rest."
    assert aa.main([".github/workflows/nope.yml"], root=root, out=lines.append) == 2 and lines[-1].startswith("Not found:")
    assert aa.main([], root=tmp_path / "empty", out=lines.append) == 0 and lines[-1] == "No workflow files found in .github/workflows/."


def test_named_files_match_the_OTHER_files_even_when_the_named_ones_are_the_majority(tmp_path):
    root = repo(tmp_path, {".github/workflows/a.yml": wf("actions/checkout@v7"), ".github/workflows/b.yml": wf("actions/checkout@v4"), ".github/workflows/c.yml": wf("actions/checkout@v4")})
    assert aa.main([".github/workflows/b.yml", ".github/workflows/c.yml"], root=root, out=lambda m: None) == 0
    assert "checkout@v7" in read(root, ".github/workflows/b.yml") and "checkout@v7" in read(root, ".github/workflows/c.yml")                       # two named files on v4 do not outvote the one other file


def test_windows_line_endings_in_the_files_survive_the_command(tmp_path):
    crlf = lambda s: s.replace("\n", "\r\n")                                                                                                       # noqa: E731
    root = repo(tmp_path, {".github/workflows/a.yml": crlf(wf("actions/checkout@v7")), ".github/workflows/b.yml": crlf(wf("actions/checkout@v7")), ".github/workflows/c.yml": crlf(wf("actions/checkout@v4"))})
    aa.main([], root=root, out=lambda m: None)
    assert read(root, ".github/workflows/c.yml") == crlf(wf("actions/checkout@v7"))


# ---------------------------------------------------------------------------------------------- the update tool uses it
def make_zip(tmp_path, files, name="ufc-update-20261030-9.zip"):
    z = tmp_path / name
    return z, au.make_update_zip({rel: text.encode("utf-8") for rel, text in files.items()}, z, note="t")


def test_a_workflow_in_a_zip_adopts_the_versions_the_repository_already_uses_and_the_checksum_is_still_the_zips(tmp_path):
    root = todays_mess(tmp_path)
    (root / ".github/workflows/smoke-test.yml").unlink()
    z, sha = make_zip(tmp_path, {".github/workflows/smoke-test.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v4"), "docs/RUNBOOK.md": "uses: actions/checkout@v4\n"})
    r = au.apply(z, root, expected_sha256=sha)
    assert r["aligned"] == [(".github/workflows/smoke-test.yml", "actions/upload-artifact", 4, 7)]
    assert "upload-artifact@v7" in read(root, ".github/workflows/smoke-test.yml")
    assert read(root, "docs/RUNBOOK.md") == "uses: actions/checkout@v4\n"                                                                        # only workflow files are touched
    with pytest.raises(au.UpdateError):
        au.apply(z, root, expected_sha256="0" * 64)                                                                                              # the checksum is of the zip as made


def test_applying_it_twice_changes_nothing_and_reports_no_alignment_the_second_time(tmp_path):
    root = todays_mess(tmp_path)
    z, sha = make_zip(tmp_path, {".github/workflows/smoke-test.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v4")})
    first = au.apply(z, root, expected_sha256=sha)
    assert first["aligned"] and first["replaced"] == [".github/workflows/smoke-test.yml"] or first["unchanged"] == []
    second = au.apply(z, root, expected_sha256=sha)
    assert second["unchanged"] == [".github/workflows/smoke-test.yml"] and second["replaced"] == [] and second["aligned"] == []


def test_a_dry_run_says_it_would_align_and_writes_nothing(tmp_path, capsys):
    root = todays_mess(tmp_path)
    before = read(root, ".github/workflows/smoke-test.yml")
    z, sha = make_zip(tmp_path, {".github/workflows/smoke-test.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v3")})
    assert au.main([str(z), "--sha256", sha, "--dry-run"], root=root) == 0
    out = capsys.readouterr().out
    assert "WOULD ALIGN .github/workflows/smoke-test.yml: actions/upload-artifact v3 -> v7 (to match the rest of the repository)" in out and "Dry run: nothing was written." in out
    assert read(root, ".github/workflows/smoke-test.yml") == before


def test_the_command_prints_each_alignment_after_the_placed_files(tmp_path, capsys):
    root = todays_mess(tmp_path)
    z, sha = make_zip(tmp_path, {".github/workflows/smoke-test.yml": wf("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v4")})
    assert au.main([str(z), "--sha256", sha], root=root) == 0
    out = capsys.readouterr().out.splitlines()
    placed = [i for i, l in enumerate(out) if l.startswith("PLACED")]
    aligned = [i for i, l in enumerate(out) if l.startswith("ALIGNED")]
    assert placed and aligned and aligned[0] > placed[-1] and "actions/upload-artifact v4 -> v7" in out[aligned[0]]


def test_when_every_workflow_comes_in_the_zip_there_is_nothing_to_match_and_nothing_changes(tmp_path):
    root = repo(tmp_path, {".github/workflows/a.yml": wf("actions/checkout@v7")})
    z, sha = make_zip(tmp_path, {".github/workflows/a.yml": wf("actions/checkout@v4")})
    r = au.apply(z, root, expected_sha256=sha)
    assert r["aligned"] == [] and "checkout@v4" in read(root, ".github/workflows/a.yml")                                                       # a lone workflow is nobody's to correct


def test_pins_to_a_branch_and_files_that_are_not_workflows_are_never_rewritten(tmp_path):
    root = todays_mess(tmp_path)
    body = wf("actions/checkout@main", "actions/upload-artifact@v4")
    z, sha = make_zip(tmp_path, {".github/workflows/new-one.yml": body, ".github/dependabot.yml": "uses: actions/upload-artifact@v4\n"})
    au.apply(z, root, expected_sha256=sha)
    assert "checkout@main" in read(root, ".github/workflows/new-one.yml") and "upload-artifact@v7" in read(root, ".github/workflows/new-one.yml")
    assert read(root, ".github/dependabot.yml") == "uses: actions/upload-artifact@v4\n"


def test_the_tool_needs_only_the_standard_library():
    import ast
    tree = ast.parse((ROOT / "tools" / "align_actions.py").read_text(encoding="utf-8"))
    top = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert top <= {"__future__", "argparse", "re", "sys", "collections", "pathlib"}, top
