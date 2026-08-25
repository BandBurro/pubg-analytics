"""Durable record of which matches we've seen and fetched.

The ledger is what makes collection idempotent and resumable: re-running
`fetch` never re-downloads a match that already landed, and a crash mid-run
loses at most the matches that were in flight.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS match (
    match_id         TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | gone
    discovered_at    TEXT NOT NULL,
    fetched_at       TEXT,
    match_created_at TEXT,
    map_name         TEXT,
    game_mode        TEXT,
    telemetry_path   TEXT,
    event_count      INTEGER,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_status ON match(status);

-- Which matches have been shredded into typed Parquet. Separate from fetch
-- state so the shredder can be re-pointed or re-run without touching raw data.
CREATE TABLE IF NOT EXISTS shred (
    match_id    TEXT PRIMARY KEY,
    shredded_at TEXT NOT NULL,
    part        INTEGER
);

-- Positions are shredded in a separate pass with separate state. The stream is
-- ~15% of all events and roughly 5,500 rows per match, so it must be possible to
-- re-run or re-point it without touching the event tables.
CREATE TABLE IF NOT EXISTS shred_position (
    match_id    TEXT PRIMARY KEY,
    shredded_at TEXT NOT NULL,
    part        INTEGER,
    rows        INTEGER
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps reads working while a long collection run writes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_discovered(self, match_ids: list[str]) -> int:
        """Register match ids. Returns how many were genuinely new."""
        before = self.conn.execute("SELECT count(*) FROM match").fetchone()[0]
        self.conn.executemany(
            "INSERT OR IGNORE INTO match (match_id, discovered_at) VALUES (?, ?)",
            [(mid, _now()) for mid in match_ids],
        )
        self.conn.commit()
        after = self.conn.execute("SELECT count(*) FROM match").fetchone()[0]
        return after - before

    def pending(self, limit: int, max_attempts: int = 3) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT match_id FROM match
            WHERE status IN ('pending', 'failed') AND attempts < ?
            ORDER BY attempts, discovered_at
            LIMIT ?
            """,
            (max_attempts, limit),
        ).fetchall()
        return [r["match_id"] for r in rows]

    def mark_done(
        self,
        match_id: str,
        *,
        telemetry_path: str,
        event_count: int,
        match_created_at: str | None,
        map_name: str | None,
        game_mode: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE match SET status='done', fetched_at=?, telemetry_path=?, event_count=?,
                             match_created_at=?, map_name=?, game_mode=?,
                             attempts=attempts+1, error=NULL
            WHERE match_id=?
            """,
            (
                _now(),
                telemetry_path,
                event_count,
                match_created_at,
                map_name,
                game_mode,
                match_id,
            ),
        )
        self.conn.commit()

    def mark_failed(self, match_id: str, error: str, *, gone: bool = False) -> None:
        """`gone` means the match aged out of the API — never retry it."""
        self.conn.execute(
            "UPDATE match SET status=?, attempts=attempts+1, error=? WHERE match_id=?",
            ("gone" if gone else "failed", error[:500], match_id),
        )
        self.conn.commit()

    def mark_retryable(self, match_id: str, error: str) -> None:
        """Record a failure that says nothing about the match itself.

        Network and TLS errors are environmental — the match is probably fine.
        Burning the attempt budget on them would permanently discard perfectly
        good matches just because a firewall was in the way.
        """
        self.conn.execute(
            "UPDATE match SET status='pending', error=? WHERE match_id=?",
            (error[:500], match_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------- shredding

    def unshredded(self, limit: int) -> list[tuple[str, str]]:
        """Fetched matches that haven't been shredded yet, oldest first."""
        rows = self.conn.execute(
            """
            SELECT m.match_id, m.telemetry_path
            FROM match m
            LEFT JOIN shred s USING (match_id)
            WHERE m.status = 'done' AND m.telemetry_path IS NOT NULL AND s.match_id IS NULL
            ORDER BY m.match_created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r["match_id"], r["telemetry_path"]) for r in rows]

    def mark_shredded(self, match_ids: list[str], part: int) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO shred (match_id, shredded_at, part) VALUES (?, ?, ?)",
            [(mid, _now(), part) for mid in match_ids],
        )
        self.conn.commit()

    def unshredded_positions(self, limit: int) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT m.match_id, m.telemetry_path
            FROM match m
            LEFT JOIN shred_position s USING (match_id)
            WHERE m.status = 'done' AND m.telemetry_path IS NOT NULL AND s.match_id IS NULL
            ORDER BY m.match_created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r["match_id"], r["telemetry_path"]) for r in rows]

    def mark_positions_shredded(self, entries: list[tuple[str, int]], part: int) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO shred_position (match_id, shredded_at, part, rows) "
            "VALUES (?, ?, ?, ?)",
            [(mid, _now(), part, n) for mid, n in entries],
        )
        self.conn.commit()

    def next_position_part(self) -> int:
        row = self.conn.execute("SELECT coalesce(max(part), -1) + 1 FROM shred_position").fetchone()
        return int(row[0])

    def position_stats(self) -> dict[str, int]:
        r = self.conn.execute(
            "SELECT count(*), coalesce(sum(rows), 0) FROM shred_position"
        ).fetchone()
        fetched = self.conn.execute("SELECT count(*) FROM match WHERE status='done'").fetchone()[0]
        return {"matches": int(r[0]), "rows": int(r[1]), "remaining": fetched - int(r[0])}

    def next_shred_part(self) -> int:
        row = self.conn.execute("SELECT coalesce(max(part), -1) + 1 FROM shred").fetchone()
        return int(row[0])

    def shred_stats(self) -> dict[str, int]:
        shredded = self.conn.execute("SELECT count(*) FROM shred").fetchone()[0]
        fetched = self.conn.execute("SELECT count(*) FROM match WHERE status='done'").fetchone()[0]
        return {"shredded": shredded, "fetched": fetched, "remaining": fetched - shredded}

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, count(*) n FROM match GROUP BY status").fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["total"] = sum(out.values())
        out["events"] = (
            self.conn.execute("SELECT coalesce(sum(event_count), 0) FROM match").fetchone()[0] or 0
        )
        return out
