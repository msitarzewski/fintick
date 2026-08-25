"""Offline tests for external event validation and status transitions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fintick.ingest import ingest_fixture
from fintick.storage import V2Event, load_events, open_database, upsert_event
from fintick.validate import validate_pending

FIXTURE = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
FIRST_SEEN = "2026-08-24T15:00:11+00:00"


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
