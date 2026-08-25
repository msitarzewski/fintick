"""Offline tests for external event validation and status transitions."""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from unittest import mock

from fintick.ingest import ingest_fixture
from fintick.storage import (
    V2Event,
    load_events,
    open_database,
    record_validation,
    set_event_status,
    upsert_event,
)
from fintick.validate import (
    ValidationClaim,
    _inferred_stance,
    _lead_seconds,
    _source,
    search_common_vision,
    search_validation_news,
    validate_pending,
)

FIXTURE = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
FIRST_SEEN = "2026-08-24T15:00:11+00:00"
SAMPLE_PARTNER_CREDENTIAL = "partner-token"


class CommonVisionProviderTests(unittest.TestCase):
    def test_search_uses_bearer_token_and_excludes_social_posts(self) -> None:
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return json.dumps({
                    "data": [
                    {
                        "title": "US Consumer Confidence Falls on Jobs, Business Outlook",
                        "url": "https://example.com/consumer-confidence",
                        "published_at": "2026-08-25T14:25:56+00:00",
                        "metadata": {"source": "Example Wire"},
                        "feed": {
                            "name": "Google News — Business",
                            "url": "https://news.google.com/rss/business",
                            "feed_type": "rss",
                        },
                    },
                    {
                        "title": "A social post repeating the same claim",
                        "url": "https://bsky.app/profile/example/post/123",
                        "published_at": "2026-08-25T14:26:00+00:00",
                        "feed": {"name": "Bluesky"},
                    },
                    ],
                    "meta": {
                        "total": 2, "per_page": 5, "current_page": 1,
                        "last_page": 1, "from": 1, "to": 2,
                    },
                    "links": {
                        "first": "https://common.vision/api/v1/articles?page=1",
                        "last": "https://common.vision/api/v1/articles?page=1",
                        "prev": None,
                        "next": None,
                    },
                }).encode()

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response()

        stories = search_common_vision(
            "US consumer confidence index falls to a seven month low in August",
            5,
            token=SAMPLE_PARTNER_CREDENTIAL,
            opener=opener,
        )

        self.assertEqual(stories, [{
            "url": "https://example.com/consumer-confidence",
            "title": "US Consumer Confidence Falls on Jobs, Business Outlook",
            "publisher": "Example Wire",
            "published_at": "2026-08-25T14:25:56+00:00",
            "feed_name": "Google News — Business",
            "feed_url": "https://news.google.com/rss/business",
            "feed_type": "rss",
        }])
        request, timeout = requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer partner-token")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["search"], ["consumer confidence"])
        self.assertEqual(query["per_page"], ["5"])
        self.assertIn("from", query)
        self.assertIn("to", query)
        self.assertEqual(timeout, 20.0)

    def test_rate_limit_uses_documented_json_retry_after(self) -> None:
        attempts = 0
        delays: list[float] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"data":[],"meta":{},"links":{}}'

        def opener(request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    Message(),
                    io.BytesIO(b'{"message":"Slow down","error":"rate_limit_exceeded","retry_after":17}'),
                )
            return Response()

        stories = search_common_vision(
            "consumer confidence",
            token=SAMPLE_PARTNER_CREDENTIAL,
            opener=opener,
            sleep=delays.append,
        )

        self.assertEqual(stories, [])
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [17.0])

    def test_rate_limit_prefers_valid_header_without_reading_body(self) -> None:
        attempts = 0
        delays: list[float] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"data":[],"meta":{},"links":{}}'

        class UnreadableBody(io.BytesIO):
            def read(self, _limit: int | None = -1):
                raise RuntimeError("body must not be read when Retry-After is valid")

        def opener(request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                headers = Message()
                headers["Retry-After"] = "9"
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    headers,
                    UnreadableBody(),
                )
            return Response()

        stories = search_common_vision(
            "consumer confidence",
            token=SAMPLE_PARTNER_CREDENTIAL,
            opener=opener,
            sleep=delays.append,
        )

        self.assertEqual(stories, [])
        self.assertEqual(delays, [9.0])

    def test_search_query_respects_openapi_maximum_length(self) -> None:
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"data":[],"meta":{},"links":{}}'

        def opener(request, timeout):
            requests.append(request)
            return Response()

        search_common_vision(
            "A" * 300,
            token=SAMPLE_PARTNER_CREDENTIAL,
            opener=opener,
        )

        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(requests[0].full_url).query
        )
        self.assertEqual(len(query["search"][0]), 255)

    def test_documented_http_failures_remain_non_confirming_errors(self) -> None:
        for status in (401, 403, 404, 422, 500):
            with self.subTest(status=status):
                def opener(request, timeout, status=status):
                    raise urllib.error.HTTPError(
                        request.full_url,
                        status,
                        "Partner API error",
                        Message(),
                        io.BytesIO(b'{"message":"request failed"}'),
                    )

                with self.assertRaisesRegex(
                    RuntimeError,
                    rf"common\.vision search failed: HTTP {status}",
                ):
                    search_common_vision(
                        "consumer confidence",
                        token=SAMPLE_PARTNER_CREDENTIAL,
                        opener=opener,
                    )

    def test_search_rejects_non_paginated_response_envelope(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"articles":[]}'

        with self.assertRaisesRegex(RuntimeError, "requires a data array"):
            search_common_vision(
                "consumer confidence",
                token=SAMPLE_PARTNER_CREDENTIAL,
                opener=lambda _request, timeout: Response(),
            )

    @mock.patch.dict(os.environ, {"FINTICK_COMMON_VISION_TOKEN": "partner-token"})
    @mock.patch("fintick.validate.search_external_news")
    @mock.patch("fintick.validate.search_common_vision")
    def test_configured_token_selects_common_vision_without_direct_google_query(
        self,
        common_search,
        google_search,
    ) -> None:
        expected = [{"url": "https://example.com/story", "title": "A story"}]
        common_search.return_value = expected

        result = search_validation_news("market claim", 5)

        self.assertEqual(result, expected)
        common_search.assert_called_once_with(
            "market claim", 5, token=SAMPLE_PARTNER_CREDENTIAL
        )
        google_search.assert_not_called()


