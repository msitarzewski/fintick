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
POST_AGGREGATION_MAX_ATTEMPTS = 3

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


def _assert_event_signal_ownership(connection: sqlite3.Connection) -> None:
    duplicate = connection.execute(
        """
        SELECT post_uri, COUNT(DISTINCT event_id)
        FROM event_signals
        GROUP BY post_uri
        HAVING COUNT(DISTINCT event_id) > 1
        ORDER BY post_uri
        LIMIT 1
        """
    ).fetchone()
    if duplicate:
        raise RuntimeError(
            "ambiguous signal ownership: "
            f"{duplicate[0]!r} is linked to {duplicate[1]} events; repair the database before startup"
        )


def _migrate_post_aggregation_decisions(connection: sqlite3.Connection) -> None:
    """Apply additive v2.1 ledger columns to databases opened pre-release."""
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(post_aggregation_decisions)"
        ).fetchall()
    }
    if "retry_group" not in columns:
        connection.execute(
            "ALTER TABLE post_aggregation_decisions ADD COLUMN retry_group TEXT"
        )


def _migrate_event_validation_provenance(connection: sqlite3.Connection) -> None:
    """Add feed provenance to validation rows created before v2.1."""
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(event_validations)").fetchall()
    }
    additions = (
        ("feed_name", "ALTER TABLE event_validations ADD COLUMN feed_name TEXT"),
        ("feed_url", "ALTER TABLE event_validations ADD COLUMN feed_url TEXT"),
        ("feed_type", "ALTER TABLE event_validations ADD COLUMN feed_type TEXT"),
    )
    for name, statement in additions:
        if name not in columns:
            connection.execute(statement)


