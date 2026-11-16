# dupedisk

Find duplicate files and see where your disk went. One small CLI, no dependencies,
Python 3.10+.

- **Reports by default.** `dupes`, `similar`, `usage` and `largest` never change a file.
  `dedupe` is the only command that deletes, and it is a dry run unless you add `--apply`.
- Never follows symlinks or NTFS junctions, so it can't loop through `Application Data`.
- Hard links to the same data are not reported as duplicates.
- Unreadable paths are skipped and counted, not fatal.

## Usage

```
python -m dupedisk dupes   PATH... [-m SIZE] [-t N] [-x PAT] [-j N] [--against DIR] [--cache FILE] [--json | --csv] [-v]
python -m dupedisk dedupe  PATH... (--keep RULE | --against DIR) [--hardlink | --move-to DIR] [--apply] [-m SIZE] [-t N] [-x PAT] [-j N] [--json] [-v]
python -m dupedisk restore DIR_OR_MANIFEST [-i PAT] [--apply] [-t N] [--json] [-v]
python -m dupedisk similar PATH... [-m SIZE] [-t N] [--identical] [-x PAT] [--json] [-v]
python -m dupedisk usage   [PATH]  [-d DEPTH] [-t N] [--since AGE] [--json] [-x PAT] [-v]
python -m dupedisk largest [PATH]  [-n COUNT] [--since AGE] [--json] [-x PAT] [-v]
```

```
$ python -m dupedisk dupes C:\Users\me\projects -m 1M -t 1
scanned 39,561 files (14.5 GiB)
190 duplicate sets, 201 redundant files, 1.0 GiB reclaimable

[1] 2 x 103.2 MiB  (103.2 MiB reclaimable)
    C:\Users\me\projects\app-a\target\release\deps\libwindows-4d77425db3ec1321.rlib
    C:\Users\me\projects\app-b\target\release\deps\libwindows-4d77425db3ec1321.rlib

... 189 more sets (--top 0 for all)
```

```
$ python -m dupedisk usage C:\Users\me\projects -d 1 -t 4
C:\Users\me\projects  -  14.5 GiB in 39,561 files

   9.9 GiB   68.4%  ##############        app-a\
   2.8 GiB   19.2%  ####                  app-b\
   1.2 GiB    8.5%  ##                    app-c\
 506.1 MiB    3.4%  #                     app-d\
  73.7 MiB                                ... 3 more
```

- `--since 30d`: `usage` and `largest` count only files modified in the last 30 days (`s`,
  `m`, `h`, `d`, `w`), so you see what grew instead of what is merely big. Directories with
  nothing recent are dropped. It goes by modification time, so a file that was copied in
  with an old timestamp won't show up.
- `--json` on `usage` and `largest` (as on `dupes` and `similar`) for scripts. `usage --json`
  is the whole kept tree, nested as `children`, and ignores `--top`; `-d` still sets how
  deep it goes.
- `-m 10M`: ignore files under 10 MiB (units are binary: K, M, G, T).
- `-x PAT`: skip names or paths matching a glob, e.g. `-x node_modules -x "*.tmp"`.
- Overlapping roots are merged, so `dupes C:\a C:\a\b` doesn't double-count.

## Against a reference folder

`--against DIR` answers "which of these files do I already have in my archive?". DIR is the
copies you keep, and PATH is what you're cleaning up:

```
$ python -m dupedisk dupes C:\Users\me\Downloads --against C:\Users\me\Documents -m 1K
scanned 11,772 files (5.3 GiB)
reference (kept): C:\Users\me\Documents
1 duplicate sets, 1 redundant files, 4.7 KiB reclaimable

[1] 2 x 4.7 KiB  (4.7 KiB reclaimable)
    keep  C:\Users\me\Documents\cookielogger.cpp
    dupe  C:\Users\me\Downloads\cookielogger.cpp
```

- Only sets with a copy inside DIR **and** one outside it are listed. Duplicates that sit
  entirely inside DIR, or entirely outside it, are left to plain `dupes`.
- Reclaimable space counts the copies outside DIR only, so a file stored twice in the archive
  doesn't make its one download look like two.
