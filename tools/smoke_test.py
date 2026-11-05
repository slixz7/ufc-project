#!/usr/bin/env python3
"""
Open the LIVE site in a real browser and check that every page works.

    python tools/smoke_test.py https://ufc-predictor.streamlit.app/ [--out DIR] [--retries 1] [--retry-wait 60]

What it checks, for each of the six pages:
  * the page opens (a sleeping app is woken first: the button "Yes, get this app back up!");
  * the text that page always shows is there (the footer, and a heading or two that only appears when the page's data loaded);
  * none of the signs of a broken page: "Couldn't load data", a Python traceback, Streamlit's own error boxes, the hosting platform's error page;
and, from the header, that "Results through <date>" is not more than 21 days old (the data has not silently stopped).
If anything fails it waits and tries the whole pass once more before giving up (a site that is just restarting after a deploy is not an outage), writes a run summary and a
screenshot of the failing page, and exits 1. It only READS the site: no sign-in, no clicks beyond the navigation and the wake-up button.

The checking logic is plain Python and is tested without a browser (tests/test_smoke_checks.py); the browser part is a thin Playwright driver. Which page shows what is checked
against the real app in the test suite too (a page that is added or renamed makes a test fail until PAGES here is updated).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# (the navigation label, text that page shows whenever its data loaded). Text is compared without regard to case: the browser reports the capitals the stylesheet applies.
PAGES = [
    ("Upcoming", []),                                    # its content depends on whether a card is announced; the footer and header are still required
    ("Past events", []),
    ("Fighters", ["Strikes landed / min"]),
    ("Track record", ["The record that counts", "How much evidence is there?"]),
    ("Insights", ["What the data says", "Fights counted since"]),
    ("Game", ["Beat the model"]),
]
EVERY_PAGE = ["UFC Fight Predictor", "Data and credits.", "Results through"]
BROKEN = ("Couldn't load data", "Traceback (most recent call last)", "StreamlitAPIException", "Error running app", "Oh no.", "Something went wrong",
          "ModuleNotFoundError", "This app has gone to sleep")
MAX_RESULTS_AGE_DAYS = 21
RESULTS_THROUGH = re.compile(r"Results through ([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def has_all(text: str, markers) -> list[str]:
    """The markers that are NOT in the text (case-insensitive)."""
    low = text.lower()
    return [m for m in markers if m.lower() not in low]


def broken_signs(text: str, exceptions: int = 0) -> list[str]:
    found = [f"the page says {s!r}" for s in BROKEN if s.lower() in text.lower()]
    if exceptions:
        found.append(f"{exceptions} Streamlit error box(es) on the page")
    return found


def results_through(text: str) -> date | None:
    m = RESULTS_THROUGH.search(text)
    if not m or m.group(1) not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
    except ValueError:
        return None


def freshness_problem(text: str, today: date, max_age: int = MAX_RESULTS_AGE_DAYS) -> str | None:
    d = results_through(text)
    if d is None:
        return "the header does not show a readable 'Results through' date"
    age = (today - d).days
    return f"the header says results run only through {d:%b} {d.day}, {d.year}, which is {age} days ago" if age > max_age else None


@dataclass
class PageResult:
    label: str
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Outcome:
    pages: list[PageResult] = field(default_factory=list)
    site_problems: list[str] = field(default_factory=list)
    woke: bool = False
    attempts: int = 1
    screenshot: str | None = None

    @property
    def ok(self) -> bool:
        return not self.site_problems and all(p.ok for p in self.pages)


def poll_text(driver, markers, timeout: float, pause=time.sleep, clock=time.monotonic, step: float = 1.0) -> str:
    """The page's text once every marker is in it, or the last text read when the time is up."""
    deadline = clock() + timeout
    text = ""
    while True:
        try:
            text = driver.text()
        except Exception:                                  # the page is still loading or re-rendering: try again
            text = ""
        if text and not has_all(text, markers):
            return text
        if clock() >= deadline:
            return text
        pause(step)


