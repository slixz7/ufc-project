"""
What a stranger sees when they open the public repository: a license, credits that keep the conditions the data's owners set, links that work,
and no raw data files.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*\n(.*?)(?=^## |\Z)", README, flags=re.S | re.M)
    assert m, f"the README has no '## {title}' section"
    return m.group(1)


def test_the_repository_has_the_mit_license_with_a_named_copyright_holder():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and re.search(r"^Copyright \(c\) 20\d\d \S", text, flags=re.M) and "Sheldon" in text
    assert "Permission is hereby granted, free of charge" in text and 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text
    assert "The above copyright notice and this permission notice shall be included" in text


def test_the_readme_says_the_license_covers_the_code_only_and_that_the_data_has_its_own_terms():
    lic = _section("License")
    assert "[MIT License](LICENSE)" in lic and "code only" in lic and "non-commercial" in lic and "Data and credits" in lic


def test_the_credits_keep_the_conditions_each_data_owner_set():
    credits = _section("Data and credits")
    assert "https://www.kaggle.com/datasets/scarekrow/ufc-data" in credits                                      # a link back to the Kaggle page
    assert "non-commercial" in credits and "permission" in credits and "not included in this repository" in credits
    assert "CC BY 4.0" in credits and "https://github.com/nlbair/ufc-data" in credits and "https://doi.org/10.5281/zenodo.23458636" in credits and "Changes made here" in credits
    assert "CC BY-SA 4.0" in credits and "Wikipedia" in credits                                                 # the card pages' license
    assert "The Odds API" in credits and "not affiliated with or endorsed by the UFC" in credits


def test_the_readme_warns_that_the_free_hosting_may_show_a_sleep_screen():
    top = README[:1500]
    assert "https://ufc-predictor.streamlit.app/" in top and "sleep screen" in top


def test_every_link_and_image_in_the_readme_that_points_into_the_repository_exists():
    targets = re.findall(r"\]\(([^)\s]+)\)", README)
    local = [t.split("#")[0] for t in targets if not re.match(r"^(https?:|mailto:|#)", t) and t.split("#")[0]]
    assert len(local) >= 8, local                                                                                # the check is really looking at something
    missing = [t for t in local if not (ROOT / t).exists()]
    assert not missing, f"broken links or images in the README: {missing}"


def test_every_link_in_the_other_public_documents_that_points_into_the_repository_exists():
    for doc in ("demo/README.md", "picks_log/ABOUT.md", "docs/RUNBOOK.md"):
        f = ROOT / doc
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        local = [t.split("#")[0] for t in re.findall(r"\]\(([^)\s]+)\)", text) if not re.match(r"^(https?:|mailto:|#)", t) and t.split("#")[0]]
        missing = [t for t in local if not (f.parent / t).resolve().exists()]
        assert not missing, f"{doc}: broken links or images: {missing}"


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="not a git checkout")
def test_no_raw_data_file_is_committed():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    big = [f for f in files if re.search(r"\.(csv|parquet|xlsx|db|sqlite|zip)$", f) and (ROOT / f).exists() and (ROOT / f).stat().st_size > 200_000 and not f.startswith("picks_log/")]
    assert not big, f"large data files are tracked by git: {big}"                                               # the Kaggle data must not be redistributed here
    assert not [f for f in files if f.startswith("data/")]


_PLACEHOLDER_USERS = {"USER", "user", "someone", "username", "postgres", "owner", "reader"}
_PLACEHOLDER_HOSTS = {"HOST", "host", "localhost", "example.com", "db", "hostname"}


def _looks_like_a_real_credential(text: str) -> list[str]:
    """Real tokens (Neon passwords, Discord webhooks), and connection strings that point at a real host (a dotted name, not an address on this computer)."""
    hits = re.findall(r"npg" + r"_[A-Za-z0-9]{8,}|webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{50,}", text)      # written in pieces so this file does not trip the pre-push secret scan
    for user, host in re.findall(r"postgres(?:ql)?(?:\+\w+)?://([^:\s/@]+):[^@\s]{4,}@([^\s/:?]+)", text):
        if host in _PLACEHOLDER_HOSTS or host.endswith(".example") or re.match(r"^(127\.|0\.0\.0\.0|::1)", host):
            continue
        if "." in host:
            hits.append(f"{user}:***@{host}")
    return hits


def test_the_credential_check_flags_real_looking_secrets_and_ignores_placeholders():
    assert _looks_like_a_real_credential("postgresql://USER:PASSWORD@HOST/DB") == []
    assert _looks_like_a_real_credential("postgres://someone:secret@127.0.0.1:1/x") == []
    assert _looks_like_a_real_credential("postgresql+psycopg2://postgres:postgres@localhost:5432/postgres") == []
    fake_host = "ep-cool-123.us-east-2.aws." + "neon" + ".tech"                                                  # a made-up address, assembled so the scan does not mistake it for a real one
    assert _looks_like_a_real_credential(f"postgresql://owner:pw-longer@{fake_host}/neondb") == [f"owner:***@{fake_host}"]
    fake_token = "npg" + "_AbCdEfGh1234"
    assert _looks_like_a_real_credential(f"password is {fake_token}") == [fake_token]
    assert _looks_like_a_real_credential("https://discord.com/api/webhooks/" + "1" * 18 + "/" + "a" * 60)[0].startswith("webhooks/")


def test_secrets_are_not_present_in_any_text_file_of_the_project():
    skip = {".git", "node_modules", "venv", ".venv", ".update_backup", "__pycache__", "data"}
    hits = []
    for f in ROOT.rglob("*"):
        if not f.is_file() or any(part in skip for part in f.relative_to(ROOT).parts) or f.suffix in {".png", ".zip", ".ipynb", ".pyc"} or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if f.name in {"test_repo_hygiene.py"}:                                                                    # this file's own examples
            continue
        if _looks_like_a_real_credential(text):
            hits.append(str(f.relative_to(ROOT)))
    assert hits == [], f"something that looks like a credential is in: {hits}"
