# dupedisk

Find duplicate files and see where your disk went. One small CLI, no dependencies,
Python 3.10+.

- **Report only.** v0.1 never deletes anything.
- Never follows symlinks or NTFS junctions, so it can't loop through `Application Data`.
- Hard links to the same data are not reported as duplicates.
- Unreadable paths are skipped and counted, not fatal.

## Usage

```
python -m dupedisk dupes   PATH... [-m SIZE] [-t N] [-x PAT] [--cache FILE] [--json | --csv] [-v]
python -m dupedisk usage   [PATH]  [-d DEPTH] [-t N] [-x PAT] [-v]
python -m dupedisk largest [PATH]  [-n COUNT] [-x PAT] [-v]
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

- `-m 10M`: ignore files under 10 MiB (units are binary: K, M, G, T).
- `-x PAT`: skip names or paths matching a glob, e.g. `-x node_modules -x "*.tmp"`.
- Overlapping roots are merged, so `dupes C:\a C:\a\b` doesn't double-count.

## How it works

Group by size, then hash the first 64 KiB (BLAKE2b), then hash the whole file. Only files
that collide at one stage are read for the next, so a big tree with few real duplicates
stays cheap.

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
- [ ] parallel hashing
- [ ] `--similar-names` mode for `file (1).ext` style copies
- [x] CSV export (`dupes --csv`)
- [ ] `usage --since 30d` to see what grew recently
- [ ] PyPI release

## License

MIT
