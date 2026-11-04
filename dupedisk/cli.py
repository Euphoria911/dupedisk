from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .dupes import DupeReport, find_duplicates
from .sizes import human, parse_size
from .usage import largest_files, render_tree, scan


def _size_arg(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


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
    d.add_argument("paths", nargs="+", metavar="PATH")
    d.add_argument("-m", "--min-size", type=_size_arg, default=1, metavar="SIZE",
                   help="ignore files smaller than SIZE, e.g. 1M (default: 1 byte)")
    d.add_argument("-t", "--top", type=int, default=20,
                   help="show the N sets with the most reclaimable space (0 = all)")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    _add_common(d)

    u = sub.add_parser("usage", help="directory size tree")
    u.add_argument("path", nargs="?", default=".")
    u.add_argument("-d", "--depth", type=int, default=2, help="levels to show (default 2)")
    u.add_argument("-t", "--top", type=int, default=15, help="entries per directory (0 = all)")
    _add_common(u)

    g = sub.add_parser("largest", help="biggest files under a path")
    g.add_argument("path", nargs="?", default=".")
    g.add_argument("-n", "--count", type=int, default=20)
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


def _print_dupes(report: DupeReport, top: int) -> None:
    print(f"scanned {report.files_scanned:,} files ({human(report.bytes_scanned)})")
    if not report.groups:
        print("no duplicates found")
        return
    print(
        f"{len(report.groups):,} duplicate sets, {report.redundant_files:,} redundant files, "
        f"{human(report.reclaimable)} reclaimable\n"
    )
    shown = report.groups[:top] if top > 0 else report.groups
    for i, group in enumerate(shown, 1):
        print(f"[{i}] {len(group.paths)} x {human(group.size)}  ({human(group.reclaimable)} reclaimable)")
        for path in group.paths:
            print(f"    {path}")
    if len(shown) < len(report.groups):
        print(f"\n... {len(report.groups) - len(shown)} more sets (--top 0 for all)")


def _cmd_dupes(args: argparse.Namespace) -> int:
    for path in args.paths:
        if not os.path.isdir(path):
            print(f"dupedisk: not a directory: {path}", file=sys.stderr)
            return 2
    progress = None
    if sys.stderr.isatty() and not args.json:
        progress = lambda n: print(f"\rscanning... {n:,} files", end="", file=sys.stderr, flush=True)
    report = find_duplicates(args.paths, args.min_size, args.exclude, progress)
    if progress:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "files_scanned": report.files_scanned,
            "bytes_scanned": report.bytes_scanned,
            "reclaimable": report.reclaimable,
            "groups": [{"size": g.size, "paths": list(g.paths)} for g in report.groups],
            "skipped": len(report.errors),
        }, indent=2))
    else:
        _print_dupes(report, args.top)
    _report_errors(report.errors, args.verbose)
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.path):
        print(f"dupedisk: not a directory: {args.path}", file=sys.stderr)
        return 2
    errors: list[str] = []
    root = scan(args.path, args.depth, args.exclude, errors)
    sys.stdout.write(render_tree(root, args.top))
    _report_errors(errors, args.verbose)
    return 0


def _cmd_largest(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.path):
        print(f"dupedisk: not a directory: {args.path}", file=sys.stderr)
        return 2
    errors: list[str] = []
    for info in largest_files(args.path, args.count, args.exclude, errors):
        print(f"{human(info.size):>10}  {info.path}")
    _report_errors(errors, args.verbose)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # odd filenames must not crash a cp1252 console
    args = build_parser().parse_args(argv)
    handler = {"dupes": _cmd_dupes, "usage": _cmd_usage, "largest": _cmd_largest}[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
