"""Final offline v2 product acceptance."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fintick.aggregate import aggregate_once
from fintick.dashboard import DASHBOARD_HTML, read_feed
from fintick.ingest import ingest_fixture
from fintick.storage import open_database
from fintick.validate import validate_pending

ROOT = Path(__file__).parents[1]
NVDA = ROOT / "reference" / "nvda_repost_cluster.json"


class V2AcceptanceTests(unittest.TestCase):
    def test_nvda_signal_reaches_breaking_event_board_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(NVDA, database)
            with open_database(database) as connection:
                uris = [
                    row[0] for row in connection.execute(
                        "SELECT uri FROM posts ORDER BY created_at"
                    )
                ]
            model_response = json.dumps({"events": [{
                "canonical_headline": "NVIDIA falls for a seventh day",
                "summary": "NVIDIA extended its longest losing streak since 2022 to seven sessions.",
                "importance": 5,
                "instruments": [{
                    "symbol": "$NVDA", "name": "NVIDIA", "type": "equity", "direction": "down",
                }],
                "facts": [
                    {"label": "consecutive down days", "value": 7},
                    {"label": "longest slide since", "value": 2022},
                ],
                "stream_post_uris": uris,
            }]})

            aggregated = aggregate_once(database, call_model=lambda _: model_response)
            validated = validate_pending(database, lookup=lambda _query, _limit: [], min_age=0)
            board = read_feed(database)

        self.assertEqual((aggregated.events, aggregated.created), (1, 1))
        self.assertEqual((validated.breaking, validated.confirmed), (1, 0))
        self.assertEqual(board["count"], 1)
        event = board["items"][0]
        self.assertEqual(event["status"], "breaking")
        self.assertEqual(event["stream_seen"], 4)
        self.assertEqual(event["validations"], [])
        self.assertEqual(event["instruments"][0]["symbol"], "NVDA")
        self.assertEqual(event["facts"][0]["value"], 7)
        self.assertIn("BREAKING — no corroboration yet", DASHBOARD_HTML)
        self.assertIn("via the stream · seen ", DASHBOARD_HTML)

    def test_process_docs_are_v2_and_non_8080(self) -> None:
        supervisor = (ROOT / "setup-fintick-supervisor.sh").read_text()
        readme = (ROOT / "README.md").read_text()
        demo = (ROOT / "run-demo.sh").read_text()

        self.assertIn("fintick-aggregate", supervisor)
        self.assertIn("fintick-validate", supervisor)
        self.assertNotIn("fintick-enrich", supervisor)
        self.assertNotIn("fintick-research", supervisor)
        self.assertNotIn("8080", supervisor)
        self.assertIn("8137", supervisor)
        self.assertIn("python3 -m fintick aggregate --watch", readme)
        self.assertIn("python3 -m fintick validate --watch", readme)
        self.assertIn("http://127.0.0.1:8137", readme)
        self.assertNotIn("python3 -m fintick enrich --watch", readme)
        self.assertNotIn("python3 -m fintick research --watch", readme)
        self.assertIn("PORT=\"${PORT:-8137}\"", demo)
        self.assertIn("python3 -m fintick aggregate", demo)


if __name__ == "__main__":
    unittest.main()
