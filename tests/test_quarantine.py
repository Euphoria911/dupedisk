import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dupedisk import quarantine as qmod
from dupedisk.cli import main
from dupedisk.dedupe import Plan, apply_plans
from dupedisk.quarantine import MANIFEST_NAME, Quarantine, free_name, move_file, quarantine_path


def write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def manifest_lines(qdir: Path) -> list[dict]:
    text = (qdir / MANIFEST_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.qdir = self.root / "quarantine"
        self.data = self.root / "data"
        self.data.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def stray_parts(self):
        return [p.name for p in self.root.rglob("*.part")]


class PathTests(TempTree):
    @unittest.skipUnless(os.name == "nt", "drive letters")
    def test_windows_drive_and_unc(self):
        self.assertEqual(
            quarantine_path(r"D:\q", r"C:\Users\me\a.txt"), os.path.join(r"D:\q", "C", "Users", "me", "a.txt")
        )
        self.assertEqual(
            quarantine_path(r"D:\q", r"\\srv\share\x\y.txt"),
            os.path.join(r"D:\q", "_unc", "srv", "share", "x", "y.txt"),
        )

    @unittest.skipIf(os.name == "nt", "POSIX roots")
    def test_posix_root(self):
        self.assertEqual(quarantine_path("/q", "/home/me/f"), os.path.join("/q", "home", "me", "f"))

    def test_result_stays_inside_the_root(self):
        root = str(self.qdir)
        for original in (str(self.data / "a.txt"), str(self.data / ".." / "x" / "b.txt"), str(self.data)):
            target = os.path.abspath(quarantine_path(root, original))
            self.assertTrue(target.startswith(os.path.abspath(root) + os.sep), target)

    def test_free_name_counts_up(self):
        base = self.data / "f.txt"
        self.assertEqual(free_name(str(base)), str(base))
        base.write_bytes(b"1")
        self.assertEqual(free_name(str(base)), f"{base}.1")
        Path(f"{base}.1").write_bytes(b"2")
        self.assertEqual(free_name(str(base)), f"{base}.2")


class MoveFileTests(TempTree):
    def test_same_volume_is_a_rename(self):
        src = write(self.data, "a.bin", b"payload")
        dst = self.root / "moved.bin"
        move_file(str(src), str(dst))
        self.assertEqual((dst.read_bytes(), src.exists()), (b"payload", False))

    def cross_device(self, src: Path):
        """os.replace refuses the first rename like a different volume would."""
        real = os.replace
        calls = []

        def fake(a, b):
            calls.append((a, b))
            if len(calls) == 1:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(a, b)

        return mock.patch.object(os, "replace", fake)

    def test_across_volumes_copy_verify_then_delete(self):
        src = write(self.data, "a.bin", b"payload" * 1000)
        dst = self.root / "moved.bin"
        with self.cross_device(src):
            move_file(str(src), str(dst))
        self.assertEqual((dst.read_bytes(), src.exists()), (b"payload" * 1000, False))
        self.assertEqual(self.stray_parts(), [])

    def test_a_bad_copy_leaves_the_source_alone(self):
        src = write(self.data, "a.bin", b"payload" * 1000)
        dst = self.root / "moved.bin"

        def bad_copy(s, d, **kw):
            Path(d).write_bytes(b"corrupt" * 1000)  # same length, different bytes

        with self.cross_device(src), mock.patch.object(qmod.shutil, "copy2", bad_copy):
            with self.assertRaises(OSError):
                move_file(str(src), str(dst))
        self.assertEqual(src.read_bytes(), b"payload" * 1000)
        self.assertFalse(dst.exists())
        self.assertEqual(self.stray_parts(), [])

    def test_if_the_original_cannot_be_removed_only_one_copy_remains(self):
        src = write(self.data, "a.bin", b"payload")
        dst = self.root / "moved.bin"
        with self.cross_device(src), mock.patch.object(os, "remove", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                move_file(str(src), str(dst))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        self.assertEqual(self.stray_parts(), [])

    def test_other_errors_are_not_mistaken_for_a_volume_change(self):
        src = write(self.data, "a.bin", b"x")
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                move_file(str(src), str(self.root / "m.bin"))
        self.assertTrue(src.exists())


class QuarantineTests(TempTree):
    def test_move_parks_the_file_and_records_it(self):
        victim = write(self.data, "sub/a.txt", b"hello")
        with Quarantine(str(self.qdir)) as q:
            target = q.move(str(victim))
        self.assertFalse(victim.exists())
        self.assertEqual(Path(target).read_bytes(), b"hello")
        self.assertEqual(Path(target), self.qdir / os.path.relpath(quarantine_path(str(self.qdir), str(victim)), self.qdir))
        (rec,) = manifest_lines(self.qdir)
        self.assertEqual(rec["original"], os.path.abspath(victim))
        self.assertEqual(rec["size"], 5)
        self.assertEqual((self.qdir / rec["stored"]).read_bytes(), b"hello")
        self.assertTrue(rec["time"].endswith("+00:00"))

    def test_same_path_quarantined_twice_gets_distinct_names(self):
        with Quarantine(str(self.qdir)) as q:
            first = q.move(str(write(self.data, "a.txt", b"one")))
            second = q.move(str(write(self.data, "a.txt", b"two")))
        self.assertNotEqual(first, second)
        self.assertEqual((Path(first).read_bytes(), Path(second).read_bytes()), (b"one", b"two"))
        self.assertEqual(len(manifest_lines(self.qdir)), 2)

    def test_manifest_is_appended_across_runs(self):
        with Quarantine(str(self.qdir)) as q:
            q.move(str(write(self.data, "a.txt", b"1")))
        with Quarantine(str(self.qdir)) as q:
            q.move(str(write(self.data, "b.txt", b"2")))
        self.assertEqual(len(manifest_lines(self.qdir)), 2)

    def test_failed_move_is_marked_aborted_and_the_file_stays(self):
        victim = write(self.data, "a.txt", b"hello")
        with Quarantine(str(self.qdir)) as q:
            with mock.patch.object(qmod, "move_file", side_effect=OSError(5, "boom")):
                with self.assertRaises(OSError):
                    q.move(str(victim))
        self.assertEqual(victim.read_bytes(), b"hello")
        first, second = manifest_lines(self.qdir)
        self.assertEqual(second, {"aborted": first["stored"]})

    def test_unusable_location(self):
        blocker = write(self.root, "afile", b"x")
        with self.assertRaises(OSError):
            Quarantine(str(blocker / "inside"))

    def test_apply_plans_quarantines_instead_of_deleting(self):
        keep = write(self.data, "keep.txt", b"identical")
        a = write(self.data, "a.txt", b"identical")
        b = write(self.data, "sub/b.txt", b"identical")
        plan = Plan(len(b"identical"), (str(keep),), (str(a), str(b)))
        with Quarantine(str(self.qdir)) as q:
            out = apply_plans([plan], quarantine=q)
        self.assertEqual((sorted(out.removed), out.failed), (sorted([str(a), str(b)]), []))
        self.assertEqual(out.freed, 2 * len(b"identical"))
        self.assertTrue(keep.exists() and not a.exists() and not b.exists())
        stored = [p for p in self.qdir.rglob("*") if p.is_file() and p.name != MANIFEST_NAME]
        self.assertEqual(sorted(p.name for p in stored), ["a.txt", "b.txt"])
        self.assertTrue(all(p.read_bytes() == b"identical" for p in stored))

    def test_verification_still_guards_quarantining(self):
        keep = write(self.data, "keep.txt", b"identical")
        edited = write(self.data, "edited.txt", b"identical")
        plan = Plan(len(b"identical"), (str(keep),), (str(edited),))
        edited.write_bytes(b"IDENTICAL")
        with Quarantine(str(self.qdir)) as q:
            out = apply_plans([plan], quarantine=q)
        self.assertEqual(out.removed, [])
        self.assertEqual(edited.read_bytes(), b"IDENTICAL")
        self.assertEqual((self.qdir / MANIFEST_NAME).read_text(), "")  # nothing was moved, nothing logged

    def test_links_and_quarantine_are_exclusive(self):
        with Quarantine(str(self.qdir)) as q:
            with self.assertRaises(ValueError):
                apply_plans([], link=True, quarantine=q)


class QuarantineCliTests(TempTree):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def make_sets(self):
        for i in range(2):
            for name in ("a", "b"):
                write(self.data, f"set{i}/{name}.txt", f"content of set {i}".encode())
        write(self.data, "unique.txt", b"nobody else has this")

    def files(self, base):
        return sorted(p.relative_to(base).as_posix() for p in Path(base).rglob("*") if p.is_file())

    def test_dry_run_creates_nothing(self):
        self.make_sets()
        before = self.files(self.data)
        code, out, _ = self.run_cli("dedupe", str(self.data), "--keep", "first", "--move-to", str(self.qdir))
        self.assertEqual(code, 0)
        self.assertIn("plan: move to", out)
        self.assertRegex(out, r"move\s+.*b\.txt")
        self.assertIn("files marked 'move'", out)
        self.assertFalse(self.qdir.exists())
        self.assertEqual(self.files(self.data), before)

    def test_apply_moves_files_and_writes_the_manifest(self):
        self.make_sets()
        code, out, err = self.run_cli(
            "dedupe", str(self.data), "--keep", "first", "--move-to", str(self.qdir), "--apply"
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("moved 2 files", out)
        self.assertIn(f"manifest: {self.qdir / MANIFEST_NAME}", out)
        self.assertEqual(self.files(self.data), ["set0/a.txt", "set1/a.txt", "unique.txt"])
        records = manifest_lines(self.qdir)
        self.assertEqual(sorted(Path(r["original"]).name for r in records), ["b.txt", "b.txt"])
        for r in records:
            self.assertTrue((self.qdir / r["stored"]).is_file())
        # moved, not duplicated: a second pass sees no duplicates left
        self.assertIn("no duplicates found", self.run_cli("dupes", str(self.data))[1])

    def test_json_result(self):
        self.make_sets()
        data = json.loads(self.run_cli("dedupe", str(self.data), "--keep", "first", "--move-to",
                                       str(self.qdir), "--apply", "--json")[1])
        self.assertEqual((data["mode"], data["dry_run"], len(data["removed"]), data["failed"]),
                         ("move", False, 2, []))
        self.assertEqual(data["move_to"], str(self.qdir))
        self.assertEqual(data["manifest"], str(self.qdir / MANIFEST_NAME))
        plain = json.loads(self.run_cli("dedupe", str(self.data), "--keep", "first", "--json")[1])
        self.assertEqual((plain["mode"], plain["move_to"], plain["manifest"]), ("delete", None, None))

    def test_works_with_against(self):
        write(self.root, "archive/a.txt", b"alpha-content")
        write(self.root, "inbox/a_copy.txt", b"alpha-content")
        write(self.root, "inbox/new.txt", b"genuinely new")
        code, out, _ = self.run_cli("dedupe", str(self.root / "inbox"), "--against", str(self.root / "archive"),
                                    "--move-to", str(self.qdir), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.files(self.root / "inbox"), ["new.txt"])
        self.assertEqual(self.files(self.root / "archive"), ["a.txt"])
        self.assertEqual(len(manifest_lines(self.qdir)), 1)

    def test_rejects_overlap_with_the_scanned_folders_and_hardlink(self):
        self.make_sets()
        before = self.files(self.data)
        for target in (self.data / "trash", self.data, self.root):
            code, _, err = self.run_cli("dedupe", str(self.data), "--keep", "first",
                                        "--move-to", str(target), "--apply")
            self.assertEqual(code, 2, target)
            self.assertIn("overlaps", err)
        code, _, err = self.run_cli("dedupe", str(self.data), "--keep", "first",
                                    "--move-to", str(self.qdir), "--hardlink")
        self.assertEqual(code, 2)
        self.assertIn("alternatives", err)
        self.assertEqual(self.files(self.data), before)
        self.assertFalse(self.qdir.exists())

    def test_unusable_quarantine_folder_changes_nothing(self):
        self.make_sets()
        before = self.files(self.data)
        blocker = write(self.root, "afile", b"x")
        code, _, err = self.run_cli("dedupe", str(self.data), "--keep", "first",
                                    "--move-to", str(blocker / "inside"), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("cannot use --move-to", err)
        self.assertEqual(self.files(self.data), before)

    def test_nothing_to_do_creates_no_quarantine(self):
        write(self.data, "only.txt", b"one file")
        code, out, _ = self.run_cli("dedupe", str(self.data), "--keep", "first",
                                    "--move-to", str(self.qdir), "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(self.qdir.exists())


if __name__ == "__main__":
    unittest.main()
