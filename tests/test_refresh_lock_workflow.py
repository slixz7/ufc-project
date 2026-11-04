"""
.github/workflows/refresh-lock.yml: the monthly lock refresh. Its design rests on a few properties that must not erode quietly:
  * the job that installs and runs brand-new, unreviewed packages has read-only access, no secrets and no stored token;
  * the job that can write installs nothing and only runs this repository's own code;
  * nothing is spliced into a shell script from an expression (a classic injection route);
  * a failure is announced; the old lock stays in force; every action is pinned to a major version.
The file was also checked with actionlint (the standard static checker for workflows).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "refresh-lock.yml").read_text(encoding="utf-8")
TESTS_YML = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
SECRET_EXPRESSION = re.compile(r"\$\{\{\s*secrets\.")


def jobs() -> dict:
    body = TEXT[TEXT.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):\s*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def code_lines(block: str):
    return [l for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]


def test_it_runs_monthly_and_on_demand_and_on_nothing_else():
    head = TEXT[:TEXT.index("\njobs:\n")]
    assert re.search(r'^on:\n  schedule:\n    - cron: "\d{1,2} \d{1,2} 1 \* \*"', head, flags=re.M)                       # day 1 of every month
    assert re.search(r"^  workflow_dispatch:\s*$", head, flags=re.M)
    for trigger in ("push:", "pull_request", "workflow_run", "issue_comment", "repository_dispatch"):
        assert trigger not in "\n".join(code_lines(head)), trigger                                                         # nothing a stranger could start
    assert re.search(r"^permissions: \{\}", head, flags=re.M)                                                              # no permissions unless a job asks
    assert re.search(r"concurrency:\n  group: refresh-lock\n  cancel-in-progress: false", head)
    assert TEXT.startswith("name: Refresh lock\n")


def test_there_are_exactly_three_jobs_and_each_has_a_time_limit():
    j = jobs()
    assert list(j) == ["refresh", "open-pr", "alert"]
    for name, block in j.items():
        assert re.search(r"^    timeout-minutes: \d+$", block, flags=re.M), name


def test_the_job_that_runs_new_packages_is_read_only_has_no_secrets_and_leaves_no_token_behind():
    block = jobs()["refresh"]
    assert re.search(r"^    permissions:\n      contents: read\n", block, flags=re.M)
    assert not re.search(r"^\s+[a-z-]+: write\s*$", block, flags=re.M)                                                      # no write permission of any kind
    assert "persist-credentials: false" in block                                                                           # no token stored in .git/config for a package to find
    assert not SECRET_EXPRESSION.search(block)                                                                             # no ${{ secrets.X }} anywhere in this job
    for forbidden in ("github.token", "GH_TOKEN", "GITHUB_TOKEN", "gh pr", "git push", "gh api"):
        assert forbidden not in block, forbidden
    assert 'python-version: "3.12"' in block and 'python-version: "3.12"' in TESTS_YML                                      # the same Python as the workflows the lock serves


def test_it_installs_the_newest_versions_on_purpose_without_the_lock_and_only_from_the_requirements_files():
    installs = [l.strip() for l in code_lines(jobs()["refresh"]) if "pip install" in l]
    assert installs == [".refresh-venv/bin/pip install -r pipeline/requirements.txt -r app/requirements.txt pyflakes"]    # one resolution, no -c: the point is to see what is newest
    assert "python -m venv .refresh-venv" in TEXT and '>> "$GITHUB_PATH"' in TEXT and "pip freeze > freeze.txt" in TEXT


def test_it_runs_everything_the_tests_workflow_runs_against_the_new_versions_before_anything_is_proposed():
    block = jobs()["refresh"]
    assert "services:\n      postgres:" in block and "image: postgres:16" in block and "TEST_PG_ADMIN_URL" in block
    assert "python -m pyflakes app pipeline tests" in block
    assert "python tools/scan_secrets.py\n" in block and "python tools/scan_secrets.py --history" in block and "fetch-depth: 0" in block
    assert 'UFC_EXPECT_LOCKED: "1"' in block and "set -o pipefail" in block and "python -m pytest -q tests 2>&1 | tee pytest.log" in block
    assert "refresh_lock.py summarize-log pytest.log --github-output" in block                                             # fails unless the last line is a clean pass
    for step in ("Static check", "Look for credentials", "Offline tests", "Read the test result", "Hand the proposed lock"):
        after = block[block.index(step):]
        assert "if: steps.plan.outputs.changed == 'true'" in after.split("\n      - ")[0], step                           # nothing runs, and nothing is handed on, unless something changed
    assert "name: new-lock" in block and "path: constraints-ci.txt" in block and "if-no-files-found: error" in block


def test_the_plan_runs_before_the_tests_so_they_check_the_new_file_and_the_outputs_are_wired_through():
    block = jobs()["refresh"]
    assert block.index("id: plan") < block.index("Static check") < block.index("Offline tests") < block.index("id: result") < block.index("upload-artifact")
    assert "changed: ${{ steps.plan.outputs.changed }}" in block and "tests: ${{ steps.result.outputs.tests }}" in block
    assert "refresh_lock.py plan --freeze freeze.txt --current constraints-ci.txt --out constraints-ci.txt" in block


def test_the_job_that_can_write_installs_nothing_and_runs_only_this_repositorys_own_code():
    block = jobs()["open-pr"]
    assert "needs: refresh" in block and "if: needs.refresh.outputs.changed == 'true'" in block
    assert re.search(r"^    permissions:\n      contents: write\n      pull-requests: write\n    steps:", block, flags=re.M)
    assert not SECRET_EXPRESSION.search(block)
    for forbidden in ("pip install", "pip3", "setup-python", "npm ", "venv", "apt-get", "curl", "wget"):
        assert forbidden not in block, forbidden
    assert "python3 tools/refresh_lock.py publish" in block                                                                # the repository's own script, standard library only
    assert block.count("uses: ") == 2 and "actions/checkout@v4" in block and "actions/download-artifact@v4" in block      # nothing but checkout and the artifact


def test_the_publishing_job_treats_the_artifact_as_data_and_proves_only_one_file_changed():
    block = jobs()["open-pr"]
    assert "path: ${{ runner.temp }}/proposed" in block                                                                    # the artifact lands outside the repository, never over its files
    assert '--new "$RUNNER_TEMP/proposed/constraints-ci.txt" --current constraints-ci.txt' in block
    assert '--out-lock constraints-ci.txt --out-summary "$RUNNER_TEMP/pr-body.md"' in block
    assert 'if [ "$changes" != " M constraints-ci.txt" ]; then' in block and "exit 1" in block                             # exactly one modified file, or it stops
    assert "git push --force origin lock-refresh" in block and block.count("git push") == 1 and "origin main" not in block   # only ever its own branch
    assert "gh pr list --head lock-refresh --state open" in block and "gh pr edit" in block and "gh pr create --base main --head lock-refresh" in block
    assert "git add constraints-ci.txt" in block and "git add ." not in block and "git add -A" not in block


def test_no_expression_is_ever_spliced_into_a_shell_script():
    """Values reach scripts through env: and are read as "$NAME": the safe way. An expression inside a script is how workflow injection happens."""
    lines = TEXT.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[i])
        if m:
            indent, rest = len(m.group(1)), m.group(2)
            script = [rest] if rest and rest not in ("|", ">", "|-") else []
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent + (2 if lines[i].lstrip().startswith("- ") else 0)):
                script.append(lines[j])
                j += 1
            assert not any("${{" in s for s in script), f"an expression inside a script near line {i + 1}: {script}"
            i = j
        else:
            i += 1
    assert 'TESTS: ${{ needs.refresh.outputs.tests }}' in TEXT and '--tests "$TESTS"' in TEXT                              # the test result arrives as a variable
    assert "echo ${{" not in TEXT and "echo \"${{" not in TEXT


def test_a_failure_is_announced_through_the_same_private_channel_as_the_other_workflows():
    block = jobs()["alert"]
    assert "needs: [refresh, open-pr]" in block and "if: failure()" in block
    assert re.search(r"^    permissions:\n      contents: read\n", block, flags=re.M)
    assert 'run: python3 pipeline/notify_failure.py "Refresh lock"' in block and "ALERT_WEBHOOK_URL: ${{ secrets.ALERT_WEBHOOK_URL }}" in block
    assert len(SECRET_EXPRESSION.findall(TEXT)) == 1 and SECRET_EXPRESSION.search(block)                                   # the only secret in the whole file, in the only job that does nothing else
    assert "pip install" not in block


def test_every_action_is_pinned_to_a_major_version_and_matches_the_tests_workflow():
    uses = re.findall(r"uses: ([^\s#]+)", TEXT)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", u) for u in uses), uses
    for action in ("actions/checkout", "actions/setup-python"):
        assert re.search(rf"{action}@v\d+", TEXT).group(0) == re.search(rf"{action}@v\d+", TESTS_YML).group(0)           # the same major versions the rest of the project uses
    assert "actions/upload-artifact@v4" in TEXT and "actions/download-artifact@v4" in TEXT                                 # a matching pair: v4 artifacts only work with v4


def test_the_other_workflows_still_install_through_the_lock_and_this_one_is_the_only_deliberate_exception():
    for other in ("tests.yml", "auto-update.yml", "backup.yml"):
        text = (ROOT / ".github" / "workflows" / other).read_text(encoding="utf-8")
        assert all("-c constraints-ci.txt" in l for l in text.splitlines() if "pip install" in l and not l.strip().startswith("#")), other
    unlocked = [p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")
                if any("pip install" in l and "-c constraints-ci.txt" not in l and not l.strip().startswith("#") for l in p.read_text(encoding="utf-8").splitlines())]
    assert unlocked == ["refresh-lock.yml"], unlocked


def test_when_pyyaml_is_available_the_file_parses_and_the_job_graph_is_what_the_comments_say():
    yaml = pytest.importorskip("yaml")
    d = yaml.safe_load(TEXT)
    triggers = d.get("on", d.get(True))                                                                                    # YAML 1.1 reads a bare `on` as true
    assert set(triggers) == {"schedule", "workflow_dispatch"} and d["permissions"] == {}
    j = d["jobs"]
    assert j["open-pr"]["needs"] == "refresh" and j["alert"]["needs"] == ["refresh", "open-pr"]
    assert j["refresh"]["permissions"] == {"contents": "read"} and j["open-pr"]["permissions"] == {"contents": "write", "pull-requests": "write"}
    ids = {s["id"] for s in j["refresh"]["steps"] if "id" in s}
    assert ids == {"plan", "result"} and set(j["refresh"]["outputs"]) == {"changed", "tests"}


def test_the_runbook_and_the_readme_describe_it_and_the_one_setting_it_needs():
    runbook, readme = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8"), (ROOT / "README.md").read_text(encoding="utf-8")
    assert ".github/workflows/refresh-lock.yml" in runbook and "tools/refresh_lock.py" in runbook and "Allow GitHub Actions to create and approve pull requests" in runbook
    assert "tests/test_refresh_lock.py" in runbook and "tests/test_refresh_lock_workflow.py" in runbook
    assert "ask for a new lock" not in runbook                                                                              # the old, manual instruction is gone
    assert "refreshed automatically" in readme and "monthly" in readme
