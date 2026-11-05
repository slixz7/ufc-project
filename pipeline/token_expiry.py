"""
Warn before the backup token expires, instead of when the weekly backup starts failing.

    python pipeline/token_expiry.py --repo OWNER/ufc-project-backups [--warn-days 21]      (token in the environment variable BACKUP_REPO_TOKEN)

A fine-grained GitHub token has an expiry date, and an expired one makes the weekly backup fail (after GitHub has already emailed you once). GitHub reports a token's expiry in the
response header `github-authentication-token-expiration` when the token is used. This makes one read-only request with the backup token, reads that header, and:

  * says how long is left in the run summary;
  * at or below --warn-days (default 21) also posts a short message to the private alert channel and prints a warning annotation;
  * if GitHub refuses the token (401) says the token is already invalid and posts the same message;
  * if GitHub does not report an expiry (a token with no expiry, or one that does not report it) says so, quietly.
It never fails the backup (it always exits 0), never prints the token, and uses only the standard library. Whether GitHub really reports the header for your token is confirmed by the
first run: the run summary says which of these happened.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import notify_failure

HEADER = "github-authentication-token-expiration"
API = "https://api.github.com/repos/"
WARN_DAYS = 21
TIMEOUT_SECONDS = 15


def parse_expiry(value: str | None) -> datetime | None:
    """The expiry as a UTC datetime, from '2026-12-01 04:00:00 UTC' or an ISO form; None when it cannot be read."""
    if not value:
        return None
    v = str(value).strip().replace("UTC", "").replace("Z", "+00:00").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(v, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def days_left(expiry: datetime, now: datetime) -> int:
    """Whole days until the expiry; negative once it has passed."""
    return (expiry - now).days if expiry >= now else -((now - expiry).days + 1)


def fetch(repo: str, token: str, opener=None) -> tuple[int, str | None]:
    """(HTTP status, the expiry header or None). A refused token (401/403) is a status, not an exception."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(API + repo, method="GET", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                                                         "User-Agent": "ufc-predictor-token-check/1.0"})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.headers.get(HEADER)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get(HEADER) if exc.headers else None


def assess(status: int, header: str | None, now: datetime, warn_days: int = WARN_DAYS) -> tuple[str, bool]:
    """(the sentence for the run summary, whether to raise the alarm)."""
    if status in (401, 403):
        return ("GitHub refused the backup token (it has expired, been revoked, or lost its permission). Renew it now: Settings, Developer settings, Personal access tokens, "
                "then update the BACKUP_REPO_TOKEN secret.", True)
    if status != 200:
        return f"The expiry check could not read the token's status (HTTP {status}); nothing was concluded.", False
    expiry = parse_expiry(header)
    if expiry is None:
        return ("GitHub did not report an expiry date for this token (it may have none, or not report it), so there is nothing to warn about. "
                "Keep the date in your calendar anyway.", False)
    left = days_left(expiry, now)
    when = expiry.strftime("%Y-%m-%d")
    if left < 0:
        return f"The backup token expired on {when}. Renew it now and update the BACKUP_REPO_TOKEN secret.", True
    if left <= warn_days:
        return f"The backup token expires on {when}, in {left} day{'s' if left != 1 else ''}. Renew it soon and update the BACKUP_REPO_TOKEN secret.", True
    return f"The backup token expires on {when}, in {left} days: no action needed yet.", False


def main(argv=None, env=None, opener=None, now=None, send=None) -> int:
    env = os.environ if env is None else env
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default
    repo, warn = opt("--repo"), opt("--warn-days", str(WARN_DAYS))
    token = (env.get("BACKUP_REPO_TOKEN") or "").strip()
    if not repo or "/" not in repo:
        print("Give the backup repository as --repo OWNER/NAME.")
        return 0
    if not token:
        print("BACKUP_REPO_TOKEN is not set, so the token's expiry was not checked.")
        return 0
    try:
        warn_days = int(warn)
    except ValueError:
        warn_days = WARN_DAYS
    try:
        status, header = fetch(repo, token, opener)
    except Exception as exc:                              # no network, a timeout: the class only (the text of an error could contain the token)
        print(f"The token's expiry could not be checked ({type(exc).__name__}).")
        return 0
    sentence, alarm = assess(status, header, now or datetime.now(timezone.utc), warn_days)
    print(sentence)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Backup token\n{sentence}\n\n")
    if alarm:
        print(f"::warning title=Backup token::{sentence}")
        result = (send or notify_failure.send_text)(f"**Backup token**: {sentence}", env)
        print({"sent": "The alert was sent.", "not set": "ALERT_WEBHOOK_URL is not set, so no alert was sent.", "not discord": "ALERT_WEBHOOK_URL is not a Discord address.",
               "failed": "The alert could not be delivered."}.get(result, "The alert was not sent."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
