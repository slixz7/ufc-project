"""
tools/apply_update.py: one zip, one command. It must place exactly what the zip says, change nothing at all when anything is wrong, keep a copy of what it
replaces, undo a half-finished write, and never be talked into writing outside the project.
"""
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import apply_update as au  # noqa: E402

NOW = datetime(2026, 10, 30, 9, 15, 0)
FILES = {"pipeline/new_module.py": b"print('new')\n", "docs/RUNBOOK.md": b"# runbook\r\nwith CRLF\r\n", ".github/workflows/backup.yml": b"name: Backup\n", "upcoming/odds_aliases.csv": "feed_name,card_name\nSeok Hyun Ko,Ko Seok-hyeon\n".encode()}


def project(tmp_path, existing=None):
    root = tmp_path / "proj"
    root.mkdir()
    for rel, data in (existing or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def snapshot(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def make(tmp_path, files=None, name="ufc-update-20261030-1.zip", note="test"):
    z = tmp_path / name
    sha = au.make_update_zip(files if files is not None else FILES, z, note=note)
    return z, sha


def raw_zip(path, manifest_files, members, **manifest_extra):
    """A zip written by hand, to build the broken and hostile ones: manifest_files are the manifest's entries, members what is really inside."""
    manifest = {"project": au.PROJECT, "format": au.FORMAT, "note": "", "files": manifest_files, **manifest_extra}
    with zipfile.ZipFile(path, "w") as zf:
        if manifest is not None:
            zf.writestr(au.MANIFEST_NAME, json.dumps(manifest))
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def entry(rel, data):
    return {"path": rel, "sha256": au._sha256(data), "size": len(data)}


# ---------------------------------------------------------------------------------------------- the normal case
def test_an_update_places_every_file_exactly_as_it_was_made_including_hidden_folders_and_windows_line_endings(tmp_path):
    root = project(tmp_path)
    z, sha = make(tmp_path)
    r = au.apply(z, root, expected_sha256=sha, now=NOW)
    assert sorted(r["new"]) == sorted(FILES) and r["replaced"] == [] and r["unchanged"] == []
    after = snapshot(root)
    for rel, data in FILES.items():
        assert after[rel] == data                                                                                    # byte for byte, the CRLF file too
    assert not [p for p in after if p.endswith(".update_tmp")]                                                      # no temporary files left behind


def test_a_file_that_is_replaced_is_copied_aside_first_and_an_identical_file_is_left_alone(tmp_path):
    old = b"print('old')\n"
    root = project(tmp_path, {"pipeline/new_module.py": old, "docs/RUNBOOK.md": FILES["docs/RUNBOOK.md"], "keep_me.txt": b"mine\n"})
    keep_mtime = (root / "docs" / "RUNBOOK.md").stat().st_mtime_ns
    z, _ = make(tmp_path)
    r = au.apply(z, root, now=NOW)
    assert r["replaced"] == ["pipeline/new_module.py"] and r["unchanged"] == ["docs/RUNBOOK.md"] and sorted(r["new"]) == [".github/workflows/backup.yml", "upcoming/odds_aliases.csv"]
    after = snapshot(root)
    assert after["pipeline/new_module.py"] == FILES["pipeline/new_module.py"]
    assert after[".update_backup/20261030-091500/pipeline/new_module.py"] == old                                    # the old copy is kept
    assert (root / "docs" / "RUNBOOK.md").stat().st_mtime_ns == keep_mtime                                         # an identical file is not even rewritten
    assert after["keep_me.txt"] == b"mine\n"                                                                        # nothing else is touched, and nothing is deleted
    assert not any(k.startswith(".update_backup/") and "RUNBOOK" in k for k in after)                              # and an unchanged file is not backed up


def test_applying_the_same_zip_twice_changes_nothing_the_second_time(tmp_path):
    root = project(tmp_path)
    z, _ = make(tmp_path)
    au.apply(z, root, now=NOW)
    first = snapshot(root)
    r = au.apply(z, root, now=datetime(2026, 10, 30, 9, 16, 0))
    assert r["new"] == [] and r["replaced"] == [] and sorted(r["unchanged"]) == sorted(FILES) and r["backup_dir"] is None
    assert snapshot(root) == first


def test_a_dry_run_says_what_would_happen_and_writes_nothing_at_all(tmp_path, capsys):
    root = project(tmp_path, {"pipeline/new_module.py": b"old\n"})
    before = snapshot(root)
    z, _ = make(tmp_path)
    assert au.main([str(z), "--dry-run"], root=root) == 0
    out = capsys.readouterr().out
    assert "WOULD PLACED   pipeline/new_module.py  (replaced" in out and "WOULD PLACED   docs/RUNBOOK.md  (new)" in out and "nothing was written" in out
    assert snapshot(root) == before and not (root / ".gitignore").exists()


def test_the_backup_folder_is_added_to_gitignore_once_keeping_what_was_there(tmp_path):
    root = project(tmp_path, {".gitignore": b"data/backup/\r\n__pycache__/\r\n"})
    z, _ = make(tmp_path)
    assert au.apply(z, root, now=NOW)["gitignore_updated"] is True
    text = (root / ".gitignore").read_bytes().decode()
    assert text.startswith("data/backup/\r\n__pycache__/\r\n") and text.count(".update_backup/") == 1 and "\r\n.update_backup/\r\n" in text      # same line endings as the file
    assert au.apply(z, root, now=NOW)["gitignore_updated"] is False and (root / ".gitignore").read_text().count(".update_backup") == 1


# ---------------------------------------------------------------------------------------------- the zip itself
def test_the_checksum_from_the_message_must_match_the_zip(tmp_path):
    root = project(tmp_path)
    z, sha = make(tmp_path)
    au.apply(z, root, dry_run=True, expected_sha256=sha.upper())                                                    # either case is fine
    with pytest.raises(au.UpdateError, match="checksum is not the one you were given"):
        au.apply(z, root, expected_sha256="0" * 64)
    assert snapshot(root) == {}


def test_a_changed_file_inside_the_zip_is_refused_and_nothing_at_all_is_written(tmp_path):
    root = project(tmp_path, {"pipeline/first.py": b"keep\n"})
    before = snapshot(root)
    good, bad = b"good\n", b"tampered\n"
    z = raw_zip(tmp_path / "t.zip", [entry("pipeline/first.py", good), entry("pipeline/second.py", good)], {"pipeline/first.py": good, "pipeline/second.py": bad})
    with pytest.raises(au.UpdateError, match=r"pipeline/second.py: does not match its checksum"):
        au.apply(z, root)
    assert snapshot(root) == before                                                                                 # not even the first, good file was written


@pytest.mark.parametrize("what", ["unlisted", "missing", "no_manifest", "bad_manifest", "wrong_project", "wrong_format", "not_a_zip", "no_such_file"])
def test_a_zip_that_is_not_what_it_claims_is_refused_with_a_reason(tmp_path, what):
    root = project(tmp_path)
    data = b"x\n"
    z = tmp_path / "z.zip"
    if what == "unlisted":
        raw_zip(z, [entry("a.py", data)], {"a.py": data, "extra.py": data})
        message = "contains files its manifest does not list: extra.py"
    elif what == "missing":
        raw_zip(z, [entry("a.py", data), entry("b.py", data)], {"a.py": data})
        message = "lists files that are not in the zip: b.py"
    elif what == "no_manifest":
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.py", data)
        message = "has no UPDATE_MANIFEST.json"
    elif what == "bad_manifest":
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(au.MANIFEST_NAME, "{not json")
        message = "UPDATE_MANIFEST.json is damaged"
    elif what == "wrong_project":
        raw_zip(z, [entry("a.py", data)], {"a.py": data}, project="some-other-project")
        message = "is for 'some-other-project'"
    elif what == "wrong_format":
        raw_zip(z, [entry("a.py", data)], {"a.py": data}, format=99)
        message = "is format 99"
    elif what == "not_a_zip":
        z.write_bytes(b"this is not a zip file")
        message = "not a readable zip"
    else:
        z = tmp_path / "absent.zip"
        message = "is not a file"
    with pytest.raises(au.UpdateError, match=message):
        au.apply(z, root)
    assert snapshot(root) == {}


def test_a_zip_with_an_oversized_file_or_too_many_files_is_refused(tmp_path):
    root = project(tmp_path)
    z, _ = make(tmp_path, {"a.py": b"x" * 1000})
    with pytest.raises(au.UpdateError, match="larger than an update should ever be"):
        au.apply(z, root, max_file=500)
    with pytest.raises(au.UpdateError, match="larger than an update should ever be"):
        au.apply(z, root, max_total=999)
    z2, _ = make(tmp_path, {f"f{i}.py": b"x" for i in range(au.MAX_FILES + 1)}, name="many.zip")
    with pytest.raises(au.UpdateError, match="more than 200 files"):
        au.apply(z2, root)
    assert snapshot(root) == {}


# ---------------------------------------------------------------------------------------------- never write outside the project
@pytest.mark.parametrize("rel", ["../evil.py", "a/../../evil.py", "/etc/evil.py", "C:/Windows/evil.py", "C:evil.py", "..\\evil.py", "a\\b.py", ".git/config", ".git/hooks/pre-commit",
                                 ".update_backup/x.py", "data/backup/fights.csv", " pipeline/x.py", "", "a/./b.py\x00"])
def test_a_path_that_could_leave_the_project_or_touch_git_is_refused(tmp_path, rel):
    root = project(tmp_path)
    data = b"x\n"
    z = raw_zip(tmp_path / "evil.zip", [entry(rel, data)], {rel: data})
    with pytest.raises(au.UpdateError):
        au.apply(z, root)
    assert snapshot(root) == {} and not (tmp_path / "evil.py").exists() and not (tmp_path / "etc").exists()


def test_a_symbolic_link_or_directory_entry_in_the_zip_is_refused(tmp_path):
    root = project(tmp_path)
    z = tmp_path / "link.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(au.MANIFEST_NAME, json.dumps({"project": au.PROJECT, "format": au.FORMAT, "files": [entry("a.py", b"x")]}))
        info = zipfile.ZipInfo("a.py")
        info.external_attr = (0o120777 << 16)                                                                       # a symbolic link
        zf.writestr(info, b"x")
    with pytest.raises(au.UpdateError, match="directory entry or a symbolic link"):
        au.apply(z, root)
    d = tmp_path / "dir.zip"
    with zipfile.ZipFile(d, "w") as zf:
        zf.writestr(au.MANIFEST_NAME, json.dumps({"project": au.PROJECT, "format": au.FORMAT, "files": []}))
        zf.writestr("pipeline/", b"")
    with pytest.raises(au.UpdateError, match="directory entry"):
        au.apply(d, root)
    assert snapshot(root) == {}


# ---------------------------------------------------------------------------------------------- a failure part-way is undone
def test_a_write_that_fails_part_way_puts_everything_back_as_it_was(tmp_path, monkeypatch):
    existing = {"pipeline/new_module.py": b"old module\n", "docs/RUNBOOK.md": b"old runbook\n"}
    root = project(tmp_path, existing)
    before = snapshot(root)
    z, _ = make(tmp_path)
    real, calls = os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(au.os, "replace", flaky)
    with pytest.raises(au.UpdateError, match="disk full.*everything already written was put back"):
        au.apply(z, root, now=NOW)
    after = {k: v for k, v in snapshot(root).items() if not k.startswith(".update_backup/") and not k.endswith(".update_tmp")}
    assert after == before                                                                                          # the two replaced files are back to their old content, and no new file remains


def test_a_file_held_for_a_moment_by_a_sync_client_is_retried(tmp_path, monkeypatch):
    root = project(tmp_path)
    z, _ = make(tmp_path, {"a.py": b"x\n"})
    real, calls = os.replace, {"n": 0}

    def once_busy(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("in use")
        return real(src, dst)
    monkeypatch.setattr(au.os, "replace", once_busy)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    assert au.apply(z, root, now=NOW)["new"] == ["a.py"] and (root / "a.py").read_bytes() == b"x\n" and calls["n"] == 2


# ---------------------------------------------------------------------------------------------- finding the zip and the command line
def test_the_newest_matching_zip_in_downloads_is_found_and_other_files_are_ignored(tmp_path):
    d = tmp_path / "Downloads"
    d.mkdir()
    old, new, other = d / "ufc-update-20261029-1.zip", d / "ufc-update-20261030-2.zip", d / "holiday-photos.zip"
    for i, p in enumerate((old, new, other)):
        p.write_bytes(b"x")
        os.utime(p, (1_000_000 + [0, 10, 99][i], 1_000_000 + [0, 10, 99][i]))
    assert au.find_newest_zip(d) == new                                                                             # not the newer file of another name
    assert au.find_newest_zip(tmp_path / "empty") is None


def test_the_command_line_finds_the_zip_applies_it_and_says_what_to_do_next(tmp_path, capsys):
    root = project(tmp_path, {"pipeline/new_module.py": b"old\n"})
    d = tmp_path / "dl"
    d.mkdir()
    z = d / "ufc-update-20261030-1.zip"
    au.make_update_zip(FILES, z, note="alias for the Ko Seok-hyeon line")
    assert au.main(["--downloads", str(d)], root=root) == 0
    out = capsys.readouterr().out
    assert "About this update: alias for the Ko Seok-hyeon line" in out and "PLACED   pipeline/new_module.py  (replaced; the old copy is in .update_backup/)" in out
    assert "Done: 4 file(s) placed, 0 already up to date." in out and "python -m pytest -q tests" in out and "git status" in out


def test_the_command_line_says_nothing_was_changed_when_something_is_wrong(tmp_path, capsys):
    root = project(tmp_path)
    z = raw_zip(tmp_path / "x.zip", [entry("../evil.py", b"x")], {"../evil.py": b"x"})
    assert au.main([str(z)], root=root) == 1
    assert "NOTHING WAS CHANGED." in capsys.readouterr().out and snapshot(root) == {}
    assert au.main(["--downloads", str(tmp_path / "nothing_here")], root=root) == 2
    assert "No ufc-update-*.zip found" in capsys.readouterr().out


def test_a_folder_inside_the_project_that_secretly_points_elsewhere_cannot_be_used_to_write_outside(tmp_path):
    root = project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / "shortcut", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this computer does not allow creating symbolic links")
    data = b"x\n"
    z = raw_zip(tmp_path / "link.zip", [entry("shortcut/evil.py", data)], {"shortcut/evil.py": data})                 # no '..' anywhere: only the resolved location gives it away
    with pytest.raises(au.UpdateError, match="resolves outside the project folder"):
        au.apply(z, root)
    assert not (outside / "evil.py").exists()
