"""Find ``report (1).pdf`` / ``report - Copy.pdf`` style copies that sit next to their original.

Only a copy whose original exists in the same folder is reported. A lone ``photo (1).jpg``
is usually just a name, not a copy of anything. Each copy is checked against the original
byte for byte, so the report separates safe-to-remove copies from ones that were edited.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .dupes import _digest, _prune_roots
from .walk import walk_files

# "name (1)", "name - Copy", "name - Copy (2)", "name copy", "name copy 2", "name_copy3"
_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?:\s\(\d+\)|\s-\sCopy(?:\s\(\d+\))?|\scopy(?:\s\d+)?|_copy\d*)$", re.I)
# older Windows: "Copy of name", "Copy (2) of name"
_PREFIX_RE = re.compile(r"^Copy(?:\s\(\d+\))?\sof\s(?P<base>.+)$", re.I)


def original_stem(stem: str) -> str | None:
    """The stem a copy-style name points at, or ``None`` if ``stem`` doesn't look like a copy."""
    for pattern in (_SUFFIX_RE, _PREFIX_RE):
        m = pattern.match(stem)
        if m:
            return m.group("base")
    return None


@dataclass(frozen=True)
class Copy:
    path: str
    size: int
    identical: bool  # byte-for-byte equal to the original


@dataclass(frozen=True)
class CopySet:
    original: str
    size: int
    copies: tuple[Copy, ...]

    @property
    def reclaimable(self) -> int:
        return sum(c.size for c in self.copies if c.identical)


@dataclass
class SimilarReport:
    sets: list[CopySet] = field(default_factory=list)
    files_scanned: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def copy_count(self) -> int:
        return sum(len(s.copies) for s in self.sets)

    @property
    def identical_count(self) -> int:
        return sum(c.identical for s in self.sets for c in s.copies)

    @property
    def reclaimable(self) -> int:
        return sum(s.reclaimable for s in self.sets)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _compare(original: str, original_size: int, copy: str, size: int, errors: list[str]) -> bool | None:
    """True if identical, False if different, None if unreadable (error recorded)."""
    if size != original_size:
        return False
    try:
        return _digest(original, None) == _digest(copy, None)
    except OSError as exc:
        errors.append(f"{copy}: {exc.strerror or exc}")
        return None


def find_similar_names(
    roots: Iterable[str], min_size: int = 1, excludes: Iterable[str] = ()
) -> SimilarReport:
    report = SimilarReport()
    # (folder, stem, ext), all case-folded -> (path, size) of every file, to find originals
    files: dict[tuple[str, str, str], tuple[str, int]] = {}
    candidates: list[tuple[tuple[str, str, str], str, int]] = []

    for root in _prune_roots(roots):
        for info in walk_files(root, excludes, report.errors):
            report.files_scanned += 1
            folder, name = os.path.split(info.path)
            stem, ext = os.path.splitext(name)
            key = (os.path.normcase(folder), stem.lower(), ext.lower())
            files[key] = (info.path, info.size)
            base = original_stem(stem)
            if base is not None and info.size >= min_size:
                candidates.append(((key[0], base.lower(), key[2]), info.path, info.size))

    grouped: dict[str, list[Copy]] = {}
    originals: dict[str, int] = {}
    for original_key, path, size in candidates:
        found = files.get(original_key)
        if found is None:
            continue
        original, original_size = found
        if original == path or _same_file(original, path):
            continue  # a hard link to the original costs no space
        verdict = _compare(original, original_size, path, size, report.errors)
        if verdict is None:
            continue
        originals[original] = original_size
        grouped.setdefault(original, []).append(Copy(path, size, verdict))

    for original, copies in grouped.items():
        copies.sort(key=lambda c: c.path)
        report.sets.append(CopySet(original, originals[original], tuple(copies)))
    report.sets.sort(key=lambda s: (-s.reclaimable, s.original))
    return report
