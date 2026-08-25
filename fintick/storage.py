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
    """
    CREATE TABLE IF NOT EXISTS research (
        uri TEXT PRIMARY KEY REFERENCES posts(uri) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK (status IN ('processing', 'complete', 'error')),
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        query TEXT NOT NULL,
        links_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        researched_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS research_status_idx ON research(status, attempts)",
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
        # v2 tables (PRD.md §6): pure additions — existing databases gain
        # them on next open, no ALTER or backfill.
        for statement in V2_SCHEMA:
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

# ---------------------------------------------------------------------------
# v2: stream-signal + validation (PRD.md §6). Additive alongside v1 —
# ingested posts and the v1 tables are untouched; v2 tables start empty,
# so this needs no ALTER or backfill.
# ---------------------------------------------------------------------------

EVENT_STATUSES = ("breaking", "confirmed", "contradicted", "developing")
STANCES = ("corroborating", "disputing", "partial")

V2_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        headline TEXT NOT NULL,
        summary TEXT,
        status TEXT NOT NULL DEFAULT 'breaking'
            CHECK (status IN ('breaking', 'confirmed', 'contradicted', 'developing')),
        importance INTEGER CHECK (importance BETWEEN 1 AND 5),
        facts_json TEXT NOT NULL DEFAULT '[]',
        instruments_json TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        validated_at TEXT,
        validation_attempts INTEGER NOT NULL DEFAULT 0,
        lead_seconds INTEGER,
        error TEXT,
        observed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_first_seen_idx ON events(first_seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS events_status_idx ON events(status, first_seen_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS event_signals (
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        post_uri TEXT NOT NULL REFERENCES posts(uri) ON DELETE CASCADE,
        first_note TEXT,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (event_id, post_uri)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_validations (
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        title TEXT,
        publisher TEXT,
        stance TEXT NOT NULL DEFAULT 'corroborating'
            CHECK (stance IN ('corroborating', 'disputing', 'partial')),
        published_at TEXT,
        collected_at TEXT NOT NULL,
        PRIMARY KEY (event_id, url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS event_validations_url_idx ON event_validations(url)",
)


@dataclass(frozen=True, slots=True)
class V2Event:
    """One aggregated stream event (PRD F2), stored by a stable key."""

    key: str
    headline: str
    summary: str | None
    facts: tuple[dict[str, Any], ...]
    instruments: tuple[dict[str, Any], ...]
    importance: int | None
    post_uris: tuple[str, ...]
    first_seen_at: str
    last_seen_at: str
    signal_notes: tuple[dict[str, Any], ...] | None = None

    @classmethod
    def from_key(
        cls,
        headline: str,
        summary: str | None,
        *,
        primary_instrument: str | None,
        facts: tuple[dict[str, Any], ...] = (),
        instruments: tuple[dict[str, Any], ...] = (),
        importance: int | None = None,
        post_uris: tuple[str, ...] = (),
        first_seen_at: str,
        last_seen_at: str,
        signal_notes: tuple[dict[str, Any], ...] | None = None,
    ) -> "V2Event":
        """Build an event from its identifying fields, deriving the key."""
        return V2Event(
            key=event_key(headline, primary_instrument),
            headline=headline,
            summary=summary,
            facts=tuple(facts),
            instruments=tuple(instruments),
            importance=importance,
            post_uris=tuple(post_uris),
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            signal_notes=signal_notes,
        )


def event_key(headline: str, primary_instrument: str | None = None) -> str:
    """Deterministic identity for one distinct event (see STATUS.md D1).

    Key = SHA-1 over the normalized headline plus the primary instrument, so
    repeated aggregation passes over the rolling window land on the same row
    and a merge can happen deterministically. Two genuinely distinct events
    are expected to produce distinct keys; merging is the model's job (F2).
    """
    normalized = normalize_text(headline)
    # Ticker forms differ across sources ($NVDA, NVDA, nvda) — collapse them.
    instrument = ""
    if primary_instrument:
        instrument = primary_instrument.strip().lower().lstrip("$").strip()
    return "evt-" + text_hash(f"event|{normalized}|{instrument}")


def upsert_event(connection: sqlite3.Connection, event: V2Event) -> tuple[int, bool]:
    """Record one merged event, returning (event_id, created).

    Idempotent on the event ``key`` and never touches validation state —
    status, validating sources, and lead time are owned exclusively by the
    F4 validate stage, so re-aggregation can never clobber a flip to
    ``confirmed``. Re-running aggregation over the rolling window therefore
    never creates a second row or resets a validated event: it only widens
    the first/last-seen span, takes the max importance, and unions the
    stream signals.
    """
    notes = event.signal_notes or []
    facts_json = json.dumps(event.facts, separators=(",", ":"), ensure_ascii=False)
    instruments_json = json.dumps(event.instruments, separators=(",", ":"), ensure_ascii=False)
    now = datetime.now(UTC).isoformat()

    cursor = connection.execute(
        """
        INSERT INTO events (
            key, headline, summary, importance, facts_json, instruments_json,
            first_seen_at, last_seen_at, observed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (
            event.key, event.headline, event.summary, event.importance,
            facts_json, instruments_json,
            event.first_seen_at, event.last_seen_at, now, now,
        ),
    )
    if cursor.rowcount == 1:
        event_id = int(cursor.lastrowid or 0)
        created = True
    else:
        # Already known — merge into it. Status, validation state, and lead
        # time are owned by the F4 validate stage, so touch nothing but the
        # identity/summary fields and the stream-seen span.
        existing_id, existing_first, existing_last, existing_importance = (
            connection.execute(
                "SELECT id, first_seen_at, last_seen_at, importance "
                "FROM events WHERE key = ?",
                (event.key,),
            ).fetchone()
        )
        event_id = int(existing_id)
        created = False
        # Widen the span; both columns are NOT NULL.
        merged_first = min(existing_first, event.first_seen_at)
        merged_last = max(existing_last, event.last_seen_at)
        if existing_importance is None:
            merged_importance = event.importance
        elif event.importance is None:
            merged_importance = existing_importance
        else:
            merged_importance = max(existing_importance, event.importance)
        connection.execute(
            """
            UPDATE events SET
                headline = ?, summary = ?, importance = ?,
                facts_json = ?, instruments_json = ?,
                first_seen_at = ?, last_seen_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                event.headline, event.summary, merged_importance,
                facts_json, instruments_json,
                merged_first, merged_last, now, event_id,
            ),
        )

    for i, post_uri in enumerate(event.post_uris):
        note: str | None = None
        if i < len(notes):
            first_note_entry = notes[i].get("note")
            if first_note_entry is not None:
                note = str(first_note_entry)
        connection.execute(
            """
            INSERT OR IGNORE INTO event_signals (event_id, post_uri, first_note, observed_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, post_uri, note, now),
        )
    return int(event_id), created


