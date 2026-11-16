import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dupedisk.cli import main
from dupedisk.quarantine import MANIFEST_NAME, Quarantine, manifest_path_for, read_entries, restore


def write(root: Path, rel: str, data: bytes, mtime: float | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class RestoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.qdir = self.root / "quarantine"
        self.manifest = self.qdir / MANIFEST_NAME

    def tearDown(self):
        self._tmp.cleanup()

    def quarantine(self, *paths: Path) -> list[str]:
        with Quarantine(str(self.qdir)) as q:
            return [q.move(str(p)) for p in paths]

    def lines(self) -> list[dict]:
        return [json.loads(x) for x in self.manifest.read_text(encoding="utf-8").splitlines()]

    def stored_files(self):
        return sorted(
            p.relative_to(self.qdir).as_posix()
            for p in self.qdir.rglob("*") if p.is_file() and p.name != MANIFEST_NAME
        )


class RestoreTests(RestoreBase):
    def test_round_trip_puts_everything_back_unchanged(self):
        a = write(self.data, "sub/a.bin", b"alpha" * 100, mtime=1_500_000_000)
        b = write(self.data, "b.txt", b"bravo", mtime=1_600_000_000)
        self.quarantine(a, b)
        self.assertFalse(a.exists() or b.exists())

        result = restore(str(self.qdir), apply=True)
        self.assertEqual((len(result.restored), result.skipped, result.malformed), (2, [], 0))
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"alpha" * 100, b"bravo"))
        self.assertEqual((int(a.stat().st_mtime), int(b.stat().st_mtime)), (1_500_000_000, 1_600_000_000))
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(
            [p.name for p in self.qdir.iterdir()], [MANIFEST_NAME]
        )  # emptied folders are cleaned up, the manifest stays

    def test_dry_run_moves_nothing(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        before = self.stored_files()
        result = restore(str(self.qdir))
        self.assertEqual([e.original for e in result.restored], [os.path.abspath(a)])
        self.assertFalse(a.exists())
        self.assertEqual(self.stored_files(), before)
        self.assertEqual(len(self.lines()), 1)  # and nothing was logged

    def test_restoring_twice_is_a_no_op(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        restore(str(self.qdir), apply=True)
        again = restore(str(self.qdir), apply=True)
        self.assertEqual((again.restored, again.skipped), ([], []))
        self.assertEqual(a.read_bytes(), b"alpha")
        self.assertEqual(self.lines()[-1].keys() >= {"restored"}, True)

    def test_never_overwrites_a_file_that_is_back_at_the_original_path(self):
        a = write(self.data, "a.txt", b"quarantined")
        self.quarantine(a)
        write(self.data, "a.txt", b"new file with the same name")
        result = restore(str(self.qdir), apply=True)
        self.assertEqual(result.restored, [])
        self.assertIn("already exists", result.skipped[0][1])
        self.assertEqual(a.read_bytes(), b"new file with the same name")
        self.assertEqual(self.stored_files() != [], True)  # still safe in the quarantine

    def test_file_that_left_the_quarantine_is_skipped(self):
        a = write(self.data, "a.txt", b"alpha")
        b = write(self.data, "b.txt", b"bravo")
        (stored_a, _) = self.quarantine(a, b)
        os.remove(stored_a)
        result = restore(str(self.qdir), apply=True)
        self.assertEqual([Path(e.original).name for e in result.restored], ["b.txt"])
        self.assertIn("no longer in the quarantine", result.skipped[0][1])

    def test_recreates_missing_parent_folders(self):
        a = write(self.data, "deep/er/a.txt", b"alpha")
        self.quarantine(a)
        for d in (a.parent, a.parent.parent):
            d.rmdir()
        restore(str(self.qdir), apply=True)
        self.assertEqual(a.read_bytes(), b"alpha")

    def test_include_filters_by_original_path(self):
        a = write(self.data, "photos/a.jpg", b"1")
        b = write(self.data, "docs/b.pdf", b"2")
        c = write(self.data, "docs/c.pdf", b"3")
        self.quarantine(a, b, c)
        result = restore(str(self.qdir), apply=True, only=["DOCS"])  # substring, any case
        self.assertEqual(sorted(Path(e.original).name for e in result.restored), ["b.pdf", "c.pdf"])
        self.assertFalse(a.exists())
        self.assertEqual(self.stored_files() != [], True)
        result = restore(str(self.qdir), apply=True, only=["*.jpg"])  # glob
        self.assertEqual([Path(e.original).name for e in result.restored], ["a.jpg"])

    def test_same_path_quarantined_twice_restores_one_and_keeps_the_other(self):
        first = write(self.data, "a.txt", b"one")
        self.quarantine(first)
        write(self.data, "a.txt", b"two")
        self.quarantine(self.data / "a.txt")
        result = restore(str(self.qdir), apply=True)
        self.assertEqual(len(result.restored), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(first.read_bytes(), b"one")  # the older move is replayed first
        self.assertEqual(len(self.stored_files()), 1)

    def test_aborted_moves_and_unreadable_lines_are_ignored(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        stored = self.lines()[0]["stored"]
        with open(self.manifest, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
            fh.write("[1, 2]\n")
            fh.write('{"something": "else"}\n')
            fh.write("\n")
            fh.write(json.dumps({"original": str(self.data / "ghost.txt"), "stored": "ghost"}) + "\n")
            fh.write(json.dumps({"aborted": "ghost"}) + "\n")
        entries, malformed = read_entries(str(self.manifest))
        self.assertEqual([e.stored for e in entries], [stored])
        self.assertEqual(malformed, 3)
        self.assertEqual(restore(str(self.qdir), apply=True).malformed, 3)

    def test_a_tampered_manifest_cannot_pull_files_from_outside_the_quarantine(self):
        outside = write(self.root, "outside/secret.txt", b"not quarantined")
        self.qdir.mkdir()
        self.manifest.write_text(
            json.dumps({"original": str(self.data / "grabbed.txt"), "stored": os.path.join("..", "outside", "secret.txt")})
            + "\n",
            encoding="utf-8",
        )
        result = restore(str(self.qdir), apply=True)
        self.assertEqual(result.restored, [])
        self.assertIn("outside the quarantine", result.skipped[0][1])
        self.assertTrue(outside.exists())
        self.assertFalse((self.data / "grabbed.txt").exists())

    def test_accepts_the_folder_or_the_manifest_file(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        self.assertEqual(manifest_path_for(str(self.qdir)), str(self.manifest))
        self.assertEqual(manifest_path_for(str(self.manifest)), str(self.manifest))
        self.assertEqual(len(restore(str(self.manifest), apply=True).restored), 1)

    def test_manifest_moves_with_the_quarantine_folder(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        moved = self.root / "relocated"
        self.qdir.rename(moved)  # entries store relative paths, so this still works
        self.assertEqual(len(restore(str(moved), apply=True).restored), 1)
        self.assertEqual(a.read_bytes(), b"alpha")


class RestoreCliTests(RestoreBase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_full_cycle_dedupe_then_restore(self):
        for name in ("a", "b"):
            write(self.data, f"set/{name}.txt", b"identical content", mtime=1_500_000_000)
        write(self.data, "unique.txt", b"only one of me")
        code, out, _ = self.run_cli("dedupe", str(self.data), "--keep", "first",
                                    "--move-to", str(self.qdir), "--apply")
        self.assertEqual(code, 0)
        self.assertIn(f"undo with: dupedisk restore {self.qdir}", out)
        self.assertFalse((self.data / "set" / "b.txt").exists())

        code, out, _ = self.run_cli("restore", str(self.qdir))
        self.assertEqual(code, 0)
        self.assertIn("would restore 1 files", out)
        self.assertIn("dry run: nothing was moved", out)
        self.assertFalse((self.data / "set" / "b.txt").exists())

        code, out, err = self.run_cli("restore", str(self.qdir), "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("restored 1 files", out)
        self.assertEqual((self.data / "set" / "b.txt").read_bytes(), b"identical content")
        self.assertIn("nothing to restore", self.run_cli("restore", str(self.qdir), "--apply")[1])

    def test_json_output(self):
        a = write(self.data, "a.txt", b"alpha")
        self.quarantine(a)
        data = json.loads(self.run_cli("restore", str(self.qdir), "--json")[1])
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["manifest"], str(self.manifest))
        self.assertEqual(data["restored"][0]["original"], os.path.abspath(a))
        self.assertTrue(os.path.isfile(data["restored"][0]["stored"]))
        self.assertEqual((data["skipped"], data["unreadable_lines"]), ([], 0))

    def test_skipped_entries_are_reported_and_set_the_exit_status(self):
        a = write(self.data, "a.txt", b"quarantined")
        self.quarantine(a)
        write(self.data, "a.txt", b"occupied now")
        code, out, err = self.run_cli("restore", str(self.qdir), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("restored 0 files, 1 left in the quarantine", out)
        self.assertIn("left in quarantine:", err)
        self.assertIn("already exists", err)
        self.assertEqual(self.run_cli("restore", str(self.qdir))[0], 0)  # a dry run only informs

    def test_top_limits_the_listing_not_the_work(self):
        files = [write(self.data, f"f{i}.txt", str(i).encode()) for i in range(5)]
        self.quarantine(*files)
        _, out, _ = self.run_cli("restore", str(self.qdir), "-t", "2")
        self.assertIn("would restore 5 files", out)
        self.assertIn("... 3 more", out)
        self.run_cli("restore", str(self.qdir), "-t", "2", "--apply")
        self.assertTrue(all(f.exists() for f in files))

    def test_missing_manifest_and_include_filter(self):
        code, _, err = self.run_cli("restore", str(self.root / "nowhere"))
        self.assertEqual(code, 2)
        self.assertIn("no manifest", err)
        a = write(self.data, "keep-away.txt", b"1")
        b = write(self.data, "bring-back.txt", b"2")
        self.quarantine(a, b)
        self.run_cli("restore", str(self.qdir), "-i", "bring-back", "--apply")
        self.assertTrue(b.exists() and not a.exists())


if __name__ == "__main__":
    unittest.main()
