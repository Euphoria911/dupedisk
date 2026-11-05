import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dupedisk.cli import main
from dupedisk.dupes import PARTIAL_BYTES, find_duplicates
from dupedisk.sizes import human, parse_size
from dupedisk.usage import largest_files, render_tree, scan


def write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class SizeTests(unittest.TestCase):
    def test_human(self):
        self.assertEqual(human(0), "0 B")
        self.assertEqual(human(1023), "1023 B")
        self.assertEqual(human(1024), "1.0 KiB")
        self.assertEqual(human(5 * 1024**3), "5.0 GiB")

    def test_parse(self):
        self.assertEqual(parse_size("512"), 512)
        self.assertEqual(parse_size("10K"), 10 * 1024)
        self.assertEqual(parse_size("1.5m"), int(1.5 * 1024**2))
        self.assertEqual(parse_size("2GiB"), 2 * 1024**3)
        with self.assertRaises(ValueError):
            parse_size("ten")


class DupeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_identical_files(self):
        write(self.root, "a/x.txt", b"hello world")
        write(self.root, "b/y.txt", b"hello world")
        write(self.root, "c/z.txt", b"hello worlD")  # same size, different content
        report = find_duplicates([str(self.root)])
        self.assertEqual(len(report.groups), 1)
        names = sorted(Path(p).name for p in report.groups[0].paths)
        self.assertEqual(names, ["x.txt", "y.txt"])
        self.assertEqual(report.reclaimable, len(b"hello world"))
        self.assertEqual(report.files_scanned, 3)

    def test_full_hash_separates_files_that_share_a_prefix(self):
        head = b"A" * (PARTIAL_BYTES + 10)
        write(self.root, "one.bin", head + b"1")
        write(self.root, "two.bin", head + b"2")
        write(self.root, "three.bin", head + b"1")
        report = find_duplicates([str(self.root)])
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(
            sorted(Path(p).name for p in report.groups[0].paths), ["one.bin", "three.bin"]
        )

    def test_empty_files_ignored_by_default(self):
        write(self.root, "e1", b"")
        write(self.root, "e2", b"")
        self.assertEqual(find_duplicates([str(self.root)]).groups, [])

    def test_min_size_and_exclude(self):
        write(self.root, "a/small1", b"abc")
        write(self.root, "b/small2", b"abc")
        write(self.root, "a/big1", b"x" * 100)
        write(self.root, "b/big2", b"x" * 100)
        write(self.root, "skipme/big3", b"x" * 100)
        report = find_duplicates([str(self.root)], min_size=50, excludes=["skipme"])
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(len(report.groups[0].paths), 2)
        self.assertEqual(report.groups[0].size, 100)

    def test_hard_links_are_not_duplicates(self):
        a = write(self.root, "a.txt", b"same data")
        try:
            os.link(a, self.root / "b.txt")
        except (OSError, NotImplementedError):
            self.skipTest("hard links unsupported here")
        self.assertEqual(find_duplicates([str(self.root)]).groups, [])

    def test_overlapping_roots_do_not_double_count(self):
        write(self.root, "sub/x", b"data!")
        write(self.root, "sub/y", b"data!")
        report = find_duplicates([str(self.root), str(self.root / "sub")])
        self.assertEqual(report.files_scanned, 2)
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(len(report.groups[0].paths), 2)

    def test_groups_sorted_by_reclaimable(self):
        write(self.root, "s1", b"a" * 10)
        write(self.root, "s2", b"a" * 10)
        write(self.root, "b1", b"b" * 1000)
        write(self.root, "b2", b"b" * 1000)
        report = find_duplicates([str(self.root)])
        self.assertEqual([g.size for g in report.groups], [1000, 10])


class UsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write(self.root, "big/a.bin", b"x" * 3000)
        write(self.root, "big/deep/b.bin", b"x" * 2000)
        write(self.root, "small/c.bin", b"x" * 10)
        write(self.root, "top.bin", b"x" * 500)

    def tearDown(self):
        self._tmp.cleanup()

    def test_totals_and_order(self):
        node = scan(str(self.root), max_depth=2)
        self.assertEqual(node.size, 5510)
        self.assertEqual(node.files, 4)
        self.assertEqual([c.name for c in node.children], ["big", "top.bin", "small"])
        big = node.children[0]
        self.assertEqual(big.size, 5000)
        self.assertEqual([c.name for c in big.children], ["a.bin", "deep"])

    def test_depth_limit_keeps_totals(self):
        node = scan(str(self.root), max_depth=1)
        self.assertEqual(node.size, 5510)
        self.assertEqual(node.children[0].children, [])

    def test_exclude(self):
        node = scan(str(self.root), max_depth=1, excludes=["big"])
        self.assertEqual(node.size, 510)

    def test_render_is_ascii_and_truncates(self):
        text = render_tree(scan(str(self.root), max_depth=1), top=2)
        text.encode("ascii", errors="strict") if str(self.root).isascii() else None
        self.assertIn("... 1 more", text)
        self.assertIn("big", text)

    def test_largest(self):
        files = largest_files(str(self.root), count=2)
        self.assertEqual([Path(f.path).name for f in files], ["a.bin", "b.bin"])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        return code, out.getvalue()

    def test_dupes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "a", b"dupe-content")
            write(root, "b", b"dupe-content")
            code, out = self.run_cli("dupes", tmp, "--json")
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(len(data["groups"]), 1)
            self.assertEqual(data["reclaimable"], len(b"dupe-content"))

    def test_dupes_csv(self):
        import csv

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "a,1.txt", b"dupe-content")  # comma in the name must stay one field
            write(root, "b.txt", b"dupe-content")
            write(root, "c.txt", b"other-stuff!!")
            write(root, "d.txt", b"other-stuff!!")
            code, out = self.run_cli("dupes", tmp, "--csv")
            rows = list(csv.reader(io.StringIO(out)))
            self.assertEqual(code, 0)
            self.assertEqual(rows[0], ["set", "size_bytes", "path"])
            self.assertEqual(len(rows) - 1, 4)
            self.assertEqual({r[0] for r in rows[1:]}, {"1", "2"})
            self.assertTrue(any(r[2].endswith("a,1.txt") for r in rows[1:]))
            self.assertEqual({int(r[1]) for r in rows[1:]}, {12, 13})

    def test_csv_and_json_conflict(self):
        with self.assertRaises(SystemExit):
            self.run_cli("dupes", ".", "--csv", "--json")

    def test_dupes_text_and_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli("dupes", tmp)
            self.assertEqual(code, 0)
            self.assertIn("no duplicates found", out)
            self.assertEqual(self.run_cli("dupes", os.path.join(tmp, "nope"))[0], 2)

    def test_usage_and_largest(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "f.bin", b"x" * 2048)
            code, out = self.run_cli("usage", tmp)
            self.assertEqual(code, 0)
            self.assertIn("f.bin", out)
            code, out = self.run_cli("largest", tmp, "-n", "1")
            self.assertIn("2.0 KiB", out)


if __name__ == "__main__":
    unittest.main()
