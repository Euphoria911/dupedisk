"""SQLite-backed hash cache so re-scans only re-read files that changed.

An entry is trusted only while the file's size and mtime (ns) still match what was
recorded, the same rule rsync and make use. Edit a file and keep its size *and* mtime
identical and the cache cannot tell, which is the usual trade-off of stat-based caches.
"""

from __future__ import annotations

import os
import sqlite3
from types import TracebackType

_COMMIT_EVERY = 500


class HashCache:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        try:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS hashes ("
                " path TEXT NOT NULL, kind TEXT NOT NULL,"
                " size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, digest TEXT NOT NULL,"
                " PRIMARY KEY (path, kind))"
            )
        except sqlite3.Error:
            self._db.close()  # a non-database file must not leave its handle open
            raise
        self.hits = 0
        self.misses = 0
        self._pending = 0

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get(self, path: str, kind: str, size: int, mtime_ns: int) -> str | None:
        row = self._db.execute(
            "SELECT digest FROM hashes WHERE path = ? AND kind = ? AND size = ? AND mtime_ns = ?",
            (self._key(path), kind, size, mtime_ns),
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return row[0]

    def put(self, path: str, kind: str, size: int, mtime_ns: int, digest: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO hashes (path, kind, size, mtime_ns, digest) VALUES (?, ?, ?, ?, ?)",
            (self._key(path), kind, size, mtime_ns, digest),
        )
        self._pending += 1
        if self._pending >= _COMMIT_EVERY:
            self._db.commit()
            self._pending = 0

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]

    def prune(self) -> int:
        """Delete entries whose file is gone or no longer matches its recorded size/mtime.

        Returns how many entries were removed, then compacts the database file.
        """
        rows = self._db.execute("SELECT path, kind, size, mtime_ns FROM hashes").fetchall()
        stamps: dict[str, tuple[int, int] | None] = {}
        stale = []
        for path, kind, size, mtime_ns in rows:
            if path not in stamps:
                try:
                    st = os.stat(path)
                    stamps[path] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    stamps[path] = None
            if stamps[path] != (size, mtime_ns):
                stale.append((path, kind))
        self._db.executemany("DELETE FROM hashes WHERE path = ? AND kind = ?", stale)
        self._db.commit()
        self._pending = 0
        if stale:
            self._db.execute("VACUUM")
        return len(stale)

    def close(self) -> None:
        self._db.commit()
        self._db.close()

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
