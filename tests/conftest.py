"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_job_summary(monkeypatch):
    """On GitHub, the job summary is a file named by GITHUB_STEP_SUMMARY, and the daily job's report writer appends to it. The tests exercise that writer with
    pretend dates, which put a fake 'UFC auto-update, 2026-10-27 (DRY RUN)' report on the Tests run's page, easy to mistake for a real run. A test that wants
    the writer sets the variable itself, inside the test."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
