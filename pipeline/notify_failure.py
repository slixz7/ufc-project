"""
Tell a private Discord channel that a workflow run failed.

    python pipeline/notify_failure.py "Auto update"

It is the last step of the daily job and of the weekly backup, run with `if: failure()`. It posts one short message: which workflow failed, how it was
started, the branch, the commit and a link to the run.

  * It has its OWN webhook, the repository secret ALERT_WEBHOOK_URL, so that failures never land in the channel your friends read. If that secret is not
    set it says so and does nothing.
  * It never prints the webhook address, not even inside an error message, and it only talks to Discord's own webhook addresses.
  * It never pings anyone (the message is sent with mentions switched off).
  * It always exits 0: an alert that cannot be delivered must not turn a failing run into a confusing second failure.
  * It uses only Python's standard library, so it works even when the failure was the package install.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

ALLOWED_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
TIMEOUT_SECONDS = 10
MAX_MESSAGE = 1800                                   # Discord's limit is 2000 characters


def build_payload(workflow: str, env=None) -> dict:
    """The message, from the standard GitHub Actions environment variables. Mentions are switched off, so nothing in it can ping anybody."""
    env = os.environ if env is None else env
    name = " ".join(str(workflow).split())[:80] or "A workflow"
    repo, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    lines = [f"**{name}** failed" + (f" ({env['GITHUB_EVENT_NAME']})" if env.get("GITHUB_EVENT_NAME") else "")]
    where = " ".join(x for x in (f"branch {env['GITHUB_REF_NAME']}" if env.get("GITHUB_REF_NAME") else "", f"commit {env['GITHUB_SHA'][:7]}" if env.get("GITHUB_SHA") else "") if x)
    if where:
        lines.append(where)
    if repo and run_id:
        lines.append(f"{server}/{repo}/actions/runs/{run_id}")
    return {"content": "\n".join(lines)[:MAX_MESSAGE], "allowed_mentions": {"parse": []}}


def post(webhook_url: str, payload: dict, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Send the message. True if Discord accepted it. Any failure is False, and nothing about the address is printed."""
    try:
        request = urllib.request.Request(webhook_url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "ufc-predictor-alert/1.0"})   # Discord refuses requests with no User-Agent
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception as exc:                          # a bad address, no network, a timeout, an HTTP error: all the same here
        print(f"The alert could not be delivered ({type(exc).__name__}).")      # the class only: the text of an error can contain the address
        return False


def send_text(text: str, env=None, post_func=None) -> str:
    """Post a short plain message to the same private channel, with the same checks as a failure alert. Returns 'sent', 'not set', 'not discord' or 'failed', so a caller can
    say what happened. The address is never printed, and nothing in the message can ping anybody."""
    env = os.environ if env is None else env
    webhook = (env.get("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook:
        return "not set"
    if not webhook.startswith(ALLOWED_PREFIXES):
        return "not discord"
    payload = {"content": " ".join(str(text).split())[:MAX_MESSAGE], "allowed_mentions": {"parse": []}}
    return "sent" if (post_func or post)(webhook, payload) else "failed"


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    webhook = (env.get("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook:
        print("ALERT_WEBHOOK_URL is not set, so no alert was sent. (Make a webhook in a PRIVATE Discord channel and add it as that repository secret.)")
        return 0
    if not webhook.startswith(ALLOWED_PREFIXES):
        print("ALERT_WEBHOOK_URL is not a Discord webhook address, so no alert was sent.")
        return 0
    if post(webhook, build_payload(argv[0] if argv else "A workflow", env)):
        print("The failure alert was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
