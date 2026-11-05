"""
Dependabot proposes newer versions of the GitHub Actions the workflows use. The tests must therefore check the RULES (pinned to a major, consistent, matching pairs) and never a literal
version, or the first bump fails for a reason that has nothing to do with the new version. This happened once (checkout 4 to 7), and these tests keep it from happening again.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml")}
LITERAL = re.compile(r"actions/[\w.-]+@v[0-9]")


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
    assert versions["actions/upload-artifact"] == versions["actions/download-artifact"]                                       # the pair must stay on the same major


def test_dependabot_moves_the_two_artifact_actions_in_one_pull_request_and_says_what_is_true_about_the_python_packages():
    cfg = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert 'package-ecosystem: "github-actions"' in cfg and "groups:\n      artifacts:" in cfg and '- "actions/upload-artifact"' in cfg and '- "actions/download-artifact"' in cfg
    assert "refreshed monthly by .github/workflows/refresh-lock.yml" in cfg and "refreshed by hand" not in cfg


def test_the_runbook_says_what_to_do_with_one():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## Dependabot pull requests (the versions of GitHub Actions)", "@dependabot rebase", "ONE pull request", "tests/test_dependabot_compat.py", "Run workflow", "dry run"):
        assert needle in runbook, needle
