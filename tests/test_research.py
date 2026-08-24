"""Tests for bounded, cached related-story research."""

from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path

from fintick.ingest import ingest_fixture
from fintick.research import parse_news_rss, research_pending, search_news_rss
from fintick.storage import open_database

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "reference" / "feed_sample.json"


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "fintick.db"
        ingest_fixture(FIXTURE, self.database)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def add_enrichment(self, uri: str, importance: int, *, summary: str = "Market headline") -> None:
        with open_database(self.database) as connection:
            connection.execute(
                """
                INSERT INTO enrichments (
                    uri, status, attempts, summary, category, importance, sentiment,
                    instruments_json, entities_json, regions_json, enriched_at
                ) VALUES (?, 'complete', 1, ?, 'macro', ?, 'neutral', '[]', '[]', '[]',
                          '2026-08-24T12:00:00+00:00')
                """,
                (uri, summary, importance),
            )

    def canonical_uris(self, count: int = 2) -> list[str]:
        with open_database(self.database) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT uri FROM posts WHERE is_duplicate=0 ORDER BY created_at LIMIT ?",
                    (count,),
                )
            ]

    def test_researches_only_eligible_items_and_caches_links(self) -> None:
        high_uri, low_uri = self.canonical_uris()
        self.add_enrichment(high_uri, 4, summary="Nvidia raises its forecast")
        self.add_enrichment(low_uri, 2)
        calls: list[str] = []

        def lookup(query: str, limit: int) -> list[dict[str, str]]:
            calls.append(query)
            return [{"title": "Nvidia outlook rises", "url": "https://example.com/nvda", "source": "Example News"}]

        first = research_pending(self.database, limit=10, lookup=lookup)
        second = research_pending(self.database, limit=10, lookup=lookup)

        self.assertEqual((first.selected, first.researched, first.errored), (1, 1, 0))
        self.assertEqual(second.selected, 0)
        self.assertEqual(len(calls), 1)
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT status, links_json FROM research WHERE uri=?", (high_uri,)
            ).fetchone()
            low_count = connection.execute(
                "SELECT COUNT(*) FROM research WHERE uri=?", (low_uri,)
            ).fetchone()[0]
        self.assertEqual(row[0], "complete")
        self.assertEqual(json.loads(row[1])[0]["url"], "https://example.com/nvda")
        self.assertEqual(low_count, 0)

    def test_one_lookup_failure_does_not_block_later_items(self) -> None:
        first_uri, second_uri = self.canonical_uris()
        self.add_enrichment(first_uri, 3, summary="First headline")
        self.add_enrichment(second_uri, 5, summary="Second headline")

        def lookup(query: str, limit: int) -> list[dict[str, str]]:
            if "First" in query:
                raise OSError("offline")
            return [{"title": "Related", "url": "https://example.com/related", "source": "Wire"}]

        stats = research_pending(self.database, limit=10, lookup=lookup)

        self.assertEqual((stats.selected, stats.researched, stats.errored), (2, 1, 1))
        with sqlite3.connect(self.database) as connection:
            statuses = dict(connection.execute("SELECT uri, status FROM research"))
        self.assertEqual(statuses[first_uri], "error")
        self.assertEqual(statuses[second_uri], "complete")

    def test_malformed_entities_do_not_abort_research_batch(self) -> None:
        first_uri, second_uri = self.canonical_uris()
        self.add_enrichment(first_uri, 4, summary="First headline")
        self.add_enrichment(second_uri, 4, summary="Second headline")
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE enrichments SET entities_json='{}' WHERE uri=?", (first_uri,)
            )

        stats = research_pending(
            self.database,
            limit=10,
            lookup=lambda query, limit: [
                {"title": query, "url": "https://example.com/story", "source": "Wire"}
            ],
        )

        self.assertEqual((stats.selected, stats.researched, stats.errored), (2, 2, 0))

    def test_parse_news_rss_returns_bounded_deduplicated_stories(self) -> None:
        rss = b"""<?xml version='1.0'?><rss><channel>
          <item><title>Oil rises - Reuters</title><link>https://news.example/a</link><source>Reuters</source></item>
          <item><title>Oil rises - Reuters</title><link>https://news.example/a</link><source>Reuters</source></item>
          <item><title>OPEC meets</title><link>https://news.example/b</link></item>
        </channel></rss>"""

        stories = parse_news_rss(rss, limit=2)

        self.assertEqual(stories, [
            {"title": "Oil rises", "url": "https://news.example/a", "source": "Reuters"},
            {"title": "OPEC meets", "url": "https://news.example/b", "source": "news.example"},
        ])

    def test_parse_news_rss_returns_nothing_for_zero_limit(self) -> None:
        rss = b"<rss><channel><item><title>A</title><link>https://example.com/a</link></item></channel></rss>"

        self.assertEqual(parse_news_rss(rss, limit=0), [])

    def test_news_search_backs_off_once_after_rate_limit(self) -> None:
        rss = b"<rss><channel><item><title>A</title><link>https://example.com/a</link></item></channel></rss>"
        attempts = 0
        sleeps: list[float] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, limit: int) -> bytes:
                return rss

        def opener(request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "rate limited", {"Retry-After": "2"}, io.BytesIO()
                )
            return Response()

        stories = search_news_rss("oil", opener=opener, sleep=sleeps.append)

        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(stories[0]["url"], "https://example.com/a")


if __name__ == "__main__":
    unittest.main()