- A PATH inside DIR is an error: there would be nothing outside the reference to compare.
- `--json` adds `against` and a `keep` list per set. `--csv` adds a `role` column (`keep` or
  `dupe`). Without `--against` the output is unchanged.

## Removing duplicates

`dedupe` finds the same sets as `dupes`, picks the copy that survives, and removes the rest.
It plans first and **does nothing without `--apply`**:

```
$ python -m dupedisk dedupe C:\Users\me\Downloads -m 100K --keep oldest
scanned 11,512 files (5.0 GiB)
plan: remove 145 files from 138 sets, freeing 757.0 MiB (keeping the oldest copy of each set)

[1] 2 x 131.7 MiB  (frees 131.7 MiB)
    keep    C:\Users\me\Downloads\maps\de_boulder.opt
    remove  C:\Users\me\Downloads\x64\Debug\maps\de_boulder.opt
...
dry run: nothing was changed. Re-run with --apply to act on the files marked 'remove'.
```

Say which copy lives, one way or the other (there is no default, on purpose):

- `--keep oldest | newest | shortest-path | first` keeps one copy per set by modification
  time, path length, or alphabetical order. Ties fall back to the path, so the same tree
  always gives the same plan.
- `--against DIR` keeps every copy inside DIR and removes the copies outside it (see above).

What `--apply` checks before each deletion, for every file, at that moment:

- it still exists and still has the size it had in the plan,
- its contents still hash the same as a kept copy (a file edited since the scan is left alone),
- it is not a hard link to the kept copy, and not the kept copy itself,
- at least one kept copy is still readable. If none is, the whole set is skipped.

`--hardlink` keeps every path and still frees the space: each extra copy is replaced by a hard
link to the kept one. The link is made under a temporary name beside the file and renamed
over it, so the path is never missing, and a failure leaves the original and removes the
temporary link. It needs the same volume and a filesystem with hard links (NTFS, ext4, APFS;
not FAT). The catch is that the paths now share one file: editing one edits all of them, and
they all take the kept copy's timestamps and permissions. After `--hardlink --apply`, `dupes`
no longer reports the set, since linked files are one file.

`--move-to DIR` moves each extra copy into a quarantine folder instead of deleting it:

```
$ python -m dupedisk dedupe C:\Users\me\Downloads --keep oldest --move-to D:\dupe-trash --apply
moved 145 files, freed 757.0 MiB
manifest: D:\dupe-trash\dedupe-manifest.jsonl
```

- A file lands at `DIR\<drive>\<its original path>` (`DIR\C\Users\me\Downloads\x.bin`;
  network paths go under `_unc`), so nothing collides and you can see where it came from. A
  second file with the same path gets a `.1`, `.2` suffix.
- `dedupe-manifest.jsonl` gets one line per move, written and flushed *before* the move, so a
  file is never in the quarantine without a record. A move that fails is marked `aborted`.
  The file keeps its modification time.
- On another volume the file is copied, the copy is checked against the original, and only
  then is the original deleted. If the copy doesn't match, or the original can't be removed,
  the original stays and the half-made copy is cleaned up.
- DIR must be outside every scanned folder, and no scanned folder may sit inside it: a
  quarantine under the scanned tree would show its own contents as duplicates next time.
  `--move-to` and `--hardlink` are alternatives. A dry run creates nothing.

### Undoing a `--move-to`

```
$ python -m dupedisk restore D:\dupe-trash                  # list what would go back
would restore 145 files, 0 left in the quarantine
    C:\Users\me\Downloads\x64\Debug\maps\de_boulder.opt
...
dry run: nothing was moved. Re-run with --apply to put these files back.

$ python -m dupedisk restore D:\dupe-trash --apply          # do it
$ python -m dupedisk restore D:\dupe-trash -i "*.opt" --apply   # only some of them
```

`restore` replays the manifest and moves every file still in the quarantine back to its
original path, creating missing folders and keeping the modification time. Like `dedupe`, it
is a dry run until you add `--apply`. It accepts the quarantine folder or the manifest file,
and `-i PAT` (glob, or a substring) limits it to matching original paths.

- **It never overwrites.** If something exists at the original path again, that file stays
  and the quarantined copy stays too, reported as `left in quarantine: PATH (why)`. A file
  that is no longer in the quarantine is skipped the same way. Either one makes `--apply`
  exit with status 1.
