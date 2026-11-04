"""Directory walking that never follows symlinks or junctions and never dies on a bad path."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int


def is_excluded(entry: os.DirEntry, excludes: Iterable[str]) -> bool:
    """Match each pattern against the entry name and its full path (case-insensitive on Windows)."""
    return any(fnmatch(entry.name, p) or fnmatch(entry.path, p) for p in excludes)


def is_link(entry: os.DirEntry) -> bool:
    """Symlinks, plus NTFS junctions (``is_junction`` exists on Python 3.12+)."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


def walk_files(root: str, excludes: Iterable[str], errors: list[str]) -> Iterator[FileInfo]:
    excludes = tuple(excludes)
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            it = os.scandir(directory)
        except OSError as exc:
            errors.append(f"{directory}: {exc.strerror or exc}")
            continue
        with it:
            for entry in it:
                try:
                    if is_link(entry) or is_excluded(entry, excludes):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield FileInfo(entry.path, entry.stat(follow_symlinks=False).st_size)
                except OSError as exc:
                    errors.append(f"{entry.path}: {exc.strerror or exc}")
