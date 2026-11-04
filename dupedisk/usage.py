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
) -> Node:
    """Total every directory under ``root``; only nodes within ``max_depth`` are kept."""
    errors = errors if errors is not None else []
    root = os.path.abspath(root)
    return _scan(root, root, 0, max_depth, tuple(excludes), errors)


def _scan(
    path: str, name: str, depth: int, max_depth: int, excludes: tuple[str, ...], errors: list[str]
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
                    child = _scan(entry.path, entry.name, depth + 1, max_depth, excludes, errors)
                    node.size += child.size
                    node.files += child.files
                    if keep:
                        node.children.append(child)
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    node.size += size
                    node.files += 1
                    if keep:
                        node.children.append(Node(entry.name, entry.path, size, False, 1))
            except OSError as exc:
                errors.append(f"{entry.path}: {exc.strerror or exc}")
    node.children.sort(key=lambda n: (-n.size, n.name.lower()))
    return node


def render_tree(root: Node, top: int = 15, bar_width: int = 20) -> str:
    lines = [f"{root.path}  -  {human(root.size)} in {root.files:,} files", ""]
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


def largest_files(
    root: str, count: int = 20, excludes: Iterable[str] = (), errors: list[str] | None = None
) -> list[FileInfo]:
    errors = errors if errors is not None else []
    return heapq.nlargest(
        count, walk_files(os.path.abspath(root), excludes, errors), key=lambda f: f.size
    )
