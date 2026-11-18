"""Remove duplicate files: decide first, verify again, then delete.

A plan names, per set of identical files, the copies to keep and the copies to remove. Nothing
is touched until ``apply_plans``, and even then each file is re-checked right before it is
deleted (or, with ``link``, replaced by a hard link): it must still exist, still have the planned size, still hash the same as a kept copy,
and not be a hard link to it. A set always keeps at least one readable copy. Symlinks and
junctions never get this far, since the scan skips them.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .dupes import DupeGroup, _digest
from .quarantine import Quarantine

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


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive match of ``path``: a glob against the whole path if the pattern has a
    wildcard, otherwise a plain substring (``Documents`` matches ``C:\\Users\\me\\Documents\\a``)."""
    low = path.lower().replace("\\", "/")
    for pattern in patterns:
        p = pattern.lower().replace("\\", "/")
        if any(c in p for c in "*?["):
            if fnmatchcase(low, p):
                return True
        elif p in low:
            return True
    return False


def _sort_key(rule: str, path: str, prefer: tuple[str, ...] = (), avoid: tuple[str, ...] = ()) -> tuple:
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
    # safety first, then what was asked for (avoided paths lose, preferred paths win), then the rule
    return (
        unreadable,
        matches_any(path, avoid),
        not matches_any(path, prefer),
        rank,
        os.path.normcase(path),
        path,
    )


def _ordered(plans: list[Plan]) -> list[Plan]:
    return sorted(plans, key=lambda p: (-p.freed, p.keep))


def plan_by_rule(
    groups: Iterable[DupeGroup],
    rule: str,
    prefer: Iterable[str] = (),
    avoid: Iterable[str] = (),
) -> list[Plan]:
    """Keep one copy per set, chosen by ``rule``; every other copy is to be removed.

    ``oldest`` / ``newest`` go by modification time, ``shortest-path`` by path length,
    ``first`` alphabetically. Ties fall back to the path, so a plan is reproducible.

    ``prefer`` and ``avoid`` are path patterns (see ``matches_any``) that come before the
    rule: a copy matching ``prefer`` is kept over one that doesn't, a copy matching ``avoid``
    is kept only if every copy matches. When several copies tie on that, ``rule`` decides.
    """
    if rule not in RULES:
        raise ValueError(f"unknown keep rule {rule!r} (use one of: {', '.join(RULES)})")
    prefer, avoid = tuple(prefer), tuple(avoid)
    plans = []
    for group in groups:
        ordered = sorted(group.paths, key=lambda p: _sort_key(rule, p, prefer, avoid))
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


def _link_in_place(victim: str, keeper: str) -> None:
    """Make ``victim`` a hard link to ``keeper`` without a moment where the path is missing.

    The link is created under a temporary name beside the victim and then renamed over it.
    If either step fails the temporary link is removed and the victim is untouched.
    """
    tmp = f"{victim}.dupedisk-{os.getpid()}.tmp"
    os.link(keeper, tmp)  # fails across volumes, on FAT, or past the filesystem's link limit
    try:
        os.replace(tmp, victim)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _delete(victim: str, keeper: str) -> None:
    os.remove(victim)


def _act_verified(
    victim: str, keeper: str, digest: str, size: int, act: Callable[[str, str], object]
) -> str | None:
    """Run ``act(victim, keeper)`` (delete, hard-link, quarantine) if ``victim`` is still an
    identical, separate copy; else return why it was left alone."""
    try:
        if os.path.normcase(os.path.abspath(victim)) == os.path.normcase(os.path.abspath(keeper)):
            return "same path as the kept copy"
        if _same_file(victim, keeper):
            return "already a hard link to the kept copy (nothing to free)"
        if os.path.getsize(victim) != size:
            return "changed since the scan"
        if _digest(victim, None) != digest:
            return "no longer identical to the kept copy"
        act(victim, keeper)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def apply_plans(
    plans: Iterable[Plan],
    on_remove: Callable[[str], None] | None = None,
    link: bool = False,
    quarantine: Quarantine | None = None,
) -> Outcome:
    """Carry out ``plans``: delete each copy to remove, with ``link`` replace it by a hard
    link to the kept copy, or with ``quarantine`` move it into that quarantine. A file that
    fails any check is left alone and reported in ``Outcome.failed``; the rest of the plan
    still runs. ``Outcome.removed`` lists the paths that were deleted, replaced or moved."""
    if link and quarantine is not None:
        raise ValueError("choose hard links or a quarantine, not both")
    if quarantine is not None:
        act = lambda victim, keeper: quarantine.move(victim)
    elif link:
        act = _link_in_place
    else:
        act = _delete
    out = Outcome()
    for plan in plans:
        keeper = _pick_keeper(plan)
        if keeper is None:
            out.failed.extend((v, "no kept copy could be read, so nothing was removed") for v in plan.remove)
            continue
        keeper_path, digest = keeper
        for victim in plan.remove:
            why = _act_verified(victim, keeper_path, digest, plan.size, act)
            if why is None:
                out.removed.append(victim)
                out.freed += plan.size
                if on_remove:
                    on_remove(victim)
            else:
                out.failed.append((victim, why))
    return out
