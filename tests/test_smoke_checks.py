"""
tools/smoke_test.py: the live-site check. The logic is plain Python, so it is tested here with a FAKE browser (every branch: waking, markers, broken signs, retries); the page list is
checked against the real app so a page that is added or renamed cannot leave the check stale. The browser itself was also run against the real demo site, passing, and against a demo
with a page deliberately broken (it caught it, retried, saved screenshots and exited 1).
"""
import ast
import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import smoke_test as sm  # noqa: E402

TODAY = date(2026, 11, 5)
GOOD = "UFC FIGHT PREDICTOR  Thursday, November 05, 2026 · Results through Oct 31, 2026  Data and credits. Fight data: Kaggle"


class Clock:
    """A fake clock and pause, so no test ever really waits."""
    def __init__(self):
        self.t = 0.0
        self.pauses = []

    def now(self):
        return self.t

    def pause(self, s):
        self.pauses.append(s)
        self.t += s


class Fake:
    """A fake browser. `pages` maps a navigation label to the text that page shows ('' means the page never renders)."""
    def __init__(self, pages=None, home=GOOD, asleep=False, exceptions=None, broken_go=(), clock=None, per_pass=None):
        self.pages, self.home, self.asleep, self.exc, self.broken_go, self.clock = pages or {}, home, asleep, exceptions or {}, set(broken_go), clock
        self.current, self.log, self.per_pass = None, [], per_pass or []
        self.opened = 0

    def open(self, url):
        self.opened += 1
        self.current = None
        self.log.append(("open", url))

    def wake(self, timeout):
        self.log.append(("wake", timeout))
        return self.asleep

    def text(self):
        if self.per_pass and self.opened > len(self.per_pass):
            raise RuntimeError("more passes than scripted")
        if self.per_pass:
            override = self.per_pass[self.opened - 1]
            if self.current in override:
                return override[self.current]
        if self.current is None:
            return self.home
        return self.pages.get(self.current, GOOD + " " + self.current)

    def go(self, label):
        if label in self.broken_go:
            raise TimeoutError("no such link")
        self.current = label
        self.log.append(("go", label))

    def exception_count(self):
        return self.exc.get(self.current, 0)

    def screenshot(self, name):
        self.log.append(("shot", name))

    def close(self):
        self.log.append(("close",))


def good_pages():
    return {label: GOOD + " " + " ".join(markers) for label, markers in sm.PAGES}


# ---------------------------------------------------------------------------------------------- the small checks
def test_markers_are_compared_without_regard_to_case_and_the_missing_ones_are_named():
    assert sm.has_all("THE RECORD THAT COUNTS  how much evidence is there?", ["The record that counts", "How much evidence is there?"]) == []
    assert sm.has_all("nothing here", ["Data and credits.", "Results through"]) == ["Data and credits.", "Results through"]
    assert sm.has_all("", []) == []


def test_every_sign_of_a_broken_page_is_recognised_and_a_healthy_page_has_none():
    for sign in sm.BROKEN:
        assert sm.broken_signs(f"before {sign} after"), sign
    assert sm.broken_signs("couldn't load data right now") == ["the page says \"Couldn't load data\""]                           # case-insensitive
    assert sm.broken_signs(GOOD) == [] and sm.broken_signs(GOOD, exceptions=2) == ["2 Streamlit error box(es) on the page"]
    assert "This app has gone to sleep" in sm.BROKEN and "Couldn't load data" in sm.BROKEN


@pytest.mark.parametrize("text, expected", [("Results through Oct 24, 2026", date(2026, 10, 24)), ("x Results through Jan 1, 2027 y", date(2027, 1, 1)), ("Results through Dec 31, 2025", date(2025, 12, 31)),
                                            ("Results through Foo 24, 2026", None), ("Results through Feb 30, 2026", None), ("no date here", None), ("", None)])
def test_the_headers_results_date_is_read_or_not_at_all(text, expected):
    assert sm.results_through(text) == expected


