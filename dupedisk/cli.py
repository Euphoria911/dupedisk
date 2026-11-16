from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time

from . import __version__
from .cache import HashCache
from .dedupe import RULES, Plan, apply_plans, plan_against, plan_by_rule
from .dupes import DupeReport, against_reference, find_duplicates, is_within
from .similar import CopySet, find_similar_names
from .sizes import human, parse_age, parse_size
from .usage import largest_files, node_to_dict, render_tree, scan

_TAG_WIDTH = len("differs, 1023.9 MiB")  # widest tag, so paths line up


def _size_arg(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _jobs_arg(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"invalid job count: {text!r} (use 0 for one per CPU)")
    return n or os.cpu_count() or 1


def _age_arg(text: str) -> str:
    """Validate at parse time, keep the text so the report can echo it back."""
    try:
        parse_age(text)
        return text.strip()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_since(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", type=_age_arg, metavar="AGE",
                   help="count only files modified within AGE, e.g. 30d, 12h, 2w")


def _cutoff(args: argparse.Namespace) -> float | None:
    return None if args.since is None else time.time() - parse_age(args.since)


def _add_scan_options(p: argparse.ArgumentParser) -> None:
    """What `dupes` and `dedupe` share: where to look, how to hash, what to compare against."""
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("-m", "--min-size", type=_size_arg, default=1, metavar="SIZE",
                   help="ignore files smaller than SIZE, e.g. 1M (default: 1 byte)")
    p.add_argument("-j", "--jobs", type=_jobs_arg, default=1, metavar="N",
                   help="hash N files at once (0 = one per CPU; default 1, best on SSDs)")
    p.add_argument("--against", metavar="DIR",
                   help="treat DIR as the copies to keep: only files outside it that "
                        "already exist inside it are considered")
    p.add_argument("--cache", metavar="FILE",
                   help="SQLite hash cache; unchanged files (same size and mtime) are not re-read")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-x", "--exclude", action="append", default=[], metavar="PAT",
        help="skip files/dirs whose name or path matches PAT (glob); repeatable",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="list unreadable paths on stderr")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dupedisk", description="Find duplicate files and see where your disk went."
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dupes", help="find duplicate files (report only, never deletes)")
    _add_scan_options(d)
    d.add_argument("-t", "--top", type=int, default=20,
                   help="show the N sets with the most reclaimable space (0 = all)")
    fmt = d.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="machine-readable output")
    fmt.add_argument("--csv", action="store_true",
                     help="one row per file: set,size_bytes,path (all sets, ignores --top); "
                          "with --against a role column says keep or dupe")
    _add_common(d)

    r = sub.add_parser(
        "dedupe",
        help="delete duplicate copies (dry run unless --apply; every file is re-verified first)",
    )
    _add_scan_options(r)
    r.add_argument("--keep", choices=RULES, metavar="RULE",
                   help="which copy of each set survives: " + ", ".join(RULES)
                        + " (not used with --against, where the reference copies survive)")
    r.add_argument("--hardlink", action="store_true",
                   help="instead of deleting, replace each extra copy with a hard link to the kept "
                        "copy: every path stays, the space is freed (same volume, NTFS/ext4/...)")
    r.add_argument("--apply", action="store_true",
                   help="actually do it; without it this only prints what would happen")
    r.add_argument("-t", "--top", type=int, default=20,
                   help="print the N sets that free the most space (0 = all); "
                        "limits the printout only, --apply acts on every set")
    r.add_argument("--json", action="store_true", help="machine-readable plan and result")
    _add_common(r)

    s = sub.add_parser(
        "similar", help="find 'name (1).ext' style copies next to their original (report only)"
    )
    s.add_argument("paths", nargs="+", metavar="PATH")
    s.add_argument("-m", "--min-size", type=_size_arg, default=1, metavar="SIZE",
                   help="ignore copies smaller than SIZE (default: 1 byte)")
    s.add_argument("-t", "--top", type=int, default=20,
                   help="show the N originals with the most reclaimable space (0 = all)")
    s.add_argument("--identical", action="store_true",
                   help="only list copies that are byte-for-byte equal to their original")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    _add_common(s)

    u = sub.add_parser("usage", help="directory size tree")
    u.add_argument("path", nargs="?", default=".")
    u.add_argument("-d", "--depth", type=int, default=2, help="levels to show (default 2)")
    u.add_argument("-t", "--top", type=int, default=15, help="entries per directory (0 = all)")
    _add_since(u)
    u.add_argument("--json", action="store_true",
                   help="the whole tree as JSON (ignores --top)")
    _add_common(u)

    c = sub.add_parser("cache", help="inspect or clean a hash cache created with dupes --cache")
    c.add_argument("file", metavar="FILE")
    c.add_argument("--prune", action="store_true",
                   help="drop entries for files that are gone or changed since they were hashed")

    g = sub.add_parser("largest", help="biggest files under a path")
    g.add_argument("path", nargs="?", default=".")
    g.add_argument("-n", "--count", type=int, default=20)
    _add_since(g)
    g.add_argument("--json", action="store_true", help="machine-readable output")
    _add_common(g)
    return p