- Each restored file gets a `restored` line in the manifest and the quarantine folders it
  leaves empty are removed (the manifest stays), so a second run reports `nothing to restore`.
- Manifest entries hold paths relative to the quarantine folder, so you can move or rename
  the whole folder and `restore` still finds everything. A stored path that points outside
  the folder is refused, and unreadable manifest lines are counted and ignored.
- If the same path was quarantined twice, the older move is restored and the newer one is
  left in the quarantine until the path is free again.

A file that fails a check is left alone and listed on stderr as `left alone: PATH (why)`, the
rest of the plan still runs, and the exit status is 1. Nothing is ever force-deleted: a
read-only file that Windows refuses to remove is reported, not chmod'ed. Deletion is
permanent (unless you use `--move-to`). `-t` only limits what is printed; `--apply` acts on every
set. `--json` gives the plan and, with `--apply`, what was removed and what failed.

## Copies by name

`similar` finds the `report (1).pdf` / `report - Copy.pdf` / `Copy of report.pdf` files that
browsers and Explorer leave behind, and checks each one against its original byte for byte.

```
$ python -m dupedisk similar C:\Users\me\Downloads
scanned 8,213 files
3 copies of 3 originals, 2 identical, 50.3 KiB reclaimable

[1] C:\Users\me\Downloads\notes.docx  (44.1 KiB)
    identical            C:\Users\me\Downloads\notes (1).docx
[2] C:\Users\me\Downloads\build.zip  (23.8 KiB)
    differs, 25.2 KiB    C:\Users\me\Downloads\build (1).zip
```

- A copy only counts if its original is in the **same folder** with the same extension. A lone
  `photo (1).jpg` is just a name.
- `differs` means the copy was edited or is a newer download: look before you delete it.
- A hard link to the original is skipped, since it costs no space.
- `--identical` lists only the copies that are safe to drop. `dupes` finds identical files
  under any name; `similar` also finds the near-misses.

## How it works

Group by size, then hash the first 64 KiB (BLAKE2b), then hash the whole file. Only files
that collide at one stage are read for the next, so a big tree with few real duplicates
stays cheap.

## Parallel hashing

`-j N` hashes N files at once (`-j 0` = one per CPU). The default is 1, which is the right
choice for a spinning disk, where parallel reads just make the head seek. Every candidate
of a stage goes into one batch, so it helps even when each size group holds only two or
three files. The report is identical for any `-j`, and a `--cache` still works (it is read
and written from the main thread only).

Measured on 72,106 files (`-m 1M`, 24 logical CPUs, files already in the OS cache, so this
is hashing cost without disk latency): 1.58 s at `-j 1`, 0.99 s at `-j 4`, 0.90 s at `-j 8`.
Cold-disk results depend on the device, so test `-j 4` against `-j 1` on yours.

## Cache

`--cache hashes.db` stores each digest in SQLite, keyed by path and checked against the
file's size and mtime. Re-runs only read files that changed. On the 14.5 GiB folder
above with `-m 1M`: 69.5 s cold, 0.74 s warm.

```
python -m dupedisk cache hashes.db            # entry count and file size
python -m dupedisk cache hashes.db --prune    # drop entries for files that are gone or changed
```

As with any stat-based cache, a file edited in place with the same size *and* the same
mtime would be missed. Delete the cache file to force a full re-hash.

## Tests

```
python -m unittest discover -s tests
```

## Roadmap

- [x] `dedupe`: dry run by default, `--apply` to delete, keep rules, re-verified per file
- [x] quarantine folder instead of unlinking (`dedupe --move-to DIR`)
- [x] `restore` command to put quarantined files back
- [x] replace duplicates with hard links (`dedupe --hardlink`)
- [x] hash cache between runs (`dupes --cache FILE`)
- [x] `cache --prune` to drop entries for files that are gone or changed
- [x] parallel hashing (`dupes -j N`)
- [x] `similar` command for `file (1).ext` style copies
- [x] CSV export (`dupes --csv`)
- [x] `usage --since 30d` to see what changed recently
- [ ] PyPI release

## License

MIT