class ValidatePipelineTests(unittest.TestCase):
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
            upsert_event(connection, V2Event.from_key(
                "NVIDIA falls for a seventh day",
                "NVIDIA extended its longest losing streak since 2022 to seven sessions.",
                primary_instrument="NVDA",
                facts=(
                    {"label": "consecutive down days", "value": 7},
                    {"label": "longest losing streak since", "value": 2022},
                ),
                instruments=({
                    "symbol": "NVDA", "name": "NVIDIA", "type": "equity", "direction": "down",
                },),
                importance=4,
                post_uris=uris,
                first_seen_at=FIRST_SEEN,
                last_seen_at="2026-08-24T15:08:22+00:00",
            ))

    def test_empty_hunt_is_breaking_then_later_source_confirms(self) -> None:
        queries: list[str] = []

        def empty(query: str, limit: int) -> list[dict[str, str]]:
            queries.append(query)
            return []

        first = validate_pending(self.database, lookup=empty, min_age=0)
        with open_database(self.database) as connection:
            breaking = load_events(connection)[0]

        def corroborating(query: str, limit: int) -> list[dict[str, str]]:
            queries.append(query)
            return [{
                "url": "https://example.com/nvidia-seven-day-slide",
                "title": "Nvidia falls for seventh day in longest slide since 2022",
                "publisher": "Example Wire",
                "stance": "corroborating",
                "published_at": "2026-08-24T15:10:11+00:00",
            }]

        second = validate_pending(self.database, lookup=corroborating, min_age=0)
        with open_database(self.database) as connection:
            confirmed = load_events(connection)[0]

        self.assertEqual((first.selected, first.breaking, first.confirmed, first.errored), (1, 1, 0, 0))
        self.assertEqual(breaking["status"], "breaking")
        self.assertEqual(breaking["validation_attempts"], 1)
        self.assertEqual(breaking["validations"], [])
        self.assertEqual((second.selected, second.breaking, second.confirmed), (1, 0, 1))
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["lead_seconds"], 600)
        self.assertEqual(confirmed["validation_attempts"], 2)
        self.assertEqual(confirmed["validations"][0]["publisher"], "Example Wire")
        self.assertEqual(len(queries), 2)
        self.assertIn("NVIDIA", queries[0])
        self.assertIn("seven", queries[0].lower())

    def test_naive_first_seen_timestamp_has_no_lead_time(self) -> None:
        self.assertIsNone(
            _lead_seconds(
                "2026-08-24T15:00:11",
                "2026-08-24T15:10:11+00:00",
            )
        )

    def test_unrelated_search_hit_does_not_confirm_event(self) -> None:
        result = validate_pending(
            self.database,
            lookup=lambda _query, _limit: [{
                "url": "https://example.com/new-gpu",
                "title": "NVIDIA Corporation announces seven new products after shares fall",
                "publisher": "Example Wire",
                "published_at": "2026-08-24T15:10:11+00:00",
            }],
            min_age=0,
        )
        with open_database(self.database) as connection:
            event = load_events(connection)[0]
        self.assertEqual((result.breaking, result.confirmed), (1, 0))
        self.assertEqual(event["status"], "breaking")
        self.assertEqual(event["validations"], [])

    def test_untrusted_explicit_stance_cannot_bypass_claim_matching(self) -> None:
        result = validate_pending(
            self.database,
            lookup=lambda _query, _limit: [{
                "url": "https://example.com/unrelated-labeled-story",
                "title": "NVIDIA Corporation announces seven new products after shares fall",
                "publisher": "Example Wire",
                "stance": "corroborating",
                "published_at": "2026-08-24T15:10:11+00:00",
            }],
            min_age=0,
        )
        with open_database(self.database) as connection:
            event = load_events(connection)[0]

        self.assertEqual((result.breaking, result.confirmed), (1, 0))
        self.assertEqual(event["validations"], [])

    def test_generic_live_blog_overlap_does_not_corroborate_specific_claim(self) -> None:
        claim = ValidationClaim(
            event_id=27,
            query="",
            first_seen_at=FIRST_SEEN,
            previous_status="breaking",
            headline=(
                "US Navy clears mines from Strait of Hormuz; Trump warns of zero "
                "tolerance for new placements"
            ),
            facts_json=(
                '[{"label":"Action","value":"All mines removed and/or detonated '
                'from international waters of the Strait of Hormuz"},'
                '{"label":"Policy","value":"Zero tolerance policy on mine placement"}]'
            ),
            instruments_json="[]",
        )

        stance = _inferred_stance(
            "US removes Syria's designation as a State Sponsor of Terrorism; "
            "al-Sharaa thanks Trump | LIVE BLOG",
            claim,
        )

        self.assertIsNone(stance)

    def test_revalidation_removes_historical_false_confirmation(self) -> None:
        with open_database(self.database) as connection:
            event_id = connection.execute("SELECT id FROM events").fetchone()[0]
            record_validation(
                connection,
                event_id,
                url="https://example.com/historical-false-positive",
                title="Nvidia announces seven new products after shares fall",
                publisher="Example Wire",
                stance="corroborating",
                published_at="2026-08-24T15:10:11+00:00",
            )
            set_event_status(connection, event_id, "confirmed", lead_seconds=600)

        result = validate_pending(
            self.database,
            lookup=lambda _query, _limit: [],
            min_age=0,
        )
        with open_database(self.database) as connection:
            event = load_events(connection)[0]

        self.assertEqual((result.selected, result.breaking), (1, 1))
        self.assertEqual(event["status"], "breaking")
        self.assertEqual(event["validations"], [])

    def test_revalidation_removes_social_post_confirmation(self) -> None:
        with open_database(self.database) as connection:
            event_id = connection.execute("SELECT id FROM events").fetchone()[0]
            record_validation(
                connection,
                event_id,
                url="https://bsky.app/profile/example/post/123",
                title="Nvidia falls for seventh day in longest slide since 2022",
                publisher="Bluesky",
                stance="corroborating",
                published_at="2026-08-24T15:10:11+00:00",
            )
            set_event_status(connection, event_id, "confirmed", lead_seconds=600)

        result = validate_pending(
            self.database,
            lookup=lambda _query, _limit: [],
            min_age=0,
        )
        with open_database(self.database) as connection:
            event = load_events(connection)[0]

        self.assertEqual((result.selected, result.breaking), (1, 1))
        self.assertEqual(event["status"], "breaking")
        self.assertEqual(event["validations"], [])

    def test_source_boundary_rejects_disguised_social_hosts(self) -> None:
        claim = ValidationClaim(
            event_id=27,
            query="",
            first_seen_at=FIRST_SEEN,
            previous_status="breaking",
            headline="Nvidia falls for seventh day in longest slide since 2022",
            facts_json="[]",
            instruments_json="[]",
        )
        for url in (
            "https://bsky.app:443/profile/example/post/123",
            "https://user@bsky.app/profile/example/post/123",
            "https://bsky.app./profile/example/post/123",
            "https://sub.bsky.app:443/profile/example/post/123",
            "https://bsky%2eapp/profile/example/post/123",
            "https://b%73ky.app/profile/example/post/123",
            "https://bsky.app%2e/profile/example/post/123",
            "https://bsky。app/profile/example/post/123",
            "https://bsky．app/profile/example/post/123",
            "https://bsky｡app/profile/example/post/123",
            "https://bsky.app\\@example.com/profile/example/post/123",
            "https://bsky.app%5c@example.com/profile/example/post/123",
        ):
            with self.subTest(url=url):
                self.assertIsNone(_source({
                    "url": url,
                    "title": "Nvidia falls for seventh day in longest slide since 2022",
                    "publisher": "Bluesky",
                }, claim))

    def test_matching_unlabeled_search_hit_confirms_event(self) -> None:
        result = validate_pending(
            self.database,
            lookup=lambda _query, _limit: [{
                "url": "https://example.com/seven-day-slide",
                "title": "Nvidia falls for seventh day in longest slide since 2022",
                "publisher": "Example Wire",
                "published_at": "2026-08-24T15:10:11+00:00",
            }],
            min_age=0,
        )
        with open_database(self.database) as connection:
            event = load_events(connection)[0]
        self.assertEqual((result.breaking, result.confirmed), (0, 1))
        self.assertEqual(event["status"], "confirmed")
        self.assertEqual(len(event["validations"]), 1)

    def test_lead_time_uses_earliest_corroborating_story(self) -> None:
        stories = [
            {
                "url": "https://example.com/malformed-time",
                "title": "Nvidia falls for seventh day in longest slide since 2022",
                "publisher": "Broken Clock Wire",
                "published_at": "not-a-timestamp",
            },
            {
                "url": "https://example.com/later",
                "title": "Nvidia falls for seventh day in longest slide since 2022",
                "publisher": "Later Wire",
                "published_at": "2026-08-24T15:20:11+00:00",
            },
            {
                "url": "https://example.com/earlier",
                "title": "Nvidia falls for seventh day in longest slide since 2022",
                "publisher": "Earlier Wire",
                "published_at": "2026-08-24T15:05:11+00:00",
            },
        ]
        validate_pending(self.database, lookup=lambda _query, _limit: stories, min_age=0)
        with open_database(self.database) as connection:
            event = load_events(connection)[0]
        malformed = next(
            source for source in event["validations"]
            if source["publisher"] == "Broken Clock Wire"
        )
        self.assertIsNone(malformed["published_at"])
        self.assertEqual(event["lead_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
