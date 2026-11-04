"""Duplicate finder: group by size, then by a partial hash, then by a full hash.

Only files that collide at one stage are read for the next, so a large tree with few
real duplicates costs little I/O. Hard links to the same data are collapsed to one
file and are never reported as duplicates of each other.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .walk import walk_files

PARTIAL_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DupeGroup:
    size: int
    paths: tuple[str, ...]

    @property
    def reclaimable(self) -> int:
        return self.size * (len(self.paths) - 1)


@dataclass
class DupeReport:
    groups: list[DupeGroup] = field(default_factory=list)
    files_scanned: int = 0
    bytes_scanned: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def reclaimable(self) -> int:
        return sum(g.reclaimable for g in self.groups)

    @property
    def redundant_files(self) -> int:
        return sum(len(g.paths) - 1 for g in self.groups)


def _hash_file(path: str, limit: int | None, errors: list[str]) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    remaining = limit
    try:
        with open(path, "rb") as fh:
            while True:
                want = CHUNK_BYTES if remaining is None else min(CHUNK_BYTES, remaining)
                if want == 0:
                    break
                block = fh.read(want)
                if not block:
                    break
                h.update(block)
                if remaining is not None:
                    remaining -= len(block)
    except OSError as exc:
        errors.append(f"{path}: {exc.strerror or exc}")
        return None
    return h.hexdigest()


def _group_by_hash(
    paths: Iterable[str], limit: int | None, errors: list[str]
) -> list[list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        digest = _hash_file(path, limit, errors)
        if digest is not None:
            buckets[digest].append(path)
    return [b for b in buckets.values() if len(b) > 1]


def _collapse_hardlinks(paths: list[str], errors: list[str]) -> list[str]:
    """Keep one path per underlying file. DirEntry.stat() zeroes st_ino on Windows, so use os.stat."""
    seen: set[tuple[int, int]] = set()
    unique = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror or exc}")
            continue
        identity = (st.st_dev, st.st_ino)
        if st.st_ino and identity in seen:
            continue
        seen.add(identity)
        unique.append(path)
    return unique


def _prune_roots(roots: Iterable[str]) -> list[str]:
    """Absolute-ize roots and drop any root that is equal to or inside another root."""
    norm = sorted({os.path.abspath(r) for r in roots}, key=lambda p: (len(p), p))
    kept: list[str] = []
    for root in norm:
        key = os.path.normcase(root)
        if not any(
            key == os.path.normcase(k) or key.startswith(os.path.normcase(k).rstrip(os.sep) + os.sep)
            for k in kept
        ):
            kept.append(root)
    return kept


def find_duplicates(
    roots: Iterable[str],
    min_size: int = 1,
    excludes: Iterable[str] = (),
    progress: Callable[[int], None] | None = None,
) -> DupeReport:
    report = DupeReport()
    by_size: dict[int, list[str]] = defaultdict(list)

    for root in _prune_roots(roots):
        for info in walk_files(root, excludes, report.errors):
            report.files_scanned += 1
            report.bytes_scanned += info.size
            if info.size >= min_size:
                by_size[info.size].append(info.path)
            if progress and report.files_scanned % 2000 == 0:
                progress(report.files_scanned)

    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        paths = _collapse_hardlinks(paths, report.errors)
        if len(paths) < 2:
            continue
        candidates = _group_by_hash(paths, PARTIAL_BYTES, report.errors)
        for bucket in candidates:
            # a file no bigger than the partial window is already fully hashed
            confirmed = [bucket] if size <= PARTIAL_BYTES else _group_by_hash(bucket, None, report.errors)
            for group in confirmed:
                report.groups.append(DupeGroup(size, tuple(sorted(group))))

    report.groups.sort(key=lambda g: (-g.reclaimable, g.paths))
    return report
