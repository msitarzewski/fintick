"""Offline ingest and durable storage tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fintick.ingest import build_feed_url, ingest_author_feed, ingest_fixture


FIXTURE = Path(__file__).parents[1] / "reference" / "feed_sample.json"


class OfflineIngestTests(unittest.TestCase):
    def test_feed_url_encodes_actor_filter_limit_and_cursor(self) -> None:
        url = build_feed_url("fintwitter.bsky.social", "opaque cursor/+", limit=100)
        self.assertEqual(
            url,
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?"
            "actor=fintwitter.bsky.social&limit=100&filter=posts_no_replies&"
            "cursor=opaque+cursor%2F%2B",
        )

    def test_fixture_populates_database_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"

            first = ingest_fixture(FIXTURE, database)
            second = ingest_fixture(FIXTURE, database)

            self.assertEqual(first.fetched, 60)
            self.assertEqual(first.inserted, 60)
            self.assertEqual(first.deduplicated, 5)
            self.assertEqual(second.fetched, 60)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.deduplicated, 0)
            with sqlite3.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
                row = connection.execute(
                    "SELECT text, created_at, raw_json FROM posts ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(count, 60)
            self.assertEqual(row[0], "The Treasury announces the quantum-readiness task force.")
            self.assertTrue(row[1].startswith("2026-08-24T19:05:19"))
            self.assertEqual(json.loads(row[2])["uri"].split("/")[2], "did:plc:43fdk46qa5gsokzygzildsaq")

    def test_pagination_stops_on_an_entirely_known_page(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        newest = {"feed": fixture["feed"][:2], "cursor": "older-page"}
        known = {"feed": fixture["feed"][2:4], "cursor": "should-not-be-fetched"}
        calls: list[str | None] = []

        def fetch(cursor: str | None) -> dict:
            calls.append(cursor)
            return newest if cursor is None else known

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(FIXTURE, database)
            stats = ingest_author_feed(fetch, database, max_pages=8)

        self.assertEqual(calls, [None])
        self.assertEqual(stats.pages, 1)
        self.assertEqual(stats.fetched, 2)
        self.assertEqual(stats.inserted, 0)

    def test_pagination_follows_cursor_until_known_posts(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        page_one = {"feed": fixture["feed"][:2], "cursor": "page-two"}
        page_two = {"feed": fixture["feed"][2:4], "cursor": "page-three"}
        calls: list[str | None] = []

        def fetch(cursor: str | None) -> dict:
            calls.append(cursor)
            return page_one if cursor is None else page_two

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            stats = ingest_author_feed(fetch, database, max_pages=2)

            with sqlite3.connect(database) as connection:
                newest_uri = connection.execute(
                    "SELECT value FROM ingest_state WHERE key='newest_uri'"
                ).fetchone()[0]

        self.assertEqual(calls, [None, "page-two"])
        self.assertEqual(stats.pages, 2)
        self.assertEqual(stats.fetched, 4)
        self.assertEqual(stats.inserted, 4)
        self.assertEqual(newest_uri, page_one["feed"][0]["post"]["uri"])


if __name__ == "__main__":
    unittest.main()
