"""SQLite record of every job ever seen, so each one is only processed once."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key        TEXT PRIMARY KEY,
    company    TEXT,
    title      TEXT,
    location   TEXT,
    url        TEXT,
    kept       INTEGER,
    category   TEXT,
    reason     TEXT,
    source     TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    at        TEXT,
    fetched   INTEGER,
    new_jobs  INTEGER,
    matches   INTEGER,
    errors    TEXT
);
"""


class Store:
    def __init__(self, path="radar.db"):
        self.db = sqlite3.connect(path, timeout=60)  # wait up to 60s if another process is writing
        self.db.executescript(SCHEMA)

    def unseen(self, jobs):
        """Return only the jobs whose key isn't in the table yet."""
        if not jobs:
            return []
        keys = [j.key for j in jobs]
        seen = set()
        for i in range(0, len(keys), 500):  # stay under SQLite's variable limit
            chunk = keys[i:i + 500]
            q = f"SELECT key FROM seen WHERE key IN ({','.join('?' * len(chunk))})"
            seen.update(r[0] for r in self.db.execute(q, chunk))
        return [j for j in jobs if j.key not in seen]

    def mark(self, job, verdict):
        self.db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.key, job.company, job.title, job.location, job.url, int(verdict.keep),
             verdict.category, verdict.reason, verdict.source,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    def log_run(self, fetched, new, matches, errors):
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         fetched, new, matches, "; ".join(errors)))

    def commit(self):
        self.db.commit()
        self.db.close()

    def discard(self):
        """Throw away this run's writes and release the file lock."""
        self.db.rollback()
        self.db.close()