def test_results_older_than_three_weeks_are_a_problem_and_the_boundary_is_exact():
    assert sm.freshness_problem("Results through Oct 15, 2026", TODAY) is None                                                   # 21 days: fine
    assert sm.freshness_problem("Results through Oct 14, 2026", TODAY) == "the header says results run only through Oct 14, 2026, which is 22 days ago"
    assert sm.freshness_problem("Results through Nov 5, 2026", TODAY) is None and sm.freshness_problem("Results through Nov 9, 2026", TODAY) is None   # a date ahead is not stale
    assert sm.freshness_problem("nothing", TODAY) == "the header does not show a readable 'Results through' date"
    assert sm.MAX_RESULTS_AGE_DAYS == 21


# ---------------------------------------------------------------------------------------------- waiting
def test_polling_returns_as_soon_as_every_marker_is_there_and_gives_the_last_text_when_time_runs_out():
    clock = Clock()
    texts = iter(["", "loading", "loading Data and credits.", "never read"])

    class D:
        def text(self):
            return next(texts)
    assert sm.poll_text(D(), ["Data and credits."], 30, clock.pause, clock.now) == "loading Data and credits." and clock.pauses == [1.0, 1.0]
    clock = Clock()
    slow = type("S", (), {"text": lambda self: "still loading"})()
    assert sm.poll_text(slow, ["Data and credits."], 5, clock.pause, clock.now) == "still loading" and sum(clock.pauses) == 5.0


def test_a_page_that_is_still_re_rendering_does_not_crash_the_wait():
    clock = Clock()
    calls = iter([RuntimeError("detached"), RuntimeError("detached"), "ok Data and credits."])

    class D:
        def text(self):
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v
    assert sm.poll_text(D(), ["Data and credits."], 30, clock.pause, clock.now) == "ok Data and credits."


# ---------------------------------------------------------------------------------------------- one pass
def run_pass(fake, **kw):
    c = fake.clock or Clock()
    return sm.one_pass(fake, "https://site.example/", TODAY, pause=c.pause, clock=c.now, **kw)


def test_a_healthy_site_passes_every_page_in_order():
    out = run_pass(Fake(pages=good_pages(), clock=Clock()))
    assert out.ok and [p.label for p in out.pages] == [l for l, _ in sm.PAGES] and not out.woke and out.site_problems == []


def test_a_sleeping_app_is_woken_and_given_the_longer_wait_before_it_counts_as_down():
    fake = Fake(pages=good_pages(), asleep=True, clock=Clock())
    out = run_pass(fake, wake_timeout=150)
    assert out.ok and out.woke and ("wake", 150) in fake.log
    never = Fake(home="This app has gone to sleep due to inactivity", asleep=True, clock=Clock())
    out = run_pass(never, wake_timeout=150)
    assert not out.ok and out.woke and out.site_problems[0] == "the site did not load: no header after the wait (it had to be woken); the page showed: \"This app has gone to sleep due to inactivity\"" and any("gone to sleep" in p for p in out.site_problems)
    assert out.screenshot == "site" and out.pages == []                                                                           # nothing else is tried on a site that did not load
    assert sum(never.clock.pauses) >= 150                                                                                           # and a woken app really was given the long wait first
    quick = Fake(home="", clock=Clock())
    run_pass(quick, page_timeout=45, wake_timeout=150)
    assert sm.FIRST_LOAD_TIMEOUT <= sum(quick.clock.pauses) < sm.FIRST_LOAD_TIMEOUT + 15                                          # while a site that was not asleep gets the first-load wait (a host that is restarting needs minutes)
    assert sm.FIRST_LOAD_TIMEOUT == 120.0 and sm.RETRY_WAIT == 90.0


def test_a_site_that_loads_nothing_is_reported_once_without_visiting_pages():
    out = run_pass(Fake(home="", clock=Clock()), first_load_timeout=3)
    assert out.site_problems == ["the site did not load: no header after the wait; the page showed: nothing (the page was empty)"] and out.pages == []


