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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .cache import HashCache
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
    cache_used: bool = False
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def reclaimable(self) -> int:
        return sum(g.reclaimable for g in self.groups)

    @property
    def redundant_files(self) -> int:
        return sum(len(g.paths) - 1 for g in self.groups)


def _digest(path: str, limit: int | None) -> str:
    h = hashlib.blake2b(digest_size=20)
    remaining = limit
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
    return h.hexdigest()


def _try_digest(path: str, limit: int | None) -> tuple[str | None, OSError | None]:
    try:
        return _digest(path, limit), None
    except OSError as exc:
        return None, exc


def _hash_many(
    paths: Iterable[str],
    limit: int | None,
    errors: list[str],
    cache: HashCache | None = None,
    jobs: int = 1,
) -> dict[str, str]:
    """Digest every path (the first ``limit`` bytes, or the whole file); unreadable ones are
    recorded in ``errors`` and left out.

    The cache (one SQLite connection, so main thread only) is read before and written after;
    only the files it can't answer go to the thread pool. Results come back in input order,
    so the output does not depend on ``jobs``.
    """
    kind = "full" if limit is None else f"head{limit}"  # the window size is part of the key
    digests: dict[str, str] = {}
    todo: list[tuple[str, tuple[int, int] | None]] = []
    for path in paths:
        stamp: tuple[int, int] | None = None
        if cache is not None:
            try:
                st = os.stat(path)
            except OSError as exc:
                errors.append(f"{path}: {exc.strerror or exc}")
                continue
            stamp = (st.st_size, st.st_mtime_ns)
            cached = cache.get(path, kind, *stamp)
            if cached is not None:
                digests[path] = cached
                continue
        todo.append((path, stamp))

    paths_to_read = [path for path, _ in todo]
    if jobs > 1 and len(todo) > 1:
        # hashlib drops the GIL on large buffers, so threads scale on SSDs and cached reads
        pool = ThreadPoolExecutor(max_workers=jobs)
        try:
            results = list(pool.map(lambda p: _try_digest(p, limit), paths_to_read))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)  # Ctrl-C must not wait out the queue
    else:
        results = [_try_digest(p, limit) for p in paths_to_read]

    for (path, stamp), (digest, exc) in zip(todo, results):
        if exc is not None or digest is None:
            errors.append(f"{path}: {(exc.strerror or exc) if exc else 'unreadable'}")
            continue
        digests[path] = digest
        if cache is not None and stamp is not None:
            cache.put(path, kind, *stamp, digest)
    return digests


def _split_by_digest(paths: list[str], digests: dict[str, str]) -> list[list[str]]:
    """Bucket ``paths`` by digest; keep only buckets that still hold two or more files."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        if path in digests:
            buckets[digests[path]].append(path)
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
    cache: HashCache | None = None,
    jobs: int = 1,
) -> DupeReport:
    """``jobs`` > 1 hashes that many files at once. The result is the same for any value."""
    report = DupeReport(cache_used=cache is not None)
    by_size: dict[int, list[str]] = defaultdict(list)

    for root in _prune_roots(roots):
        for info in walk_files(root, excludes, report.errors):
            report.files_scanned += 1
            report.bytes_scanned += info.size
            if info.size >= min_size:
                by_size[info.size].append(info.path)
            if progress and report.files_scanned % 2000 == 0:
                progress(report.files_scanned)

    sized: list[tuple[int, list[str]]] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        paths = _collapse_hardlinks(paths, report.errors)
        if len(paths) >= 2:
            sized.append((size, paths))

    # Hash every candidate of a stage in one batch: size groups are usually just 2-3 files,
    # so a pool per group would sit idle. A stage only reads files that survived the last one.
    heads = _hash_many((p for _, ps in sized for p in ps), PARTIAL_BYTES, report.errors, cache, jobs)
    partial = [(size, b) for size, ps in sized for b in _split_by_digest(ps, heads)]

    # a file no bigger than the partial window is already fully hashed
    confirmed = [(size, b) for size, b in partial if size <= PARTIAL_BYTES]
    large = [(size, b) for size, b in partial if size > PARTIAL_BYTES]
    full = _hash_many((p for _, b in large for p in b), None, report.errors, cache, jobs)
    for size, bucket in large:
        confirmed.extend((size, g) for g in _split_by_digest(bucket, full))

    for size, group in confirmed:
        report.groups.append(DupeGroup(size, tuple(sorted(group))))

    report.groups.sort(key=lambda g: (-g.reclaimable, g.paths))
    if cache is not None:
        report.cache_hits, report.cache_misses = cache.hits, cache.misses
    return report
