"""Tests for the self-contained v2 FinTick event board."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fintick.dashboard import BREAKING_TTL_SECONDS, DASHBOARD_HTML, DashboardServer, read_feed
from fintick.ingest import ingest_fixture
from fintick.service_handoff import snapshot_database
from fintick.storage import (
    V2Event,
    open_database,
    record_validation,
    set_event_status,
    upsert_event,
)

FIXTURE = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"


class EventBoardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "fintick.db"
        ingest_fixture(FIXTURE, self.database)
        with open_database(self.database) as connection:
            uris = tuple(
                row[0] for row in connection.execute(
                    "SELECT uri FROM posts ORDER BY created_at"
                )
            )
            self.event_id, _ = upsert_event(connection, V2Event.from_key(
                "NVIDIA falls for a seventh day",
                "NVIDIA extended its longest losing streak since 2022 to seven sessions.",
                primary_instrument="NVDA",
                facts=(
                    {"label": "consecutive down days", "value": 7},
                    {"label": "longest slide since", "value": 2022},
                ),
                instruments=({
                    "symbol": "NVDA", "name": "NVIDIA", "type": "equity", "direction": "down",
                },),
                importance=4,
                post_uris=uris,
                # Fresh, so it is genuinely breaking (past the TTL it reads as unconfirmed).
                first_seen_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
                last_seen_at=datetime.now(UTC).isoformat(),
            ))


class DashboardFeedTests(EventBoardFixture):
    def test_feed_returns_events_not_raw_posts(self) -> None:
        payload = read_feed(self.database)

        self.assertEqual(payload["count"], 1)
        event = payload["items"][0]
        self.assertEqual(event["headline"], "NVIDIA falls for a seventh day")
        self.assertEqual(event["status"], "breaking")
        self.assertEqual(event["stream_seen"], 4)
        self.assertEqual(event["facts"][0], {"label": "consecutive down days", "value": 7})
        self.assertEqual(event["instruments"][0]["symbol"], "NVDA")
        self.assertNotIn("uri", event)
        self.assertNotIn("enrichment_status", event)

    def test_feed_exposes_post_accounting_backlog(self) -> None:
        pipeline = read_feed(self.database)["pipeline"]

        self.assertEqual(pipeline["posts"], 4)
        self.assertEqual(pipeline["accounted"], 0)
        self.assertEqual(pipeline["backlog"], 4)
        self.assertEqual(pipeline["pending"], 4)
        self.assertEqual(pipeline["terminal_errors"], 0)
        self.assertEqual(pipeline["oldest_pending_at"], "2026-08-24T15:00:11.000000+00:00")

    def test_feed_identifies_exact_database_not_same_cardinality_copy(self) -> None:
        copied_database = Path(self.tmp.name) / "copied.db"
        snapshot_database(self.database, copied_database)

        operational = read_feed(self.database)["pipeline"]
        copied = read_feed(copied_database)["pipeline"]

        self.assertEqual(operational["posts"], copied["posts"])
        self.assertNotEqual(
            operational["database_identity"],
            copied["database_identity"],
        )

    def test_breaking_events_sort_ahead_of_newer_confirmed_events(self) -> None:
        with open_database(self.database) as connection:
            confirmed_id, _ = upsert_event(connection, V2Event.from_key(
                "A newer confirmed event",
                "A later event already covered by the wire.",
                primary_instrument="SPX",
                instruments=({
                    "symbol": "SPX", "name": "S&P 500", "type": "index", "direction": "up",
                },),
                importance=5,
                first_seen_at="2026-08-24T16:00:00+00:00",
                last_seen_at="2026-08-24T16:00:00+00:00",
            ))
            record_validation(
                connection,
                confirmed_id,
                url="https://example.test/confirmed",
                title="Wire confirms later event",
                publisher="Example Wire",
                stance="corroborating",
                published_at="2026-08-24T16:05:00+00:00",
                feed_name="Google News — Business",
                feed_url="https://news.google.com/rss/business",
                feed_type="rss",
            )
            set_event_status(connection, confirmed_id, "confirmed", lead_seconds=300)

        items = read_feed(self.database)["items"]

        self.assertEqual([item["status"] for item in items], ["breaking", "confirmed"])
        self.assertEqual(items[1]["validations"][0]["publisher"], "Example Wire")
        self.assertEqual(items[1]["validations"][0]["feed_name"], "Google News — Business")
        self.assertEqual(
            items[1]["validations"][0]["feed_url"],
            "https://news.google.com/rss/business",
        )
        self.assertEqual(items[1]["validations"][0]["feed_type"], "rss")
        self.assertEqual(items[1]["lead_seconds"], 300)

    def test_stale_breaking_event_reads_as_unconfirmed(self) -> None:
        # A breaking event the wire never caught up on ages into 'unconfirmed'
        # purely with time — no re-validation, no stored-status change.
        stale_at = (
            datetime.now(UTC) - timedelta(seconds=BREAKING_TTL_SECONDS + 120)
        ).isoformat()
        with open_database(self.database) as connection:
            connection.execute(
                "UPDATE events SET first_seen_at = ?, status = 'breaking' WHERE id = ?",
                (stale_at, self.event_id),
            )

        event = read_feed(self.database)["items"][0]

        self.assertEqual(event["status"], "unconfirmed")
        with open_database(self.database) as connection:
            stored = connection.execute(
                "SELECT status FROM events WHERE id = ?", (self.event_id,)
            ).fetchone()[0]
        self.assertEqual(stored, "breaking")  # derived for display only

    def test_limit_validation_and_cap(self) -> None:
        with self.assertRaises(ValueError):
            read_feed(self.database, limit=0)
        self.assertEqual(read_feed(self.database, limit=10_000)["count"], 1)


class DashboardHttpTests(EventBoardFixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = DashboardServer(("127.0.0.1", 0), self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_dashboard_is_v2_event_board_and_auto_refreshing(self) -> None:
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode()
            headers = response.headers

        self.assertEqual(response.status, 200)
        self.assertIn("FinTick_", body)
        self.assertIn("The Edge Board", body)
        self.assertIn("BREAKING — no corroboration yet", body)
        self.assertIn("via the stream · seen ", body)
        self.assertIn("setInterval(refresh, 20000)", body)
        self.assertIn("document.createTextNode", body)
        self.assertNotIn("<script src=", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_json_endpoint_returns_event_feed(self) -> None:
        with urllib.request.urlopen(self.base_url + "/api/feed?limit=1", timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["stream_seen"], 4)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_concurrent_feed_reads_are_safe(self) -> None:
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

    def test_html_has_reduced_motion_and_accessibility_status(self) -> None:
        self.assertIn("prefers-reduced-motion", DASHBOARD_HTML)
        self.assertIn('aria-live="polite"', DASHBOARD_HTML)
        self.assertIn('role="status"', DASHBOARD_HTML)
        self.assertIn('aria-label="Pipeline accounting"', DASHBOARD_HTML)
        self.assertIn("CAUGHT UP", DASHBOARD_HTML)
        self.assertIn("CATCHING UP", DASHBOARD_HTML)
        self.assertIn("TERMINAL ERRORS", DASHBOARD_HTML)
        self.assertIn('href="#feed"', DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
