"""
The workflows that carry the operations checks: the live site check (new), and the heartbeat, size-limit and token-expiry steps added to the daily job and the weekly backup. The design
rules are the ones refresh-lock.yml has: nothing is spliced into a shell script, the job that runs a browser has no secrets, the alert is its own job, every action is pinned.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
SMOKE = (WF / "smoke-test.yml").read_text(encoding="utf-8")
DAILY = (WF / "auto-update.yml").read_text(encoding="utf-8")
BACKUP = (WF / "backup.yml").read_text(encoding="utf-8")
SECRET = re.compile(r"\$\{\{\s*secrets\.")


def jobs(text):
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):\s*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def step(text, name_start):
    """The step whose name starts with this, up to the next step."""
    i = text.index(f"- name: {name_start}")
    j = text.find("\n      - ", i + 5)
    return text[i:j if j != -1 else len(text)]


def scripts(text):
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[i])
        if m:
            indent, rest = len(m.group(1)), m.group(2)
            block = [rest] if rest and rest not in ("|", ">", "|-") else []
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent + (2 if lines[i].lstrip().startswith("- ") else 0)):
                block.append(lines[j])
                j += 1
            out.append("\n".join(block))
            i = j
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------------------------- the live site check
def test_the_live_site_check_runs_daily_on_demand_and_after_a_passing_push_to_main_and_on_nothing_a_stranger_could_start():
    head = SMOKE[:SMOKE.index("\njobs:\n")]
    code = "\n".join(l for l in head.splitlines() if l.strip() and not l.strip().startswith("#"))
    assert 'cron: "20 15 * * *"' in code and "workflow_dispatch:" in code and 'workflows: ["Tests"]' in code and "types: [completed]" in code and "branches: [main]" in code
    for trigger in ("pull_request", "push:", "issue_comment", "repository_dispatch"):
        assert trigger not in code, trigger
    assert SMOKE.startswith("name: Live site check\n") and (WF / "tests.yml").read_text(encoding="utf-8").startswith("name: Tests\n")      # the name it listens for exists
    assert re.search(r"^permissions: \{\}", head, flags=re.M) and "group: live-site-check" in head and "cancel-in-progress: false" in head


def test_the_daily_time_is_an_hour_after_the_daily_job():
    daily_minute, daily_hour = re.search(r'cron: "(\d+) (\d+) \* \* \*"', DAILY).groups()
    smoke_minute, smoke_hour = re.search(r'cron: "(\d+) (\d+) \* \* \*"', SMOKE).groups()
    assert (int(smoke_hour) * 60 + int(smoke_minute)) - (int(daily_hour) * 60 + int(daily_minute)) == 63


def test_the_browser_job_is_read_only_has_no_secrets_leaves_no_token_and_only_reads_the_site():
    check = jobs(SMOKE)["check"]
    assert re.search(r"^    permissions:\n      contents: read\n", check, flags=re.M) and not re.search(r"^\s+[a-z-]+: write\s*$", check, flags=re.M)
    assert not SECRET.search(check) and "GH_TOKEN" not in check and "github.token" not in check and "persist-credentials: false" in check
    assert "timeout-minutes: 20" in check
    assert "if: ${{ github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success' }}" in check                            # a failed Tests run is not a reason to check the site
    assert "pip install -r tools/smoke-requirements.txt" in check and "python -m playwright install --with-deps chromium" in check and check.count("pip install") == 1
    assert 'python tools/smoke_test.py "$SITE_URL" --out smoke_out' in check and "SITE_URL: ${{ vars.SITE_URL || 'https://ufc-predictor.streamlit.app/' }}" in check


def test_it_waits_for_the_host_only_after_a_push_and_keeps_a_screenshot_only_when_it_fails():
    check = jobs(SMOKE)["check"]
    wait = step(check, "Give the host time to redeploy")
    assert "if: ${{ github.event_name == 'workflow_run' }}" in wait and "run: sleep 240" in wait
    keep = step(check, "Keep the screenshots of a failing page")
    assert "if: failure()" in keep and re.search(r"actions/upload-artifact@v\d+", keep) and "name: smoke-failure" in keep and "retention-days: 7" in keep and "path: smoke_out" in keep


def test_the_alert_is_its_own_job_with_the_only_secret_in_the_file():
    alert = jobs(SMOKE)["alert"]
    assert "needs: check" in alert and "if: failure()" in alert and re.search(r"^    permissions:\n      contents: read\n", alert, flags=re.M)
    assert 'run: python3 pipeline/notify_failure.py "Live site check"' in alert and len(SECRET.findall(SMOKE)) == 1 and SECRET.search(alert)
    assert list(jobs(SMOKE)) == ["check", "alert"]


def test_nothing_is_spliced_into_a_script_and_every_action_is_pinned_to_a_major_version():
    for text in (SMOKE, DAILY, BACKUP):
        assert not any("${{" in s for s in scripts(text))
    uses = re.findall(r"uses: ([^\s#]+)", SMOKE)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", u) for u in uses)
    tests = (WF / "tests.yml").read_text(encoding="utf-8")
    for action in ("actions/checkout", "actions/setup-python"):
        assert re.search(rf"{action}@v\d+", SMOKE).group(0) == re.search(rf"{action}@v\d+", tests).group(0)


# ---------------------------------------------------------------------------------------------- the daily job
def test_the_daily_job_pings_a_heartbeat_on_success_and_on_failure_but_not_on_a_dry_run():
    ok, bad = step(DAILY, "Heartbeat, the run succeeded"), step(DAILY, "Heartbeat, the run failed")
    assert "if: ${{ success() && (github.event_name == 'schedule' || inputs.dry_run != true) }}" in ok and "run: python pipeline/heartbeat.py\n" in ok + "\n"
    assert "if: ${{ failure() && (github.event_name == 'schedule' || inputs.dry_run != true) }}" in bad and "run: python pipeline/heartbeat.py fail" in bad
    for s in (ok, bad):
        assert "HEARTBEAT_URL: ${{ secrets.HEARTBEAT_URL }}" in s
    assert DAILY.index("Publish the pick log") < DAILY.index("Heartbeat, the run succeeded") < DAILY.index("Heartbeat, the run failed") < DAILY.index("Tell the alert channel that the run failed")
    assert DAILY.rstrip().splitlines()[-1].strip() == 'run: python pipeline/notify_failure.py "Auto update"'                                                     # the failure alert stays the very last step


def test_the_daily_job_hands_the_watchdog_the_size_limit_variable_and_still_alerts_on_failure():
    upkeep = step(DAILY, "Run the upkeep")
    assert "DB_SIZE_LIMIT_MB: ${{ vars.DB_SIZE_LIMIT_MB }}" in upkeep and "run: python pipeline/auto_update.py all" in upkeep
    assert "if: failure()" in step(DAILY, "Tell the alert channel that the run failed")


# ---------------------------------------------------------------------------------------------- the weekly backup
def test_the_backup_checks_its_token_without_ever_failing_and_before_the_failure_alert():
    s = step(BACKUP, "Warn if the backup token is about to expire")
    assert "if: always()" in s and "continue-on-error: true" in s
    assert "BACKUP_REPO_TOKEN: ${{ secrets.BACKUP_REPO_TOKEN }}" in s and "ALERT_WEBHOOK_URL: ${{ secrets.ALERT_WEBHOOK_URL }}" in s and "BACKUP_REPO: ${{ github.repository_owner }}/ufc-project-backups" in s
    assert 'run: python pipeline/token_expiry.py --repo "$BACKUP_REPO"' in s
    assert BACKUP.index("Warn if the backup token") < BACKUP.index("Heartbeat, the backup succeeded") < BACKUP.index("Heartbeat, the backup failed") < BACKUP.index("Tell the alert channel that the backup failed")
    assert BACKUP.rstrip().splitlines()[-1].strip() == 'run: python pipeline/notify_failure.py "Backup"'                                                          # the failure alert stays the very last step


def test_the_backup_has_its_own_heartbeat_address_so_a_missing_weekly_backup_is_noticed():
    ok, bad = step(BACKUP, "Heartbeat, the backup succeeded"), step(BACKUP, "Heartbeat, the backup failed")
    assert "if: success()" in ok and "run: python pipeline/heartbeat.py --env BACKUP_HEARTBEAT_URL" in ok and "BACKUP_HEARTBEAT_URL: ${{ secrets.BACKUP_HEARTBEAT_URL }}" in ok
    assert "if: failure()" in bad and "run: python pipeline/heartbeat.py fail --env BACKUP_HEARTBEAT_URL" in bad and "BACKUP_HEARTBEAT_URL: ${{ secrets.BACKUP_HEARTBEAT_URL }}" in bad


def test_every_script_the_workflows_run_exists():
    for text in (SMOKE, DAILY, BACKUP):
        for path in re.findall(r"python3? ((?:pipeline|tools)/[\w.-]+\.py)", text):
            assert (ROOT / path).exists(), path


def test_the_documentation_describes_each_check_and_what_to_set():
    runbook, readme = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8"), (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("## Watching for silent failures", "**The live site check**", "**The watchdog**", "**The heartbeat**", "**The backup token's expiry**", "HEARTBEAT_URL", "BACKUP_HEARTBEAT_URL",
                   "DB_SIZE_LIMIT_MB", "SITE_URL", "github-authentication-token-expiration", "smoke-failure", "tests/test_smoke_checks.py", "tests/test_operations_workflows.py",
                   "checks that pages WORK, not that their numbers are right", "I could not confirm", "**When the live site check fails.**", "failure.png"):
        assert needle in runbook, needle
    assert "**Silent failures.**" in readme and "smoke-test.yml" in readme and "five workflows" in readme
