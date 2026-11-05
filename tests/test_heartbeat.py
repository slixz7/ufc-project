"""pipeline/heartbeat.py: tell an outside "dead man's switch" service that a scheduled job ran. The address is a secret: it is never printed, and a monitor that cannot be reached never fails a run."""
import ast
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import heartbeat as hb  # noqa: E402

SECRET = "8f14e45f-ceea-467a-9575-secretkey"
BASE = f"https://hc-ping.com/{SECRET}"


class Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_recording(calls, statuses=(200,)):
    seq = iter(statuses)

    def opener(request, timeout=None):
        calls.append((request.full_url, request.get_method(), timeout, request.get_header("User-agent")))
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return Response(v)
    return opener


@pytest.mark.parametrize("base, kind, expected", [
    (BASE, "success", BASE), (BASE + "/", "success", BASE), (BASE, "fail", BASE + "/fail"), (BASE + "/", "start", BASE + "/start"), (f"  {BASE}  ", "fail", BASE + "/fail"),
    ("https://hc-ping.com/slug-name?create=1", "fail", "https://hc-ping.com/slug-name/fail?create=1"), ("https://example.org", "success", "https://example.org"),
])
def test_the_address_for_each_kind_of_ping(base, kind, expected):
    assert hb.build_url(base, kind) == expected


@pytest.mark.parametrize("bad", ["http://hc-ping.com/x", "ftp://hc-ping.com/x", "hc-ping.com/x", "https://", "https:///path", "", "   ", None, "https://hc ping.com/x", "javascript:alert(1)", "file:///etc/passwd"])
def test_only_an_https_address_with_a_host_is_accepted(bad):
    with pytest.raises(hb.BadAddress):
        hb.build_url(bad, "success")


def test_an_unknown_kind_of_ping_is_refused():
    with pytest.raises(hb.BadAddress):
        hb.build_url(BASE, "explode")


def test_a_delivered_ping_is_one_get_with_a_timeout_and_a_user_agent():
    calls = []
    assert hb.ping(BASE, opener=opener_recording(calls), sleep=lambda s: None) == (True, "delivered")
    assert calls == [(BASE, "GET", 10, "ufc-predictor-heartbeat/1.0")]


def test_a_failing_monitor_is_retried_twice_with_a_growing_pause_then_reported_without_its_address():
    calls, pauses = [], []
    ok, what = hb.ping(BASE, opener=opener_recording(calls, [urllib.error.URLError("secret in message " + SECRET)] * 3), sleep=pauses.append)
    assert not ok and what == "URLError" and len(calls) == 3 and pauses == [1, 2]                                                # the class only: the message could hold the address
    calls.clear()
    assert hb.ping(BASE, opener=opener_recording(calls, [TimeoutError(), 500, 200]), sleep=lambda s: None) == (True, "delivered") and len(calls) == 3   # it recovers
    assert hb.ping(BASE, opener=opener_recording([], [503, 503, 503]), sleep=lambda s: None) == (False, "HTTP 503")


def test_it_never_raises_whatever_the_opener_does():
    for exc in (OSError("x"), ValueError("x"), RuntimeError("x"), KeyboardInterrupt.__mro__[1]("x")):
        hb.ping(BASE, opener=opener_recording([], [exc, exc, exc]), sleep=lambda s: None)


def run(argv, env, calls=None, statuses=(200,), capsys=None):
    calls = [] if calls is None else calls
    code = hb.main(argv, env=env, opener=opener_recording(calls, statuses), sleep=lambda s: None)
    return code, calls


def test_the_command_pings_the_right_address_for_each_kind_and_always_exits_zero(capsys):
    assert run([], {"HEARTBEAT_URL": BASE})[1][0][0] == BASE
    assert run(["fail"], {"HEARTBEAT_URL": BASE})[1][0][0] == BASE + "/fail"
    assert run(["start"], {"HEARTBEAT_URL": BASE})[1][0][0] == BASE + "/start"
    for statuses in ([500, 500, 500], [urllib.error.URLError("x")] * 3):
        code, calls = run([], {"HEARTBEAT_URL": BASE}, statuses=statuses)
        assert code == 0 and len(calls) == 3
    out = capsys.readouterr().out
    assert "Heartbeat sent (success)." in out and "Heartbeat sent (fail)." in out and "The heartbeat could not be delivered" in out


def test_a_second_variable_serves_the_weekly_backup():
    other = "https://hc-ping.com/backup-key-123"
    code, calls = run(["--env", "BACKUP_HEARTBEAT_URL"], {"HEARTBEAT_URL": BASE, "BACKUP_HEARTBEAT_URL": other})
    assert code == 0 and [c[0] for c in calls] == [other]
    code, calls = run(["fail", "--env", "BACKUP_HEARTBEAT_URL"], {"BACKUP_HEARTBEAT_URL": other})
    assert [c[0] for c in calls] == [other + "/fail"]


def test_not_setting_the_variable_is_fine_and_says_so(capsys):
    code, calls = run([], {})
    assert code == 0 and calls == [] and "HEARTBEAT_URL is not set, so no heartbeat was sent." in capsys.readouterr().out
    code, calls = run([], {"HEARTBEAT_URL": "   "})
    assert code == 0 and calls == []
    code, calls = run(["--env", "NOPE_URL"], {"HEARTBEAT_URL": BASE})
    assert calls == []


def test_a_bad_address_is_not_used_and_is_never_echoed(capsys):
    for bad in ("http://hc-ping.com/" + SECRET, "not a url " + SECRET, "https://" + SECRET + " with space"):
        code, calls = run([], {"HEARTBEAT_URL": bad})
        out = capsys.readouterr().out
        assert code == 0 and calls == [] and "was not used" in out and SECRET not in out


def test_the_secret_address_never_reaches_the_output_in_any_outcome(capsys):
    for statuses in ([200], [500] * 3, [urllib.error.URLError("boom " + SECRET)] * 3, [ValueError(SECRET)] * 3):
        run(["fail"], {"HEARTBEAT_URL": BASE}, statuses=statuses)
    assert SECRET not in capsys.readouterr().out


def test_it_uses_only_the_standard_library_so_it_works_even_when_the_install_failed():
    tree = ast.parse((ROOT / "pipeline" / "heartbeat.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "os", "sys", "time", "urllib"}, imported
