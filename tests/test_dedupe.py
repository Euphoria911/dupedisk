import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dupedisk.cli import main
from dupedisk.dedupe import Plan, apply_plans, matches_any, plan_against, plan_by_rule
from dupedisk.dupes import DupeGroup, against_reference, find_duplicates


def write(root: Path, rel: str, data: bytes, mtime: float | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def groups(self, *roots):
        return find_duplicates([str(self.root / r) for r in roots] or [str(self.root)]).groups


class PlanTests(TempTree):
    def four_copies(self):
        # each file is the winner of exactly one rule
        write(self.root, "a/long-name-here.bin", b"same-bytes", mtime=2_000_000)  # first (alphabetical)
        write(self.root, "b/old.bin", b"same-bytes", mtime=1_000_000)  # oldest
        write(self.root, "c/new.bin", b"same-bytes", mtime=4_000_000)  # newest
        write(self.root, "d/s.bin", b"same-bytes", mtime=3_000_000)  # shortest path
        return self.groups()

    def kept(self, rule):
        (plan,) = plan_by_rule(self.four_copies(), rule)
        return Path(plan.keep[0]).parent.name

    def test_keep_rules(self):
        self.assertEqual(self.kept("oldest"), "b")
        self.assertEqual(self.kept("newest"), "c")
        self.assertEqual(self.kept("shortest-path"), "d")
        self.assertEqual(self.kept("first"), "a")

    def test_a_plan_partitions_its_set_and_keeps_exactly_one(self):
        group = self.four_copies()[0]
        for rule in ("oldest", "newest", "shortest-path", "first"):
            (plan,) = plan_by_rule([group], rule)
            self.assertEqual(len(plan.keep), 1)
            self.assertEqual(sorted(plan.keep + plan.remove), sorted(group.paths))
            self.assertEqual(plan.freed, len(b"same-bytes") * 3)

    def test_ties_break_by_path_so_plans_are_reproducible(self):
        for name in ("b.bin", "a.bin", "c.bin"):
            write(self.root, name, b"tie", mtime=5_000_000)
        (plan,) = plan_by_rule(self.groups(), "oldest")
        self.assertEqual(Path(plan.keep[0]).name, "a.bin")
        (again,) = plan_by_rule(list(reversed(self.groups())), "oldest")
        self.assertEqual(again, plan)

    def test_an_unreadable_path_is_never_the_one_kept(self):
        real = write(self.root, "real.bin", b"x" * 5, mtime=9_999_999_999)  # the newest, so "oldest" would skip it
        ghost = str(self.root / "ghost.bin")  # does not exist: stat fails
        for rule in ("oldest", "newest", "shortest-path", "first"):
            (plan,) = plan_by_rule([DupeGroup(5, (ghost, str(real)))], rule)
            self.assertEqual(plan.keep, (str(real),), rule)

    def test_unknown_rule(self):
        with self.assertRaises(ValueError):
            plan_by_rule([], "biggest")

    def test_matches_any(self):
        path = r"C:\Users\Me\Documents\Photos\a.jpg"
        self.assertTrue(matches_any(path, ["documents"]))  # substring, any case
        self.assertTrue(matches_any(path, ["nothing", "PHOTOS"]))
        self.assertTrue(matches_any(path, ["*/photos/*.jpg"]))  # glob on the whole path
        self.assertTrue(matches_any(path, [r"*\Documents\*"]))  # backslashes work in patterns too
        self.assertFalse(matches_any(path, ["*.png"]))
        self.assertFalse(matches_any(path, ["downloads"]))
        self.assertFalse(matches_any(path, []))

    def kept_with(self, rule, prefer=(), avoid=()):
        (plan,) = plan_by_rule(self.four_copies(), rule, prefer, avoid)
        return Path(plan.keep[0]).parent.name

    def test_prefer_beats_the_rule(self):
        self.assertEqual(self.kept_with("oldest"), "b")
        self.assertEqual(self.kept_with("oldest", prefer=["*/d/*"]), "d")
        self.assertEqual(self.kept_with("newest", prefer=["*/a/*", "*/d/*"]), "d")  # any pattern, then the rule

    def test_avoid_removes_those_copies_first(self):
        self.assertEqual(self.kept_with("oldest", avoid=["*/b/*"]), "a")  # b is oldest but avoided
        self.assertEqual(self.kept_with("oldest", avoid=["*/b/*", "*/a/*"]), "d")

    def test_when_every_copy_is_avoided_or_preferred_the_rule_decides(self):
        self.assertEqual(self.kept_with("oldest", avoid=["*"]), "b")
        self.assertEqual(self.kept_with("newest", prefer=["*"]), "c")

    def test_prefer_and_avoid_together(self):
        # c is both preferred and avoided: avoid wins, so c goes and the oldest of a, b, d stays
        self.assertEqual(self.kept_with("oldest", prefer=["*/c/*"], avoid=["*/c/*"]), "b")

    def test_an_unreadable_copy_is_not_kept_even_if_preferred(self):
        real = write(self.root, "real.bin", b"x" * 5)
        ghost = str(self.root / "ghost.bin")
        (plan,) = plan_by_rule([DupeGroup(5, (ghost, str(real)))], "first", prefer=["ghost"])
        self.assertEqual(plan.keep, (str(real),))

    def test_against_plan_keeps_the_reference_copies(self):
        write(self.root, "archive/a.txt", b"alpha-content")
        write(self.root, "archive/a2.txt", b"alpha-content")
        write(self.root, "inbox/a_copy.txt", b"alpha-content")
        write(self.root, "inbox/lonely.txt", b"nothing like it")
        groups = against_reference(self.groups("inbox", "archive"), str(self.root / "archive"))
        (plan,) = plan_against(groups)
        self.assertEqual({Path(p).name for p in plan.keep}, {"a.txt", "a2.txt"})
        self.assertEqual([Path(p).name for p in plan.remove], ["a_copy.txt"])


class ApplyTests(TempTree):
    def plan_for(self, keep: Path, *remove: Path) -> Plan:
        return Plan(keep.stat().st_size, (str(keep),), tuple(str(p) for p in remove))

    def test_removes_only_the_planned_copies(self):
        keep = write(self.root, "keep.txt", b"identical")
        a = write(self.root, "a.txt", b"identical")
        b = write(self.root, "b.txt", b"identical")
        other = write(self.root, "other.txt", b"something else")
        seen = []
        out = apply_plans([self.plan_for(keep, a, b)], seen.append)
        self.assertEqual(sorted(out.removed), sorted([str(a), str(b)]))
        self.assertEqual((out.freed, out.failed), (2 * len(b"identical"), []))
        self.assertEqual(sorted(seen), sorted(out.removed))
        self.assertTrue(keep.exists() and other.exists())
        self.assertFalse(a.exists() or b.exists())

    def test_edited_copy_with_the_same_size_is_left_alone(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        plan = self.plan_for(keep, victim)
        victim.write_bytes(b"IDENTICAL")  # edited after the scan, same length
        out = apply_plans([plan])
        self.assertEqual(out.removed, [])
        self.assertIn("no longer identical", out.failed[0][1])
        self.assertEqual(victim.read_bytes(), b"IDENTICAL")

    def test_copy_that_changed_size_is_left_alone(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        plan = self.plan_for(keep, victim)
        victim.write_bytes(b"identical, then appended to")
        out = apply_plans([plan])
        self.assertEqual(out.removed, [])
        self.assertIn("changed since the scan", out.failed[0][1])
        self.assertTrue(victim.exists())

    def test_nothing_is_removed_when_the_kept_copy_is_gone_or_changed(self):
        for damage in ("deleted", "changed size", "unreadable path"):
            with self.subTest(damage):
                keep = write(self.root, f"{damage}/keep.txt", b"identical")
                victim = write(self.root, f"{damage}/victim.txt", b"identical")
                plan = self.plan_for(keep, victim)
                if damage == "deleted":
                    keep.unlink()
                elif damage == "changed size":
                    keep.write_bytes(b"short")
                else:
                    plan = Plan(plan.size, (str(self.root / "no" / "such.txt"),), plan.remove)
                out = apply_plans([plan])
                self.assertEqual(out.removed, [])
                self.assertIn("no kept copy", out.failed[0][1])
                self.assertTrue(victim.exists())  # the last readable copy always survives

    def test_second_kept_copy_is_used_when_the_first_is_gone(self):
        k1 = write(self.root, "k1.txt", b"identical")
        k2 = write(self.root, "k2.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        plan = Plan(len(b"identical"), (str(k1), str(k2)), (str(victim),))
        k1.unlink()
        out = apply_plans([plan])
        self.assertEqual(out.removed, [str(victim)])
        self.assertTrue(k2.exists())

    def test_hard_link_to_the_kept_copy_is_not_deleted(self):
        keep = write(self.root, "keep.txt", b"identical")
        link = self.root / "link.txt"
        try:
            os.link(keep, link)
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable")
        out = apply_plans([self.plan_for(keep, link)])
        self.assertEqual(out.removed, [])
        self.assertIn("hard link", out.failed[0][1])
        self.assertTrue(link.exists())

    def test_the_kept_path_listed_as_a_victim_is_never_deleted(self):
        keep = write(self.root, "keep.txt", b"identical")
        out = apply_plans([Plan(len(b"identical"), (str(keep),), (str(keep),))])
        self.assertEqual(out.removed, [])
        self.assertTrue(keep.exists())

    def test_already_missing_victim_is_reported_and_the_rest_continue(self):
        keep = write(self.root, "keep.txt", b"identical")
        gone = write(self.root, "gone.txt", b"identical")
        there = write(self.root, "there.txt", b"identical")
        plan = self.plan_for(keep, gone, there)
        gone.unlink()
        out = apply_plans([plan])
        self.assertEqual(out.removed, [str(there)])
        self.assertEqual([p for p, _ in out.failed], [str(gone)])

    @unittest.skipUnless(os.name == "nt", "read-only files block deletion on Windows only")
    def test_read_only_file_is_reported_not_forced(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        os.chmod(victim, stat.S_IREAD)
        try:
            out = apply_plans([self.plan_for(keep, victim)])
            self.assertEqual(out.removed, [])
            self.assertEqual([p for p, _ in out.failed], [str(victim)])
            self.assertTrue(victim.exists())
        finally:
            os.chmod(victim, stat.S_IWRITE)

    def test_every_set_keeps_a_copy_whatever_goes_wrong(self):
        sets = []
        for i in range(4):
            files = [write(self.root, f"s{i}/f{j}.txt", f"set-{i}".encode() * 3) for j in range(3)]
            sets.append(files)
        plans = plan_by_rule(self.groups(), "first")
        sets[1][0].write_bytes(b"x" * 18)  # a kept copy changes size
        sets[2][1].unlink()  # a victim vanishes
        sets[3][2].write_bytes(b"y" * 18)  # a victim is edited
        apply_plans(plans)
        for files in sets:
            self.assertTrue(any(f.exists() for f in files))


def hardlinks_work(directory: Path) -> bool:
    a = directory / "probe-a"
    a.write_bytes(b"x")
    try:
        os.link(a, directory / "probe-b")
    except (OSError, NotImplementedError):
        return False
    finally:
        for name in ("probe-a", "probe-b"):
            try:
                (directory / name).unlink()
            except OSError:
                pass
    return True


class HardlinkTests(TempTree):
    def setUp(self):
        super().setUp()
        if not hardlinks_work(self.root):
            self.skipTest("hard links unavailable here")

    def leftovers(self):
        return [p.name for p in self.root.rglob("*") if ".dupedisk-" in p.name]

    def plan_for(self, keep: Path, *victims: Path) -> Plan:
        return Plan(keep.stat().st_size, (str(keep),), tuple(str(v) for v in victims))

    def test_victims_become_links_and_every_path_survives(self):
        keep = write(self.root, "keep.txt", b"identical")
        a = write(self.root, "sub/a.txt", b"identical")
        b = write(self.root, "b.txt", b"identical")
        out = apply_plans([self.plan_for(keep, a, b)], link=True)
        self.assertEqual((sorted(out.removed), out.failed), (sorted([str(a), str(b)]), []))
        self.assertEqual(out.freed, 2 * len(b"identical"))
        for path in (keep, a, b):
            self.assertEqual(path.read_bytes(), b"identical")
        self.assertTrue(os.path.samefile(a, keep) and os.path.samefile(b, keep))
        self.assertEqual(self.leftovers(), [])

    def test_a_linked_set_is_no_longer_reported_as_duplicates(self):
        keep = write(self.root, "keep.txt", b"identical")
        a = write(self.root, "a.txt", b"identical")
        (plan,) = plan_by_rule(find_duplicates([str(self.root)]).groups, "first")
        apply_plans([plan], link=True)
        self.assertEqual(find_duplicates([str(self.root)]).groups, [])
        self.assertTrue(keep.exists() and a.exists())

    def test_edited_copy_is_not_replaced(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        plan = self.plan_for(keep, victim)
        victim.write_bytes(b"IDENTICAL")
        out = apply_plans([plan], link=True)
        self.assertEqual(out.removed, [])
        self.assertFalse(os.path.samefile(victim, keep))
        self.assertEqual(victim.read_bytes(), b"IDENTICAL")
        self.assertEqual(self.leftovers(), [])

    def test_already_linked_pair_is_left_alone(self):
        keep = write(self.root, "keep.txt", b"identical")
        twin = self.root / "twin.txt"
        os.link(keep, twin)
        out = apply_plans([self.plan_for(keep, twin)], link=True)
        self.assertEqual(out.removed, [])
        self.assertIn("already a hard link", out.failed[0][1])

    def test_link_failure_leaves_the_victim_and_no_temp_file(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        with mock.patch.object(os, "link", side_effect=OSError(18, "Invalid cross-device link")):
            out = apply_plans([self.plan_for(keep, victim)], link=True)
        self.assertEqual(out.removed, [])
        self.assertIn("cross-device", out.failed[0][1])
        self.assertEqual(victim.read_bytes(), b"identical")
        self.assertFalse(os.path.samefile(victim, keep))
        self.assertEqual(self.leftovers(), [])

    def test_rename_failure_cleans_up_the_temporary_link(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Permission denied")):
            out = apply_plans([self.plan_for(keep, victim)], link=True)
        self.assertEqual(out.removed, [])
        self.assertIn("Permission denied", out.failed[0][1])
        self.assertFalse(os.path.samefile(victim, keep))  # still its own file
        self.assertEqual(self.leftovers(), [])  # and the temp link is gone

    @unittest.skipUnless(os.name == "nt", "Windows refuses to replace a read-only file")
    def test_read_only_victim_is_reported_and_kept(self):
        keep = write(self.root, "keep.txt", b"identical")
        victim = write(self.root, "victim.txt", b"identical")
        os.chmod(victim, stat.S_IREAD)
        try:
            out = apply_plans([self.plan_for(keep, victim)], link=True)
            self.assertEqual(out.removed, [])
            self.assertEqual([p for p, _ in out.failed], [str(victim)])
            self.assertEqual(self.leftovers(), [])
        finally:
            os.chmod(victim, stat.S_IWRITE)


class DedupeCliTests(TempTree):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def make_sets(self, n=3):
        for i in range(n):
            for name in ("a", "b"):
                write(self.root, f"set{i}/{name}.txt", f"content of set {i}".encode())
        write(self.root, "unique.txt", b"nobody else has this")

    def listing(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def test_needs_a_keep_rule_or_a_reference(self):
        self.make_sets(1)
        before = self.listing()
        code, _, err = self.run_cli("dedupe", str(self.root), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("--keep", err)
        archive = self.root / "set0"
        code, _, err = self.run_cli("dedupe", str(self.root / "x"), "--against", str(archive), "--keep", "first")
        self.assertEqual(code, 2)
        self.assertEqual(self.listing(), before)

    def test_prefer_and_avoid_choose_the_surviving_copy(self):
        docs = write(self.root, "Documents/report.bin", b"identical", mtime=3_000_000_000)
        dl = write(self.root, "Downloads/report.bin", b"identical", mtime=1_000_000_000)
        # "oldest" alone would keep the Downloads copy
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "oldest")
        self.assertRegex(out, r"keep\s+.*Downloads")
        for flags in (("--prefer", "Documents"), ("--avoid", "downloads"), ("--prefer", "*/documents/*")):
            code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "oldest", *flags)
            self.assertEqual(code, 0)
            self.assertRegex(out, r"keep\s+.*Documents", flags)
            self.assertRegex(out, r"remove\s+.*Downloads", flags)
        self.assertIn("preferring Documents", self.run_cli(
            "dedupe", str(self.root), "--keep", "oldest", "--prefer", "Documents")[1])
        self.assertIn("avoiding downloads", self.run_cli(
            "dedupe", str(self.root), "--keep", "oldest", "--avoid", "downloads")[1])
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "oldest",
                                    "--prefer", "Documents", "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(docs.exists() and not dl.exists())

    def test_prefer_json_and_misuse(self):
        write(self.root, "Spares/x.bin", b"identical")
        write(self.root, "Keepers/x.bin", b"identical")
        data = json.loads(self.run_cli("dedupe", str(self.root), "--keep", "first",
                                       "--prefer", "Keepers", "--avoid", "Spares", "--json")[1])
        self.assertEqual((data["prefer"], data["avoid"]), (["Keepers"], ["Spares"]))
        self.assertTrue(data["plan"][0]["keep"][0].endswith(os.path.join("Keepers", "x.bin")))
        code, _, err = self.run_cli("dedupe", str(self.root), "--prefer", "Keepers")  # still needs --keep
        self.assertEqual(code, 2)
        self.assertIn("--keep", err)
        code, _, err = self.run_cli("dedupe", str(self.root / "Spares"), "--against", str(self.root / "Keepers"),
                                    "--prefer", "x")
        self.assertEqual(code, 2)
        self.assertIn("--against", err)

    def test_dry_run_is_the_default_and_touches_nothing(self):
        self.make_sets()
        before = self.listing()
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "first")
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing was changed", out)
        self.assertIn("plan: remove 3 files from 3 sets", out)
        self.assertIn("keep    ", out)
        self.assertIn("remove  ", out)
        self.assertEqual(self.listing(), before)

    def test_apply_removes_the_extra_copies(self):
        self.make_sets()
        code, out, err = self.run_cli("dedupe", str(self.root), "--keep", "first", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("removed 3 files", out)
        self.assertEqual(
            self.listing(), ["set0/a.txt", "set1/a.txt", "set2/a.txt", "unique.txt"]
        )
        again = self.run_cli("dedupe", str(self.root), "--keep", "first", "--apply")
        self.assertIn("no duplicates found", again[1])

    def test_top_limits_the_printout_not_the_work(self):
        self.make_sets(3)
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "first", "-t", "1", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("... 2 more sets", out)
        self.assertEqual(len(self.listing()), 4)  # all three sets were reduced

    def test_against_removes_only_what_the_reference_already_has(self):
        write(self.root, "archive/a.txt", b"alpha-content")
        write(self.root, "archive/dup1.txt", b"in the archive twice")
        write(self.root, "archive/dup2.txt", b"in the archive twice")
        write(self.root, "inbox/a_copy.txt", b"alpha-content")
        write(self.root, "inbox/new.txt", b"genuinely new")
        write(self.root, "inbox/x1.txt", b"twice, outside only")
        write(self.root, "inbox/x2.txt", b"twice, outside only")
        inbox, archive = str(self.root / "inbox"), str(self.root / "archive")
        code, out, _ = self.run_cli("dedupe", inbox, "--against", archive, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("removed 1 files", out)
        self.assertEqual(
            self.listing(),
            ["archive/a.txt", "archive/dup1.txt", "archive/dup2.txt",
             "inbox/new.txt", "inbox/x1.txt", "inbox/x2.txt"],
        )

    def test_json_plan_and_result(self):
        self.make_sets(2)
        data = json.loads(self.run_cli("dedupe", str(self.root), "--keep", "first", "--json")[1])
        self.assertTrue(data["dry_run"])
        self.assertEqual((len(data["plan"]), data["would_free"]), (2, 2 * len(b"content of set 0")))
        self.assertNotIn("removed", data)
        self.assertEqual(len(self.listing()), 5)
        data = json.loads(self.run_cli("dedupe", str(self.root), "--keep", "newest", "--apply", "--json")[1])
        self.assertFalse(data["dry_run"])
        self.assertEqual((len(data["removed"]), data["failed"]), (2, []))
        self.assertEqual(data["freed"], data["would_free"])
        self.assertEqual(len(self.listing()), 3)

    def test_exit_1_and_a_message_when_a_file_cannot_be_removed(self):
        self.make_sets(2)
        real_remove = os.remove

        def refuse(path, *a, **kw):
            if str(path).endswith(os.path.join("set1", "b.txt")):
                raise PermissionError(13, "Permission denied")
            return real_remove(path, *a, **kw)

        with mock.patch.object(os, "remove", refuse):
            code, out, err = self.run_cli("dedupe", str(self.root), "--keep", "first", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("removed 1 files", out)
        self.assertIn("left alone:", err)
        self.assertIn("Permission denied", err)
        self.assertTrue((self.root / "set1" / "b.txt").exists())

    def test_hardlink_mode_dry_run_then_apply(self):
        self.make_sets(2)
        before = self.listing()
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "first", "--hardlink")
        self.assertEqual(code, 0)
        self.assertIn("plan: replace with hard links 2 files", out)
        self.assertRegex(out, r"link\s+.*b\.txt")
        self.assertNotIn("remove  ", out)
        self.assertIn("files marked 'link'", out)
        self.assertEqual(self.listing(), before)
        if not hardlinks_work(self.root):
            self.skipTest("hard links unavailable here")
        self.assertFalse(os.path.samefile(self.root / "set0" / "a.txt", self.root / "set0" / "b.txt"))

        code, out, err = self.run_cli("dedupe", str(self.root), "--keep", "first", "--hardlink", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("replaced with hard links 2 files", out)
        self.assertEqual(self.listing(), before)  # every path is still there
        for i in range(2):
            self.assertTrue(os.path.samefile(self.root / f"set{i}" / "a.txt", self.root / f"set{i}" / "b.txt"))
        self.assertIn("no duplicates found", self.run_cli("dupes", str(self.root))[1])

    def test_hardlink_json_and_against(self):
        if not hardlinks_work(self.root):
            self.skipTest("hard links unavailable here")
        write(self.root, "archive/a.txt", b"alpha-content")
        write(self.root, "inbox/a_copy.txt", b"alpha-content")
        inbox, archive = str(self.root / "inbox"), str(self.root / "archive")
        data = json.loads(self.run_cli("dedupe", inbox, "--against", archive, "--hardlink",
                                       "--apply", "--json")[1])
        self.assertEqual((data["mode"], data["dry_run"], len(data["removed"]), data["failed"]),
                         ("hardlink", False, 1, []))
        self.assertTrue(os.path.samefile(self.root / "inbox" / "a_copy.txt", self.root / "archive" / "a.txt"))
        plain = json.loads(self.run_cli("dedupe", inbox, "--against", archive, "--json")[1])
        self.assertEqual(plain["mode"], "delete")

    def test_bad_directory_and_empty_result(self):
        self.assertEqual(self.run_cli("dedupe", str(self.root / "nope"), "--keep", "first")[0], 2)
        write(self.root, "only.txt", b"one file")
        code, out, _ = self.run_cli("dedupe", str(self.root), "--keep", "oldest", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("no duplicates found, nothing to remove", out)


if __name__ == "__main__":
    unittest.main()
