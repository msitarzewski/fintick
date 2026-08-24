"""Exact normalized-hash deduplication tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fintick.dedup import normalize_text, text_hash
from fintick.ingest import ingest_fixture
from fintick.storage import insert_post, open_database

FIXTURE = Path(__file__).parents[1] / "reference" / "feed_sample.json"


def make_post(uri: str, text: str, created_at: str) -> dict:
    return {
        "uri": uri,
        "cid": f"cid-{uri}",
        "record": {"text": text, "createdAt": created_at, "langs": ["en"]},
        "indexedAt": created_at,
    }


class DedupTests(unittest.TestCase):
    def test_normalization_collapses_case_whitespace_and_trailing_punctuation(self) -> None:
        first = "  BRENT   CRUDE FUTURES SETTLE!  "
        second = "Brent crude futures settle."
        self.assertEqual(normalize_text(first), "brent crude futures settle")
        self.assertEqual(text_hash(first), text_hash(second))

    def test_fixture_has_five_duplicates_linked_to_earliest_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(FIXTURE, database)
            with sqlite3.connect(database) as connection:
                counts = connection.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END),
                           SUM(is_duplicate)
                    FROM posts
                    """
                ).fetchone()
                links = connection.execute(
                    """
                    SELECT duplicate.created_at, canonical.created_at
                    FROM posts AS duplicate
                    JOIN posts AS canonical ON canonical.uri = duplicate.canonical_uri
                    WHERE duplicate.is_duplicate = 1
                    """
                ).fetchall()

        self.assertEqual(counts, (60, 55, 5))
        self.assertEqual(len(links), 5)
        self.assertTrue(all(canonical < duplicate for duplicate, canonical in links))

    def test_older_post_becomes_canonical_even_when_inserted_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            newer = make_post("at://newer", "SAME HEADLINE", "2026-08-24T12:00:30+00:00")
            older = make_post("at://older", "Same headline.", "2026-08-24T12:00:00+00:00")
            with open_database(database) as connection:
                insert_post(connection, newer)
                insert_post(connection, older)
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT uri, is_duplicate, canonical_uri FROM posts ORDER BY created_at"
                ).fetchall()

        self.assertEqual(rows, [("at://older", 0, None), ("at://newer", 1, "at://older")])

    def test_identical_headline_outside_window_is_a_new_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            first = make_post("at://first", "Repeated boilerplate", "2026-08-24T10:00:00+00:00")
            later = make_post("at://later", "REPEATED BOILERPLATE!", "2026-08-24T11:01:00+00:00")
            with open_database(database) as connection:
                insert_post(connection, first)
                insert_post(connection, later)
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT is_duplicate, canonical_uri FROM posts ORDER BY created_at"
                ).fetchall()

        self.assertEqual(rows, [(0, None), (0, None)])

    def test_exact_window_boundary_is_still_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            first = make_post("at://first", "Same", "2026-08-24T10:00:00+00:00")
            boundary = make_post("at://boundary", "Same", "2026-08-24T11:00:00+00:00")
            with open_database(database) as connection:
                insert_post(connection, first)
                insert_post(connection, boundary)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT is_duplicate, canonical_uri FROM posts WHERE uri='at://boundary'"
                ).fetchone()

        self.assertEqual(row, (1, "at://first"))

    def test_new_posts_require_timezone_aware_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            post = make_post("at://naive", "Headline", "2026-08-24T10:00:00")
            with open_database(database) as connection:
                with self.assertRaisesRegex(ValueError, "UTC offset"):
                    insert_post(connection, post)

    def test_overlapping_windows_never_link_duplicate_to_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            posts = [
                make_post("at://first", "Same", "2026-08-24T00:00:00+00:00"),
                make_post("at://middle", "Same", "2026-08-24T00:50:00+00:00"),
                make_post("at://last", "Same", "2026-08-24T01:40:00+00:00"),
            ]
            with open_database(database) as connection:
                for post in reversed(posts):
                    insert_post(connection, post)
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT uri, is_duplicate, canonical_uri FROM posts ORDER BY created_at"
                ).fetchall()

        self.assertEqual(
            rows,
            [
                ("at://first", 0, None),
                ("at://middle", 1, "at://first"),
                ("at://last", 0, None),
            ],
        )

    def test_canonical_order_uses_instants_not_iso_string_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            earlier = make_post("at://earlier", "Same", "2026-08-24T10:30:00+01:00")
            later = make_post("at://later", "Same", "2026-08-24T10:00:00+00:00")
            with open_database(database) as connection:
                insert_post(connection, later)
                insert_post(connection, earlier)
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT uri, is_duplicate, canonical_uri FROM posts ORDER BY uri"
                ).fetchall()

        self.assertEqual(
            rows,
            [("at://earlier", 0, None), ("at://later", 1, "at://earlier")],
        )

    def test_existing_pre_dedup_database_is_backfilled(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE posts (
                        uri TEXT PRIMARY KEY, cid TEXT NOT NULL, text TEXT NOT NULL,
                        created_at TEXT NOT NULL, indexed_at TEXT NOT NULL,
                        langs_json TEXT NOT NULL DEFAULT '[]', embed_type TEXT,
                        raw_json TEXT NOT NULL, inserted_at TEXT NOT NULL
                    );
                    """
                )
                for item in fixture["feed"]:
                    post = item["post"]
                    record = post["record"]
                    connection.execute(
                        "INSERT INTO posts VALUES (?, ?, ?, ?, ?, '[]', NULL, '{}', ?)",
                        (post["uri"], post["cid"], record["text"], record["createdAt"],
                         post["indexedAt"], record["createdAt"]),
                    )
            with open_database(database):
                pass
            with sqlite3.connect(database) as connection:
                result = connection.execute(
                    "SELECT COUNT(*), SUM(is_duplicate) FROM posts WHERE normalized_hash IS NOT NULL"
                ).fetchone()

        self.assertEqual(result, (60, 5))

    def test_malformed_legacy_timestamp_does_not_block_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE posts (
                        uri TEXT PRIMARY KEY, cid TEXT NOT NULL, text TEXT NOT NULL,
                        created_at TEXT NOT NULL, indexed_at TEXT NOT NULL,
                        langs_json TEXT NOT NULL DEFAULT '[]', embed_type TEXT,
                        raw_json TEXT NOT NULL, inserted_at TEXT NOT NULL
                    );
                    INSERT INTO posts VALUES
                        ('at://bad', 'cid', 'Headline', 'not-a-time', 'not-a-time',
                         '[]', NULL, '{}', 'not-a-time');
                    """
                )
            with open_database(database):
                pass
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT normalized_hash IS NOT NULL, is_duplicate, canonical_uri FROM posts"
                ).fetchone()

        self.assertEqual(row, (1, 0, None))


if __name__ == "__main__":
    unittest.main()
