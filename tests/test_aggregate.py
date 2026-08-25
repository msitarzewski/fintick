"""Offline tests for the v2 rolling-window event aggregator."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fintick.aggregate import _load_window, aggregate_once, parse_aggregation
from fintick.ingest import ingest_fixture
from fintick.storage import insert_post, load_events, open_database


URIS = (
    "at://stream/post/1",
    "at://stream/post/2",
    "at://stream/post/3",
    "at://stream/post/4",
)


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "canonical_headline": "NVIDIA falls for a seventh day",
        "summary": "NVIDIA extended its longest losing streak since 2022 to seven sessions.",
        "instruments": [
            {
                "symbol": "$nvda",
                "name": "NVIDIA Corporation",
                "type": "Equity",
                "direction": "DOWN",
            }
        ],
        "stream_post_uris": list(URIS),
        "importance": 4,
    }
    event.update(overrides)
    return event


class ParseAggregationTests(unittest.TestCase):
    def test_parses_and_normalizes_one_event(self) -> None:
        parsed = parse_aggregation(
            json.dumps({"events": [_event()]}),
            allowed_uris=set(URIS),
            post_times={uri: f"2026-08-24T15:0{index}:00+00:00" for index, uri in enumerate(URIS)},
        )

        self.assertEqual(parsed.errored, 0)
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        self.assertEqual(event.post_uris, URIS)
        self.assertEqual(event.instruments, ({
            "symbol": "NVDA",
            "name": "NVIDIA Corporation",
            "type": "equity",
            "direction": "down",
        },))
        self.assertEqual(event.first_seen_at, "2026-08-24T15:00:00+00:00")
        self.assertEqual(event.last_seen_at, "2026-08-24T15:03:00+00:00")

    def test_rejects_event_that_reuses_an_already_claimed_post(self) -> None:
        second = _event(
            canonical_headline="A separate claimed event",
            stream_post_uris=[URIS[0]],
        )
        parsed = parse_aggregation(
            json.dumps({"events": [_event(), second]}),
            allowed_uris=set(URIS),
            post_times={uri: f"2026-08-24T15:0{index}:00+00:00" for index, uri in enumerate(URIS)},
        )

        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.errored, 1)


class AggregatePipelineTests(unittest.TestCase):
    def test_nvda_fixture_becomes_one_event_in_one_model_call(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            with sqlite3.connect(database) as connection:
                fixture_uris = tuple(
                    row[0] for row in connection.execute(
                        "SELECT uri FROM posts ORDER BY created_at"
                    )
                )
            response = json.dumps({"events": [_event(stream_post_uris=list(fixture_uris))]})
            prompts: list[str] = []

            def model(prompt: str) -> str:
                prompts.append(prompt)
                return response

            first = aggregate_once(database, call_model=model)
            second = aggregate_once(database, call_model=model)
            with open_database(database) as connection:
                events = load_events(connection)

        self.assertEqual(len(prompts), 2)
        self.assertEqual((first.selected, first.events, first.created, first.errored), (4, 1, 1, 0))
        self.assertEqual((second.events, second.created), (1, 0))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stream_seen"], 4)
        self.assertEqual(events[0]["instruments"][0]["symbol"], "NVDA")
        prompt_rows = json.loads(prompts[0])
        self.assertEqual(len(prompt_rows), 4)
        self.assertEqual(set(prompt_rows[0]), {"uri", "created_at", "text"})

    def test_bad_model_response_isolated_without_crashing(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            stats = aggregate_once(database, call_model=lambda _: "not json")
            with open_database(database) as connection:
                events = load_events(connection)

        self.assertEqual((stats.selected, stats.events, stats.created, stats.errored), (4, 0, 0, 1))
        self.assertEqual(events, [])

    def test_bad_event_does_not_block_valid_sibling(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            with sqlite3.connect(database) as connection:
                fixture_uris = [
                    row[0] for row in connection.execute(
                        "SELECT uri FROM posts ORDER BY created_at"
                    )
                ]
            response = json.dumps({"events": [
                {"canonical_headline": "missing required fields"},
                _event(stream_post_uris=fixture_uris),
            ]})
            stats = aggregate_once(database, call_model=lambda _: response)

        self.assertEqual((stats.events, stats.created, stats.errored), (1, 1, 1))

    def test_empty_database_skips_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            called = False

            def model(_: str) -> str:
                nonlocal called
                called = True
                return "{}"

            stats = aggregate_once(Path(tmp) / "empty.db", call_model=model)

        self.assertEqual(stats.selected, 0)
        self.assertFalse(called)

    def test_window_is_six_hours_capped_at_two_hundred_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "window.db"
            with open_database(database) as connection:
                for index in range(202):
                    minute = index % 60
                    hour = 12 + index // 60
                    created_at = f"2026-08-24T{hour:02d}:{minute:02d}:00+00:00"
                    insert_post(connection, {
                        "uri": f"at://stream/recent/{index:03d}",
                        "cid": f"cid-{index}",
                        "record": {"text": f"unique post {index}", "createdAt": created_at},
                    })
                insert_post(connection, {
                    "uri": "at://stream/too-old",
                    "cid": "cid-old",
                    "record": {
                        "text": "old post",
                        "createdAt": "2026-08-24T08:00:00+00:00",
                    },
                })

            rows = _load_window(database, 200)

        self.assertEqual(len(rows), 200)
        self.assertNotIn("at://stream/too-old", {row["uri"] for row in rows})
        self.assertEqual(rows, sorted(rows, key=lambda row: (row["created_at"], row["uri"])))
        self.assertEqual(rows[-1]["uri"], "at://stream/recent/201")


if __name__ == "__main__":
    unittest.main()
