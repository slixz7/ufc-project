"""pipeline/token_expiry.py: warn before the backup token expires. The real header could not be confirmed from here (it needs a real token), so the first run's summary says which case it was."""
import ast
import email.message
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import token_expiry as te  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TOKEN = "github_pat_11AAAA_SecretSecretSecret"


class Resp:
    def __init__(self, status=200, headers=None):
        self.status, self.headers = status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(status=200, header=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request.full_url, request.get_header("Authorization")))
        if status >= 400:
            hdrs = email.message.Message()
            if header:
                hdrs[te.HEADER] = header
            raise urllib.error.HTTPError(request.full_url, status, "x", hdrs, None)
        h = email.message.Message()
        if header:
            h[te.HEADER] = header
        return Resp(status, h)
    return opener


@pytest.mark.parametrize("value, expected", [
    ("2026-12-01 04:00:00 UTC", datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)), ("2026-12-01T04:00:00Z", datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)),
    ("2026-12-01T04:00:00+00:00", datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)), ("2026-12-01 04:00:00 +0000", datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)),
    ("2026-12-01", datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)), ("  2026-12-01 04:00:00 UTC  ", datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)),
    ("soon", None), ("", None), (None, None), ("2026-13-40 00:00:00 UTC", None)])
def test_the_expiry_is_read_in_the_forms_github_uses_or_not_at_all(value, expected):
    assert te.parse_expiry(value) == expected


def test_days_left_counts_whole_days_and_goes_negative_after_the_expiry():
    assert te.days_left(NOW + timedelta(days=21, hours=3), NOW) == 21 and te.days_left(NOW + timedelta(hours=23), NOW) == 0 and te.days_left(NOW, NOW) == 0
    assert te.days_left(NOW - timedelta(hours=1), NOW) == -1 and te.days_left(NOW - timedelta(days=3), NOW) == -4


@pytest.mark.parametrize("days, alarm", [(60, False), (22, False), (21, True), (10, True), (1, True), (0, True)])
def test_the_alarm_starts_at_three_weeks(days, alarm):
    expiry = (NOW + timedelta(days=days, hours=2)).strftime("%Y-%m-%d %H:%M:%S UTC")
    sentence, got = te.assess(200, expiry, NOW)
    assert got is alarm and (f"in {days} day" in sentence) and ("Renew it soon" in sentence) == alarm


def test_each_situation_has_its_own_plain_sentence():
    sentence, alarm = te.assess(200, "2026-11-01 00:00:00 UTC", NOW)
    assert alarm and sentence == "The backup token expired on 2026-11-01. Renew it now and update the BACKUP_REPO_TOKEN secret."
    sentence, alarm = te.assess(401, None, NOW)
    assert alarm and "GitHub refused the backup token" in sentence and te.assess(403, None, NOW)[1]
    sentence, alarm = te.assess(200, None, NOW)
    assert not alarm and "did not report an expiry date" in sentence and "calendar" in sentence
    sentence, alarm = te.assess(200, "garbage", NOW)
    assert not alarm and "did not report an expiry date" in sentence
    sentence, alarm = te.assess(500, None, NOW)
    assert not alarm and "HTTP 500" in sentence and "nothing was concluded" in sentence
    assert te.assess(200, "2027-03-01 00:00:00 UTC", NOW)[0] == "The backup token expires on 2027-03-01, in 115 days: no action needed yet."
    assert "in 1 day." in te.assess(200, "2026-11-06 13:00:00 UTC", NOW)[0]


def run(argv, header=None, status=200, env=None, sent=None, seen=None):
    env = {"BACKUP_REPO_TOKEN": TOKEN, **(env or {})}
    messages = [] if sent is None else sent
    code = te.main(argv, env=env, opener=opener_for(status, header, seen), now=NOW, send=lambda text, e: (messages.append(text) or "sent"))
    return code, messages


def test_a_token_close_to_expiry_posts_one_alert_and_a_warning_annotation(capsys, tmp_path):
    summary = tmp_path / "summary.md"
    code, messages = run(["--repo", "me/backups"], "2026-11-12 00:00:00 UTC", env={"GITHUB_STEP_SUMMARY": str(summary)})
    out = capsys.readouterr().out
    assert code == 0 and len(messages) == 1 and messages[0].startswith("**Backup token**: The backup token expires on 2026-11-12, in 6 days.")
    assert "::warning title=Backup token::The backup token expires on 2026-11-12" in out and "The alert was sent." in out
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("### Backup token\nThe backup token expires on 2026-11-12") and "\\n" not in text and text.endswith("\n\n")                    # real line breaks, not the two characters


