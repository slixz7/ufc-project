"""
What a stranger needs from the repository's front door: a map of the folders that covers every folder, an ignore file that covers every kind of secret file the scanner looks for,
and a description of the daily job that matches what it actually does, in the order it does it.
"""
import inspect
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
GENERATED = {"data", "cache", "raw", "venv", ".venv", "__pycache__", ".pytest_cache", ".ipynb_checkpoints", ".update_backup", ".git", ".idea", ".vscode"}


def tracked_top_level_folders() -> set[str]:
    """The top-level folders that hold tracked files (from git itself where there is a checkout, so local scratch folders never count)."""
    if shutil.which("git") and (ROOT / ".git").exists():
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        return {f.split("/")[0] for f in files if "/" in f}
    return {p.name for p in ROOT.iterdir() if p.is_dir() and p.name not in GENERATED}


def folder_map() -> set[str]:
    m = re.search(r"^## Where things are\s*\n(.*?)(?=^## )", README, flags=re.S | re.M)
    assert m, "the README has no 'Where things are' section"
    return set(re.findall(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*/)`", m.group(1)))


def test_the_readmes_folder_map_covers_every_top_level_folder_and_names_only_folders_that_exist():
    mapped = folder_map()
    assert len(mapped) >= 10, mapped
    missing = [d for d in sorted(tracked_top_level_folders()) if not any(p == d + "/" or p.startswith(d + "/") for p in mapped)]
    assert not missing, f"these folders are not in the README's 'Where things are' map: {missing}"
    assert not [p for p in mapped if not (ROOT / p).is_dir()], [p for p in mapped if not (ROOT / p).is_dir()]


def test_the_notebooks_are_called_unused_early_work_and_that_is_true():
    row = [l for l in README.splitlines() if l.startswith("| notebooks")]
    assert len(row) == 1 and "Kept for history" in row[0] and "no longer uses them" in row[0] and "tests do not cover them" in row[0]
    code = "\n".join(p.read_text(encoding="utf-8") for sub in ("pipeline", "app", "demo", "tools") for p in (ROOT / sub).glob("*.py"))
    assert not re.search(r"^\s*(import|from)\s+(nbformat|nbconvert|papermill)\b", code, flags=re.M)                      # no code runs or reads a notebook
    assert not re.search(r"0[1-4]_[a-z_]+\.ipynb", code)                                                                # and none of them names one of the four
    assert "testpaths = tests" in (ROOT / "pytest.ini").read_text(encoding="utf-8")                                    # and the tests run only tests/
    for nb in ("01_load_and_clean", "02_features", "03_model", "04_postgres_sql"):
        assert (ROOT / f"{nb}.ipynb").exists()


def test_the_ignore_file_covers_every_kind_of_secret_file_the_scanner_looks_for():
    import scan_secrets
    ignored = {l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    for name in ("secrets.toml", ".env", "kaggle.json", "id_rsa", "credentials"):
        assert scan_secrets.is_forbidden_file(name) and name in ignored, name                                         # a file the scanner calls forbidden is one git never sees
    assert "*.pem" in ignored and scan_secrets.is_forbidden_file("keys/server.pem")
    assert ".streamlit/secrets.toml" in ignored                                                                        # the one place a local secrets file naturally goes


def test_the_ignore_file_keeps_the_generated_folders_out_including_the_update_tools_backups():
    ignored = {l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    assert {"data/", "demo/.data/", "__pycache__/", ".pytest_cache/", "venv/", ".venv/", ".update_backup/"} <= ignored
    assert ".update_backup" in (ROOT / "tools" / "apply_update.py").read_text(encoding="utf-8")                        # the folder the tool really writes to


def test_the_readmes_description_of_the_daily_job_lists_its_steps_in_the_order_they_run():
    import auto_update
    order = re.findall(r'^\s+"([a-z0-9-]+)": lambda', inspect.getsource(auto_update.run), flags=re.M)
    assert order.index("results") < order.index("refresh-odds") < order.index("health") < order.index("readme") < order.index("digest")
    section = README[README.index("The daily job (`pipeline/auto_update.py`) runs these steps"):README.index("![The daily job's report on GitHub Actions]")]
    labels = re.findall(r"^\d+\. \*\*([^*]+?):\*\*", section, flags=re.M)
    assert labels == ["Cards", "Model-only picks", "Betting lines", "Market-informed picks", "Public pick log", "Results", "Weekly", "Model health", "Digest"], labels
    assert "refreshes this README's generated numbers" in section and "(docs/MODEL_CARDS.md)" in section


def test_the_readme_does_not_promise_a_feature_the_site_already_has():
    app = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    nxt = README[README.index("## What I would do next"):README.index("## Earlier stage")]
    assert "Compare two fighters" in app and "A head-to-head page" not in nxt
    assert "hypothetical matchup" in nxt


DOCS = ("README.md", "docs/RUNBOOK.md", "docs/MODEL_CARDS.md", "docs/SQL_HIGHLIGHTS.md", "demo/README.md")
NAMED_PATH = re.compile(r"`((?:pipeline|sql|app|docs|tests|tools|demo|upcoming|picks_log|\.github/workflows|\.streamlit)/[A-Za-z0-9_./\-]+\.(?:py|sql|md|yml|csv|toml|txt|png))`")


def test_every_file_the_documentation_names_exists_so_the_docs_cannot_describe_a_file_that_is_gone():
    checked = 0
    for doc in DOCS:
        for path in sorted(set(NAMED_PATH.findall((ROOT / doc).read_text(encoding="utf-8")))):
            checked += 1
            generated = path.startswith("picks_log/") and path.endswith(".csv")                                          # written by the daily job, so it exists in the repository, not necessarily in a fresh copy
            assert (ROOT / path).exists() or generated, f"{doc} names {path}, which does not exist"
    assert checked >= 60, checked                                                                                        # the scan really found the paths


def test_the_runbook_points_to_the_readmes_map_instead_of_keeping_a_second_hand_written_one():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    section = runbook[runbook.index("## Project structure"):]
    assert "**Where things are**" in section and "a test fails if a folder is added without being listed there" in section
    assert "the original analysis" not in runbook and "01_load_and_clean.ipynb ... 04_postgres_sql.ipynb" not in runbook      # the old tree, which had already drifted out of date
