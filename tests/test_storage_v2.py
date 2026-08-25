"""Offline v2 storage tests: events, stream signals, and validation sources.

This is the persistence layer for the v2 pivot (PRD.md). The stream is the
single origin; repeated posts of one event are signals, never "sources".
External (real-news) validation rows are the only thing ever called sources.

Acceptance anchor (see STATUS.md M1): four real stream posts of ONE event must
sit as ONE event with stream_seen == 4, default status 'breaking', and zero
external validations — even when no live news is reachable (offline).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fintick.ingest import ingest_fixture
from fintick.storage import (
    EVENT_STATUSES,
    STANCES,
    V2Event,
    event_key,
    load_events,
    open_database,
    record_validation,
    set_event_status,
    upsert_event,
)

REPO_ROOT = Path(__file__).parents[1]
NVDA_FIXTURE = REPO_ROOT / "reference" / "nvda_repost_cluster.json"

V2_TABLES = {"events", "event_signals", "event_validations"}
V1_TABLES = {"posts", "enrichments", "research", "ingest_state"}

# Fixed ISO-8601 UTC timestamps keep the tests deterministic.
T0 = "2026-08-24T15:00:11+00:00"
T1 = "2026-08-24T15:02:40+00:00"
T2 = "2026-08-24T15:05:03+00:00"
T3 = "2026-08-24T15:08:22+00:00"


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _nvda_posts() -> list[dict]:
    data = json.loads(NVDA_FIXTURE.read_text(encoding="utf-8"))
    return [entry["post"] for entry in data["feed"]]


def _make_event(
    headline: str = "NVIDIA FELL FOR A 7TH DAY, ITS LONGEST LOSING STREAK SINCE 2022",
    *,
    post_uris: tuple[str, ...] = (),
    first_seen_at: str = T0,
    last_seen_at: str = T0,
    importance: int | None = 4,
    primary_instrument: str | None = "NVDA",
) -> V2Event:
    return V2Event.from_key(
        headline,
        "NVIDIA is down for a seventh straight session, its longest slide since 2022.",
        primary_instrument=primary_instrument,
        post_uris=tuple(post_uris),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        importance=importance,
    )


class FreshDatabaseTests(unittest.TestCase):
    def test_fresh_database_has_v1_and_v2_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                tables = _tables(connection)
            self.assertTrue(V2_TABLES.issubset(tables), tables)
            self.assertTrue(V1_TABLES.issubset(tables), tables)

    def test_open_database_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                first = _tables(connection)
            with open_database(db) as connection:
                second = _tables(connection)
            self.assertEqual(first, second)
            self.assertTrue(V2_TABLES.issubset(second))

    def test_event_key_is_stable_and_instrument_scoped(self) -> None:
        a = event_key(
            "NVIDIA FELL FOR A 7TH DAY, ITS LONGEST LOSING STREAK SINCE 2022",
            "NVDA",
        )
        b = event_key(
            "nvidia fell for a 7th day, its longest losing streak since 2022.",
            "$nvda",
        )
        # Same normalized headline + instrument -> same key regardless of case,
        # surrounding punctuation, and ticker form.
        self.assertEqual(a, b)
        # A different instrument anchors a distinct key.
        self.assertNotEqual(a, event_key("NVIDIA FELL FOR A 7TH DAY", "AMD"))


class UpsertEventTests(unittest.TestCase):
    """One NVDA cluster per test method — no cross-test event accumulation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "fintick.db"
        ingest_fixture(NVDA_FIXTURE, self.db)

    def _all_uris(self) -> tuple[str, ...]:
        with open_database(self.db) as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT uri FROM posts ORDER BY created_at"
                ).fetchall()
            )

    def test_repeated_upsert_same_key_stays_one_event(self) -> None:
        uris = self._all_uris()
        with open_database(self.db) as connection:
            first_id, created = upsert_event(
                connection,
                _make_event(post_uris=uris[:3], first_seen_at=T1, last_seen_at=T2),
            )
            # A later aggregation pass adds a fourth uri and a wider span,
            # same key.
            _, created_again = upsert_event(
                connection,
                _make_event(post_uris=uris, first_seen_at=T0, last_seen_at=T3),
            )
        rows = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(created, True)
        self.assertEqual(created_again, False)
        self.assertEqual(rows, 1)

    def test_one_event_unifies_signals_and_spans(self) -> None:
        uris = self._all_uris()
        with open_database(self.db) as connection:
            # Two aggregation passes each see half the cluster.
            upsert_event(connection, _make_event(post_uris=uris[:2], first_seen_at=T1, last_seen_at=T2))
            upsert_event(connection, _make_event(post_uris=uris[2:], first_seen_at=T0, last_seen_at=T3))
            events = load_events(connection)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["stream_seen"], 4)
            self.assertEqual(event["first_seen_at"], T0)
            self.assertEqual(event["last_seen_at"], T3)
            self.assertEqual(event["status"], "breaking")
            self.assertEqual(event["validations"], [])
            self.assertIsNotNone(event["importance"])

    def test_distinct_key_creates_second_event(self) -> None:
        with open_database(self.db) as connection:
            upsert_event(connection, _make_event())
            upsert_event(
                connection,
                _make_event(headline="A SEPARATE MARKET EVENT", primary_instrument="AMD"),
            )
            rows = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        base_rows = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        self.assertEqual(base_rows, 2)