def _bootstrap_post_aggregation_decisions(connection: sqlite3.Connection) -> None:
    """Account for pre-v2.1 posts without pretending old history was reviewed."""
    rows = connection.execute(
        """
        SELECT p.uri, p.created_at, es.event_id
        FROM posts p
        LEFT JOIN event_signals es ON es.post_uri = p.uri
        LEFT JOIN post_aggregation_decisions d ON d.post_uri = p.uri
        WHERE d.post_uri IS NULL
        ORDER BY p.created_at, p.uri
        """
    ).fetchall()
    if not rows:
        return

    valid_times: list[datetime] = []
    parsed_times: dict[str, datetime] = {}
    for post_uri, created_at, _ in rows:
        try:
            parsed = _parse_timestamp(created_at)
        except (TypeError, ValueError):
            continue
        parsed_times[post_uri] = parsed
        valid_times.append(parsed)
    cutoff = max(valid_times) - timedelta(hours=6) if valid_times else None
    now = datetime.now(UTC).isoformat()

    decisions: list[tuple[str, str, int | None, str | None, str]] = []
    for post_uri, _, event_id in rows:
        if event_id is not None:
            state, reason = "assigned", None
        elif post_uri not in parsed_times:
            state, reason = "errored", "malformed legacy post timestamp"
        elif cutoff is not None and parsed_times[post_uri] < cutoff:
            state, reason = "out_of_scope", "predates v2.1 bootstrap window"
        else:
            state, reason = "pending", None
        decisions.append((post_uri, state, event_id, reason, now))
    connection.executemany(
        """
        INSERT INTO post_aggregation_decisions (
            post_uri, state, event_id, reason, attempts, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?)
        """,
        decisions,
    )


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
        _migrate_post_aggregation_decisions(connection)
        _migrate_event_validation_provenance(connection)
        _assert_event_signal_ownership(connection)
        _bootstrap_post_aggregation_decisions(connection)
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
    if inserted:
        connection.execute(
            """
            INSERT INTO post_aggregation_decisions (
                post_uri, state, event_id, reason, attempts, updated_at
            ) VALUES (?, 'pending', NULL, NULL, 0, ?)
            """,
            (post["uri"], now),
        )
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
    CREATE TRIGGER IF NOT EXISTS event_signals_one_event
    BEFORE INSERT ON event_signals
    WHEN EXISTS (
        SELECT 1 FROM event_signals
        WHERE post_uri = NEW.post_uri AND event_id != NEW.event_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'post_uri already assigned to another event');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_signals_one_event_update
    BEFORE UPDATE OF event_id, post_uri ON event_signals
    WHEN EXISTS (
        SELECT 1 FROM event_signals
        WHERE post_uri = NEW.post_uri
          AND NOT (event_id = OLD.event_id AND post_uri = OLD.post_uri)
    )
    BEGIN
        SELECT RAISE(ABORT, 'post_uri already assigned to another event');
    END
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
        feed_name TEXT,
        feed_url TEXT,
        feed_type TEXT,
        collected_at TEXT NOT NULL,
        PRIMARY KEY (event_id, url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS event_validations_url_idx ON event_validations(url)",
    """
    CREATE TABLE IF NOT EXISTS post_aggregation_decisions (
        post_uri TEXT PRIMARY KEY REFERENCES posts(uri) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'assigned', 'ignored', 'errored', 'out_of_scope')),
        event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
        reason TEXT,
        retry_group TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS post_aggregation_decisions_state_idx "
    "ON post_aggregation_decisions(state, updated_at)",
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


def set_post_aggregation_decision(
    connection: sqlite3.Connection,
    post_uri: str,
    state: str,
    *,
    event_id: int | None = None,
    reason: str | None = None,
    retry_group: str | None = None,
) -> None:
    """Persist one auditable aggregation outcome for a stream post."""
    if state not in {"assigned", "ignored", "errored"}:
        raise ValueError(f"invalid terminal aggregation state: {state}")
    if state == "assigned" and event_id is None:
        raise ValueError("assigned post requires event_id")
    if state != "assigned" and event_id is not None:
        raise ValueError(f"{state} post cannot reference an event")
    if state in {"ignored", "errored"} and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError(f"{state} post requires a reason")
    if state == "errored" and (not isinstance(retry_group, str) or not retry_group):
        raise ValueError("errored post requires retry_group")
    if state != "errored":
        retry_group = None
    cursor = connection.execute(
        """
        UPDATE post_aggregation_decisions
        SET state = ?, event_id = ?, reason = ?, retry_group = ?,
            attempts = attempts + 1, updated_at = ?
        WHERE post_uri = ?
          AND (state = 'pending' OR (state = 'errored' AND attempts < ?))
        """,
        (state, event_id, reason.strip() if isinstance(reason, str) else None,
         retry_group, datetime.now(UTC).isoformat(), post_uri,
         POST_AGGREGATION_MAX_ATTEMPTS),
    )
    if cursor.rowcount == 1:
        return
    existing = connection.execute(
        "SELECT state, event_id FROM post_aggregation_decisions WHERE post_uri = ?",
        (post_uri,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"unknown post aggregation decision: {post_uri}")
    if existing[0] == state and (state != "assigned" or existing[1] == event_id):
        return
    raise ValueError(
        f"post aggregation decision already terminal: {post_uri} is {existing[0]}"
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

    Idempotent on the event ``key`` and stable stream-post membership. A
    rolling model pass may reword the canonical headline; overlap with an
    existing signal therefore wins over the newly derived headline key. The
    merge never touches validation state — status, validating sources, and
    lead time are owned exclusively by the F4 validate stage. Re-running
    aggregation only widens the first/last-seen span, takes the max
    importance, refreshes descriptive fields, and unions stream signals.
    """
    notes = event.signal_notes or []
    facts_json = json.dumps(event.facts, separators=(",", ":"), ensure_ascii=False)
    instruments_json = json.dumps(event.instruments, separators=(",", ":"), ensure_ascii=False)
    now = datetime.now(UTC).isoformat()

    # Resolve every stable identity signal before writing. A candidate that
    # bridges distinct existing events is ambiguous model output, not evidence
    # that either event should absorb the other.
    candidate_event_ids: set[int] = set()
    if event.post_uris:
        placeholders = ",".join("?" for _ in event.post_uris)
        candidate_event_ids.update(
            int(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT event_id FROM event_signals WHERE post_uri IN ({placeholders})",
                event.post_uris,
            ).fetchall()
        )
    key_row = connection.execute(
        "SELECT id FROM events WHERE key = ?", (event.key,)
    ).fetchone()
    if key_row:
        candidate_event_ids.add(int(key_row[0]))
    if len(candidate_event_ids) > 1:
        raise ValueError("event candidate overlaps multiple existing events")

    created = False
    if candidate_event_ids:
        event_id = next(iter(candidate_event_ids))
    else:
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
            # A concurrent writer may have inserted the key after resolution.
            event_id = int(connection.execute(
                "SELECT id FROM events WHERE key = ?", (event.key,)
            ).fetchone()[0])

    if not created:
        # Already known by key or by stable stream membership. Validation state
        # remains untouched while descriptive model output is refreshed.
        existing_first, existing_last, existing_importance = connection.execute(
            "SELECT first_seen_at, last_seen_at, importance FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        merged_first = min(existing_first, event.first_seen_at, key=_parse_timestamp)
        merged_last = max(existing_last, event.last_seen_at, key=_parse_timestamp)
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
    feed_name: str | None = None,
    feed_url: str | None = None,
    feed_type: str | None = None,
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
            event_id, url, title, publisher, stance, published_at,
            feed_name, feed_url, feed_type, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, url, title, publisher, stance, published_at,
            feed_name, feed_url, feed_type, collected_at,
        ),
    )
    if cursor.rowcount == 1:
        return True
    # Re-hunt found a source already on record: refresh what we learned.
    connection.execute(
        """
        UPDATE event_validations
        SET title = ?, publisher = ?, stance = ?,
            feed_name = COALESCE(?, feed_name),
            feed_url = COALESCE(?, feed_url),
            feed_type = COALESCE(?, feed_type),
            published_at = CASE
                WHEN julianday(published_at) IS NOT NULL THEN published_at
                ELSE ?
            END
        WHERE event_id = ? AND url = ?
        """,
        (
            title, publisher, stance, feed_name, feed_url, feed_type,
            published_at, event_id, url,
        ),
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


def load_pipeline_health(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return auditable post-accounting and backlog health."""
    posts = int(connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
    counts = {
        str(state): int(count)
        for state, count in connection.execute(
            "SELECT state, COUNT(*) FROM post_aggregation_decisions GROUP BY state"
        ).fetchall()
    }
    pending = counts.get("pending", 0)
    retrying = int(connection.execute(
        "SELECT COUNT(*) FROM post_aggregation_decisions "
        "WHERE state='errored' AND attempts < ?",
        (POST_AGGREGATION_MAX_ATTEMPTS,),
    ).fetchone()[0])
    terminal_errors = counts.get("errored", 0) - retrying
    oldest_pending_row = connection.execute(
        """
        SELECT p.created_at
        FROM posts p
        JOIN post_aggregation_decisions d ON d.post_uri = p.uri
        WHERE d.state='pending' OR (d.state='errored' AND d.attempts < ?)
        ORDER BY julianday(p.created_at) IS NULL, julianday(p.created_at), p.uri
        LIMIT 1
        """,
        (POST_AGGREGATION_MAX_ATTEMPTS,),
    ).fetchone()
    oldest_pending = oldest_pending_row[0] if oldest_pending_row else None
    latest_post_row = connection.execute(
        "SELECT created_at FROM posts "
        "ORDER BY julianday(created_at) IS NULL, julianday(created_at) DESC, uri DESC "
        "LIMIT 1"
    ).fetchone()
    latest_post = latest_post_row[0] if latest_post_row else None
    latest_decision = connection.execute(
        "SELECT MAX(updated_at) FROM post_aggregation_decisions "
        "WHERE state IN ('assigned', 'ignored', 'errored')"
    ).fetchone()[0]
    accounted = sum(counts.get(state, 0) for state in (
        "assigned", "ignored", "out_of_scope"
    ))
    return {
        "posts": posts,
        "accounted": accounted,
        "backlog": pending + retrying,
        "pending": pending,
        "retrying": retrying,
        "terminal_errors": terminal_errors,
        "oldest_pending_at": oldest_pending,
        "latest_post_at": latest_post,
        "latest_decision_at": latest_decision,
        "decisions": counts,
    }


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
            SELECT url, title, publisher, stance, published_at, collected_at,
                   feed_name, feed_url, feed_type
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
                        "feed_name": s[6], "feed_url": s[7], "feed_type": s[8],
                    }
                    for s in sources
                ],
            }
        )
    return events