def _report_errors(errors: list[str], verbose: bool) -> None:
    if not errors:
        return
    if verbose:
        for line in errors:
            print(f"skipped: {line}", file=sys.stderr)
    else:
        print(f"{len(errors)} path(s) skipped (-v to list)", file=sys.stderr)


def _print_dupes(report: DupeReport, top: int, against: str | None = None) -> None:
    print(f"scanned {report.files_scanned:,} files ({human(report.bytes_scanned)})")
    if against:
        print(f"reference (kept): {os.path.abspath(against)}")
    if report.cache_used:
        print(f"cache: {report.cache_hits:,} hits, {report.cache_misses:,} misses")
    if not report.groups:
        print("nothing outside the reference is duplicated in it" if against else "no duplicates found")
        return
    print(
        f"{len(report.groups):,} duplicate sets, {report.redundant_files:,} redundant files, "
        f"{human(report.reclaimable)} reclaimable\n"
    )
    shown = report.groups[:top] if top > 0 else report.groups
    for i, group in enumerate(shown, 1):
        print(f"[{i}] {len(group.paths)} x {human(group.size)}  ({human(group.reclaimable)} reclaimable)")
        for path in group.paths:
            if group.keep:  # --against: say which copies stay
                print(f"    {'keep' if path in group.keep else 'dupe'}  {path}")
            else:
                print(f"    {path}")
    if len(shown) < len(report.groups):
        print(f"\n... {len(report.groups) - len(shown)} more sets (--top 0 for all)")


def _scan_duplicates(args: argparse.Namespace, show_progress: bool) -> DupeReport:
    """Validate the paths, open the cache, scan. Bad input raises ``ValueError`` (the CLI
    reports it and exits 2). With ``--against`` the groups are already reduced to the sets
    that straddle the reference folder."""
    for path in args.paths + ([args.against] if args.against else []):
        if not os.path.isdir(path):
            raise ValueError(f"not a directory: {path}")
    if args.against:
        for path in args.paths:
            if is_within(path, args.against):
                raise ValueError(
                    f"{path} is inside --against {args.against}; "
                    "nothing outside the reference to compare"
                )
    progress = None
    if show_progress and sys.stderr.isatty():
        progress = lambda n: print(f"\rscanning... {n:,} files", end="", file=sys.stderr, flush=True)
    cache = None
    if args.cache:
        try:
            cache = HashCache(args.cache)
        except sqlite3.Error as exc:
            raise ValueError(f"cannot open cache {args.cache}: {exc}") from None
    try:
        roots = args.paths + ([args.against] if args.against else [])
        report = find_duplicates(roots, args.min_size, args.exclude, progress, cache, args.jobs)
    finally:
        if cache is not None:
            cache.close()
    if progress:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    if args.against:
        report.groups = against_reference(report.groups, args.against)
    return report