class StatusIsOwnedByValidateTests(unittest.TestCase):
    def test_upsert_never_resets_confirmed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                event_id, _ = upsert_event(connection, _make_event())
                set_event_status(connection, event_id, "confirmed", lead_seconds=42)
                # Re-run aggregation: status and lead time must survive.
                upsert_event(connection, _make_event(first_seen_at=T0, last_seen_at=T3))
                row = connection.execute(
                    "SELECT status, lead_seconds, validated_at FROM events WHERE id=?",
                    (event_id,),
                ).fetchone()
                self.assertEqual(row[0], "confirmed")
                self.assertEqual(row[1], 42)
                self.assertIsNotNone(row[2])

    def test_invalid_status_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                event_id, _ = upsert_event(connection, _make_event())
                with self.assertRaises(ValueError):
                    set_event_status(connection, event_id, "unverified")


class RecordValidationTests(unittest.TestCase):
    def test_first_link_is_new_and_relink_is_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                event_id, _ = upsert_event(connection, _make_event())
                url = "https://example.com/nvda-seven-day-slide"
                first = record_validation(
                    connection, event_id, url=url,
                    title="Nvidia's brutal 7-day slide", publisher="Reuters",
                    stance="corroborating", published_at=T1,
                )
                again = record_validation(
                    connection, event_id, url=url,
                    title="Nvidia 7-day losing streak deepens", publisher="Reuters",
                    stance="corroborating", published_at=T2,
                )
                self.assertEqual(first, True)
                self.assertEqual(again, False)
                count = connection.execute(
                    "SELECT COUNT(*) FROM event_validations WHERE event_id=?",
                    (event_id,),
                ).fetchone()[0]
                self.assertEqual(count, 1)
                events = load_events(connection)
                self.assertEqual(len(events[0]["validations"]), 1)
                self.assertEqual(events[0]["validations"][0]["url"], url)

    def test_distinct_urls_both_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                event_id, _ = upsert_event(connection, _make_event())
                for i, url in enumerate(
                    ("https://a.example/one", "https://b.example/two")
                ):
                    record_validation(
                        connection, event_id, url=url, title=f"t{i}",
                        publisher="p", stance="corroborating", published_at=None,
                    )
                events = load_events(connection)
                self.assertEqual(len(events[0]["validations"]), 2)

    def test_invalid_stance_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            with open_database(db) as connection:
                event_id, _ = upsert_event(connection, _make_event())
                with self.assertRaises(ValueError):
                    record_validation(
                        connection, event_id, url="https://x.example/z",
                        title="t", publisher="p", stance="meh", published_at=None,
                    )


class NVDAFixtureroundtripTests(unittest.TestCase):
    """The canonical anchor: 4 real posts -> 1 event, breaking, 0 sources."""

    def test_four_posts_one_breaking_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fintick.db"
            # Ingest the four captured posts through the real F1 path.
            ingest_fixture(NVDA_FIXTURE, db)
            with open_database(db) as connection:
                uris = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT uri FROM posts ORDER BY created_at"
                    ).fetchall()
                )
                self.assertEqual(len(uris), 4)
                # Collapse the four into a single event (what F2 aggregate emits).
                upsert_event(
                    connection,
                    _make_event(
                        post_uris=uris,
                        first_seen_at=min(p["record"]["createdAt"] for p in _nvda_posts()),
                        last_seen_at=max(p["record"]["createdAt"] for p in _nvda_posts()),
                    ),
                )
                events = load_events(connection)
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["status"], "breaking")
                self.assertEqual(event["stream_seen"], 4)
                self.assertEqual(event["validations"], [])
                self.assertEqual(event["headline"], "NVIDIA FELL FOR A 7TH DAY, ITS LONGEST LOSING STREAK SINCE 2022")


if __name__ == "__main__":
    unittest.main()
