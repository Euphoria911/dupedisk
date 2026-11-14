"""Disk usage tree (du-style) and largest-files listing."""

from __future__ import annotations

import heapq
import os
from collections.abc import Iterable
from dataclasses import dataclass, field

from .sizes import human
from .walk import FileInfo, is_excluded, is_link, walk_files


@dataclass
class Node:
    name: str
    path: str
    size: int = 0
    is_dir: bool = True
    files: int = 0
    children: list["Node"] = field(default_factory=list)


def scan(
    root: str,
    max_depth: int = 2,
    excludes: Iterable[str] = (),
    errors: list[str] | None = None,
    modified_after: float | None = None,
) -> Node:
    """Total every directory under ``root``; only nodes within ``max_depth`` are kept.

    ``modified_after`` is an epoch time: older files are not counted at all, and directories
    left with no files are dropped, so the tree shows only what changed since then.
    """
    errors = errors if errors is not None else []
    root = os.path.abspath(root)
    return _scan(root, root, 0, max_depth, tuple(excludes), errors, modified_after)


def _scan(
    path: str,
    name: str,
    depth: int,
    max_depth: int,
    excludes: tuple[str, ...],
    errors: list[str],
    modified_after: float | None,
) -> Node:
    node = Node(name, path)
    keep = depth < max_depth
    try:
        it = os.scandir(path)
    except OSError as exc:
        errors.append(f"{path}: {exc.strerror or exc}")
        return node
    with it:
        for entry in it:
            try:
                if is_link(entry) or is_excluded(entry, excludes):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = _scan(
                        entry.path, entry.name, depth + 1, max_depth, excludes, errors, modified_after
                    )
                    if modified_after is not None and child.files == 0:
                        continue
                    node.size += child.size
                    node.files += child.files
                    if keep:
                        node.children.append(child)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    if modified_after is not None and st.st_mtime < modified_after:
                        continue
                    node.size += st.st_size
                    node.files += 1
                    if keep:
                        node.children.append(Node(entry.name, entry.path, st.st_size, False, 1))
            except OSError as exc:
                errors.append(f"{entry.path}: {exc.strerror or exc}")
    node.children.sort(key=lambda n: (-n.size, n.name.lower()))
    return node


def render_tree(root: Node, top: int = 15, bar_width: int = 20, note: str = "") -> str:
    """``note`` is appended to the header line, e.g. ``modified in the last 30d``."""
    header = f"{root.path}  -  {human(root.size)} in {root.files:,} files"
    lines = [f"{header} {note}" if note else header, ""]
    _render_children(root, top, bar_width, 0, lines)
    return "\n".join(lines) + "\n"


def _render_children(node: Node, top: int, bar_width: int, level: int, out: list[str]) -> None:
    shown, rest = (node.children[:top], node.children[top:]) if top > 0 else (node.children, [])
    pad = "  " * level
    for child in shown:
        pct = child.size / node.size * 100 if node.size else 0.0
        bar = "#" * round(pct / 100 * bar_width)
        label = child.name + (os.sep if child.is_dir else "")
        out.append(f"{human(child.size):>10}  {pct:5.1f}%  {bar:<{bar_width}}  {pad}{label}")
        if child.children:
            _render_children(child, top, bar_width, level + 1, out)
    if rest:
        rest_size = sum(c.size for c in rest)
        out.append(f"{human(rest_size):>10}  {'':5}   {'':<{bar_width}}  {pad}... {len(rest)} more")


def node_to_dict(node: Node) -> dict:
    """The whole kept tree as plain data, for ``usage --json``."""
    out: dict = {
        "name": node.name,
        "path": node.path,
        "type": "dir" if node.is_dir else "file",
        "size": node.size,
        "files": node.files,
    }
    if node.is_dir:
        out["children"] = [node_to_dict(child) for child in node.children]
    return out


def largest_files(
    root: str,
    count: int = 20,
    excludes: Iterable[str] = (),
    errors: list[str] | None = None,
    modified_after: float | None = None,
) -> list[FileInfo]:
    errors = errors if errors is not None else []
    files = walk_files(os.path.abspath(root), excludes, errors, modified_after)
    return heapq.nlargest(count, files, key=lambda f: f.size)