def _cmd_dupes(args: argparse.Namespace) -> int:
    try:
        report = _scan_duplicates(args, show_progress=not (args.json or args.csv))
    except ValueError as exc:
        print(f"dupedisk: {exc}", file=sys.stderr)
        return 2

    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["set", "size_bytes", "path"] + (["role"] if args.against else []))
        for i, group in enumerate(report.groups, 1):
            for path in group.paths:
                row = [i, group.size, path]
                if args.against:
                    row.append("keep" if path in group.keep else "dupe")
                writer.writerow(row)
    elif args.json:
        payload = {
            "files_scanned": report.files_scanned,
            "bytes_scanned": report.bytes_scanned,
            "reclaimable": report.reclaimable,
            "groups": [
                {"size": g.size, "paths": list(g.paths), **({"keep": list(g.keep)} if g.keep else {})}
                for g in report.groups
            ],
            "skipped": len(report.errors),
        }
        if args.against:
            payload["against"] = args.against
        if report.cache_used:
            payload["cache"] = {"hits": report.cache_hits, "misses": report.cache_misses}
        print(json.dumps(payload, indent=2))
    else:
        _print_dupes(report, args.top, args.against)
    _report_errors(report.errors, args.verbose)
    return 0


def _print_plan(plans: list[Plan], top: int, verb: str = "remove") -> None:
    shown = plans[:top] if top > 0 else plans
    for i, plan in enumerate(shown, 1):
        copies = len(plan.keep) + len(plan.remove)
        print(f"[{i}] {copies} x {human(plan.size)}  (frees {human(plan.freed)})")
        for path in plan.keep:
            print(f"    {'keep':<7} {path}")
        for path in plan.remove:
            print(f"    {verb:<7} {path}")
    if len(shown) < len(plans):
        print(f"\n... {len(plans) - len(shown)} more sets (--top 0 for all)")


def _cmd_dedupe(args: argparse.Namespace) -> int:
    if args.against and args.keep:
        print("dupedisk: --keep and --against are alternatives: with --against the copies "
              "inside the reference folder are the ones kept", file=sys.stderr)
        return 2
    if not args.against and not args.keep:
        print(f"dupedisk: say which copy survives with --keep {{{','.join(RULES)}}} "
              "(or compare against a folder with --against DIR)", file=sys.stderr)
        return 2
    try:
        report = _scan_duplicates(args, show_progress=not args.json)
    except ValueError as exc:
        print(f"dupedisk: {exc}", file=sys.stderr)
        return 2

    plans = plan_against(report.groups) if args.against else plan_by_rule(report.groups, args.keep)
    removals = sum(len(p.remove) for p in plans)
    would_free = sum(p.freed for p in plans)
    outcome = apply_plans(plans, None, link=args.hardlink) if args.apply else None
    verb = "link" if args.hardlink else "remove"

    if args.json:
        payload = {
            "dry_run": not args.apply,
            "mode": "hardlink" if args.hardlink else "delete",
            "keep": args.keep,
            "against": args.against,
            "files_scanned": report.files_scanned,
            "plan": [
                {"size": p.size, "keep": list(p.keep), "remove": list(p.remove)} for p in plans
            ],
            "would_free": would_free,
            "skipped": len(report.errors),
        }
        if outcome is not None:
            payload["removed"] = outcome.removed
            payload["freed"] = outcome.freed
            payload["failed"] = [{"path": p, "reason": why} for p, why in outcome.failed]
        print(json.dumps(payload, indent=2))
    else:
        print(f"scanned {report.files_scanned:,} files ({human(report.bytes_scanned)})")
        if not plans:
            print("nothing outside the reference is duplicated in it" if args.against
                  else "no duplicates found, nothing to remove")
        else:
            basis = (f"keeping the copies in {os.path.abspath(args.against)}" if args.against
                     else f"keeping the {args.keep} copy of each set")
            action = "replace with hard links" if args.hardlink else "remove"
            print(f"plan: {action} {removals:,} files from {len(plans):,} sets, "
                  f"freeing {human(would_free)} ({basis})\n")
            _print_plan(plans, args.top, verb)
        if outcome is None:
            if plans:
                print(f"\ndry run: nothing was changed. Re-run with --apply to act on the "
                      f"files marked '{verb}'.")
        else:
            done = "replaced with hard links" if args.hardlink else "removed"
            print(f"\n{done} {len(outcome.removed):,} files, freed {human(outcome.freed)}")
            if args.verbose:
                for path in outcome.removed:
                    print(f"{done}: {path}")
            for path, why in outcome.failed:
                print(f"left alone: {path} ({why})", file=sys.stderr)
    _report_errors(report.errors, args.verbose)
    return 1 if outcome is not None and outcome.failed else 0


