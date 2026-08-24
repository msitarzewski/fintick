"""Offline tests for resilient structured enrichment."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from fintick.enrich import (
    _claim_pending,
    _save_error,
    _save_success,
    enrich_pending,
    parse_enrichment,
)
from fintick.ingest import ingest_fixture

FIXTURE = Path(__file__).parents[1] / "reference" / "feed_sample.json"
VALID = {
    "summary": "WTI crude settled at $85.01, down $2.05 or 2.35%.",
    "category": "commodities",
    "importance": 3,
    "sentiment": "bearish",
    "instruments": [{
        "symbol": "CL", "name": "WTI crude oil", "type": "commodity future",
        "venue": "NYMEX", "direction": "down",
    }],
    "entities": ["NYMEX"],
    "regions": ["United States"],
}


class ParseEnrichmentTests(unittest.TestCase):
    def test_parses_and_normalizes_valid_structure(self) -> None:
        result = parse_enrichment(json.dumps(VALID))
        self.assertEqual(result["category"], "commodities")
        self.assertEqual(result["importance"], 3)
        self.assertEqual(result["instruments"][0]["symbol"], "CL")

    def test_strips_thinking_and_handles_wrapped_nested_json(self) -> None:
        content = "<think>private reasoning</think>```json\n" + json.dumps(VALID) + "\n```"
        result = parse_enrichment(content)
        self.assertEqual(result["summary"], VALID["summary"])
        self.assertEqual(result["instruments"][0]["direction"], "down")

    def test_clamps_importance_and_rejects_bad_structure(self) -> None:
        raw = dict(VALID)
        raw["importance"] = 99
        result = parse_enrichment(json.dumps(raw))
        self.assertEqual(result["importance"], 5)

        for broken in (
            {"summary": "Only one field"},
            {**VALID, "category": "made-up"},
            {**VALID, "sentiment": "confused"},
            {**VALID, "instruments": [{"name": "No symbol"}]},
            {**VALID, "entities": "not a list"},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    parse_enrichment(json.dumps(broken))

    def test_rejects_malformed_or_summaryless_output(self) -> None:
        with self.assertRaises(ValueError):
            parse_enrichment("not JSON")
        with self.assertRaises(ValueError):
            parse_enrichment('{"category":"macro"}')


class EnrichmentPipelineTests(unittest.TestCase):
    def _database(self, tmp: str) -> Path:
        database = Path(tmp) / "fintick.db"
        ingest_fixture(FIXTURE, database)
        return database

    def test_only_canonical_pending_rows_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            calls: list[str] = []

            def model(headline: str) -> str:
                calls.append(headline)
                return json.dumps(VALID)

            stats = enrich_pending(database, limit=100, call_model=model)
            with sqlite3.connect(database) as connection:
                stored = connection.execute("SELECT COUNT(*) FROM enrichments").fetchone()[0]
                duplicate_enrichments = connection.execute(
                    """SELECT COUNT(*) FROM enrichments e JOIN posts p ON p.uri=e.uri
                       WHERE p.is_duplicate=1"""
                ).fetchone()[0]
            self.assertEqual(stats.selected, 55)
            self.assertEqual(stats.enriched, 55)
            self.assertEqual(len(calls), 55)
            self.assertEqual(stored, 55)
            self.assertEqual(duplicate_enrichments, 0)

    def test_bad_item_is_recorded_without_blocking_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            responses = iter(["garbage", json.dumps(VALID)])
            stats = enrich_pending(database, limit=2, call_model=lambda _: next(responses))
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT status, attempts, error FROM enrichments ORDER BY enriched_at"
                ).fetchall()
            self.assertEqual(stats.errored, 1)
            self.assertEqual(stats.enriched, 1)
            self.assertEqual([row[0] for row in rows], ["error", "complete"])
            self.assertIn("ValueError", rows[0][2])

    def test_retry_cap_and_successful_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            bad = lambda _: (_ for _ in ()).throw(RuntimeError("model offline"))
            first = enrich_pending(database, limit=1, max_attempts=2, call_model=bad)
            second = enrich_pending(database, limit=1, max_attempts=2, call_model=bad)
            # The failed oldest row is now capped, so the next call selects a different row.
            third = enrich_pending(database, limit=1, max_attempts=2,
                                   call_model=lambda _: json.dumps(VALID))
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT status, attempts FROM enrichments ORDER BY enriched_at"
                ).fetchall()
            self.assertEqual((first.errored, second.errored, third.enriched), (1, 1, 1))
            self.assertIn(("error", 2), rows)
            self.assertIn(("complete", 1), rows)

    def test_completed_rows_are_idempotently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            first = enrich_pending(database, limit=1, call_model=lambda _: json.dumps(VALID))
            # A second run proceeds to the next pending row rather than redoing the first.
            second = enrich_pending(database, limit=1, call_model=lambda _: json.dumps(VALID))
            with sqlite3.connect(database) as connection:
                attempts = connection.execute(
                    "SELECT attempts FROM enrichments ORDER BY enriched_at"
                ).fetchall()
            self.assertEqual((first.enriched, second.enriched), (1, 1))
            self.assertEqual(attempts, [(1,), (1,)])

    def test_concurrent_workers_atomically_claim_distinct_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            barrier = threading.Barrier(2)
            headlines: list[str] = []
            lock = threading.Lock()

            def model(headline: str) -> str:
                with lock:
                    headlines.append(headline)
                barrier.wait(timeout=5)
                return json.dumps(VALID)

            results = []
            workers = [threading.Thread(
                target=lambda: results.append(enrich_pending(database, limit=1, call_model=model))
            ) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(len(results), 2)
            self.assertEqual(len(set(headlines)), 2)
            with sqlite3.connect(database) as connection:
                statuses = connection.execute(
                    "SELECT status, attempts FROM enrichments"
                ).fetchall()
            self.assertEqual(statuses.count(("complete", 1)), 2)

    def test_stale_worker_cannot_overwrite_reclaimed_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(tmp)
            uri, _, old_token = _claim_pending(database, 1, 3)[0]
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE enrichments SET enriched_at='2000-01-01T00:00:00+00:00' WHERE uri=?",
                    (uri,),
                )
            reclaimed_uri, _, new_token = _claim_pending(database, 1, 3)[0]

            self.assertEqual(reclaimed_uri, uri)
            self.assertNotEqual(new_token, old_token)
            self.assertFalse(_save_error(database, uri, old_token, RuntimeError("late")))
            self.assertTrue(_save_success(database, uri, new_token, VALID))
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT status, attempts, summary FROM enrichments WHERE uri=?", (uri,)
                ).fetchone()
            self.assertEqual(row, ("complete", 2, VALID["summary"]))


if __name__ == "__main__":
    unittest.main()
