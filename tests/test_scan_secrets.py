"""
tools/scan_secrets.py: looks for credentials in the files and the whole git history and must NEVER print one. Every fake secret below is assembled from
pieces, so that neither this scanner nor the pre-push scan mistakes this file for a leak.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import scan_secrets as ss  # noqa: E402

NEON = "npg" + "_AbCdEfGh1234567"
WEBHOOK = "https://discord.com/api/webhooks/" + "1" * 18 + "/" + "z" * 60
HOST = "ep-cool-123.us-east-2.aws." + "neon" + ".tech"
CONN = f"postgresql://owner:pw-longer@{HOST}/neondb"
KEY_VAR = "ODDS_API_KEY" + "=" + "q1w2e3r4t5y6u7i8o9p0zz"
KEY_URL = "https://api.example-odds.test/v4/sports?apiKey" + "=" + "A1b2C3d4E5f6G7h8I9"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ---------------------------------------------------------------------------------------------- what counts as a credential
def test_each_kind_of_credential_is_recognised_by_kind_never_by_value():
    for text, kind in ((f"password {NEON}", "Neon database password"), (f"post to {WEBHOOK}", "Discord webhook"), (f"DATABASE_URL={CONN}", "connection string"),
                       (f"export {KEY_VAR}", "API key or token assigned"), (f"GET {KEY_URL}", "API key inside a web address")):
        kinds = ss.find_credentials(text)
        assert len(kinds) >= 1 and any(kind in k for k in kinds), (text[:30], kinds)
        assert not any(secret in " ".join(kinds) for secret in (NEON, WEBHOOK, HOST, "pw-longer", "q1w2e3r4"))             # what it returns never contains the secret


def test_placeholders_local_addresses_and_secret_references_are_not_credentials():
    for text in ("postgresql://USER:PASSWORD@HOST/DB", "postgres://someone:secret@127.0.0.1:1/x", "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres",
                 "postgresql://u:longpassword@db.example/x", "ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}", "set the ODDS_API_KEY secret", "$env:DATABASE_URL = \"...\"",
                 "webhooks/[0-9]{17,20}/ is the shape of one", "an apiKey goes in the address",
                 '$env:ALERT_WEBHOOK_URL = "PASTE-THE-WEBHOOK-HERE"', "ODDS_API_KEY=your_api_key_goes_here_please", "KAGGLE_API_TOKEN: xxxxxxxxxxxxxxxxxxxxxxxx"):
        assert ss.find_credentials(text) == [], text


def test_a_documented_placeholder_is_told_apart_from_a_real_looking_key():
    for value in ("PASTE-THE-WEBHOOK-HERE", "your_api_key_goes_here", "xxxxxxxxxxxxxxxxxxxx", "paste-your-token-here-now"):
        assert ss.is_placeholder(value), value
    for value in ("q1w2e3r4t5y6u7i8o9p0zz", "AbCdEfGhIjKlMnOpQrStUv", "0123456789abcdef0123456789", "abcdefghijklmnopqrstuv", "key-with-digits-1234567890"):
        assert not ss.is_placeholder(value), value                                                                      # a plain run of letters, or anything with digits, could be a key
    assert ss.find_credentials('ALERT_WEBHOOK_URL = "PASTE-THE-WEBHOOK-HERE"') == []
    assert ss.find_credentials("ODDS_API_KEY=" + "q1w2e3r4t5y6u7i8o9p0zz")                                              # the same variable with a real-looking value is still found


def test_forbidden_file_names_are_recognised_in_any_folder():
    for name in (".env", "app/.streamlit/secrets.toml", "kaggle.json", "keys/server.pem", "home/.ssh/id_rsa", "credentials"):
        assert ss.is_forbidden_file(name), name
    for name in ("pipeline/environment.py", "docs/credentials_policy.md.txt", "README.md", ".env.example.txt", "app/secrets_helper.py"):
        assert not ss.is_forbidden_file(name), name


# ---------------------------------------------------------------------------------------------- the files
def test_scanning_files_reports_the_file_and_kind_and_skips_the_places_it_should(tmp_path):
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "oops.py").write_text(f'URL = "{CONN}"\n', encoding="utf-8")
    (tmp_path / "notes.md").write_text(f"my token is {NEON}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("anything\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("print('hello')\nURL = 'postgresql://USER:PASSWORD@HOST/DB'\n", encoding="utf-8")
    for skipped in (".git", "venv", "data", ".update_backup"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "ignored.txt").write_text(NEON, encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(NEON.encode())
    hits = ss.scan_files(tmp_path)
    assert [h[0] for h in hits] == [".env", "notes.md", "pipeline/oops.py"]
    assert dict(hits)[".env"] == "a file that should never be committed" and "Neon" in dict(hits)["notes.md"] and "real host" in dict(hits)["pipeline/oops.py"]


def test_the_command_line_on_files_exits_nonzero_only_when_something_is_found_and_prints_no_secret(tmp_path, capsys):
    (tmp_path / "ok.txt").write_text("nothing here\n", encoding="utf-8")
    assert ss.main([], root=tmp_path) == 0 and "Nothing that looks like a credential" in capsys.readouterr().out
    (tmp_path / "bad.txt").write_text(f"{NEON} and {WEBHOOK} and {CONN}\n", encoding="utf-8")
    assert ss.main([], root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "bad.txt" in out and "Neon" in out and "Discord" in out
    for secret in (NEON, WEBHOOK, HOST, "pw-longer", "z" * 20, "1" * 18):
        assert secret not in out                                                                                         # the output is safe to paste anywhere


def test_this_project_itself_contains_no_credential_in_any_file():
    assert ss.scan_files(ROOT) == []


# ---------------------------------------------------------------------------------------------- the history
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.test", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("print('clean')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "clean")
    return repo


@needs_git
def test_a_clean_history_passes_and_says_so(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert ss.scan_history(repo) == [] and ss.main(["--history"], root=repo) == 0
    assert "Nothing that looks like a credential in any of the 1 commit(s)" in capsys.readouterr().out


@needs_git
def test_a_repository_with_no_commits_has_no_history_to_scan_and_says_so(tmp_path, capsys):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    assert ss.scan_history(repo) == [] and ss.commit_count(repo) == 0
    assert ss.main(["--history"], root=repo) == 0 and "no commits yet" in capsys.readouterr().out


@needs_git
def test_a_folder_git_cannot_read_is_reported_plainly_not_as_a_traceback(tmp_path, capsys):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(ss.HistoryUnreadable):
        ss.scan_history(not_a_repo)
    assert ss.main(["--history"], root=not_a_repo) == 2                                                                   # exit 2: nothing was scanned, which is not the same as "clean"
    assert "could not read the history" in capsys.readouterr().out


def test_a_missing_git_is_reported_plainly(monkeypatch, tmp_path, capsys):
    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(ss.subprocess, "run", no_git)
    assert ss.main(["--history"], root=tmp_path) == 2 and "git is not installed" in capsys.readouterr().out


@needs_git
def test_a_secret_that_was_committed_and_later_deleted_is_still_found_by_commit_and_file(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "config.py").write_text(f'TOKEN = "{NEON}"\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "oops")
    bad_commit = _git(repo, "rev-parse", "--short", "HEAD").strip()
    _git(repo, "rm", "-q", "config.py")
    _git(repo, "commit", "-q", "-m", "removed it again")                                                                  # gone from the files, but not from the history
    assert ss.scan_files(repo) == []
    hits = ss.scan_history(repo)
    assert len(hits) == 1 and hits[0][0] == bad_commit                                                                  # the commit that added it, not the one that removed it
    assert hits[0][1] == "config.py" and "Neon" in hits[0][2]
    assert ss.main(["--history"], root=repo) == 1
    out = capsys.readouterr().out
    assert "config.py" in out and NEON not in out and "AbCdEfGh" not in out


@needs_git
def test_a_secret_on_another_branch_is_found_too(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "x.txt").write_text(f"{WEBHOOK}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    assert [h[2] for h in ss.scan_history(repo)] == ["a Discord webhook address"]


@needs_git
def test_a_forbidden_file_ever_committed_is_found_even_with_harmless_contents(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".env").write_text("HARMLESS=1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "env file")
    assert [(h[1], h[2]) for h in ss.scan_history(repo)] == [(".env", "a file that should never be committed")]


@needs_git
def test_only_lines_that_were_added_count_so_a_placeholder_that_replaced_a_secret_is_not_a_fresh_finding(tmp_path):
    repo = _repo(tmp_path)
    (repo / "c.txt").write_text("URL = 'postgresql://USER:PASSWORD@HOST/DB'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "placeholder")
    assert ss.scan_history(repo) == []


@needs_git
def test_a_shallow_clone_is_refused_not_silently_half_scanned(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "b.py").write_text("print(2)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow)], check=True, capture_output=True)       # as_uri: a valid address on Windows paths too
    assert ss.is_shallow(shallow) and not ss.is_shallow(repo)
    assert ss.main(["--history"], root=shallow) == 2 and "shallow" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- how the test workflow uses it
def test_the_test_workflow_scans_the_files_and_the_full_history_before_the_tests():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in text and "python tools/scan_secrets.py\n" in text and "python tools/scan_secrets.py --history" in text
    assert text.index("scan_secrets.py --history") < text.index("python -m pytest -q tests")
