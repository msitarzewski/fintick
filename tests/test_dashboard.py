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


class ServedBoardFixture(EventBoardFixture):
    """A running board. Carries no tests, so subclasses do not re-run each other's."""

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


class DashboardHttpTests(ServedBoardFixture):
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


class DiscoverabilityTests(ServedBoardFixture):
    """Metadata and crawler routes: a broken one fails silently in production."""

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        with urllib.request.urlopen(self.base_url + path, timeout=2) as response:
            return response.status, dict(response.headers), response.read()

    def test_share_metadata_is_complete_and_absolute(self) -> None:
        head = DASHBOARD_HTML.split("</head>")[0]
        for tag in (
            '<meta name="description"',
            '<link rel="canonical" href="https://fintick.fyi/">',
            '<meta property="og:title"',
            '<meta property="og:description"',
            '<meta property="og:image" content="https://fintick.fyi/og.png">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta property="og:image:alt"',
            '<meta name="twitter:card" content="summary_large_image">',
            '<meta name="twitter:image" content="https://fintick.fyi/og.png">',
            '<link rel="icon" href="/favicon.svg"',
        ):
            self.assertIn(tag, head, f"missing share metadata: {tag}")
        # Relative og:image is the classic silent break — scrapers do not resolve it.
        for prop in ("og:image", "twitter:image", "og:url"):
            value = head.split(f'"{prop}" content="')[1].split('"')[0]
            self.assertTrue(value.startswith("https://"), f"{prop} must be absolute")

    def test_structured_data_parses(self) -> None:
        head = DASHBOARD_HTML.split("</head>")[0]
        block = head.split('<script type="application/ld+json">')[1].split("</script>")[0]
        graph = json.loads(block)["@graph"]
        self.assertEqual({node["@type"] for node in graph}, {"WebSite", "WebApplication"})

    def test_robots_points_at_sitemap_and_shields_api_and_ops(self) -> None:
        status, headers, body = self._get("/robots.txt")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn("Disallow: /api/", text)
        self.assertIn("Disallow: /*?ops", text)
        self.assertIn("Sitemap: https://fintick.fyi/sitemap.xml", text)

    def test_sitemap_is_well_formed_xml(self) -> None:
        import xml.etree.ElementTree as ET

        status, _, body = self._get("/sitemap.xml")
        self.assertEqual(status, 200)
        root = ET.fromstring(body.decode())
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        self.assertEqual(root.tag, f"{namespace}urlset")
        locations = [url.findtext(f"{namespace}loc") for url in root]
        self.assertEqual(locations, ["https://fintick.fyi/"])

    def test_llms_txt_states_the_method_and_its_limits(self) -> None:
        status, _, body = self._get("/llms.txt")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("# FinTick", text)
        for status_name in ("breaking", "unconfirmed", "confirmed", "contradicted"):
            self.assertIn(status_name, text)
        self.assertIn("not investment advice", text)

    def test_assets_are_served_with_types_and_cached(self) -> None:
        for path, content_type in (
            ("/og.png", "image/png"),
            ("/favicon.svg", "image/svg+xml"),
            ("/apple-touch-icon.png", "image/png"),
        ):
            status, headers, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], content_type, path)
            self.assertIn("max-age", headers["Cache-Control"], path)
            self.assertTrue(body, f"{path} served empty")

    def test_head_is_answered_for_scrapers(self) -> None:
        # Social scrapers HEAD an og:image first; the base handler 501s without do_HEAD,
        # which reads to them as a broken image.
        for path in ("/", "/og.png"):
            request = urllib.request.Request(self.base_url + path, method="HEAD")
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200, path)
                self.assertEqual(response.read(), b"", f"{path} HEAD returned a body")
                self.assertTrue(response.headers["Content-Length"], path)


if __name__ == "__main__":
    unittest.main()
