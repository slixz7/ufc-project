"""
The packages the daily job uses to fetch the results must be COMPLETE once installed: every module their own files import at the top level has to exist.

Why: the kaggle package imports `six` in its own code without declaring it as a dependency. It worked only because python-dateutil happened to depend on six; when the monthly lock refresh
moved python-dateutil to 4.0 (which does not), six stopped being installed and the daily job failed with "No module named 'six'". Nothing tested the Kaggle client (it needs credentials), so
the refresh's passing tests said nothing. This test reads the installed packages' source and checks every unconditional top-level import is importable, so a gap like that fails the tests (and so
the monthly refresh, which runs them on the new versions, and the daily job's offline tests) instead of the job.
"""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKED = ("kaggle", "kagglesdk")                      # what the daily job downloads the results with


def module_level_imports(source: str) -> set:
    """Top-level names imported unconditionally at module level. Imports inside try/if/def are optional or lazy, and relative imports are the package's own."""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_imports(package_dir: Path, own: set) -> dict:
    """{module that cannot be imported: the files (relative to the package) that import it}."""
    missing: dict = {}
    for path in sorted(Path(package_dir).rglob("*.py")):
        try:
            wanted = module_level_imports(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue                                    # not this interpreter's code (a vendored Python 2 file, say): never imported here
        for name in sorted(wanted - own - set(sys.stdlib_module_names)):
            if not importable(name):
                missing.setdefault(name, []).append(path.relative_to(package_dir).as_posix())
    return missing


# ---------------------------------------------------------------------------------------------- the checker itself
def test_only_unconditional_absolute_top_level_imports_count():
    src = ("import os, six\nfrom requests import get\nfrom . import sibling\nfrom .deep import thing\nimport a.b.c\n"
           "try:\n    import optional_one\nexcept ImportError:\n    optional_one = None\nif False:\n    import optional_two\n"
           "def lazy():\n    import optional_three\nclass K:\n    import optional_four\n")
    assert module_level_imports(src) == {"os", "six", "requests", "a"}


def test_a_package_that_imports_something_missing_is_reported_with_the_files(tmp_path):
    pkg = tmp_path / "fakepkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "a.py").write_text("import not_installed_abc\nimport json\n", encoding="utf-8")
    (pkg / "b.py").write_text("try:\n    import optional_abc\nexcept ImportError:\n    pass\n", encoding="utf-8")
    (pkg / "sub" / "d.py").write_text("from not_installed_abc import q\nimport fakepkg\nfrom fakepkg import z\n", encoding="utf-8")
    (pkg / "py2.py").write_text("print 'not python 3'\n", encoding="utf-8")
    (pkg / "bad.py").write_bytes(b"\xff\xfe import nothing")
    assert missing_imports(pkg, own={"fakepkg"}) == {"not_installed_abc": ["a.py", "sub/d.py"]}
    assert missing_imports(tmp_path / "empty", own=set()) == {}


def test_the_standard_library_and_installed_packages_are_never_reported(tmp_path):
    pkg = tmp_path / "p"
    pkg.mkdir()
    (pkg / "a.py").write_text("import sys, os, json, __future__\nimport pytest\nimport ast\n", encoding="utf-8")
    assert missing_imports(pkg, own=set()) == {}


def test_standard_library_modules_that_exist_only_on_another_platform_are_never_reported(tmp_path):
    pkg = tmp_path / "p"
    pkg.mkdir()
    (pkg / "a.py").write_text("import winreg\nimport msvcrt\nimport _winapi\nimport fcntl\nimport termios\n", encoding="utf-8")                  # each exists on Windows or on Unix only
    assert missing_imports(pkg, own=set()) == {}                                                                                      # so none may be mistaken for a missing package


# ---------------------------------------------------------------------------------------------- the installed packages
@pytest.mark.parametrize("name", CHECKED)
def test_every_module_the_installed_package_imports_is_installed(name):
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        pytest.skip(f"{name} is not installed in this environment")
    missing = {}
    for folder in spec.submodule_search_locations:
        missing.update(missing_imports(Path(folder), own=set(CHECKED)))
    assert missing == {}, (f"the installed {name} imports modules that are not installed: {missing}. Declare each in pipeline/requirements.txt, then pin it with "
                           "python tools/refresh_lock.py pin NAME==VERSION --date YYYY-MM-DD --why '...'")


def test_the_requirement_that_fixed_the_incident_is_declared_and_explained_and_pinned():
    reqs = (ROOT / "pipeline" / "requirements.txt").read_text(encoding="utf-8")
    line = next(l for l in reqs.splitlines() if l.startswith("six>="))
    assert "kaggle package imports it without declaring it" in line
    assert any(l.startswith("six==") for l in (ROOT / "constraints-ci.txt").read_text(encoding="utf-8").splitlines())
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## Packages the code needs but does not declare", "No module named 'six'", "python tools/refresh_lock.py pin", "tests/test_installed_imports.py"):
        assert needle in runbook, needle
