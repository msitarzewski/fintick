"""SQLite persistence for FinTick's durable, deduplicated feed."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fintick.dedup import normalize_text, text_hash

DEDUP_WINDOW = timedelta(minutes=60)

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    uri TEXT PRIMARY KEY,
    cid TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    langs_json TEXT NOT NULL DEFAULT '[]',
    embed_type TEXT,
    raw_json TEXT NOT NULL,
    inserted_at TEXT NOT NULL
)
"""

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS enrichments (
        uri TEXT PRIMARY KEY REFERENCES posts(uri) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK (status IN ('processing', 'complete', 'error')),
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        summary TEXT,
        category TEXT,
        importance INTEGER CHECK (importance BETWEEN 1 AND 5),
        sentiment TEXT,
        instruments_json TEXT NOT NULL DEFAULT '[]',
        entities_json TEXT NOT NULL DEFAULT '[]',
        regions_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        enriched_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS enrichments_status_idx ON enrichments(status, attempts)",
    "CREATE INDEX IF NOT EXISTS posts_created_at_idx ON posts(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS posts_dedup_idx "
    "ON posts(normalized_hash, created_at)",
    "CREATE INDEX IF NOT EXISTS posts_canonical_idx ON posts(canonical_uri)",
    """
    CREATE TABLE IF NOT EXISTS ingest_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

DEDUP_COLUMNS = {
    "normalized_text": "TEXT",
    "normalized_hash": "TEXT",
    "is_duplicate": "INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1))",
    "canonical_uri": "TEXT REFERENCES posts(uri)",
}


@dataclass(frozen=True, slots=True)
class InsertResult:
    inserted: bool
    deduplicated: int = 0


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("feed timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def _reconcile_hash(connection: sqlite3.Connection, normalized_hash: str) -> int:
    """Deterministically cluster one hash and return its duplicate-count change.

    Each chronological canonical anchors its own 60-minute window. This avoids
    transitive chaining and guarantees every duplicate points directly to a
    canonical row, independent of insertion order.
    """
    raw_rows = connection.execute(
        "SELECT uri, created_at FROM posts WHERE normalized_hash = ?",
        (normalized_hash,),
    ).fetchall()
    before = connection.execute(
        "SELECT COUNT(*) FROM posts WHERE normalized_hash = ? AND is_duplicate = 1",
        (normalized_hash,),
    ).fetchone()[0]

    valid: list[tuple[datetime, str]] = []
    malformed: list[str] = []
    for uri, created_at in raw_rows:
        try:
            valid.append((_parse_timestamp(created_at), uri))
        except (TypeError, ValueError):
            # Preserve malformed legacy rows for audit without letting one bad
            # timestamp poison database startup or unrelated feed processing.
            malformed.append(uri)
    valid.sort(key=lambda row: (row[0], row[1]))

    assignments: list[tuple[int, str | None, str]] = []
    canonical_time: datetime | None = None
    canonical_uri: str | None = None
    for timestamp, uri in valid:
        if canonical_time is None or timestamp - canonical_time > DEDUP_WINDOW:
            canonical_time, canonical_uri = timestamp, uri
            assignments.append((0, None, uri))
        else:
            assignments.append((1, canonical_uri, uri))
    assignments.extend((0, None, uri) for uri in sorted(malformed))

    connection.executemany(
        "UPDATE posts SET is_duplicate=?, canonical_uri=? WHERE uri=?",
        assignments,
    )
    after = sum(is_duplicate for is_duplicate, _, _ in assignments)
    return after - before


def _migrate_and_backfill(connection: sqlite3.Connection) -> None:
    """Upgrade databases from pre-dedup milestones and classify old rows."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(posts)").fetchall()
    }
    for name, declaration in DEDUP_COLUMNS.items():
        if name not in columns:
            # Names and declarations come only from the fixed constant above.
            connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {declaration}")

    stale = connection.execute(
        "SELECT uri, text FROM posts WHERE normalized_hash IS NULL"
    ).fetchall()
    affected_hashes: set[str] = set()
    for uri, text in stale:
        normalized = normalize_text(text)
        digest = text_hash(text)
        connection.execute(
            "UPDATE posts SET normalized_text=?, normalized_hash=? WHERE uri=?",
            (normalized, digest, uri),
        )
        affected_hashes.add(digest)
    for digest in affected_hashes:
        _reconcile_hash(connection, digest)


@contextmanager
def open_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open and initialize a FinTick database, committing on success."""
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        # Legacy databases need columns before indexes can refer to them.
        connection.execute(BASE_SCHEMA)
        _migrate_and_backfill(connection)
        for statement in SCHEMA:
            connection.execute(statement)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_post(connection: sqlite3.Connection, post: dict[str, Any]) -> InsertResult:
    """Insert and classify one post, returning insertion and collapse counts."""
    record = post["record"]
    embed = post.get("embed") or {}
    text = record["text"]
    created_at = record["createdAt"]
    _parse_timestamp(created_at)  # reject malformed new input before insertion
    normalized = normalize_text(text)
    digest = text_hash(text)
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO posts (
            uri, cid, text, created_at, indexed_at, langs_json,
            embed_type, raw_json, inserted_at, normalized_text, normalized_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post["uri"], post["cid"], text, created_at,
            post.get("indexedAt", created_at),
            json.dumps(record.get("langs", []), separators=(",", ":")),
            embed.get("$type"),
            json.dumps(post, separators=(",", ":"), ensure_ascii=False),
            now, normalized, digest,
        ),
    )
    inserted = cursor.rowcount == 1
    deduplicated = _reconcile_hash(connection, digest) if inserted else 0
    return InsertResult(inserted=inserted, deduplicated=deduplicated)


def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    """Persist a high-water mark or other ingest state."""
    connection.execute(
        """
        INSERT INTO ingest_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, datetime.now(UTC).isoformat()),
    )
