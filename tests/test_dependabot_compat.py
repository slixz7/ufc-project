"""
Dependabot proposes newer versions of the GitHub Actions the workflows use. The tests must therefore check the RULES (pinned to a major, consistent, matching pairs) and never a literal
version, or the first bump fails for a reason that has nothing to do with the new version. This happened once (checkout 4 to 7), and these tests keep it from happening again.

The artifact actions: GitHub's real constraint is that artifacts made by the old v3 actions cannot be read by v4 or newer. From v4 on the upload and the download share one artifact
service, so they need NOT be on the same major version (a grouped Dependabot bump can land them on different ones). An earlier version of these tests required them to be equal,
which was an over-generalisation of the v3-to-v4 break and made a valid pull request fail; the rule below is the real one.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml")}
LITERAL = re.compile(r"actions/[\w.-]+@v[0-9]")
ARTIFACT_SERVICE_FROM = 4                       # the first major whose upload and download share one artifact service


def artifact_problems(upload: int, download: int) -> list[str]:
    """What is wrong with this pair of majors for the artifact actions: only a version older than v4 is (it cannot exchange artifacts with the newer service)."""
    return [f"{name} is on v{v}, older than v{ARTIFACT_SERVICE_FROM}: it cannot exchange artifacts with the newer artifact service"
            for name, v in (("upload-artifact", upload), ("download-artifact", download)) if v < ARTIFACT_SERVICE_FROM]


def test_no_test_names_a_literal_action_version():
    offenders = {p.name: LITERAL.findall(p.read_text(encoding="utf-8")) for p in (ROOT / "tests").glob("test_*.py") if p.name != Path(__file__).name}
    assert {k: v for k, v in offenders.items() if v} == {}, "a test that names a version rejects every future bump of that action"


def test_every_action_in_every_workflow_is_pinned_to_a_major_version_and_each_action_has_one_version_everywhere():
    versions = {}
    for name, text in WORKFLOWS.items():
        for action, major in re.findall(r"uses: ([\w.-]+/[\w.-]+)@v(\d+)\b", text):
            versions.setdefault(action, set()).add(major)
        assert not re.search(r"uses: [\w.-]+/[\w.-]+@(?!v\d+\b)\S+", text), f"{name} uses an action that is not pinned to a major version tag"
    assert versions and {a: v for a, v in versions.items() if len(v) > 1} == {}, "a Dependabot pull request moves an action in every workflow at once, so they must agree"
    up, down = (int(next(iter(versions[f"actions/{a}-artifact"]))) for a in ("upload", "download"))
    assert artifact_problems(up, down) == []                                                                                    # neither is older than v4; they need not be equal


def test_dependabot_moves_the_two_artifact_actions_in_one_pull_request_and_says_what_is_true_about_the_python_packages():
    cfg = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert 'package-ecosystem: "github-actions"' in cfg and "groups:\n      artifacts:" in cfg and '- "actions/upload-artifact"' in cfg and '- "actions/download-artifact"' in cfg
    assert "refreshed monthly by .github/workflows/refresh-lock.yml" in cfg and "refreshed by hand" not in cfg
    assert "must stay on the same major" not in cfg and "need not be on the same major" in cfg


def test_the_runbook_says_what_to_do_with_one():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## Dependabot pull requests (the versions of GitHub Actions)", "@dependabot rebase", "ONE pull request", "tests/test_dependabot_compat.py", "Run workflow", "dry run",
                   "both on v4 or newer"):
        assert needle in runbook, needle
    assert "versions must match" not in runbook and "on the same major" not in runbook


import pytest  # noqa: E402


@pytest.mark.parametrize("up, down", [(4, 4), (5, 5), (5, 6), (6, 5), (4, 7), (9, 9)])
def test_artifact_actions_on_v4_or_newer_are_fine_whether_or_not_their_majors_are_equal(up, down):
    assert artifact_problems(up, down) == []                                                                                    # (5, 6) is what a grouped Dependabot bump can produce


@pytest.mark.parametrize("up, down, which", [(3, 4, "upload-artifact"), (4, 3, "download-artifact"), (2, 9, "upload-artifact")])
def test_an_artifact_action_older_than_v4_is_refused(up, down, which):
    problems = artifact_problems(up, down)
    assert len(problems) == 1 and problems[0].startswith(f"{which} is on v") and "cannot exchange artifacts" in problems[0]
    assert len(artifact_problems(3, 3)) == 2
