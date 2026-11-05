"""
The front door's last details: status badges for the five workflows, and CITATION.cff (which makes GitHub show "Cite this repository"). The citation file was also validated against the
official CFF 1.2.0 schema with cffconvert; these tests keep it consistent with the rest of the project and need nothing but the standard library (PyYAML is used only if present).
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import credits_ui  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
CFF = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")


def test_the_readme_shows_a_status_badge_for_each_workflow_in_a_row_under_the_title():
    lines = README.splitlines()
    top = lines[lines.index("# UFC Fight Predictor") + 2:][:5]
    expected = [("Tests", "tests.yml"), ("Daily update", "auto-update.yml"), ("Weekly backup", "backup.yml"), ("Lock refresh", "refresh-lock.yml"), ("Live site check", "smoke-test.yml")]
    for line, (name, workflow) in zip(top, expected):
        url = f"{credits_ui.REPO}/actions/workflows/{workflow}"
        assert line == f"[![{name}]({url}/badge.svg)]({url})", line                                                       # the image and the link both point at that workflow
        assert (ROOT / ".github" / "workflows" / workflow).exists(), workflow                                             # and the workflow exists
    assert lines[lines.index("# UFC Fight Predictor") + 7] == ""                                                           # five consecutive lines, so they render as one row


def test_the_badges_cover_every_workflow_the_repository_has_exactly_once():
    names = {p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")}
    assert names == {"tests.yml", "auto-update.yml", "backup.yml", "refresh-lock.yml", "smoke-test.yml"}
    assert all(README.count(f"/workflows/{n}/badge.svg") == 1 for n in names)                                              # a new workflow must get a badge, and none may appear twice (an earlier one was left behind lower down)


def test_the_citation_file_has_what_github_and_the_schema_require():
    assert CFF.startswith("cff-version: 1.2.0\n") and 'message: "' in CFF and 'title: "UFC Fight Predictor"' in CFF and "type: software" in CFF
    assert "\t" not in CFF and CFF.endswith("\n")                                                                          # YAML forbids tabs
    assert re.search(r"^authors:\n  - given-names: Sheldon\n    alias: slixz7\n", CFF, flags=re.M)
    assert re.search(r"^keywords:\n(  - .+\n)+", CFF, flags=re.M)


def test_the_citation_file_says_the_same_things_as_the_rest_of_the_project():
    assert f'repository-code: "{credits_ui.REPO}"' in CFF
    assert 'url: "https://ufc-predictor.streamlit.app/"' in CFF and "https://ufc-predictor.streamlit.app/" in README
    assert "license: MIT" in CFF and (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert "Copyright (c) 2026 Sheldon (slixz7)" in (ROOT / "LICENSE").read_text(encoding="utf-8")                          # the same name and handle
    assert credits_ui.REPO.endswith("/slixz7/ufc-project")                                                                   # the alias is the owner of the repository
    assert README.splitlines()[README.splitlines().index("# UFC Fight Predictor")] in ("# UFC Fight Predictor",) and 'title: "UFC Fight Predictor"' in CFF
    description = re.search(r'Description: "([^"]+)"', RUNBOOK).group(1)                                                     # the About text recorded in the runbook
    assert re.search(r'abstract: "([^"]+)"', CFF).group(1) == description


def test_every_link_in_the_citation_file_is_https_and_the_readme_points_to_it():
    assert all(u.startswith("https://") for u in re.findall(r'"(https?://[^"]+)"', CFF))
    assert "[CITATION.cff](CITATION.cff)" in README and (ROOT / "CITATION.cff").exists()


def test_when_pyyaml_is_available_the_file_parses_to_the_expected_structure():
    yaml = pytest.importorskip("yaml")
    d = yaml.safe_load(CFF)
    assert d["cff-version"] == "1.2.0" and d["title"] == "UFC Fight Predictor" and d["license"] == "MIT" and d["type"] == "software"
    assert d["authors"] == [{"given-names": "Sheldon", "alias": "slixz7"}] and d["repository-code"] == credits_ui.REPO
    assert isinstance(d["keywords"], list) and all(isinstance(k, str) for k in d["keywords"]) and len(d["keywords"]) >= 4
