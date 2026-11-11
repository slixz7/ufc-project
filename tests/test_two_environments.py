"""
The daily job runs the whole suite in an environment with ONLY the pipeline's packages; the Tests workflow has the web app's packages and Postgres as well. A test that needs a package the daily job lacks
passes in one and stops the other (it did once: the tab-icon tests needed Pillow). The Tests workflow therefore has a second job that reproduces the daily environment exactly, and these tests keep it exact.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
DAILY = (ROOT / ".github" / "workflows" / "auto-update.yml").read_text(encoding="utf-8")


def job(text, name):
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):\s*$", body, flags=re.M)
    jobs = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
    return jobs[name]


def run_lines(block):
    return [m.group(1).strip() for m in re.finditer(r"^\s*run: (?!\|)(.+)$", block, flags=re.M)]


def test_the_tests_workflow_has_a_second_job_for_the_daily_jobs_own_environment():
    jobs = re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", TESTS[TESTS.index("\njobs:\n"):], flags=re.M)
    assert jobs == ["tests", "daily-environment"]


def test_that_job_installs_exactly_what_the_daily_job_installs_and_nothing_else():
    env = job(TESTS, "daily-environment")
    daily_install = re.search(r"^\s*run: (pip install -r pipeline/requirements\.txt -c constraints-ci\.txt)\s*$", DAILY, flags=re.M).group(1)
    assert run_lines(env)[0] == daily_install
    assert env.count("pip install") == 1
    for extra in ("app/requirements.txt", "pyflakes", "services:", "TEST_PG_ADMIN_URL", "postgres"):
        assert extra not in env, extra                                                                                         # no web app packages, no database: that is the point


def test_it_runs_the_same_test_command_with_the_same_lock_check_and_python_as_the_daily_job():
    env = job(TESTS, "daily-environment")
    daily_tests = re.search(r"^\s*run: (python -m pytest -q tests)\s*$", DAILY, flags=re.M).group(1)
    assert run_lines(env)[-1] == daily_tests and run_lines(job(TESTS, "tests"))[-1] == daily_tests                             # all three run the same command
    assert 'UFC_EXPECT_LOCKED: "1"' in env and 'UFC_EXPECT_LOCKED: "1"' in DAILY
    assert re.search(r'python-version: "([\d.]+)"', env).group(1) == re.search(r'python-version: "([\d.]+)"', DAILY).group(1)
    assert "timeout-minutes:" in env


def test_the_main_job_still_has_the_web_apps_packages_and_the_database_the_second_one_deliberately_lacks():
    main = job(TESTS, "tests")
    assert "app/requirements.txt" in main and "services:" in main and "TEST_PG_ADMIN_URL" in main


def test_the_runbook_states_the_rule_a_test_needing_another_package_must_skip_not_crash():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## Two test environments", "daily-environment", "No module named 'PIL'", "must skip when it is missing", "tests/test_two_environments.py"):
        assert needle in runbook, needle


def test_no_test_file_imports_pillow_without_skipping_when_it_is_missing():
    for path in (ROOT / "tests").glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == Path(__file__).name:
            continue
        if re.search(r"^\s*(import PIL|from PIL)\b", text, flags=re.M):
            assert "importorskip" in text and "PIL" in text[text.index("importorskip"):], f"{path.name} imports Pillow, which the daily job does not have: use pytest.importorskip"
