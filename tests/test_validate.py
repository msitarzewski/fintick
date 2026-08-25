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


if __name__ == "__main__":
    unittest.main()