def record_validation(
    connection: sqlite3.Connection,
    event_id: int,
    *,
    url: str,
    title: str | None,
    publisher: str | None,
    stance: str,
    published_at: str | None,
) -> bool:
    """Attach one external validating source; True when newly linked.

    Re-hunts (``breaking`` flipping to ``confirmed``) must be re-runnable,
    so the (event, url) pair upserts: the first-seen ``collected_at`` is
    preserved, and title/publisher/stance/published_at are refreshed when the
    re-hunt learned more.
    """
    if stance not in STANCES:
        raise ValueError(f"stance must be one of {STANCES}")
    collected_at = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO event_validations (
            event_id, url, title, publisher, stance, published_at, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, url, title, publisher, stance, published_at, collected_at),
    )
    if cursor.rowcount == 1:
        return True
    # Re-hunt found a source already on record: refresh what we learned.
    connection.execute(
        """
        UPDATE event_validations
        SET title = ?, publisher = ?, stance = ?,
            published_at = COALESCE(published_at, ?)
        WHERE event_id = ? AND url = ?
        """,
        (title, publisher, stance, published_at, event_id, url),
    )
    return False


def set_event_status(
    connection: sqlite3.Connection,
    event_id: int,
    status: str,
    *,
    lead_seconds: int | None = None,
    error: str | None = None,
) -> None:
    """Set an event's validation status and (for confirmed) the wire lag."""
    if status not in EVENT_STATUSES:
        raise ValueError(f"status must be one of {EVENT_STATUSES}")
    connection.execute(
        """
        UPDATE events SET status = ?, lead_seconds = ?, error = ?,
            validated_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            int(lead_seconds) if lead_seconds is not None else None,
            error,
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
            event_id,
        ),
    )


def load_events(
    connection: sqlite3.Connection,
    *,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """Events joined with stream-seen counts and validating-source detail.

    One read shape for the M5 dashboard board and for acceptance tests:
    headline, facts/instruments, the stream's own count (never "N sources"),
    plus the external sources (real news, with URLs) sorted latest-first by
    event, then newest-first.
    """
    limit_clause = "" if limit is None else " LIMIT ?"
    params: list[Any] = [limit] if limit is not None else []
    rows = connection.execute(
        f"""
        SELECT e.id, e.key, e.headline, e.summary, e.status, e.importance,
               e.facts_json, e.instruments_json, e.first_seen_at, e.last_seen_at,
               e.validated_at, e.validation_attempts, e.lead_seconds, e.error,
               e.updated_at,
               (SELECT COUNT(*) FROM event_signals s WHERE s.event_id = e.id) AS stream_seen
        FROM events e
        ORDER BY e.first_seen_at DESC, e.id DESC
        {limit_clause}
        """,
        params,
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event_id = row[0]
        sources = connection.execute(
            """
            SELECT url, title, publisher, stance, published_at, collected_at
            FROM event_validations WHERE event_id = ?
            ORDER BY COALESCE(published_at, collected_at) DESC, collected_at DESC
            """,
            (event_id,),
        ).fetchall()
        events.append(
            {
                "id": event_id,
                "key": row[1],
                "headline": row[2],
                "summary": row[3],
                "status": row[4],
                "importance": row[5],
                "facts": json.loads(row[6]),
                "instruments": json.loads(row[7]),
                "first_seen_at": row[8],
                "last_seen_at": row[9],
                "validated_at": row[10],
                "validation_attempts": row[11],
                "lead_seconds": row[12],
                "error": row[13],
                "updated_at": row[14],
                "stream_seen": row[15],
                "validations": [
                    {
                        "url": s[0], "title": s[1], "publisher": s[2],
                        "stance": s[3], "published_at": s[4], "collected_at": s[5],
                    }
                    for s in sources
                ],
            }
        )
    return events
