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
from datetime import datetime, timezone
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
