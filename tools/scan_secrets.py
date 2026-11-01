"""
Look for credentials in the project's files and in its whole git history, without ever printing one.

    python tools/scan_secrets.py              # the files in this folder
    python tools/scan_secrets.py --history    # every commit on every branch (needs the full history)

It prints only WHERE something was found (a file, or a commit and the file in it) and WHAT KIND it looks like, never the text itself, so its output is
safe to paste anywhere. It exits 1 if anything is found, so it can stop a workflow. On a shallow clone it refuses to scan the history (exit 2), because a
scan of part of the history would give false comfort.

What it looks for: Neon database passwords, Discord webhook addresses, database connection strings with a password that point at a real host (not a
placeholder such as HOST, nor an address on this computer), API keys or tokens assigned to this project's variable names, an Odds API key in a web
address, and files that should never be committed (.env, secrets.toml, kaggle.json, private keys).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", ".update_backup", "__pycache__", "data", ".data"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".ipynb", ".pyc", ".pdf", ".ttf", ".woff", ".woff2"}
MAX_FILE_BYTES = 2_000_000
PLACEHOLDER_HOSTS = {"HOST", "host", "localhost", "example.com", "db", "hostname"}

# The first pattern is written in two pieces so that this file does not contain the very text it looks for.
NEON_PASSWORD = re.compile(r"npg" + r"_[A-Za-z0-9]{8,}")
WEBHOOK = re.compile(r"webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{50,}")
CONNECTION = re.compile(r"postgres(?:ql)?(?:\+\w+)?://([^:\s/@]+):([^@\s]{4,})@([^\s/:?]+)")
ASSIGNED_KEY = re.compile(r"(?:ODDS_API_KEY|KAGGLE_API_TOKEN|KAGGLE_KEY|DISCORD_WEBHOOK_URL|ALERT_WEBHOOK_URL|BACKUP_REPO_TOKEN)\s*[=:]\s*[\"']?([A-Za-z0-9_\-]{20,})")
KEY_IN_ADDRESS = re.compile(r"apiKey=[A-Za-z0-9]{16,}")
FORBIDDEN_FILE = re.compile(r"(?:^|/)(?:secrets\.toml|\.env|kaggle\.json|id_rsa|credentials)$|\.pem$")


def is_placeholder(value: str) -> bool:
    """Text that documents where a secret goes (PASTE-THE-WEBHOOK-HERE, your_api_key_here, xxxxxxxxxxxx) is not a secret: a real key always has digits or mixed
    characters, never just words joined by hyphens or underscores."""
    return bool(re.fullmatch(r"[A-Za-z]+(?:[-_][A-Za-z]+)+", value) or re.fullmatch(r"[xX*]{8,}", value))


def find_credentials(text: str) -> list[str]:
    """The KINDS of credential this text looks like it contains (never the credential itself). Placeholders and local addresses are not credentials."""
    kinds = []
    if NEON_PASSWORD.search(text):
        kinds.append("a Neon database password")
    if WEBHOOK.search(text):
        kinds.append("a Discord webhook address")
    for _user, _password, host in CONNECTION.findall(text):
        if host in PLACEHOLDER_HOSTS or host.endswith(".example") or re.match(r"^(127\.|0\.0\.0\.0|::1)", host):
            continue
        if "." in host:
            kinds.append("a database connection string with a password, pointing at a real host")
            break
    if any(not is_placeholder(m.group(1)) for m in ASSIGNED_KEY.finditer(text)):
        kinds.append("an API key or token assigned to one of this project's variables")
    if KEY_IN_ADDRESS.search(text):
        kinds.append("an API key inside a web address")
    return kinds


def is_forbidden_file(path: str) -> bool:
    return bool(FORBIDDEN_FILE.search(path.replace("\\", "/")))


def scan_files(root: Path = ROOT) -> list[tuple[str, str]]:
    """(path, kind) for every file in the folder that looks like it contains a credential or should never be committed."""
    root = Path(root)
    found = set()
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if is_forbidden_file(str(rel)):
            found.add((str(rel).replace("\\", "/"), "a file that should never be committed"))
        if f.suffix.lower() in SKIP_SUFFIXES or f.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for kind in find_credentials(text):
            found.add((str(rel).replace("\\", "/"), kind))
    return sorted(found)


class HistoryUnreadable(Exception):
    """git could not read the history here (not a repository, a repository git will not open, or git is missing). Nothing was scanned."""


def commit_count(repo: Path) -> int:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-list", "--all", "--count"], capture_output=True, text=True)
    except FileNotFoundError:
        raise HistoryUnreadable("git is not installed")
    if out.returncode != 0:
        first = (out.stderr.strip().splitlines() or ["git failed"])[0]
        if "does not have any commits" in out.stderr or "unknown revision" in out.stderr:
            return 0                                                                      # a repository with no commits yet has nothing in its history
        raise HistoryUnreadable(first[:200])
    return int(out.stdout.strip() or 0)


def is_shallow(repo: Path) -> bool:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True)
    except FileNotFoundError:
        raise HistoryUnreadable("git is not installed")
    return out.returncode == 0 and out.stdout.strip() == "true"


def scan_history(repo: Path = ROOT) -> list[tuple[str, str, str]]:
    """(commit, path, kind) for every line ever added on any branch that looks like a credential, and every forbidden file ever added."""
    if commit_count(repo) == 0:
        return []
    proc = subprocess.Popen(["git", "-C", str(repo), "log", "--all", "-p", "--no-color", "--no-ext-diff", "--format=@@@ %h"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    found, commit, path = set(), "?", "?"
    for line in proc.stdout:
        if line.startswith("@@@ "):
            commit, path = line[4:].strip(), "?"
        elif line.startswith("diff --git "):
            m = re.match(r"diff --git a/.+ b/(.+)$", line.rstrip("\n"))
            path = m.group(1) if m else "?"
            if is_forbidden_file(path):
                found.add((commit, path, "a file that should never be committed"))
        elif line.startswith("+") and not line.startswith("+++"):
            for kind in find_credentials(line[1:]):
                found.add((commit, path, kind))
    proc.wait()
    if proc.returncode not in (0, None):
        raise HistoryUnreadable("git stopped while reading the history")
    return sorted(found)


def main(argv=None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description="Look for credentials in the project's files or its whole git history, without printing them")
    ap.add_argument("--history", action="store_true", help="scan every commit on every branch instead of the files")
    args = ap.parse_args(argv)
    if args.history:
        try:
            if is_shallow(root):
                print("The git history here is shallow (only part of it was downloaded), so scanning it would give false comfort. Fetch the full history first.")
                return 2
            count = commit_count(root)
            hits = scan_history(root)
        except HistoryUnreadable as exc:
            print(f"git could not read the history here ({exc}), so nothing was scanned.")
            return 2
        if not hits:
            print(f"Nothing that looks like a credential in any of the {count} commit(s) on any branch." if count else "This repository has no commits yet, so there is no history to scan.")
            return 0
        print("Something that looks like a credential was added in these commits (the text itself is not shown):")
        for commit, path, kind in hits:
            print(f"  commit {commit}  {path}  : {kind}")
        return 1
    hits = scan_files(root)
    if not hits:
        print("Nothing that looks like a credential in the project's files.")
        return 0
    print("Something that looks like a credential is in these files (the text itself is not shown):")
    for path, kind in hits:
        print(f"  {path}  : {kind}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