def test_a_comfortable_token_only_reports_the_date_and_sends_nothing(capsys):
    code, messages = run(["--repo", "me/backups"], "2027-06-01 00:00:00 UTC")
    out = capsys.readouterr().out
    assert code == 0 and messages == [] and "no action needed yet" in out and "::warning" not in out


def test_a_refused_token_is_reported_as_an_alarm(capsys):
    code, messages = run(["--repo", "me/backups"], status=401)
    assert code == 0 and len(messages) == 1 and "GitHub refused the backup token" in messages[0]


def test_a_custom_warning_window_is_honoured_and_an_unreadable_one_falls_back_to_three_weeks():
    assert run(["--repo", "me/backups", "--warn-days", "60"], "2027-01-01 00:00:00 UTC")[1] != []      # 57 days left, inside a 60-day window
    assert run(["--repo", "me/backups", "--warn-days", "7"], "2026-11-20 00:00:00 UTC")[1] == []       # 15 days left, outside a 7-day window
    assert run(["--repo", "me/backups", "--warn-days", "soon"], "2026-11-20 00:00:00 UTC")[1] != []    # unreadable: 21 days, so 15 days left alarms


def test_the_request_is_one_authenticated_read_of_the_backup_repository_and_the_token_is_never_printed(capsys):
    seen = []
    run(["--repo", "me/backups"], "2026-11-12 00:00:00 UTC", seen=seen)
    assert seen == [("https://api.github.com/repos/me/backups", f"Bearer {TOKEN}")]
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("env, argv, said", [({}, ["--repo", "me/backups"], "BACKUP_REPO_TOKEN is not set"), ({"BACKUP_REPO_TOKEN": TOKEN}, [], "Give the backup repository"),
                                              ({"BACKUP_REPO_TOKEN": TOKEN}, ["--repo", "nonsense"], "Give the backup repository")])
def test_missing_inputs_make_no_request_and_say_so(capsys, env, argv, said):
    calls = []
    code = te.main(argv, env=env, opener=lambda *a, **k: calls.append(1), now=NOW, send=lambda *a: "sent")
    assert code == 0 and calls == [] and said in capsys.readouterr().out


def test_a_network_failure_is_reported_by_class_only_and_never_fails_the_backup(capsys):
    def boom(request, timeout=None):
        raise OSError("connection to api.github.com failed using " + TOKEN)
    assert te.main(["--repo", "me/backups"], env={"BACKUP_REPO_TOKEN": TOKEN}, opener=boom, now=NOW, send=lambda *a: "sent") == 0
    out = capsys.readouterr().out
    assert "could not be checked (OSError)" in out and TOKEN not in out


def test_what_happened_to_the_alert_is_said_plainly(capsys):
    for result, words in (("not set", "ALERT_WEBHOOK_URL is not set"), ("not discord", "not a Discord address"), ("failed", "could not be delivered"), ("sent", "The alert was sent.")):
        te.main(["--repo", "me/backups"], env={"BACKUP_REPO_TOKEN": TOKEN}, opener=opener_for(200, "2026-11-08 00:00:00 UTC"), now=NOW, send=lambda *a, r=result: r)
        assert words in capsys.readouterr().out


def test_the_shared_message_sender_validates_the_webhook_like_the_failure_alert_and_never_pings(capsys):
    import notify_failure as nf
    sent = []
    assert nf.send_text("hi", {}) == "not set" and nf.send_text("hi", {"ALERT_WEBHOOK_URL": "https://example.org/hook"}) == "not discord"
    hook = "https://discord.com/api/webhooks/123456789012345678/" + "A" * 60
    assert nf.send_text("line one\nline two   x", {"ALERT_WEBHOOK_URL": hook}, post_func=lambda url, payload: sent.append((url, payload)) or True) == "sent"
    url, payload = sent[0]
    assert url == hook and payload == {"content": "line one line two x", "allowed_mentions": {"parse": []}}
    assert nf.send_text("x", {"ALERT_WEBHOOK_URL": hook}, post_func=lambda *a: False) == "failed"
    assert "A" * 60 not in capsys.readouterr().out


def test_it_uses_only_the_standard_library_and_the_alert_module():
    tree = ast.parse((ROOT / "pipeline" / "token_expiry.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "os", "sys", "urllib", "datetime", "notify_failure"}, imported
