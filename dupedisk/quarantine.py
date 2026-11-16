"""Move files into a quarantine folder instead of deleting them, and put them back later.

Each file lands under ``<quarantine>/<drive>/<original path>``, so nothing collides and the
origin of every file is readable from where it sits. A manifest (``dedupe-manifest.jsonl``,
one JSON object per line, appended and flushed as it goes) records every move, which is what
``restore`` reads. The record is written *before* the move, so a file can never be in the
quarantine without a record of where it came from; a move that fails is marked ``aborted``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from types import TracebackType

from .dupes import _digest

MANIFEST_NAME = "dedupe-manifest.jsonl"


def quarantine_path(root: str, original: str) -> str:
    """Where ``original`` is parked: ``C:\\a\\b.txt`` -> ``<root>\\C\\a\\b.txt``,
    ``\\\\srv\\share\\x`` -> ``<root>\\_unc\\srv\\share\\x``, ``/home/me/f`` -> ``<root>/home/me/f``."""
    drive, rest = os.path.splitdrive(os.path.abspath(original))
    if drive[:2] in ("\\\\", "//"):
        parts = ["_unc", *[p for p in re.split(r"[\\/]+", drive) if p]]
    else:
        parts = [drive.rstrip(":")] if drive else []
    parts += [p for p in re.split(r"[\\/]+", rest) if p]
    return os.path.join(root, *parts)


def free_name(path: str) -> str:
    """``path``, or ``path.1``, ``path.2`` ... if something is already there."""
    candidate, n = path, 0
    while os.path.lexists(candidate) or os.path.lexists(candidate + ".part"):
        n += 1
        candidate = f"{path}.{n}"
    return candidate


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def move_file(src: str, dst: str) -> None:
    """Rename ``src`` to ``dst``; across volumes, copy it, check the copy, then delete ``src``.

    If anything goes wrong the source is left exactly as it was and no partial copy remains.
    """
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if not _cross_device(exc):
            raise
    part = dst + ".part"
    try:
        shutil.copy2(src, part)
        if os.path.getsize(part) != os.path.getsize(src) or _digest(part, None) != _digest(src, None):
            raise OSError(errno.EIO, "the copy did not match the original")
        os.replace(part, dst)
    except BaseException:
        _silent_unlink(part)
        raise
    try:
        os.remove(src)
    except OSError:
        _silent_unlink(dst)  # keep one copy, not two: the original stays where it was
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Entry:
    original: str  # where the file used to live
    stored: str  # where it is now, relative to the quarantine folder
    size: int = 0
    time: str = ""


@dataclass
class RestoreResult:
    restored: list[Entry] = field(default_factory=list)  # done, or with apply=False what would be
    skipped: list[tuple[Entry, str]] = field(default_factory=list)  # (entry, why it stays put)
    malformed: int = 0  # manifest lines that could not be read


def manifest_path_for(location: str) -> str:
    """``location`` may be the quarantine folder or the manifest file itself."""
    path = os.path.abspath(location)
    return os.path.join(path, MANIFEST_NAME) if os.path.isdir(path) else path


def read_entries(manifest: str) -> tuple[list[Entry], int]:
    """Replay the manifest: the files that are in the quarantine now, oldest move first,
    and how many lines were unreadable. A later ``restored`` or ``aborted`` line cancels the
    move it names."""
    active: dict[str, Entry] = {}
    malformed = 0
    with open(manifest, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(rec, dict):
                malformed += 1
            elif isinstance(rec.get("original"), str) and isinstance(rec.get("stored"), str):
                size = rec.get("size")
                active.pop(rec["stored"], None)  # a reused name moves to the back
                active[rec["stored"]] = Entry(
                    rec["original"], rec["stored"], size if isinstance(size, int) else 0, str(rec.get("time", ""))
                )
            elif isinstance(rec.get("restored", rec.get("aborted")), str):
                active.pop(rec.get("restored", rec.get("aborted")), None)
            else:
                malformed += 1
    return list(active.values()), malformed


def _prune_empty_dirs(directory: str, root: str) -> None:
    """Remove ``directory`` and its parents while they are empty, never ``root`` itself."""
    root = os.path.normcase(os.path.abspath(root))
    current = os.path.abspath(directory)
    while os.path.normcase(current) != root and os.path.normcase(current).startswith(root + os.sep):
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def restore(location: str, apply: bool = False, only: Iterable[str] = ()) -> RestoreResult:
    """Put quarantined files back where they came from.

    Nothing is ever overwritten: an entry whose original path is occupied again, or whose
    file has left the quarantine, is skipped with a reason. With ``apply=False`` the result
    lists what would be restored and nothing moves. ``only`` keeps entries whose original
    path matches any pattern (glob, or case-insensitive substring if it has no wildcard).
    """
    manifest = manifest_path_for(location)
    root = os.path.dirname(manifest)
    entries, malformed = read_entries(manifest)
    patterns = [p.lower() if any(c in p for c in "*?[") else f"*{p.lower()}*" for p in only]
    result = RestoreResult(malformed=malformed)
    for entry in entries:
        if patterns and not any(fnmatchcase(entry.original.lower(), p) for p in patterns):
            continue
        src = os.path.abspath(os.path.join(root, entry.stored))
        if not os.path.normcase(src).startswith(os.path.normcase(root) + os.sep):
            result.skipped.append((entry, "stored path points outside the quarantine"))
        elif not os.path.lexists(src):
            result.skipped.append((entry, "no longer in the quarantine"))
        elif os.path.lexists(entry.original):
            result.skipped.append((entry, "something already exists at the original path"))
        elif not apply:
            result.restored.append(entry)
        else:
            try:
                os.makedirs(os.path.dirname(entry.original), exist_ok=True)
                move_file(src, entry.original)
            except OSError as exc:
                result.skipped.append((entry, exc.strerror or str(exc)))
                continue
            result.restored.append(entry)
            _prune_empty_dirs(os.path.dirname(src), root)
            try:
                with open(manifest, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"restored": entry.stored, "time": _now()}) + "\n")
            except OSError:
                pass  # the file is back; a missing marker only means a re-run reports it as gone
    return result


class Quarantine:
    """Context manager that moves files into ``root`` and logs each move."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.manifest = os.path.join(self.root, MANIFEST_NAME)
        self._fh = open(self.manifest, "a", encoding="utf-8")

    def _write(self, record: dict) -> None:
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def move(self, victim: str) -> str:
        """Park ``victim`` in the quarantine and return where it went."""
        size = os.path.getsize(victim)
        target = free_name(quarantine_path(self.root, victim))
        stored = os.path.relpath(target, self.root)
        self._write({"original": os.path.abspath(victim), "stored": stored, "size": size, "time": _now()})
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            move_file(victim, target)
        except BaseException:
            try:
                self._write({"aborted": stored})
            except OSError:
                pass
            raise
        return target

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "Quarantine":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
