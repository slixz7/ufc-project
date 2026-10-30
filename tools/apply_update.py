"""
Apply an update zip: one download, one command, nothing placed unless everything checks out.

    python tools/apply_update.py                      # the newest ufc-update-*.zip in your Downloads folder
    python tools/apply_update.py PATH_TO_ZIP          # a particular zip
    python tools/apply_update.py --sha256 HASH        # also check the zip itself against the checksum in the message that came with it
    python tools/apply_update.py --dry-run            # say what would happen; write nothing

What it does, in this order:
  1. checks the zip: it has an UPDATE_MANIFEST.json for this project; every file in the zip is listed in it and every listed file is in the zip;
     every file's SHA-256 and size match; every destination is inside this project folder (no absolute paths, no "..", never inside .git);
  2. only then writes. Each file it replaces is first copied to .update_backup/<date-time>/ (that folder is added to .gitignore), so nothing is lost;
  3. if a write fails part-way, it puts everything back as it was.
It never deletes a file of yours, never runs anything from the zip, and never touches a file that is already identical. Afterwards run
`python -m pytest -q tests` and look at `git status`.

The same module makes the zips (make_update_zip), so the format is defined in one place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROJECT = "ufc-project"
FORMAT = 1
MANIFEST_NAME = "UPDATE_MANIFEST.json"
ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ".update_backup"
MAX_FILES = 200
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
NEVER_WRITE = (".git", BACKUP_DIR, "data/backup")           # folders an update never writes into


class UpdateError(Exception):
    """Something is wrong with the zip or the destination. Nothing has been changed (or, if a write failed, everything has been put back)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------------------------
