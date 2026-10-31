"""Things about how the tests themselves run on GitHub."""
import os


def test_the_tests_never_write_into_the_github_job_summary_unless_they_set_it_themselves():
    assert os.getenv("GITHUB_STEP_SUMMARY") is None                  # conftest.py removes it before every test, even when GitHub has set it for the whole run


def test_a_test_that_sets_the_summary_variable_itself_still_gets_it(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    assert os.getenv("GITHUB_STEP_SUMMARY") == str(tmp_path / "s.md")