def test_stale_results_are_a_site_problem_even_if_every_page_works():
    stale_home = GOOD.replace("Oct 31, 2026", "Sep 1, 2026")
    out = run_pass(Fake(pages=good_pages(), home=stale_home, clock=Clock()))
    assert not out.ok and "which is 65 days ago" in out.site_problems[0] and all(p.ok for p in out.pages)


def test_a_page_missing_what_it_always_shows_names_exactly_what_is_missing():
    pages = good_pages()
    pages["Track record"] = GOOD + " The record that counts"                                                                      # the evidence meter's heading is gone
    out = run_pass(Fake(pages=pages, clock=Clock()))
    bad = [p for p in out.pages if not p.ok]
    assert len(bad) == 1 and bad[0].label == "Track record" and bad[0].problems == ["missing: 'How much evidence is there?'"]


def test_a_page_that_shows_an_error_is_caught_even_when_all_its_text_is_there():
    pages = good_pages()
    pages["Insights"] = pages["Insights"] + " Couldn't load data right now."
    out = run_pass(Fake(pages=pages, exceptions={"Fighters": 1}, clock=Clock()))
    problems = {p.label: p.problems for p in out.pages if not p.ok}
    assert problems == {"Fighters": ["1 Streamlit error box(es) on the page"], "Insights": ["the page says \"Couldn't load data\""]}


def test_a_page_that_cannot_be_opened_is_reported_and_the_others_still_run():
    out = run_pass(Fake(pages=good_pages(), broken_go=["Fighters"], clock=Clock()))
    assert [p.label for p in out.pages] == [l for l, _ in sm.PAGES]                                                               # all six were attempted
    assert [(p.label, p.problems) for p in out.pages if not p.ok] == [("Fighters", ["could not open the page (TimeoutError)"])]


# ---------------------------------------------------------------------------------------------- retries
def test_a_site_that_is_only_restarting_passes_on_the_second_try_and_says_so():
    clock = Clock()
    broken = {label: "Couldn't load data right now." for label, _ in sm.PAGES}
    fake = Fake(pages=good_pages(), clock=clock, per_pass=[broken, {}])
    out = sm.check_site(fake, "https://site.example/", TODAY, retries=1, retry_wait=60, pause=clock.pause, clock=clock.now)
    assert out.ok and out.attempts == 2 and 60 in clock.pauses and ("shot", "failure-first-attempt") in fake.log
    assert "It took 2 passes and the last one succeeded." in sm.summary_markdown(out, "https://site.example/", TODAY)


def test_a_site_that_stays_broken_fails_after_exactly_one_retry():
    clock = Clock()
    broken = {label: "Couldn't load data right now." for label, _ in sm.PAGES}
    fake = Fake(pages=good_pages(), clock=clock, per_pass=[broken, broken])
    out = sm.check_site(fake, "https://site.example/", TODAY, retries=1, retry_wait=60, pause=clock.pause, clock=clock.now)
    assert not out.ok and out.attempts == 2 and fake.opened == 2


def test_no_retry_means_one_pass_and_a_healthy_site_never_waits():
    clock = Clock()
    broken = {label: "Couldn't load data right now." for label, _ in sm.PAGES}
    out = sm.check_site(Fake(pages=good_pages(), clock=clock, per_pass=[broken]), "https://site.example/", TODAY, retries=0, retry_wait=60, pause=clock.pause, clock=clock.now)
    assert not out.ok and out.attempts == 1
    clock = Clock()
    healthy = sm.check_site(Fake(pages=good_pages(), clock=clock), "https://site.example/", TODAY, retries=1, retry_wait=60, pause=clock.pause, clock=clock.now)
    assert healthy.ok and healthy.attempts == 1 and 60 not in clock.pauses


# ---------------------------------------------------------------------------------------------- the summary and the command
def test_the_summary_is_a_readable_table_with_the_problems_in_it():
    out = sm.Outcome(pages=[sm.PageResult("Upcoming", [], 1.2), sm.PageResult("Insights", ["missing: 'What the data says'"], 45.0)], site_problems=["stale"], woke=True, attempts=2)
    md = sm.summary_markdown(out, "https://site.example/", TODAY)
    assert md.startswith("## Live site check, 2026-11-05\n\nhttps://site.example/\n\n**The site has a problem.**\nThe app was asleep and had to be woken.\nIt took 2 passes, and the last one failed too.\n- SITE: stale\n")
    assert "| Upcoming | ok | 1 |" in md and "| Insights | missing: 'What the data says' | 45 |" in md
    assert "**All pages work.**" in sm.summary_markdown(sm.Outcome(pages=[sm.PageResult("Upcoming")]), "u", TODAY)