# making a zip
# ----------------------------------------------------------------------------------------------
def make_update_zip(files: dict[str, Path | bytes], out: Path, note: str = "") -> str:
    """`files` maps a path inside the project (forward slashes) to the file to copy there (a Path, or the bytes). Returns the zip's own SHA-256."""
    entries, payload = [], {}
    for rel, src in sorted(files.items()):
        data = Path(src).read_bytes() if isinstance(src, (str, Path)) else bytes(src)
        payload[rel] = data
        entries.append({"path": rel, "sha256": _sha256(data), "size": len(data)})
    manifest = {"project": PROJECT, "format": FORMAT, "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "note": note, "files": entries}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        for rel, data in payload.items():
            zf.writestr(rel, data)
    return _sha256(out.read_bytes())


# ----------------------------------------------------------------------------------------------
# checking a zip
# ----------------------------------------------------------------------------------------------
def safe_destination(root: Path, rel: str) -> Path:
    """The place inside `root` where `rel` goes, or UpdateError. No absolute paths, drive letters, backslashes, '..', or protected folders."""
    if not rel or rel != rel.strip() or "\\" in rel or "\x00" in rel or re.match(r"^[A-Za-z]:", rel):
        raise UpdateError(f"{rel!r}: not a plain relative path")
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or "." in p.parts or len(p.parts) == 0:
        raise UpdateError(f"{rel!r}: must stay inside the project folder")
    for protected in NEVER_WRITE:
        q = PurePosixPath(protected)
        if p.parts[:len(q.parts)] == q.parts:
            raise UpdateError(f"{rel!r}: an update never writes into {protected}")
    dest = (Path(root) / Path(*p.parts)).resolve()
    if Path(root).resolve() not in dest.parents:
        raise UpdateError(f"{rel!r}: resolves outside the project folder")
    return dest


def read_and_verify(zip_path: Path, expected_sha256: str | None = None, root: Path = ROOT, max_file: int = MAX_FILE_BYTES, max_total: int = MAX_TOTAL_BYTES) -> tuple[dict, dict[str, bytes]]:
    """Everything the zip claims, checked. Returns (manifest, {path: bytes}) or raises UpdateError."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise UpdateError(f"{zip_path} is not a file")
    if expected_sha256 and _sha256(zip_path.read_bytes()) != expected_sha256.strip().lower():
        raise UpdateError("the zip's checksum is not the one you were given: it is damaged or not the file that was sent. Download it again.")
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"{zip_path.name} is not a readable zip ({exc})")
    with zf:
        names = [i for i in zf.infolist() if not i.filename.endswith("/")]
        if any(i.filename.endswith("/") or (i.external_attr >> 28) == 0xA for i in zf.infolist()):       # directories are implied; a symbolic link is never accepted
            raise UpdateError("the zip contains a directory entry or a symbolic link; only plain files are accepted")
        if MANIFEST_NAME not in {i.filename for i in names}:
            raise UpdateError(f"the zip has no {MANIFEST_NAME}: it was not made for this tool")
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            listed = {e["path"]: e for e in manifest["files"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise UpdateError(f"{MANIFEST_NAME} is damaged ({exc})")
        if manifest.get("project") != PROJECT:
            raise UpdateError(f"this zip is for {manifest.get('project')!r}, not {PROJECT!r}")
        if manifest.get("format") != FORMAT:
            raise UpdateError(f"this zip is format {manifest.get('format')!r}; this tool reads format {FORMAT}. Update the tool first.")
        in_zip = {i.filename: i for i in names if i.filename != MANIFEST_NAME}
        if len(listed) > MAX_FILES or len(in_zip) > MAX_FILES:
            raise UpdateError(f"more than {MAX_FILES} files")
        if set(in_zip) - set(listed):
            raise UpdateError("the zip contains files its manifest does not list: " + ", ".join(sorted(set(in_zip) - set(listed))[:5]))
        if set(listed) - set(in_zip):
            raise UpdateError("the manifest lists files that are not in the zip: " + ", ".join(sorted(set(listed) - set(in_zip))[:5]))
        if sum(i.file_size for i in in_zip.values()) > max_total or any(i.file_size > max_file for i in in_zip.values()):
            raise UpdateError("the zip is larger than an update should ever be")
        out: dict[str, bytes] = {}
        for rel, entry in sorted(listed.items()):
            safe_destination(root, rel)
            data = zf.read(rel)
            if len(data) != entry.get("size") or _sha256(data) != entry.get("sha256"):
                raise UpdateError(f"{rel}: does not match its checksum in the manifest (changed or damaged)")
            out[rel] = data
    return manifest, out


# ----------------------------------------------------------------------------------------------
# applying it
# ----------------------------------------------------------------------------------------------
def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".update_tmp")
    tmp.write_bytes(data)
    for attempt in range(4):                                  # a synced folder (OneDrive) can hold a file for a moment
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 3:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.3)


def _ensure_gitignore(root: Path) -> bool:
    """Make sure .gitignore ignores the backup folder, by appending to the file's own bytes so its existing lines and line endings are not touched."""
    gi = Path(root) / ".gitignore"
    raw = gi.read_bytes() if gi.exists() else b""
    text = raw.decode("utf-8", errors="replace")
    if any(line.strip().rstrip("/") == BACKUP_DIR for line in text.splitlines()):
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    lead = "" if not text or text.endswith("\n") else nl
    gi.write_bytes(raw + f"{lead}# copies of files the update tool replaced{nl}{BACKUP_DIR}/{nl}".encode("utf-8"))
    return True


def apply(zip_path: Path, root: Path = ROOT, dry_run: bool = False, expected_sha256: str | None = None, now: datetime | None = None, log=print, **limits) -> dict:
    root = Path(root)
    manifest, files = read_and_verify(zip_path, expected_sha256, root, **limits)
    plan = {"new": [], "replace": [], "unchanged": []}
    for rel, data in files.items():
        dest = safe_destination(root, rel)
        if not dest.exists():
            plan["new"].append(rel)
        elif dest.read_bytes() == data:
            plan["unchanged"].append(rel)
        else:
            plan["replace"].append(rel)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    result = {"new": plan["new"], "replaced": plan["replace"], "unchanged": plan["unchanged"], "backup_dir": None, "dry_run": dry_run, "note": manifest.get("note", "")}
    if dry_run:
        return result
    backup = root / BACKUP_DIR / stamp
    done: list[tuple[str, Path | None]] = []                  # (path, the backup copy, or None for a new file): to undo
    try:
        for rel in plan["replace"] + plan["new"]:
            dest = safe_destination(root, rel)
            saved = None
            if rel in plan["replace"]:
                saved = backup / Path(*PurePosixPath(rel).parts)
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(dest.read_bytes())
                result["backup_dir"] = str(backup)
            _write_atomic(dest, files[rel])
            done.append((rel, saved))
    except Exception as exc:
        for rel, saved in reversed(done):                     # put everything back as it was
            dest = safe_destination(root, rel)
            if saved is not None:
                _write_atomic(dest, saved.read_bytes())
            else:
                dest.unlink(missing_ok=True)
        raise UpdateError(f"writing {rel!r} failed ({exc}); everything already written was put back, nothing has changed")
    result["gitignore_updated"] = _ensure_gitignore(root)
    return result


def find_newest_zip(folder: Path | None = None) -> Path | None:
    folder = Path(folder) if folder else Path.home() / "Downloads"
    zips = sorted(folder.glob("ufc-update-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def main(argv=None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description="Apply an update zip: check everything, back up what it replaces, place the files")
    ap.add_argument("zip", nargs="?", type=Path, help="the zip (default: the newest ufc-update-*.zip in your Downloads folder)")
    ap.add_argument("--sha256", help="the zip's checksum from the message that came with it")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; write nothing")
    ap.add_argument("--downloads", type=Path, help="where to look for the zip (default: your Downloads folder)")
    args = ap.parse_args(argv)
    zip_path = args.zip or find_newest_zip(args.downloads)
    if zip_path is None:
        print("No ufc-update-*.zip found in your Downloads folder. Download it again, or give its path: python tools/apply_update.py PATH_TO_ZIP")
        return 2
    print(f"Update: {zip_path}")
    try:
        r = apply(zip_path, root, dry_run=args.dry_run, expected_sha256=args.sha256)
    except UpdateError as exc:
        print(f"NOTHING WAS CHANGED. {exc}")
        return 1
    if r["note"]:
        print(f"About this update: {r['note']}")
    verb = "WOULD " if r["dry_run"] else ""
    for rel in r["new"]:
        print(f"{verb}PLACED   {rel}  (new)")
    for rel in r["replaced"]:
        print(f"{verb}PLACED   {rel}  (replaced" + ("" if r["dry_run"] else f"; the old copy is in {BACKUP_DIR}/") + ")")
    for rel in r["unchanged"]:
        print(f"unchanged {rel}")
    if r["dry_run"]:
        print("\nDry run: nothing was written.")
    else:
        print(f"\nDone: {len(r['new']) + len(r['replaced'])} file(s) placed, {len(r['unchanged'])} already up to date." + (f" Old copies: {r['backup_dir']}" if r["backup_dir"] else ""))
        print("Next: python -m pytest -q tests   and then   git status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
