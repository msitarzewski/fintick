"""Tests for the self-contained FinTick dashboard."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fintick.dashboard import DASHBOARD_HTML, DashboardServer, read_feed
from fintick.ingest import ingest_fixture


FIXTURE = Path(__file__).parents[1] / "reference" / "feed_sample.json"


class DashboardFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "fintick.db"
        ingest_fixture(FIXTURE, self.database)

    def _connection(self):
        import sqlite3
        return sqlite3.connect(self.database)

    def test_feed_contains_only_latest_canonical_posts(self) -> None:
        payload = read_feed(self.database, limit=100)

        self.assertEqual(payload["count"], 55)
        self.assertEqual(len(payload["items"]), 55)
        timestamps = [item["created_at"] for item in payload["items"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        self.assertTrue(all(item["enrichment_status"] == "pending" for item in payload["items"]))

    def test_feed_combines_complete_enrichment_and_research(self) -> None:
        uri = read_feed(self.database, limit=1)["items"][0]["uri"]
        instruments = [{
            "symbol": "NVDA", "name": "NVIDIA", "type": "equity",
            "venue": "NASDAQ", "direction": "up",
        }]
        links = [
            {"title": "NVIDIA advances", "url": "https://example.test/nvda", "source": "Example"},
            {"title": "Unsafe legacy link", "url": "javascript:alert(1)", "source": "Bad"},
        ]
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO enrichments (
                    uri,status,attempts,summary,category,importance,sentiment,
                    instruments_json,entities_json,regions_json,enriched_at
                ) VALUES (?, 'complete', 1, ?, 'equities', 4, 'bullish', ?, ?, ?, ?)
                """,
                (uri, "NVIDIA shares gained after strong results.", json.dumps(instruments),
                 '["NVIDIA"]', '["United States"]', "2026-08-24T20:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO research (uri,status,attempts,query,links_json,researched_at)
                VALUES (?, 'complete', 1, 'NVIDIA results', ?, ?)
                """,
                (uri, json.dumps(links), "2026-08-24T20:01:00+00:00"),
            )

        item = read_feed(self.database, limit=1)["items"][0]
        self.assertEqual(item["summary"], "NVIDIA shares gained after strong results.")
        self.assertEqual(item["category"], "equities")
        self.assertEqual(item["importance"], 4)
        self.assertEqual(item["instruments"], instruments)
        self.assertEqual(item["related"], links[:1])

    def test_feed_orders_timezone_offsets_by_actual_instant(self) -> None:
        first, second = read_feed(self.database, limit=2)["items"]
        with self._connection() as connection:
            # Lexically 10:00 sorts first, but it is 08:00Z and therefore older.
            connection.execute(
                "UPDATE posts SET created_at='2030-01-01T10:00:00+02:00' WHERE uri=?",
                (first["uri"],),
            )
            connection.execute(
                "UPDATE posts SET created_at='2030-01-01T09:00:00+00:00' WHERE uri=?",
                (second["uri"],),
            )

        self.assertEqual(read_feed(self.database, limit=1)["items"][0]["uri"], second["uri"])

    def test_incomplete_or_malformed_enrichment_degrades_to_raw(self) -> None:
        uri = read_feed(self.database, limit=1)["items"][0]["uri"]
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO enrichments (
                    uri,status,attempts,summary,category,importance,sentiment,
                    instruments_json,entities_json,regions_json,error,enriched_at
                ) VALUES (?, 'error', 1, 'stale', 'macro', 3, 'neutral',
                          'not-json', 'not-json', 'not-json', 'bad output', ?)
                """,
                (uri, "2026-08-24T20:00:00+00:00"),
            )

        item = read_feed(self.database, limit=1)["items"][0]
        self.assertEqual(item["enrichment_status"], "error")
        self.assertIsNone(item["summary"])
        self.assertEqual(item["instruments"], [])
        self.assertEqual(item["related"], [])

    def test_limit_validation_and_cap(self) -> None:
        with self.assertRaises(ValueError):
            read_feed(self.database, limit=0)
        self.assertEqual(read_feed(self.database, limit=10_000)["count"], 55)


class DashboardHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "fintick.db"
        ingest_fixture(FIXTURE, self.database)
        self.server = DashboardServer(("127.0.0.1", 0), self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_dashboard_is_self_contained_and_auto_refreshing(self) -> None:
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode()
            headers = response.headers

        self.assertEqual(response.status, 200)
        self.assertIn("FinTick_", body)
        self.assertIn('class="tape"', body)
        self.assertIn('class="feed"', body)
        self.assertIn("setInterval(refresh, 20000)", body)
        self.assertIn("document.createTextNode", body)
        self.assertNotIn("<script src=", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_json_endpoint_returns_canonical_feed(self) -> None:
        request = urllib.request.Request(self.base_url + "/api/feed?limit=3")
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_concurrent_feed_reads_do_not_contend_on_schema_migrations(self) -> None:
        def fetch(_: int) -> int:
            with urllib.request.urlopen(self.base_url + "/api/feed?limit=1", timeout=5) as response:
                return json.load(response)["count"]

        with ThreadPoolExecutor(max_workers=20) as pool:
            counts = list(pool.map(fetch, range(40)))
        self.assertEqual(counts, [1] * 40)

    def test_bad_limit_and_unknown_route_return_json_errors(self) -> None:
        for route, status in (("/api/feed?limit=nope", 400), ("/missing", 404)):
            with self.subTest(route=route):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(self.base_url + route, timeout=2)
                self.assertEqual(caught.exception.code, status)
                self.assertIn("error", json.load(caught.exception))

    def test_html_declares_reduced_motion_and_accessibility_status(self) -> None:
        self.assertIn("prefers-reduced-motion", DASHBOARD_HTML)
        self.assertIn('aria-live="polite"', DASHBOARD_HTML)
        self.assertIn('role="status"', DASHBOARD_HTML)
        self.assertIn('href="#feed"', DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
