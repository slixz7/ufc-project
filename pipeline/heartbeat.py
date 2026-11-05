"""
Tell an outside service that a scheduled job ran, so that a job that did NOT run at all is noticed.

    python pipeline/heartbeat.py            # the job succeeded        (GET <url>)
    python pipeline/heartbeat.py fail       # the job failed            (GET <url>/fail)
    python pipeline/heartbeat.py start      # the job is starting       (GET <url>/start)
    python pipeline/heartbeat.py --env BACKUP_HEARTBEAT_URL      # read a different variable (the weekly backup has its own)

Every other alert fires when a job FAILS. If GitHub pauses the schedule, or the run is cancelled, nothing fails and nothing is said. A "dead man's switch" service (healthchecks.io
and similar) expects a ping on a schedule and emails you when one is missing. The address of the check is the secret HEARTBEAT_URL: whoever has it can send pings for it, so:

  * it must be https, and is never printed, not even inside an error message;
  * not setting it is fine (it says so and does nothing);
  * it always exits 0 and retries a couple of times: a monitor that cannot be reached must not turn a good run into a failed one;
  * the "/fail" and "/start" suffixes are the convention of healthchecks.io; a service without them simply ignores or refuses the extra ping.
It uses only Python's standard library.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_ENV = "HEARTBEAT_URL"
SUFFIX = {"success": "", "fail": "/fail", "start": "/start"}
TIMEOUT_SECONDS = 10
RETRIES = 2


class BadAddress(ValueError):
    pass


def build_url(base: str, kind: str = "success") -> str:
    """The address to ping for this kind of ping. The base must be an https address with a host."""
    if kind not in SUFFIX:
        raise BadAddress(f"unknown kind of ping: {kind!r}")
    base = (base or "").strip()
    parts = urllib.parse.urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or any(ch.isspace() for ch in base):
        raise BadAddress("the heartbeat address must be an https address with a host and no spaces")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + SUFFIX[kind], parts.query, ""))


def ping(url: str, opener=None, retries: int = RETRIES, timeout: float = TIMEOUT_SECONDS, sleep=time.sleep) -> tuple[bool, str]:
    """(delivered, what happened). Never raises and never includes the address."""
    opener = opener or urllib.request.urlopen
    last = "no attempt"
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": "ufc-predictor-heartbeat/1.0"})
            with opener(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True, "delivered"
                last = f"HTTP {response.status}"
        except Exception as exc:                          # no network, a timeout, an HTTP error: all the same here
            last = type(exc).__name__                    # the class only: the text of an error can contain the address
        if attempt < retries:
            sleep(2 ** attempt)
    return False, last


def main(argv=None, env=None, opener=None, sleep=time.sleep) -> int:
    env = os.environ if env is None else env
    args = list(sys.argv[1:] if argv is None else argv)
    name = DEFAULT_ENV
    if "--env" in args:
        i = args.index("--env")
        name = args[i + 1] if i + 1 < len(args) else DEFAULT_ENV
        del args[i:i + 2]
    kind = args[0] if args else "success"
    base = (env.get(name) or "").strip()
    if not base:
        print(f"{name} is not set, so no heartbeat was sent. (Make a check at a dead-man's-switch service and add its ping address as that repository secret.)")
        return 0
    try:
        url = build_url(base, kind)
    except BadAddress as exc:
        print(f"{name} was not used: {exc}.")
        return 0
    ok, what = ping(url, opener=opener, sleep=sleep)
    print(f"Heartbeat sent ({kind})." if ok else f"The heartbeat could not be delivered ({what}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
