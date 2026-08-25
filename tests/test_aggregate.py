"""Offline tests for the v2 rolling-window event aggregator."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from fintick.aggregate import _load_window, aggregate_once, call_local_model, parse_aggregation
from fintick.ingest import ingest_fixture
from fintick.storage import V2Event, insert_post, load_events, open_database, upsert_event


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
        "facts": [
            {"label": "consecutive down days", "value": "7"},
            {"label": "longest losing streak since", "value": "2022"},
        ],
        "stream_post_uris": list(URIS),
        "importance": 4,
    }
    event.update(overrides)
    return event


class LocalModelRequestTests(unittest.TestCase):
    @mock.patch("fintick.aggregate.urllib.request.urlopen")
    def test_disables_reasoning_and_bounds_json_output(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = BytesIO(json.dumps({
            "message": {"content": "{\"events\":[]}"}
        }).encode())

        content = call_local_model("[]")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)

        self.assertEqual(content, '{"events":[]}')
        self.assertIs(payload["think"], False)
        self.assertLessEqual(payload["options"]["num_predict"], 4096)


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
        self.assertEqual(event.facts, (
            {"label": "consecutive down days", "value": "7"},
            {"label": "longest losing streak since", "value": "2022"},
        ))
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

    def test_rejects_factless_event(self) -> None:
        invalid_facts = ([], [{"label": "move", "value": "   "}], [{"label": "move", "value": float("nan")}])
        for facts in invalid_facts:
            with self.subTest(facts=facts):
                parsed = parse_aggregation(
                    json.dumps({"events": [_event(facts=facts)]}),
                    allowed_uris=set(URIS),
                    post_times={
                        uri: f"2026-08-24T15:0{index}:00+00:00"
                        for index, uri in enumerate(URIS)
                    },
                )
                self.assertEqual(parsed.events, ())
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

    def test_headline_drift_does_not_duplicate_event_or_signal_links(self) -> None:
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
            first_response = json.dumps({"events": [
                _event(stream_post_uris=fixture_uris)
            ]})
            second_response = json.dumps({"events": [
                _event(
                    canonical_headline="Nvidia losing streak reaches seven sessions",
                    stream_post_uris=fixture_uris,
                )
            ]})
            first = aggregate_once(database, call_model=lambda _: first_response)
            second = aggregate_once(database, call_model=lambda _: second_response)
            with open_database(database) as connection:
                event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                signal_count = connection.execute("SELECT COUNT(*) FROM event_signals").fetchone()[0]
                distinct_signal_count = connection.execute(
                    "SELECT COUNT(DISTINCT post_uri) FROM event_signals"
                ).fetchone()[0]

        self.assertEqual((first.created, second.created), (1, 0))
        self.assertEqual(event_count, 1)
        self.assertEqual((signal_count, distinct_signal_count), (4, 4))

    def test_ambiguous_persistence_is_isolated_as_event_error(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            with open_database(database) as connection:
                rows = connection.execute(
                    "SELECT uri, created_at FROM posts ORDER BY created_at"
                ).fetchall()
                fixture_uris = [row[0] for row in rows]
                for headline, symbol, selected in (
                    ("EVENT A", "NVDA", rows[:2]),
                    ("EVENT B", "AMD", rows[2:]),
                ):
                    upsert_event(connection, V2Event.from_key(
                        headline,
                        headline,
                        primary_instrument=symbol,
                        facts=({"label": "signals", "value": len(selected)},),
                        post_uris=tuple(row[0] for row in selected),
                        first_seen_at=selected[0][1],
                        last_seen_at=selected[-1][1],
                    ))
            response = json.dumps({"events": [
                _event(
                    canonical_headline="MODEL COMBINED A AND B",
                    stream_post_uris=fixture_uris,
                )
            ]})
            stats = aggregate_once(database, call_model=lambda _: response)
            with open_database(database) as connection:
                event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                duplicate_signals = connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT post_uri FROM event_signals GROUP BY post_uri HAVING COUNT(*) > 1)"
                ).fetchone()[0]

        self.assertEqual((stats.events, stats.created, stats.errored), (0, 0, 1))
        self.assertEqual((event_count, duplicate_signals), (2, 0))

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