def drive(argv, fake, today=TODAY, monkeypatch=None):
    clock = Clock()
    fake.clock = clock
    return sm.main(argv, driver_factory=lambda out: fake, today=today, pause=clock.pause, clock=clock.now)


def test_the_command_exits_zero_for_a_healthy_site_and_one_for_a_broken_one_and_always_closes_the_browser(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    ok = Fake(pages=good_pages())
    assert drive(["https://site.example/", "--out", str(tmp_path)], ok) == 0 and ok.log[-1] == ("close",)
    assert "All pages work" in capsys.readouterr().out and "All pages work" in summary.read_text(encoding="utf-8")
    bad = Fake(pages={**good_pages(), "Game": "Couldn't load data"})
    assert drive(["https://site.example/", "--retries", "0"], bad) == 1
    assert ("shot", "failure") in bad.log and bad.log[-1] == ("close",)                                                           # a screenshot of the failure, and the browser is closed


def test_only_an_https_site_or_a_local_one_may_be_checked(capsys):
    for url in ("http://site.example/", "ftp://x", "site.example", "javascript:alert(1)"):
        assert drive([url], Fake()) == 2
    assert "must be https" in capsys.readouterr().out
    for url in ("http://127.0.0.1:8501/", "http://localhost:8501/", "https://ufc-predictor.streamlit.app/"):
        assert drive([url, "--retries", "0"], Fake(pages=good_pages())) == 0


def test_the_browser_is_closed_even_when_the_check_itself_crashes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something nobody planned for")
    monkeypatch.setattr(sm, "check_site", boom)                                                                                   # a crash the tool does not handle: the browser must still be closed
    fake = Fake()
    with pytest.raises(RuntimeError):
        drive(["https://site.example/", "--retries", "0"], fake)
    assert fake.log[-1] == ("close",)


# ---------------------------------------------------------------------------------------------- it fits the real site
def test_the_tool_needs_only_the_standard_library_to_be_imported_the_browser_driver_loads_lazily():
    tree = ast.parse((ROOT / "tools" / "smoke_test.py").read_text(encoding="utf-8"))
    top = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert top <= {"__future__", "argparse", "os", "re", "sys", "time", "dataclasses", "datetime", "pathlib"}, top
    assert "from playwright.sync_api import sync_playwright" in (ROOT / "tools" / "smoke_test.py").read_text(encoding="utf-8")


def test_the_pinned_browser_requirement_is_an_exact_version():
    lines = [l for l in (ROOT / "tools" / "smoke-requirements.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9._-]+==\d+(\.\d+)+", l) for l in lines)


def test_the_error_text_it_looks_for_is_text_the_app_really_uses():
    app = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "Couldn't load data" in app and "Data and credits." in (ROOT / "app" / "credits_ui.py").read_text(encoding="utf-8")
    assert "Results through" in app and "How much evidence is there?" in (ROOT / "app" / "evidence_ui.py").read_text(encoding="utf-8")
    assert "What the data says" in (ROOT / "app" / "insights_ui.py").read_text(encoding="utf-8") or "What the data says" in app


@pytest.mark.skipif(not os.getenv("TEST_PG_ADMIN_URL"), reason="needs the demo database (set TEST_PG_ADMIN_URL)")
def test_every_page_in_the_list_exists_in_the_real_app_with_the_text_the_check_requires(tmp_path):
    pytest.importorskip("streamlit")
    sys.path.insert(0, str(ROOT / "tests"))
    from test_demo import _app, _plain
    import sqlalchemy as sa, uuid, build_demo  # noqa: E401
    sys.path.insert(0, str(ROOT / "demo"))
    admin = sa.create_engine(os.environ["TEST_PG_ADMIN_URL"], isolation_level="AUTOCOMMIT")
    name = f"smk_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE DATABASE {name}")
    try:
        url = sa.engine.make_url(os.environ["TEST_PG_ADMIN_URL"]).set(database=name).render_as_string(hide_password=False)
        info = build_demo.build(url, today=date(2026, 10, 29), size="small", log=lambda *a: None)
        with _app(info) as at:
            assert list(at.radio[0].options) == [label for label, _ in sm.PAGES]                                                   # the navigation and the list agree, in order
            for label, markers in sm.PAGES:
                at = at.radio[0].set_value(label).run()
                page = re.sub(r"\s+", " ", _plain(at))
                assert not at.exception, label
                assert sm.has_all(page, sm.EVERY_PAGE + list(markers)) == [], (label, sm.has_all(page, sm.EVERY_PAGE + list(markers)))
                assert sm.broken_signs(page, len(at.exception)) == [], label
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            for r in build_demo.login_names(name):
                c.exec_driver_sql(f"DROP ROLE IF EXISTS {r}")


