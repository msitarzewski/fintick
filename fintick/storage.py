"""SQLite persistence for FinTick's durable raw feed."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
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
);
CREATE INDEX IF NOT EXISTS posts_created_at_idx ON posts(created_at DESC);
CREATE TABLE IF NOT EXISTS ingest_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def open_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open and initialize a FinTick database, committing on success."""
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_post(connection: sqlite3.Connection, post: dict[str, Any]) -> bool:
    """Insert one Bluesky post, returning false when its URI already exists."""
    record = post["record"]
    embed = post.get("embed") or {}
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO posts (
            uri, cid, text, created_at, indexed_at, langs_json,
            embed_type, raw_json, inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post["uri"],
            post["cid"],
            record["text"],
            record["createdAt"],
            post.get("indexedAt", record["createdAt"]),
            json.dumps(record.get("langs", []), separators=(",", ":")),
            embed.get("$type"),
            json.dumps(post, separators=(",", ":"), ensure_ascii=False),
            now,
        ),
    )
    return cursor.rowcount == 1


def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    """Persist a high-water mark or other ingest state."""
    connection.execute(
        """
        INSERT INTO ingest_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, datetime.now(UTC).isoformat()),
    )
