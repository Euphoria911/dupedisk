"""Remove duplicate files: decide first, verify again, then delete.

A plan names, per set of identical files, the copies to keep and the copies to remove. Nothing
is touched until ``apply_plans``, and even then each file is re-checked right before it is
deleted: it must still exist, still have the planned size, still hash the same as a kept copy,
and not be a hard link to it. A set always keeps at least one readable copy. Symlinks and
junctions never get this far, since the scan skips them.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .dupes import DupeGroup, _digest

RULES = ("oldest", "newest", "shortest-path", "first")


@dataclass(frozen=True)
class Plan:
    size: int
    keep: tuple[str, ...]
    remove: tuple[str, ...]

    @property
    def freed(self) -> int:
        return self.size * len(self.remove)


@dataclass
class Outcome:
    removed: list[str] = field(default_factory=list)
    freed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, why it was left alone)


def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _sort_key(rule: str, path: str) -> tuple:
    mtime = _mtime_ns(path)
    unreadable = mtime is None  # a file that can't even be stat'ed is never the one we keep
    if rule == "oldest":
        rank = mtime or 0
    elif rule == "newest":
        rank = -(mtime or 0)
    elif rule == "shortest-path":
        rank = len(path)
    else:  # "first": plain alphabetical order
        rank = 0
    return (unreadable, rank, os.path.normcase(path), path)


def _ordered(plans: list[Plan]) -> list[Plan]:
    return sorted(plans, key=lambda p: (-p.freed, p.keep))


def plan_by_rule(groups: Iterable[DupeGroup], rule: str) -> list[Plan]:
    """Keep one copy per set, chosen by ``rule``; every other copy is to be removed.

    ``oldest`` / ``newest`` go by modification time, ``shortest-path`` by path length,
    ``first`` alphabetically. Ties fall back to the path, so a plan is reproducible.
    """
    if rule not in RULES:
        raise ValueError(f"unknown keep rule {rule!r} (use one of: {', '.join(RULES)})")
    plans = []
    for group in groups:
        ordered = sorted(group.paths, key=lambda p: _sort_key(rule, p))
        plans.append(Plan(group.size, (ordered[0],), tuple(ordered[1:])))
    return _ordered(plans)


def plan_against(groups: Iterable[DupeGroup]) -> list[Plan]:
    """With a reference folder: keep every copy inside it, remove the copies outside."""
    return _ordered([Plan(g.size, g.keep, g.extras) for g in groups if g.keep and g.extras])


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _pick_keeper(plan: Plan) -> tuple[str, str] | None:
    """The first kept copy that is still readable and still the planned size, with its hash."""
    for path in plan.keep:
        try:
            if os.path.getsize(path) == plan.size:
                return path, _digest(path, None)
        except OSError:
            continue
    return None


def _remove_verified(victim: str, keeper: str, digest: str, size: int) -> str | None:
    """Delete ``victim`` if it is still an identical, separate copy; else return why not."""
    try:
        if os.path.normcase(os.path.abspath(victim)) == os.path.normcase(os.path.abspath(keeper)):
            return "same path as the kept copy"
        if _same_file(victim, keeper):
            return "hard link to the kept copy (deleting it would free nothing)"
        if os.path.getsize(victim) != size:
            return "changed since the scan"
        if _digest(victim, None) != digest:
            return "no longer identical to the kept copy"
        os.remove(victim)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def apply_plans(plans: Iterable[Plan], on_remove: Callable[[str], None] | None = None) -> Outcome:
    """Carry out ``plans``. A file that fails any check is left alone and reported in
    ``Outcome.failed``; the rest of the plan still runs."""
    out = Outcome()
    for plan in plans:
        keeper = _pick_keeper(plan)
        if keeper is None:
            out.failed.extend((v, "no kept copy could be read, so nothing was removed") for v in plan.remove)
            continue
        keeper_path, digest = keeper
        for victim in plan.remove:
            why = _remove_verified(victim, keeper_path, digest, plan.size)
            if why is None:
                out.removed.append(victim)
                out.freed += plan.size
                if on_remove:
                    on_remove(victim)
            else:
                out.failed.append((victim, why))
    return out