@pytest.mark.parametrize("text, expected", [
    ("", "nothing (the page was empty)"), ("   \n\t ", "nothing (the page was empty)"), ("Your app is in the oven", '"Your app is in the oven"'),
    ("a\n\n  b\tc", '"a b c"'), ("pipe | char", '"pipe / char"'), ("x" * 160, '"' + "x" * 160 + '"'), ("x" * 161, '"' + "x" * 160 + '..."'), (None, '"None"')])
def test_a_failure_report_quotes_what_the_page_showed_on_one_line_and_cuts_it_short(text, expected):
    assert sm.snippet(text) == expected


def test_the_report_says_what_a_restarting_host_showed_instead_of_the_site():
    clock = Clock()
    fake = Fake(home="Please wait while your app is being deployed", clock=clock)
    out = sm.check_site(fake, "https://site.example/", TODAY, retries=0, pause=clock.pause, clock=clock.now)
    md = sm.summary_markdown(out, "https://site.example/", TODAY)
    assert '- SITE: the site did not load: no header after the wait; the page showed: "Please wait while your app is being deployed"' in md


def test_a_site_that_is_slow_to_start_but_comes_up_within_two_minutes_passes():
    clock = Clock()

    class Starting(Fake):
        def text(self):
            return super().text() if clock.t >= 100 else "Your app is in the oven"                                               # shows the host's loading page for 100 seconds, then the real pages
    out = sm.check_site(Starting(pages=good_pages(), clock=clock), "https://site.example/", TODAY, retries=0, pause=clock.pause, clock=clock.now)
    assert out.ok and out.attempts == 1


def test_a_site_the_browser_cannot_even_reach_is_a_failure_to_report_not_a_crash():
    class Unreachable(Fake):
        def open(self, url):
            raise TimeoutError("Page.goto: Timeout 60000ms exceeded.\nCall log:\n  - navigating to the site")
    clock = Clock()
    out = sm.check_site(Unreachable(clock=clock), "https://site.example/", TODAY, retries=1, retry_wait=90, pause=clock.pause, clock=clock.now)
    assert not out.ok and out.attempts == 2 and 90 in clock.pauses and out.pages == []                                          # still retried once, like any other failure
    assert out.site_problems == ['could not open the site (TimeoutError: "Page.goto: Timeout 60000ms exceeded.")']
    assert "- SITE: could not open the site (TimeoutError:" in sm.summary_markdown(out, "https://site.example/", TODAY)


def test_waking_that_raises_is_handled_the_same_way_and_the_command_still_exits_one_with_a_report(capsys):
    class BadWake(Fake):
        def wake(self, timeout):
            raise RuntimeError("the page closed")
    assert drive(["https://site.example/", "--retries", "0"], BadWake(pages=good_pages())) == 1
    out = capsys.readouterr().out
    assert "**The site has a problem.**" in out and 'could not open the site (RuntimeError: "the page closed")' in out
