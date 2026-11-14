# dupedisk

Find duplicate files and see where your disk went. One small CLI, no dependencies,
Python 3.10+.

- **Report only.** v0.1 never deletes anything.
- Never follows symlinks or NTFS junctions, so it can't loop through `Application Data`.
- Hard links to the same data are not reported as duplicates.
- Unreadable paths are skipped and counted, not fatal.

## Usage

```
python -m dupedisk dupes   PATH... [-m SIZE] [-t N] [-x PAT] [-j N] [--against DIR] [--cache FILE] [--json | --csv] [-v]
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

- [ ] `--delete` with a dry-run default, keep-rule (`--keep oldest|newest|shortest-path`)
- [ ] send to Recycle Bin instead of unlinking
- [ ] replace duplicates with hard links
- [x] hash cache between runs (`dupes --cache FILE`)
- [x] `cache --prune` to drop entries for files that are gone or changed
- [x] parallel hashing (`dupes -j N`)
- [x] `similar` command for `file (1).ext` style copies
- [x] CSV export (`dupes --csv`)
- [x] `usage --since 30d` to see what changed recently
- [ ] PyPI release

## License

MIT
