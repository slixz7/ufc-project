"""tools/refresh_lock.py pin: add or move ONE exact pin in the lock, keeping it sorted and well-formed and recording why in the header (the file is otherwise only ever rebuilt from a frozen environment)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import refresh_lock as rl  # noqa: E402

HEAD = "# a header line\n#\n# another header line\n"
LOCK = HEAD + "beautifulsoup4==4.14.3\nkaggle==2.2.5\npandas==3.1.1\nSQLAlchemy==2.1.0\n"
WHY = "kaggle imports six without declaring it"


def pin(text, spec="six==1.17.0", date="2026-11-06", why=WHY):
    return rl.pin_one(text, spec, date, why)


def names(text):
    return list(rl.parse_pins(text))


def test_a_missing_package_is_added_in_sorted_place_with_a_note_in_the_header_and_nothing_else_moves():
    new, said = pin(LOCK)
    assert said == "six==1.17.0 added as a pin in the lock"
    assert names(new) == ["beautifulsoup4", "kaggle", "pandas", "six", "sqlalchemy"]                                                   # sorted without regard to case
    assert [v for k, v in rl.parse_pins(new).items() if k != "six"] == [v for v in rl.parse_pins(LOCK).values()]                       # the others are exactly as they were
    top, body = new.split("beautifulsoup4==")[0], new[len(new.split("beautifulsoup4==")[0]):]
    assert top.startswith(HEAD) and top.count("# Pinned by hand") == 1
    assert top.splitlines()[-1] == f"# Pinned by hand on 2026-11-06: six==1.17.0 ({WHY}). The next monthly refresh regenerates this file, including it."
    assert body.splitlines() == ["beautifulsoup4==4.14.3", "kaggle==2.2.5", "pandas==3.1.1", "six==1.17.0", "SQLAlchemy==2.1.0"]


def test_pinning_what_is_already_pinned_at_that_version_changes_nothing_and_says_so():
    new, _ = pin(LOCK)
    again, said = pin(new)
    assert again == new and said == "six==1.17.0 is already pinned; nothing changed" and again.count("# Pinned by hand") == 1
    same, said = pin(LOCK, spec="Pandas==3.1.1")                                                                                     # names are compared the way pip compares them
    assert same == LOCK and "already pinned" in said


def test_a_different_version_moves_the_pin_keeps_its_spelling_and_adds_one_more_note():
    new, said = pin(LOCK, spec="sqlalchemy==2.0.9", why="the new release broke the pages")
    assert said == "SQLAlchemy==2.0.9 moved to a pin in the lock" and "SQLAlchemy==2.0.9\n" in new and "SQLAlchemy==2.1.0" not in new
    both, _ = pin(new)
    assert both.count("# Pinned by hand") == 2 and len(rl.parse_pins(both)) == 5


@pytest.mark.parametrize("spec", ["six", "six>=1.16", "six==", "==1.0", "six==1.0 extra", "-e .", "git+https://example.org/x.git", "six==1.0\nkaggle==9", "", None, "six==abc!!", "../x==1"])
def test_only_an_exact_pin_is_accepted(spec):
    with pytest.raises(rl.LockError):
        pin(LOCK, spec=spec)


@pytest.mark.parametrize("date, why", [("yesterday", WHY), ("2026-13", WHY), ("", WHY), ("2026-11-06", ""), ("2026-11-06", "   "), ("2026-11-06", None)])
def test_a_real_date_and_a_reason_are_required(date, why):
    with pytest.raises(rl.LockError):
        pin(LOCK, date=date, why=why)


def test_the_reason_stays_on_one_line_and_is_cut_short():
    new, _ = pin(LOCK, why="line one\nline two\r\n# sneaky\n" + "x" * 300)
    note = [l for l in new.splitlines() if l.startswith("# Pinned by hand")]
    assert len(note) == 1 and "\n" not in note[0] and len(note[0]) < 260
    assert len(rl.parse_pins(new)) == 5                                                                                                 # nothing the reason said became a pin


def test_a_lock_that_is_not_well_formed_is_refused_not_repaired():
    with pytest.raises(rl.LockError):
        pin(HEAD + "pandas==1\npandas==2\n")
    with pytest.raises(rl.LockError):
        pin(HEAD + "pandas>=1\n")


def test_windows_line_endings_are_kept():
    crlf = LOCK.replace("\n", "\r\n")
    new, _ = pin(crlf)
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "") and names(new)[3] == "six"


def test_the_command_edits_the_file_in_place_prints_what_it_did_and_a_second_run_changes_nothing(tmp_path, capsys):
    lock = tmp_path / "constraints-ci.txt"
    lock.write_bytes(LOCK.encode())
    argv = ["pin", "six==1.17.0", "--date", "2026-11-06", "--why", WHY, "--lock", str(lock)]
    assert rl.main(argv) == 0 and "six==1.17.0 added as a pin in the lock" in capsys.readouterr().out
    first = lock.read_bytes()
    assert b"six==1.17.0" in first
    assert rl.main(argv) == 0 and "already pinned" in capsys.readouterr().out and lock.read_bytes() == first


def test_the_command_reports_problems_without_a_traceback_and_leaves_the_file_alone(tmp_path, capsys):
    lock = tmp_path / "constraints-ci.txt"
    lock.write_bytes(LOCK.encode())
    assert rl.main(["pin", "six>=1", "--date", "2026-11-06", "--why", WHY, "--lock", str(lock)]) == 2
    assert "refresh_lock: give an exact pin" in capsys.readouterr().err and lock.read_bytes() == LOCK.encode()
    assert rl.main(["pin", "six==1.17.0", "--date", "2026-11-06", "--why", WHY, "--lock", str(tmp_path / "missing.txt")]) == 2


def test_the_real_lock_stays_valid_after_a_pin_and_keeps_every_other_line():
    real = (ROOT / "constraints-ci.txt").read_text(encoding="utf-8")
    new, _ = pin(real, spec="zzz-test-package==1.0", why="a test")
    before, after = rl.parse_pins(real), rl.parse_pins(new)
    assert len(after) == len(before) + 1 and all(after[k] == v for k, v in before.items())
    assert names(new) == sorted(names(new), key=rl.norm)
