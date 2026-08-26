"""Offline tests for the v2 rolling-window event aggregator."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from fintick.aggregate import (
    _load_pending,
    _load_window,
    aggregate_once,
    call_inference,
    parse_accounted_aggregation,
    parse_aggregation,
)
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


class InferenceCallTests(unittest.TestCase):
    @mock.patch("fintick.aggregate.urllib.request.urlopen")
    def test_posts_openai_compatible_forced_json_request(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = BytesIO(json.dumps({
            "choices": [{"message": {"content": "{\"events\":[]}"}}]
        }).encode())

        content = call_inference(
            "[]", base_url="https://llm.test/v1", api_key="k-123", model="m-1"
        )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)

        self.assertEqual(content, '{"events":[]}')
        self.assertEqual(request.full_url, "https://llm.test/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer k-123")
        self.assertEqual(payload["model"], "m-1")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_completion_tokens"], 16384)

    @mock.patch("fintick.aggregate.urllib.request.urlopen")
    def test_empty_content_raises(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = BytesIO(json.dumps({
            "choices": [{"message": {"content": "   "}}]
        }).encode())
        with self.assertRaises(RuntimeError):
            call_inference("[]")

    @mock.patch("fintick.aggregate.urllib.request.urlopen")
    def test_usage_sink_receives_token_counts(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = BytesIO(json.dumps({
            "choices": [{"message": {"content": "{\"events\":[]}"}}],
            "usage": {"prompt_tokens": 1735, "completion_tokens": 3497},
        }).encode())
        captured: list[dict[str, object]] = []
        call_inference("[]", model="gpt-5.6-luna", usage_sink=captured.append)
        self.assertEqual(captured, [
            {"model": "gpt-5.6-luna", "prompt_tokens": 1735, "completion_tokens": 3497},
        ])

    @mock.patch("fintick.aggregate.urllib.request.urlopen")
    def test_usage_recorded_even_when_content_is_empty(self, urlopen: mock.Mock) -> None:
        # Empty output still burned tokens — the cost must be captured before raising.
        urlopen.return_value = BytesIO(json.dumps({
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 1700, "completion_tokens": 16384},
        }).encode())
        captured: list[dict[str, object]] = []
        with self.assertRaises(RuntimeError):
            call_inference("[]", model="gpt-5.6-luna", usage_sink=captured.append)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["completion_tokens"], 16384)


class InferenceCostTests(unittest.TestCase):
    def test_prices_cloud_model_and_frees_local(self) -> None:
        from fintick.aggregate import inference_cost_usd
        self.assertAlmostEqual(
            inference_cost_usd("gpt-5.6-luna", 1_000_000, 1_000_000), 0.20 + 1.20
        )
        self.assertEqual(inference_cost_usd("qwen3.8:27b", 5000, 4000), 0.0)
        self.assertEqual(inference_cost_usd(None, 5000, 4000), 0.0)

    def test_usage_windows_sum_per_model(self) -> None:
        import tempfile
        from fintick.storage import (
            open_database, record_inference_usage, load_inference_usage,
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.db"
            with open_database(db) as connection:
                record_inference_usage(connection, "gpt-5.6-luna", 1735, 3497)
                record_inference_usage(connection, "gpt-5.6-luna", 1700, 5000)
            with open_database(db) as connection:
                windows = load_inference_usage(connection)
            self.assertEqual(set(windows), {"hour", "day", "week", "month"})
            hour = windows["hour"]
            self.assertEqual(len(hour), 1)
            self.assertEqual(hour[0]["calls"], 2)
            self.assertEqual(hour[0]["prompt_tokens"], 3435)
            self.assertEqual(hour[0]["completion_tokens"], 8497)


class AccountedAggregationTests(unittest.TestCase):
    def test_short_ids_map_to_uris_and_every_post_gets_a_decision(self) -> None:
        posts = {
            f"p{index:03d}": {
                "uri": uri,
                "created_at": f"2026-08-24T15:0{index}:00+00:00",
                "text": f"post {index}",
            }
            for index, uri in enumerate(URIS, 1)
        }
        event = _event()
        event.pop("stream_post_uris")
        event["post_ids"] = ["p001", "p002"]
        parsed = parse_accounted_aggregation(
            json.dumps({
                "events": [event],
                "ignored_posts": [{"id": "p003", "reason": "non-event fragment"}],
            }),
            posts=posts,
        )

        self.assertEqual(parsed.events[0].post_uris, URIS[:2])
        self.assertEqual(parsed.ignored, ((URIS[2], "non-event fragment"),))
        self.assertEqual(parsed.errored_uris, (URIS[3],))
        self.assertEqual(parsed.errored, 1)

    def test_duplicate_short_id_inside_event_is_rejected(self) -> None:
        posts = {
            f"p{index:03d}": {
                "uri": uri,
                "created_at": f"2026-08-24T15:0{index}:00+00:00",
                "text": f"post {index}",
            }
            for index, uri in enumerate(URIS, 1)
        }
        event = _event()
        event.pop("stream_post_uris")
        event["post_ids"] = ["p001", "p001"]
        parsed = parse_accounted_aggregation(
            json.dumps({
                "events": [event],
                "ignored_posts": [
                    {"id": post_id, "reason": "fixture"}
                    for post_id in ("p002", "p003", "p004")
                ],
            }),
            posts=posts,
        )

        self.assertEqual(parsed.events, ())
        self.assertIn(URIS[0], parsed.errored_uris)
        self.assertGreaterEqual(parsed.errored, 1)

    def test_rejected_event_counts_affected_posts_not_parser_steps(self) -> None:
        posts = {
            "p001": {
                "uri": "at://stream/short/1",
                "created_at": "2026-08-24T15:00:11+00:00",
                "text": "Futures extend gains",
            },
            "p002": {
                "uri": "at://stream/short/2",
                "created_at": "2026-08-24T15:00:12+00:00",
                "text": "Unrelated rhetoric",
            },
        }
        response = json.dumps({
            "events": [{
                "canonical_headline": "Futures extend gains",
                "summary": "Futures moved higher.",
                "importance": 2,
                "instruments": [],
                "facts": [],
                "post_ids": ["p001"],
            }],
            "ignored_posts": [{"id": "p002", "reason": "non-financial rhetoric"}],
        })

        parsed = parse_accounted_aggregation(response, posts=posts)

        self.assertEqual(parsed.errored_uris, ("at://stream/short/1",))
        self.assertEqual(parsed.errored, 1)


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

    def test_event_span_orders_mixed_offsets_by_utc_instant(self) -> None:
        earlier = "at://stream/span/earlier"
        later = "at://stream/span/later"
        result = parse_aggregation(
            json.dumps({"events": [_event(
                stream_post_uris=[later, earlier],
            )]}),
            allowed_uris={earlier, later},
            post_times={
                earlier: "2026-08-24T14:30:00+00:00",
                later: "2026-08-24T10:00:00-05:00",
            },
        )

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].first_seen_at, "2026-08-24T14:30:00+00:00")
        self.assertEqual(result.events[0].last_seen_at, "2026-08-24T10:00:00-05:00")

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

    def test_accepts_instrument_without_a_ticker_symbol(self) -> None:
        # Indices, sovereign bonds, FX, and commodities have no ticker; the model
        # identifies them by name + type and leaves symbol "" (or omits it). Both
        # forms must parse — this is the exact class of event that was erroring out.
        for symbol in ("", None):
            with self.subTest(symbol=symbol):
                instrument = {
                    "name": "Brent crude",
                    "type": "commodity",
                    "direction": "down",
                }
                if symbol is not None:
                    instrument["symbol"] = symbol
                parsed = parse_aggregation(
                    json.dumps({"events": [_event(
                        canonical_headline="Brent crude falls below $88 per barrel",
                        instruments=[instrument],
                    )]}),
                    allowed_uris=set(URIS),
                    post_times={
                        uri: f"2026-08-24T15:0{index}:00+00:00"
                        for index, uri in enumerate(URIS)
                    },
                )
                self.assertEqual(parsed.errored, 0)
                self.assertEqual(len(parsed.events), 1)
                self.assertEqual(parsed.events[0].instruments[0]["symbol"], "")
                self.assertEqual(parsed.events[0].instruments[0]["name"], "Brent crude")

    def test_untickered_event_keys_on_instrument_name_not_empty(self) -> None:
        # Two distinct untickered events must not collide on an empty instrument key.
        brent = parse_aggregation(
            json.dumps({"events": [_event(
                canonical_headline="Brent crude falls",
                instruments=[{"name": "Brent crude", "type": "commodity", "direction": "down"}],
            )]}),
            allowed_uris=set(URIS),
            post_times={uri: f"2026-08-24T15:0{i}:00+00:00" for i, uri in enumerate(URIS)},
        )
        yuan = parse_aggregation(
            json.dumps({"events": [_event(
                canonical_headline="PBOC sets yuan midpoint",
                instruments=[{"name": "Chinese yuan", "type": "fx", "direction": "flat"}],
            )]}),
            allowed_uris=set(URIS),
            post_times={uri: f"2026-08-24T15:0{i}:00+00:00" for i, uri in enumerate(URIS)},
        )
        self.assertNotEqual(brent.events[0].key, yuan.events[0].key)


class AggregatePipelineTests(unittest.TestCase):
    def test_accounted_batch_persists_assignments_and_ignores_then_does_not_repeat(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            prompts: list[list[dict[str, str]]] = []

            def model(prompt: str) -> str:
                rows = json.loads(prompt)
                prompts.append(rows)
                event = _event()
                event.pop("stream_post_uris")
                event["post_ids"] = [rows[0]["id"], rows[1]["id"]]
                return json.dumps({
                    "events": [event],
                    "ignored_posts": [
                        {"id": rows[2]["id"], "reason": "duplicate fragment"},
                        {"id": rows[3]["id"], "reason": "non-event fragment"},
                    ],
                })

            first = aggregate_once(database, call_model=model)
            second = aggregate_once(database, call_model=model)
            with open_database(database) as connection:
                decisions = connection.execute(
                    "SELECT state, COUNT(*) FROM post_aggregation_decisions "
                    "GROUP BY state ORDER BY state"
                ).fetchall()

        self.assertEqual(len(prompts), 1)
        self.assertEqual(set(prompts[0][0]), {"id", "created_at", "text"})
        self.assertNotIn("uri", prompts[0][0])
        self.assertEqual((first.events, first.created, first.ignored, first.errored), (1, 1, 2, 0))
        self.assertEqual(second.selected, 0)
        self.assertEqual(decisions, [("assigned", 2), ("ignored", 2)])

    def test_transient_batch_failure_retries_errored_posts_until_accounted(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            calls = 0

            def model(prompt: str) -> str:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary provider failure")
                rows = json.loads(prompt)
                return json.dumps({
                    "events": [],
                    "ignored_posts": [
                        {"id": row["id"], "reason": "fixture retry"} for row in rows
                    ],
                })

            first = aggregate_once(database, call_model=model)
            second = aggregate_once(database, call_model=model)
            third = aggregate_once(database, call_model=model)
            with open_database(database) as connection:
                decisions = connection.execute(
                    "SELECT state, attempts, COUNT(*) FROM post_aggregation_decisions "
                    "GROUP BY state, attempts"
                ).fetchall()

        self.assertEqual(first.errored, 4)
        self.assertEqual((second.selected, second.ignored), (4, 4))
        self.assertEqual(third.selected, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(decisions, [("ignored", 2, 4)])

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

        self.assertEqual(len(prompts), 1)
        self.assertEqual((first.selected, first.events, first.created, first.errored), (4, 1, 1, 0))
        self.assertEqual((second.selected, second.events, second.created), (0, 0, 0))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stream_seen"], 4)
        self.assertEqual(events[0]["instruments"][0]["symbol"], "NVDA")
        prompt_rows = json.loads(prompts[0])
        self.assertEqual(len(prompt_rows), 4)
        self.assertEqual(set(prompt_rows[0]), {"id", "created_at", "text"})
        self.assertNotIn("uri", prompt_rows[0])

    def test_legacy_response_assigns_event_posts_and_errors_omissions(self) -> None:
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
            response = json.dumps({
                "events": [_event(stream_post_uris=list(fixture_uris[:2]))]
            })

            stats = aggregate_once(database, call_model=lambda _: response)
            with open_database(database) as connection:
                decisions = connection.execute(
                    "SELECT post_uri, state FROM post_aggregation_decisions ORDER BY post_uri"
                ).fetchall()

        self.assertEqual((stats.selected, stats.events, stats.errored), (4, 1, 2))
        self.assertEqual(
            dict(decisions),
            {
                fixture_uris[0]: "assigned",
                fixture_uris[1]: "assigned",
                fixture_uris[2]: "errored",
                fixture_uris[3]: "errored",
            },
        )

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

        self.assertEqual((stats.events, stats.created, stats.errored), (0, 0, 4))
        self.assertEqual((event_count, duplicate_signals), (2, 0))

    def test_bad_model_response_isolated_without_crashing(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "nvda_repost_cluster.json"
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "fintick.db"
            ingest_fixture(fixture, database)
            stats = aggregate_once(database, call_model=lambda _: "not json")
            with open_database(database) as connection:
                events = load_events(connection)

        self.assertEqual((stats.selected, stats.events, stats.created, stats.errored), (4, 0, 0, 4))
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

    def test_pending_selection_is_oldest_first_and_skips_decided_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "pending.db"
            with open_database(database) as connection:
                for index in range(6):
                    insert_post(connection, {
                        "uri": f"at://stream/pending/{index}",
                        "cid": f"cid-{index}",
                        "record": {
                            "text": f"post {index}",
                            "createdAt": f"2026-08-24T15:0{index}:00+00:00",
                        },
                    })
                connection.execute(
                    "UPDATE post_aggregation_decisions SET state='ignored', reason='fixture' "
                    "WHERE post_uri IN (?, ?)",
                    ("at://stream/pending/0", "at://stream/pending/1"),
                )

            rows = _load_pending(database, 3)

        self.assertEqual(
            [row["uri"] for row in rows],
            [
                "at://stream/pending/2",
                "at://stream/pending/3",
                "at://stream/pending/4",
            ],
        )

    def test_pending_selection_orders_mixed_offsets_by_utc_instant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "mixed-offset-pending.db"
            with open_database(database) as connection:
                insert_post(connection, {
                    "uri": "at://stream/later-local-clock",
                    "cid": "cid-later-local-clock",
                    "record": {
                        "text": "later instant with earlier wall clock",
                        "createdAt": "2026-08-24T10:00:00-05:00",
                    },
                })
                insert_post(connection, {
                    "uri": "at://stream/earlier-utc",
                    "cid": "cid-earlier-utc",
                    "record": {
                        "text": "earlier instant",
                        "createdAt": "2026-08-24T14:30:00+00:00",
                    },
                })

            rows = _load_pending(database, 1)

        self.assertEqual([row["uri"] for row in rows], ["at://stream/earlier-utc"])

    def test_retryable_errors_are_isolated_from_fresh_pending_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "retry.db"
            with open_database(database) as connection:
                for index in range(6):
                    insert_post(connection, {
                        "uri": f"at://stream/retry/{index}",
                        "cid": f"cid-retry-{index}",
                        "record": {
                            "text": f"retry post {index}",
                            "createdAt": f"2026-08-24T15:0{index}:00+00:00",
                        },
                    })
                connection.execute(
                    "UPDATE post_aggregation_decisions "
                    "SET state='errored', attempts=1, reason='fixture', retry_group='group-a' "
                    "WHERE post_uri IN (?, ?)",
                    ("at://stream/retry/0", "at://stream/retry/1"),
                )
                connection.execute(
                    "UPDATE post_aggregation_decisions "
                    "SET state='errored', attempts=1, reason='fixture', retry_group='group-b' "
                    "WHERE post_uri IN (?, ?)",
                    ("at://stream/retry/2", "at://stream/retry/3"),
                )

            rows = _load_pending(database, 5)

        self.assertEqual(
            [row["uri"] for row in rows],
            ["at://stream/retry/0", "at://stream/retry/1"],
        )

    def test_retry_group_is_not_split_by_fresh_batch_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "retry-group.db"
            with open_database(database) as connection:
                for index in range(4):
                    insert_post(connection, {
                        "uri": f"at://stream/group/{index}",
                        "cid": f"cid-group-{index}",
                        "record": {
                            "text": f"group post {index}",
                            "createdAt": f"2026-08-24T15:0{index}:00+00:00",
                        },
                    })
                connection.execute(
                    "UPDATE post_aggregation_decisions "
                    "SET state='errored', attempts=1, reason='fixture', "
                    "retry_group='group-complete'"
                )

            rows = _load_pending(database, 2)

        self.assertEqual(
            [row["uri"] for row in rows],
            [
                "at://stream/group/0",
                "at://stream/group/1",
                "at://stream/group/2",
                "at://stream/group/3",
            ],
        )


if __name__ == "__main__":
    unittest.main()
