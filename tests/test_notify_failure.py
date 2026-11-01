"""
The failure alert (pipeline/notify_failure.py): one short, silent message to a PRIVATE channel when the daily job or the weekly backup fails.
It must never leak its webhook address, never ping anyone, never make a failing run worse, and never post anywhere but Discord.
"""
import ast
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import notify_failure as nf  # noqa: E402

SECRET = "https://discord.com/api/webhooks/" + "1" * 18 + "/" + "s3cretT0ken" * 6          # looks like a real webhook; assembled here so no scan mistakes it for one
RUN_ENV = {"GITHUB_REPOSITORY": "slixz7/ufc-project", "GITHUB_RUN_ID": "987654", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_SHA": "0123456789abcdef",
           "GITHUB_REF_NAME": "main", "GITHUB_EVENT_NAME": "schedule"}


# ---------------------------------------------------------------------------------------------- the message
def test_the_message_says_what_failed_how_and_where_with_a_link_to_the_run():
    p = nf.build_payload("Auto update", RUN_ENV)
    assert p["content"].splitlines() == ["**Auto update** failed (schedule)", "branch main commit 0123456", "https://github.com/slixz7/ufc-project/actions/runs/987654"]


def test_the_message_can_never_ping_anybody_whatever_the_workflow_is_called():
    p = nf.build_payload("@everyone <@123456789> look at this", RUN_ENV)
    assert p["allowed_mentions"] == {"parse": []}                                                                     # Discord is told to ignore every mention
    assert len(p["content"]) < 2000 and len(nf.build_payload("x" * 5000, RUN_ENV)["content"]) <= nf.MAX_MESSAGE


def test_the_message_still_works_with_a_partly_empty_environment():
    assert nf.build_payload("Backup", {})["content"] == "**Backup** failed"
    assert nf.build_payload("", {})["content"] == "**A workflow** failed"
    assert nf.build_payload("Backup", {"GITHUB_REPOSITORY": "a/b"})["content"] == "**Backup** failed"                 # no run id, so no half-built link


# ---------------------------------------------------------------------------------------------- really talking to a server
class _Server:
    def __init__(self, status=204):
        outer = self
        outer.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api/webhooks/1/token"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_post_sends_json_with_a_user_agent_and_succeeds_on_a_204():
    s = _Server(204)
    try:
        assert nf.post(s.url, nf.build_payload("Auto update", RUN_ENV)) is True
        r = s.requests[0]
        h = {k.lower(): v for k, v in r["headers"].items()}
        assert r["path"] == "/api/webhooks/1/token" and h["content-type"] == "application/json" and h["user-agent"].startswith("ufc-predictor-alert")      # Discord refuses a request with no User-Agent
        assert json.loads(r["body"])["allowed_mentions"] == {"parse": []} and "Auto update" in json.loads(r["body"])["content"]
    finally:
        s.close()


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_a_refusal_from_the_server_is_reported_without_the_address_and_does_not_raise(status, capsys):
    s = _Server(status)
    try:
        assert nf.post(s.url, {"content": "x"}) is False
    finally:
        s.close()
    out = capsys.readouterr().out
    assert "HTTPError" in out and s.url not in out and "token" not in out


def test_no_connection_at_all_is_reported_without_the_address_and_does_not_raise(capsys):
    assert nf.post("http://127.0.0.1:1/api/webhooks/1/hidden-token", {"content": "x"}, timeout=2) is False
    out = capsys.readouterr().out
    assert "could not be delivered" in out and "hidden-token" not in out and "127.0.0.1" not in out


def test_a_malformed_address_does_not_leak_through_the_error_text(capsys):
    assert nf.post("not-a-url-secret-value", {"content": "x"}) is False                                               # urllib's own message for this repeats the address
    assert "secret-value" not in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- the entry point
def test_with_no_alert_secret_it_says_so_sends_nothing_and_succeeds(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(nf, "post", lambda *a, **k: calls.append(a) or True)
    assert nf.main(["Auto update"], env={}) == 0 and nf.main(["Auto update"], env={"ALERT_WEBHOOK_URL": "   "}) == 0
    assert calls == [] and "ALERT_WEBHOOK_URL is not set" in capsys.readouterr().out


@pytest.mark.parametrize("address", ["https://example.com/api/webhooks/1/x", "http://discord.com/api/webhooks/1/x", "https://discord.com.evil.test/api/webhooks/1/x",
                                      "https://evil.test/https://discord.com/api/webhooks/1/x", "file:///etc/passwd"])
def test_it_only_ever_posts_to_discords_own_webhook_addresses(address, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(nf, "post", lambda *a, **k: calls.append(a) or True)
    assert nf.main(["Auto update"], env={"ALERT_WEBHOOK_URL": address}) == 0
    out = capsys.readouterr().out
    assert calls == [] and "not a Discord webhook address" in out and address not in out


def test_a_valid_secret_sends_one_message_and_prints_only_that_it_was_sent(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(nf, "post", lambda url, payload, *a, **k: calls.append((url, payload)) or True)
    assert nf.main(["Backup"], env={**RUN_ENV, "ALERT_WEBHOOK_URL": SECRET}) == 0
    assert len(calls) == 1 and calls[0][0] == SECRET and "**Backup** failed" in calls[0][1]["content"]
    out = capsys.readouterr().out
    assert out.strip() == "The failure alert was sent." and SECRET not in out and "s3cretT0ken" not in out


def test_even_when_everything_goes_wrong_it_exits_zero_and_never_prints_the_secret(capsys, monkeypatch):
    import urllib.error
    token = "t" * 70

    def boom(request, timeout=None):                                                                                  # no real network: urllib fails, and its own message repeats the address
        raise urllib.error.URLError(f"cannot reach {request.full_url}")
    monkeypatch.setattr(nf.urllib.request, "urlopen", boom)
    assert nf.main(["Backup"], env={**RUN_ENV, "ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/1/" + token}) == 0
    out = capsys.readouterr().out
    assert "URLError" in out and token not in out and "discord.com" not in out


def test_it_uses_only_the_standard_library_so_it_works_when_the_package_install_is_what_failed():
    tree = ast.parse((ROOT / "pipeline" / "notify_failure.py").read_text(encoding="utf-8"))
    imported = {n.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in [a.name for a in node.names]}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported and imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


# ---------------------------------------------------------------------------------------------- how the workflows use it
def _steps(workflow: str) -> list[str]:
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    return ["- name:" + chunk for chunk in text.split("- name:")[1:]]


@pytest.mark.parametrize("workflow, name", [("auto-update.yml", "Auto update"), ("backup.yml", "Backup")])
def test_the_daily_job_and_the_weekly_backup_alert_on_failure_as_their_very_last_step(workflow, name):
    steps = _steps(workflow)
    last = steps[-1]
    assert "if: failure()" in last and f'python pipeline/notify_failure.py "{name}"' in last
    assert "ALERT_WEBHOOK_URL: ${{ secrets.ALERT_WEBHOOK_URL }}" in last                                              # its own secret, never the friends' channel
    assert "DISCORD_WEBHOOK_URL" not in last
    assert sum("notify_failure.py" in s for s in steps) == 1


def test_the_test_workflow_does_not_post_alerts_because_github_already_emails_the_person_who_pushed():
    assert "notify_failure" not in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
