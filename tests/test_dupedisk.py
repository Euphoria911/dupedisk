import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dupedisk.cache import HashCache
from dupedisk.cli import main
from dupedisk.dupes import PARTIAL_BYTES, against_reference, find_duplicates, is_within
from dupedisk.similar import find_similar_names, original_stem
from dupedisk.sizes import human, parse_age, parse_size
from dupedisk.usage import largest_files, node_to_dict, render_tree, scan
from dupedisk.walk import in_window


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

    def test_parse_age(self):
        self.assertEqual(parse_age("90s"), 90)
        self.assertEqual(parse_age("30m"), 1800)
        self.assertEqual(parse_age("12h"), 12 * 3600)
        self.assertEqual(parse_age("30d"), 30 * 86400)
        self.assertEqual(parse_age("2W"), 14 * 86400)
        self.assertEqual(parse_age("1.5d"), 1.5 * 86400)
        for bad in ("30", "d", "-5d", "3 months", ""):
            with self.assertRaises(ValueError):
                parse_age(bad)


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

    def build_reference_tree(self):
        write(self.root, "archive/a.txt", b"alpha-content")
        write(self.root, "archive/b.txt", b"bravo-content!")
        write(self.root, "archive/c1.txt", b"charlie-content")
        write(self.root, "archive/c2.txt", b"charlie-content")  # duplicated inside the reference
        write(self.root, "archive/solo.txt", b"only in the archive")
        write(self.root, "inbox/a_copy.txt", b"alpha-content")
        write(self.root, "inbox/b.txt", b"bravo-content!")
        write(self.root, "inbox/c.txt", b"charlie-content")
        write(self.root, "inbox/new.txt", b"brand new file")
        write(self.root, "inbox/x1.txt", b"twice, outside only")  # duplicated, but not in the archive
        write(self.root, "inbox/x2.txt", b"twice, outside only")

    def test_is_within(self):
        base = str(self.root / "archive")
        self.assertTrue(is_within(base, base))
        self.assertTrue(is_within(str(self.root / "archive" / "a" / "b.txt"), base))
        self.assertTrue(is_within(str(self.root / "archive" / "x"), base + os.sep))
        self.assertFalse(is_within(str(self.root / "archive2" / "a.txt"), base))  # shared prefix only
        self.assertFalse(is_within(str(self.root), base))

    def test_against_reference_keeps_only_sets_that_straddle_it(self):
        self.build_reference_tree()
        archive = str(self.root / "archive")
        report = find_duplicates([str(self.root / "inbox"), archive])
        self.assertEqual(len(report.groups), 4)  # a, b, c, and the outside-only x pair
        groups = against_reference(report.groups, archive)
        names = {tuple(sorted(Path(p).name for p in g.paths)) for g in groups}
        self.assertEqual(
            names,
            {("a.txt", "a_copy.txt"), ("b.txt", "b.txt"), ("c.txt", "c1.txt", "c2.txt")},
        )
        by_size = {g.size: g for g in groups}
        c = by_size[len(b"charlie-content")]
        self.assertEqual(len(c.keep), 2)
        self.assertEqual([Path(p).parent.name for p in c.extras], ["inbox"])
        self.assertEqual(c.reclaimable, len(b"charlie-content"))  # one outside copy, not two
        self.assertTrue(all(is_within(p, archive) for g in groups for p in g.keep))
        self.assertTrue(all(not is_within(p, archive) for g in groups for p in g.extras))
        report.groups = groups
        self.assertEqual(report.redundant_files, 3)

    def test_against_reference_with_nothing_to_report(self):
        write(self.root, "archive/a.txt", b"one")
        write(self.root, "inbox/b.txt", b"two")
        report = find_duplicates([str(self.root / "inbox"), str(self.root / "archive")])
        self.assertEqual(against_reference(report.groups, str(self.root / "archive")), [])

    def test_plain_groups_still_reclaim_all_but_one(self):
        write(self.root, "a", b"same!")
        write(self.root, "b", b"same!")
        write(self.root, "c", b"same!")
        (group,) = find_duplicates([str(self.root)]).groups
        self.assertEqual((group.keep, group.reclaimable), ((), 2 * len(b"same!")))

    def build_mixed_tree(self):
        big = b"A" * (PARTIAL_BYTES + 10)
        for i in range(6):  # six identical big files, three near-misses sharing their head
            write(self.root, f"big/same{i}.bin", big + b"1")
        for i in range(3):
            write(self.root, f"big/near{i}.bin", big + bytes([50 + i]))
        for i in range(8):  # small sets of 2-3
            for j in range(2 + i % 2):
                write(self.root, f"small{i}/f{j}.txt", f"content-{i}".encode() * 3)
        write(self.root, "lone.txt", b"nobody else is this size, really")

    def test_jobs_give_the_same_result_as_serial(self):
        self.build_mixed_tree()
        serial = find_duplicates([str(self.root)], jobs=1)
        for jobs in (2, 4, 16):
            report = find_duplicates([str(self.root)], jobs=jobs)
            self.assertEqual(report.groups, serial.groups, jobs)
            self.assertEqual(report.errors, [])
        self.assertEqual(len(serial.groups), 9)  # 1 big set + 8 small sets
        self.assertEqual(max(len(g.paths) for g in serial.groups), 6)

    def test_jobs_with_cache_counts_and_reuse(self):
        self.build_mixed_tree()
        db = str(self.root.parent / (self.root.name + ".db"))
        try:
            with HashCache(db) as cache:
                cold = find_duplicates([str(self.root)], cache=cache, jobs=4)
            with HashCache(db) as cache:
                warm = find_duplicates([str(self.root)], cache=cache, jobs=4)
            self.assertEqual(cold.groups, warm.groups)
            self.assertEqual(cold.cache_hits, 0)
            self.assertGreater(cold.cache_misses, 0)
            self.assertEqual(warm.cache_misses, 0)
            self.assertEqual(warm.cache_hits, cold.cache_misses)
        finally:
            if os.path.exists(db):
                os.remove(db)

    def test_unreadable_file_is_recorded_and_the_rest_still_hash(self):
        from unittest import mock

        from dupedisk import dupes

        self.build_mixed_tree()
        real = dupes._digest

        def flaky(path, limit):
            if path.endswith("same0.bin"):
                raise PermissionError(13, "Permission denied")
            return real(path, limit)

        with mock.patch.object(dupes, "_digest", flaky):
            report = find_duplicates([str(self.root)], jobs=4)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("same0.bin", report.errors[0])
        big = max(report.groups, key=lambda g: len(g.paths))
        self.assertEqual(len(big.paths), 5)  # same1..same5; same0 dropped, near* told apart

    def test_groups_sorted_by_reclaimable(self):
        write(self.root, "s1", b"a" * 10)
        write(self.root, "s2", b"a" * 10)
        write(self.root, "b1", b"b" * 1000)
        write(self.root, "b2", b"b" * 1000)
        report = find_duplicates([str(self.root)])
        self.assertEqual([g.size for g in report.groups], [1000, 10])


class SimilarTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_original_stem(self):
        self.assertEqual(original_stem("report (1)"), "report")
        self.assertEqual(original_stem("report - Copy"), "report")
        self.assertEqual(original_stem("report - Copy (3)"), "report")
        self.assertEqual(original_stem("report copy 2"), "report")
        self.assertEqual(original_stem("report_copy"), "report")
        self.assertEqual(original_stem("Copy of report"), "report")
        self.assertEqual(original_stem("Copy (2) of report"), "report")
        self.assertEqual(original_stem("a (1) (2)"), "a (1)")
        for plain in ("report", "report (final)", "copyright", "photocopy", "(1)", "version 2"):
            self.assertIsNone(original_stem(plain), plain)

    def test_identical_and_edited_copies(self):
        write(self.root, "doc.txt", b"original text")
        write(self.root, "doc (1).txt", b"original text")
        write(self.root, "doc - Copy.txt", b"edited text!!")
        write(self.root, "doc copy.txt", b"edited")  # different size, different content
        report = find_similar_names([str(self.root)])
        self.assertEqual(len(report.sets), 1)
        s = report.sets[0]
        self.assertEqual(Path(s.original).name, "doc.txt")
        verdicts = {Path(c.path).name: c.identical for c in s.copies}
        self.assertEqual(
            verdicts, {"doc (1).txt": True, "doc - Copy.txt": False, "doc copy.txt": False}
        )
        self.assertEqual(s.reclaimable, len(b"original text"))
        self.assertEqual((report.copy_count, report.identical_count), (3, 1))

    def test_needs_an_original_in_the_same_folder_and_extension(self):
        write(self.root, "lonely (1).jpg", b"x")
        write(self.root, "a/pic.jpg", b"x")
        write(self.root, "b/pic (1).jpg", b"x")  # original is in another folder
        write(self.root, "c/note.txt", b"x")
        write(self.root, "c/note (1).md", b"x")  # same stem, other extension
        self.assertEqual(find_similar_names([str(self.root)]).sets, [])

    def test_case_insensitive_match(self):
        write(self.root, "Report.PDF", b"same")
        write(self.root, "report (1).pdf", b"same")
        self.assertEqual(len(find_similar_names([str(self.root)]).sets), 1)

    def test_hard_link_to_original_is_not_a_copy(self):
        write(self.root, "f.bin", b"data")
        try:
            os.link(self.root / "f.bin", self.root / "f (1).bin")
        except OSError:
            self.skipTest("hard links unavailable")
        self.assertEqual(find_similar_names([str(self.root)]).sets, [])

    def test_min_size_and_excludes(self):
        write(self.root, "k.bin", b"x" * 10)
        write(self.root, "k (1).bin", b"x" * 10)
        self.assertEqual(find_similar_names([str(self.root)], min_size=11).sets, [])
        self.assertEqual(find_similar_names([str(self.root)], excludes=["* (1).bin"]).sets, [])

    def test_sorted_by_reclaimable(self):
        write(self.root, "small.txt", b"s" * 5)
        write(self.root, "small (1).txt", b"s" * 5)
        write(self.root, "big.txt", b"b" * 500)
        write(self.root, "big (1).txt", b"b" * 500)
        names = [Path(s.original).name for s in find_similar_names([str(self.root)]).sets]
        self.assertEqual(names, ["big.txt", "small.txt"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "data"
        self.db = str(Path(self._tmp.name) / "cache.db")
        write(self.root, "a.txt", b"same bytes")
        write(self.root, "b.txt", b"same bytes")

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self):
        with HashCache(self.db) as cache:
            return find_duplicates([str(self.root)], cache=cache)

    def test_second_run_is_served_from_cache(self):
        first = self.scan()
        self.assertEqual((first.cache_hits, first.cache_misses), (0, 2))
        second = self.scan()
        self.assertEqual((second.cache_hits, second.cache_misses), (2, 0))
        self.assertEqual(second.groups, first.groups)

    def test_changed_file_is_rehashed(self):
        self.scan()
        target = self.root / "b.txt"
        target.write_bytes(b"other byte")  # same size, new content
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))  # force a different mtime
        report = self.scan()
        self.assertEqual((report.cache_hits, report.cache_misses), (1, 1))
        self.assertEqual(report.groups, [])

    def test_large_files_cache_both_stages(self):
        big = b"Z" * (PARTIAL_BYTES + 5)
        write(self.root, "big1.bin", big)
        write(self.root, "big2.bin", big)
        first = self.scan()  # 2 small heads + 2 big heads + 2 big fulls
        self.assertEqual(first.cache_misses, 6)
        second = self.scan()
        self.assertEqual((second.cache_hits, second.cache_misses), (6, 0))

    def test_prune_drops_missing_and_changed_files(self):
        big = b"Z" * (PARTIAL_BYTES + 5)
        write(self.root, "big1.bin", big)
        write(self.root, "big2.bin", big)
        self.scan()  # a, b: 1 entry each; big1, big2: 2 entries each (head + full)
        (self.root / "a.txt").unlink()  # gone: 1 entry
        target = self.root / "big2.bin"
        target.write_bytes(b"Y" * (PARTIAL_BYTES + 5))  # changed: 2 entries
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))
        with HashCache(self.db) as cache:
            self.assertEqual(cache.count(), 6)
            self.assertEqual(cache.prune(), 3)
            self.assertEqual(cache.count(), 3)
            self.assertEqual(cache.prune(), 0)

    def test_cli_cache_command(self):
        self.scan()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["cache", self.db]), 0)
            self.assertEqual(main(["cache", self.db, "--prune"]), 0)
        self.assertIn("2 entries", out.getvalue())
        self.assertIn("pruned 0 stale entries, 2 remain", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["cache", self.db + ".missing"]), 2)
            junk = Path(self._tmp.name) / "junk.db"
            junk.write_bytes(b"this is not sqlite at all" * 50)
            self.assertEqual(main(["cache", str(junk)]), 2)

    def test_cache_survives_roundtrip_and_is_per_kind(self):
        with HashCache(self.db) as cache:
            cache.put("x", "full", 5, 7, "abc")
            self.assertIsNone(cache.get("x", "head1", 5, 7))
            self.assertIsNone(cache.get("x", "full", 5, 8))
        with HashCache(self.db) as cache:
            self.assertEqual(cache.get("x", "full", 5, 7), "abc")


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

    def age_files(self, days_old: dict[str, float]) -> float:
        """Backdate files; returns a cutoff 10 days ago."""
        now = os.path.getmtime(self.root / "top.bin")
        for rel, days in days_old.items():
            stamp = now - days * 86400
            os.utime(self.root / rel, (stamp, stamp))
        return now - 10 * 86400

    def test_modified_after_counts_only_recent_files(self):
        cutoff = self.age_files({"big/a.bin": 30, "small/c.bin": 30})
        node = scan(str(self.root), max_depth=2, modified_after=cutoff)
        self.assertEqual(node.size, 2500)  # b.bin (2000) + top.bin (500)
        self.assertEqual(node.files, 2)
        self.assertEqual([c.name for c in node.children], ["big", "top.bin"])
        big = node.children[0]
        self.assertEqual(big.size, 2000)
        self.assertEqual([c.name for c in big.children], ["deep"])

    def test_modified_after_drops_directories_with_nothing_recent(self):
        cutoff = self.age_files({"small/c.bin": 30})
        names = [c.name for c in scan(str(self.root), 2, modified_after=cutoff).children]
        self.assertNotIn("small", names)
        # without the filter the same directory is still there
        self.assertIn("small", [c.name for c in scan(str(self.root), 2).children])

    def test_modified_after_future_cutoff_is_empty(self):
        node = scan(str(self.root), 2, modified_after=os.path.getmtime(self.root / "top.bin") + 3600)
        self.assertEqual((node.size, node.files, node.children), (0, 0, []))

    def test_largest_modified_after(self):
        cutoff = self.age_files({"big/a.bin": 30})
        names = [Path(f.path).name for f in largest_files(str(self.root), 10, modified_after=cutoff)]
        self.assertEqual(names, ["b.bin", "top.bin", "c.bin"])  # a.bin (3000 B) is too old
        self.assertEqual(len(largest_files(str(self.root), 10)), 4)

    def test_node_to_dict(self):
        data = node_to_dict(scan(str(self.root), max_depth=2))
        self.assertEqual((data["type"], data["size"], data["files"]), ("dir", 5510, 4))
        self.assertEqual([c["name"] for c in data["children"]], ["big", "top.bin", "small"])
        big = data["children"][0]
        self.assertEqual([c["name"] for c in big["children"]], ["a.bin", "deep"])
        leaf = big["children"][0]
        self.assertEqual((leaf["type"], leaf["size"]), ("file", 3000))
        self.assertNotIn("children", leaf)
        json.dumps(data)  # plain data only

    def test_in_window_boundaries(self):
        self.assertTrue(in_window(100, None, None))
        self.assertTrue(in_window(100, 100, None))  # "after" is inclusive
        self.assertFalse(in_window(99.9, 100, None))
        self.assertFalse(in_window(100, None, 100))  # "before" is exclusive
        self.assertTrue(in_window(99.9, None, 100))
        self.assertTrue(in_window(150, 100, 200))
        self.assertFalse(in_window(250, 100, 200))

    def test_modified_before_keeps_only_stale_files(self):
        cutoff = self.age_files({"big/a.bin": 30, "small/c.bin": 30})
        node = scan(str(self.root), max_depth=2, modified_before=cutoff)
        self.assertEqual((node.size, node.files), (3010, 2))  # a.bin + c.bin
        self.assertEqual([c.name for c in node.children], ["big", "small"])
        big = node.children[0]
        self.assertEqual([c.name for c in big.children], ["a.bin"])  # deep/ and top.bin are recent

    def test_since_and_older_than_make_a_window(self):
        now = os.path.getmtime(self.root / "top.bin")
        self.age_files({"big/a.bin": 30, "big/deep/b.bin": 20, "small/c.bin": 5})
        node = scan(str(self.root), 2, modified_after=now - 25 * 86400, modified_before=now - 10 * 86400)
        self.assertEqual((node.size, node.files), (2000, 1))  # only b.bin sits in the window
        self.assertEqual(scan(str(self.root), 2, modified_after=now - 1, modified_before=now - 2).files, 0)

    def test_largest_modified_before(self):
        cutoff = self.age_files({"big/a.bin": 30, "big/deep/b.bin": 40})
        names = [Path(f.path).name for f in largest_files(str(self.root), 10, modified_before=cutoff)]
        self.assertEqual(names, ["a.bin", "b.bin"])
        self.assertEqual(largest_files(str(self.root), 1, modified_before=cutoff)[0].size, 3000)

    def test_render_note(self):
        text = render_tree(scan(str(self.root), 1), note="modified in the last 30d")
        self.assertIn("in 4 files modified in the last 30d", text.splitlines()[0])


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

    def test_cli_cache_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "d"
            write(root, "a", b"dupe-content")
            write(root, "b", b"dupe-content")
            db = os.path.join(tmp, "c.db")
            _, out = self.run_cli("dupes", str(root), "--cache", db)
            self.assertIn("cache: 0 hits, 2 misses", out)
            _, out = self.run_cli("dupes", str(root), "--cache", db, "--json")
            self.assertEqual(json.loads(out)["cache"], {"hits": 2, "misses": 0})
            bad = os.path.join(tmp, "no", "such", "dir", "c.db")
            self.assertEqual(self.run_cli("dupes", str(root), "--cache", bad)[0], 2)

    def test_against_text_json_and_csv(self):
        import csv

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "archive/a.txt", b"alpha-content")
            write(root, "inbox/a_copy.txt", b"alpha-content")
            write(root, "inbox/new.txt", b"brand new file")
            inbox, archive = str(root / "inbox"), str(root / "archive")

            code, out = self.run_cli("dupes", inbox, "--against", archive)
            self.assertEqual(code, 0)
            self.assertIn("reference (kept):", out)
            self.assertIn("1 duplicate sets, 1 redundant files", out)
            self.assertRegex(out, r"keep  .*a\.txt")
            self.assertRegex(out, r"dupe  .*a_copy\.txt")

            data = json.loads(self.run_cli("dupes", inbox, "--against", archive, "--json")[1])
            self.assertEqual(data["against"], archive)
            self.assertEqual(data["reclaimable"], len(b"alpha-content"))
            (group,) = data["groups"]
            self.assertEqual([Path(p).name for p in group["keep"]], ["a.txt"])

            rows = list(csv.reader(io.StringIO(self.run_cli("dupes", inbox, "--against", archive, "--csv")[1])))
            self.assertEqual(rows[0], ["set", "size_bytes", "path", "role"])
            self.assertEqual({(Path(r[2]).name, r[3]) for r in rows[1:]},
                             {("a.txt", "keep"), ("a_copy.txt", "dupe")})

            plain = json.loads(self.run_cli("dupes", inbox, archive, "--json")[1])
            self.assertNotIn("against", plain)
            self.assertNotIn("keep", plain["groups"][0])  # output without --against is unchanged

    def test_against_errors_and_empty_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "archive/a.txt", b"one")
            write(root, "archive/sub/b.txt", b"two")
            write(root, "inbox/c.txt", b"three")
            inbox, archive = str(root / "inbox"), str(root / "archive")
            self.assertEqual(self.run_cli("dupes", inbox, "--against", str(root / "nope"))[0], 2)
            self.assertEqual(self.run_cli("dupes", str(root / "archive" / "sub"), "--against", archive)[0], 2)
            self.assertEqual(self.run_cli("dupes", archive, "--against", archive)[0], 2)
            code, out = self.run_cli("dupes", inbox, "--against", archive)
            self.assertEqual(code, 0)
            self.assertIn("nothing outside the reference is duplicated in it", out)

    def test_jobs_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "a", b"dupe-content")
            write(Path(tmp), "b", b"dupe-content")
            reference = json.loads(self.run_cli("dupes", tmp, "--json")[1])["groups"]
            for jobs in ("1", "4", "0"):
                code, out = self.run_cli("dupes", tmp, "-j", jobs, "--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["groups"], reference)
            for bad in ("-1", "many", "1.5"):
                with self.assertRaises(SystemExit):
                    self.run_cli("dupes", tmp, "--jobs", bad)

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

    def test_similar_text_json_and_identical_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "a.txt", b"same-bytes")
            write(root, "a (1).txt", b"same-bytes")
            write(root, "b.txt", b"first")
            write(root, "b (1).txt", b"other!")
            code, out = self.run_cli("similar", tmp)
            self.assertEqual(code, 0)
            self.assertIn("2 copies of 2 originals, 1 identical", out)
            self.assertIn("identical", out)
            self.assertIn("differs, 6 B", out)
            code, out = self.run_cli("similar", tmp, "--identical")
            self.assertIn("1 copies of 1 originals", out)
            self.assertNotIn("differs", out)
            self.assertNotIn("b (1).txt", out)
            _, out = self.run_cli("similar", tmp, "--json")
            data = json.loads(out)
            self.assertEqual(len(data["sets"]), 2)
            self.assertEqual(data["reclaimable"], len(b"same-bytes"))
            self.assertEqual(self.run_cli("similar", os.path.join(tmp, "nope"))[0], 2)

    def test_similar_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "x.txt", b"x")
            _, out = self.run_cli("similar", tmp)
            self.assertIn("no copy-style names found", out)

    def test_usage_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = write(Path(tmp), "old.bin", b"x" * 4096)
            write(Path(tmp), "new.bin", b"x" * 1024)
            stamp = old.stat().st_mtime - 60 * 86400
            os.utime(old, (stamp, stamp))
            code, out = self.run_cli("usage", tmp, "--since", "30d")
            self.assertEqual(code, 0)
            self.assertIn("modified in the last 30d", out)
            self.assertIn("in 1 files", out)
            self.assertIn("new.bin", out)
            self.assertNotIn("old.bin", out)
            _, out = self.run_cli("usage", tmp, "--since", "90d")
            self.assertIn("old.bin", out)

    def test_usage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "d/a.bin", b"x" * 100)
            write(Path(tmp), "b.bin", b"x" * 40)
            code, out = self.run_cli("usage", tmp, "--json", "-t", "1")  # --top is ignored
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertIsNone(data["since"])
            self.assertEqual(data["tree"]["size"], 140)
            self.assertEqual([c["name"] for c in data["tree"]["children"]], ["d", "b.bin"])
            self.assertEqual(data["skipped"], 0)

    def test_usage_json_with_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = write(Path(tmp), "old.bin", b"x" * 100)
            write(Path(tmp), "new.bin", b"x" * 40)
            stamp = old.stat().st_mtime - 90 * 86400
            os.utime(old, (stamp, stamp))
            data = json.loads(self.run_cli("usage", tmp, "--json", "--since", "7d")[1])
            self.assertEqual(data["since"], "7d")
            self.assertEqual(data["tree"]["size"], 40)

    def test_largest_json_and_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = write(Path(tmp), "old.bin", b"x" * 500)
            write(Path(tmp), "new.bin", b"x" * 40)
            stamp = old.stat().st_mtime - 90 * 86400
            os.utime(old, (stamp, stamp))
            data = json.loads(self.run_cli("largest", tmp, "--json")[1])
            self.assertEqual([f["size"] for f in data["files"]], [500, 40])
            data = json.loads(self.run_cli("largest", tmp, "--json", "--since", "7d")[1])
            self.assertEqual(data["since"], "7d")
            self.assertEqual([f["size"] for f in data["files"]], [40])
            _, text = self.run_cli("largest", tmp, "--since", "7d")
            self.assertIn("new.bin", text)
            self.assertNotIn("old.bin", text)
            with self.assertRaises(SystemExit):
                self.run_cli("largest", tmp, "--since", "7")

    def test_usage_and_largest_older_than(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = write(Path(tmp), "old.bin", b"x" * 500)
            write(Path(tmp), "new.bin", b"x" * 40)
            mid = write(Path(tmp), "mid.bin", b"x" * 90)
            now = old.stat().st_mtime
            os.utime(old, (now - 400 * 86400,) * 2)
            os.utime(mid, (now - 40 * 86400,) * 2)

            code, out = self.run_cli("usage", tmp, "--older-than", "30d")
            self.assertEqual(code, 0)
            self.assertIn("untouched for 30d", out.splitlines()[0])
            self.assertIn("in 2 files", out)
            self.assertNotIn("new.bin", out)

            data = json.loads(self.run_cli("usage", tmp, "--older-than", "365d", "--json")[1])
            self.assertEqual((data["older_than"], data["since"], data["tree"]["size"]), ("365d", None, 500))

            # a window: modified within 100 days but not within 30
            header = self.run_cli("usage", tmp, "--since", "100d", "--older-than", "30d")[1].splitlines()[0]
            self.assertIn("modified in the last 100d, untouched for 30d", header)
            self.assertIn("in 1 files", header)

            data = json.loads(self.run_cli("largest", tmp, "--older-than", "30d", "--json")[1])
            self.assertEqual([f["size"] for f in data["files"]], [500, 90])
            self.assertEqual(data["older_than"], "30d")
            _, text = self.run_cli("largest", tmp, "--older-than", "30d", "-n", "1")
            self.assertIn("old.bin", text)
            self.assertNotIn("mid.bin", text)
            with self.assertRaises(SystemExit):
                self.run_cli("usage", tmp, "--older-than", "1y")  # no year unit

    def test_usage_since_rejects_bad_age(self):
        with self.assertRaises(SystemExit):
            self.run_cli("usage", ".", "--since", "30")


if __name__ == "__main__":
    unittest.main()