def _cmd_similar(args: argparse.Namespace) -> int:
    for path in args.paths:
        if not os.path.isdir(path):
            print(f"dupedisk: not a directory: {path}", file=sys.stderr)
            return 2
    report = find_similar_names(args.paths, args.min_size, args.exclude)
    sets = report.sets
    if args.identical:
        sets = [
            CopySet(s.original, s.size, tuple(c for c in s.copies if c.identical))
            for s in sets
            if s.reclaimable
        ]
    copies = sum(len(s.copies) for s in sets)

    if args.json:
        payload = {
            "files_scanned": report.files_scanned,
            "reclaimable": report.reclaimable,
            "sets": [
                {
                    "original": s.original,
                    "size": s.size,
                    "copies": [{"path": c.path, "size": c.size, "identical": c.identical} for c in s.copies],
                }
                for s in sets
            ],
            "skipped": len(report.errors),
        }
        print(json.dumps(payload, indent=2))
    elif not sets:
        print(f"scanned {report.files_scanned:,} files")
        print("no copy-style names found" if not args.identical else "no identical copies found")
    else:
        print(f"scanned {report.files_scanned:,} files")
        print(
            f"{copies:,} copies of {len(sets):,} originals, "
            f"{report.identical_count:,} identical, {human(report.reclaimable)} reclaimable\n"
        )
        shown = sets[: args.top] if args.top > 0 else sets
        for i, s in enumerate(shown, 1):
            print(f"[{i}] {s.original}  ({human(s.size)})")
            for c in s.copies:
                tag = "identical" if c.identical else f"differs, {human(c.size)}"
                print(f"    {tag:<{_TAG_WIDTH}}  {c.path}")
        if len(shown) < len(sets):
            print(f"\n... {len(sets) - len(shown)} more originals (--top 0 for all)")
    _report_errors(report.errors, args.verbose)
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.path):
        print(f"dupedisk: not a directory: {args.path}", file=sys.stderr)
        return 2
    errors: list[str] = []
    root = scan(args.path, args.depth, args.exclude, errors, _cutoff(args))
    if args.json:
        payload = {"since": args.since, "tree": node_to_dict(root), "skipped": len(errors)}
        print(json.dumps(payload, indent=2))
    else:
        note = f"modified in the last {args.since}" if args.since is not None else ""
        sys.stdout.write(render_tree(root, args.top, note=note))
    _report_errors(errors, args.verbose)
    return 0


def _cmd_cache(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.file):
        print(f"dupedisk: no such cache file: {args.file}", file=sys.stderr)
        return 2
    try:
        with HashCache(args.file) as cache:
            before = cache.count()
            print(f"{args.file}: {before:,} entries, {human(os.path.getsize(args.file))}")
            if args.prune:
                removed = cache.prune()
                print(f"pruned {removed:,} stale entries, {before - removed:,} remain")
    except sqlite3.Error as exc:
        print(f"dupedisk: not a usable cache ({exc})", file=sys.stderr)
        return 2
    return 0


def _cmd_largest(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.path):
        print(f"dupedisk: not a directory: {args.path}", file=sys.stderr)
        return 2
    errors: list[str] = []
    files = largest_files(args.path, args.count, args.exclude, errors, _cutoff(args))
    if args.json:
        payload = {
            "since": args.since,
            "files": [{"path": f.path, "size": f.size} for f in files],
            "skipped": len(errors),
        }
        print(json.dumps(payload, indent=2))
    else:
        for info in files:
            print(f"{human(info.size):>10}  {info.path}")
    _report_errors(errors, args.verbose)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # odd filenames must not crash a cp1252 console
    args = build_parser().parse_args(argv)
    handler = {
        "dupes": _cmd_dupes, "dedupe": _cmd_dedupe, "similar": _cmd_similar,
        "usage": _cmd_usage, "largest": _cmd_largest, "cache": _cmd_cache,
    }[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
