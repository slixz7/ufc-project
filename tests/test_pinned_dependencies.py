"""
The dependency lock (constraints-ci.txt): the exact versions the GitHub workflows install, proven by a full test run. The lock must be well formed, must cover every
package the requirements files name, must respect their minimums, and every workflow must actually install through it.
"""
import importlib.metadata as md
import os
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "constraints-ci.txt"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    out = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9._!+-]+", line.strip()), f"not an exact pin: {line!r}"
        name, version = line.strip().split("==")
        assert _norm(name) not in out, f"{name} is pinned twice"
        out[_norm(name)] = version
    return out


def _direct_requirements() -> list[Requirement]:
    reqs = []
    for f in ("pipeline/requirements.txt", "app/requirements.txt"):
        for line in (ROOT / f).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                reqs.append(Requirement(line))
    reqs.append(Requirement("pyflakes"))                                                                              # the workflows install it beside the requirements
    return reqs


def test_the_lock_is_well_formed_every_line_an_exact_pin_and_a_real_number_of_packages():
    pins = _pins()
    assert len(pins) >= 40 and all(re.match(r"\d", v) for v in pins.values())
    assert "PROVEN" in LOCK.read_text(encoding="utf-8") and "Do not edit it by hand" in LOCK.read_text(encoding="utf-8")


def test_every_package_the_requirements_name_is_in_the_lock_at_a_version_they_allow():
    pins = _pins()
    for req in _direct_requirements():
        assert _norm(req.name) in pins, f"{req.name} is required but not locked"
        assert req.specifier.contains(Version(pins[_norm(req.name)]), prereleases=True), f"the locked {req.name} {pins[_norm(req.name)]} breaks the requirement {req}"


def test_the_lock_names_the_packages_the_tests_and_the_web_app_depend_on():
    pins = _pins()
    for name in ("pandas", "numpy", "scikit-learn", "sqlalchemy", "psycopg2-binary", "streamlit", "pytest", "pyflakes", "requests", "beautifulsoup4", "kaggle"):
        assert name in pins, name


@pytest.mark.parametrize("workflow", ["tests.yml", "auto-update.yml", "backup.yml"])
def test_every_workflow_installs_through_the_lock_and_rebuilds_its_cache_when_the_lock_changes(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    installs = [l.strip() for l in text.splitlines() if "pip install" in l and not l.strip().startswith("#")]
    assert installs, workflow
    for line in installs:
        assert "-c constraints-ci.txt" in line, f"{workflow}: this install bypasses the lock: {line}"
    assert re.search(r"cache-dependency-path: \|\n\s+pipeline/requirements.txt\n\s+constraints-ci.txt", text)


@pytest.mark.parametrize("workflow", ["tests.yml", "auto-update.yml"])
def test_the_workflows_that_run_the_tests_also_check_the_installed_versions_equal_the_lock(workflow):
    assert 'UFC_EXPECT_LOCKED: "1"' in (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")


@pytest.mark.skipif(os.getenv("UFC_EXPECT_LOCKED") != "1", reason="only checked where the packages were installed through the lock (the workflows set UFC_EXPECT_LOCKED)")
def test_the_versions_really_installed_here_are_exactly_the_locked_ones():
    wrong = []
    for name, version in _pins().items():
        try:
            installed = md.version(name)
        except md.PackageNotFoundError:
            continue                                                                                                 # a package this job does not install (the daily job has no Streamlit)
        if installed != version:
            wrong.append(f"{name}: installed {installed}, locked {version}")
    assert not wrong, wrong