def one_pass(driver, url: str, today: date, pages=PAGES, page_timeout: float = 45.0, wake_timeout: float = 150.0, pause=time.sleep, clock=time.monotonic) -> Outcome:
    out = Outcome()
    driver.open(url)
    out.woke = bool(driver.wake(wake_timeout))
    first = poll_text(driver, EVERY_PAGE[:1], wake_timeout if out.woke else page_timeout, pause, clock)
    if has_all(first, EVERY_PAGE[:1]):
        out.site_problems.append("the site did not load: no header after the wait" + (" (it had to be woken)" if out.woke else ""))
        out.site_problems += broken_signs(first, 0)
        out.screenshot = "site"
        return out
    stale = freshness_problem(first, today)
    if stale:
        out.site_problems.append(stale)
    for label, markers in pages:
        t0 = clock()
        res = PageResult(label)
        try:
            driver.go(label)
        except Exception as exc:
            res.problems.append(f"could not open the page ({type(exc).__name__})")
            out.pages.append(res)
            continue
        text = poll_text(driver, EVERY_PAGE + list(markers), page_timeout, pause, clock)
        missing = has_all(text, EVERY_PAGE + list(markers))
        if missing:
            res.problems.append("missing: " + ", ".join(repr(m) for m in missing))
        try:
            n_exc = driver.exception_count()
        except Exception:
            n_exc = 0
        res.problems += broken_signs(text, n_exc)
        res.seconds = clock() - t0
        out.pages.append(res)
    return out


def check_site(driver, url: str, today: date, retries: int = 1, retry_wait: float = 60.0, pause=time.sleep, clock=time.monotonic, **kw) -> Outcome:
    """A pass; if anything fails, wait and make up to `retries` more complete passes (a site that is just restarting is not an outage). The last pass is the result."""
    out = one_pass(driver, url, today, pause=pause, clock=clock, **kw)
    attempts = 1
    while not out.ok and attempts <= retries:
        try:
            driver.screenshot("failure-first-attempt")
        except Exception:
            pass
        pause(retry_wait)
        out = one_pass(driver, url, today, pause=pause, clock=clock, **kw)
        attempts += 1
    out.attempts = attempts
    return out


def summary_markdown(out: Outcome, url: str, today: date) -> str:
    lines = [f"## Live site check, {today.isoformat()}", "", f"{url}", ""]
    lines.append("**All pages work.**" if out.ok else "**The site has a problem.**")
    if out.woke:
        lines.append("The app was asleep and had to be woken.")
    if out.attempts > 1:
        lines.append(f"It took {out.attempts} passes" + (" and the last one succeeded." if out.ok else ", and the last one failed too."))
    for p in out.site_problems:
        lines.append(f"- SITE: {p}")
    lines += ["", "| Page | Result | Seconds |", "|---|---|---|"]
    for p in out.pages:
        lines.append(f"| {p.label} | {'ok' if p.ok else '; '.join(p.problems)} | {p.seconds:.0f} |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------
# the browser
# ----------------------------------------------------------------------------------------------
class PlaywrightDriver:
    """A thin wrapper over Playwright; the app may sit in the page itself or (on Streamlit's hosting) in an iframe."""

    def __init__(self, out_dir: Path | None = None):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self.page = self._browser.new_page(viewport={"width": 1280, "height": 1600})
        self.out_dir = Path(out_dir) if out_dir else None

    def _app(self):
        for frame in self.page.frames:
            try:
                if frame.locator('[data-testid="stApp"]').count():
                    return frame
            except Exception:
                continue
        return self.page

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def wake(self, timeout: float) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            button = self.page.get_by_role("button", name=re.compile("get this app back up", re.I))
            if button.count():
                button.first.click()
                return True
            if self._app().locator('[data-testid="stApp"]').count() and self.page.get_by_text("UFC Fight Predictor", exact=False).count():
                return False
            time.sleep(1)
        return False

    def text(self) -> str:
        return self._app().locator("body").inner_text(timeout=15000)

    def go(self, label: str) -> None:
        self._app().get_by_text(label, exact=True).first.click(timeout=15000)

    def exception_count(self) -> int:
        return self._app().locator('[data-testid="stException"]').count()

    def screenshot(self, name: str) -> None:
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.out_dir / f"{name}.png"), full_page=False)

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def main(argv=None, driver_factory=None, today: date | None = None, pause=time.sleep, clock=time.monotonic) -> int:
    ap = argparse.ArgumentParser(description="Check every page of the live site in a real browser")
    ap.add_argument("url")
    ap.add_argument("--out", type=Path, default=None, help="a folder for the screenshot of a failing page")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--retry-wait", type=float, default=60.0)
    args = ap.parse_args(argv)
    if not args.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        print("The site address must be https (or a local address for testing).")
        return 2
    today = today or date.today()
    driver = (driver_factory or PlaywrightDriver)(args.out) if driver_factory else PlaywrightDriver(args.out)
    try:
        out = check_site(driver, args.url, today, retries=args.retries, retry_wait=args.retry_wait, pause=pause, clock=clock)
        if not out.ok:
            try:
                driver.screenshot("failure")
            except Exception:
                pass
    finally:
        try:
            driver.close()
        except Exception:
            pass
    text = summary_markdown(out, args.url, today)
    print(text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    return 0 if out.ok else 1


if __name__ == "__main__":
    sys.exit(main())
