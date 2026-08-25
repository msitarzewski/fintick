"""Deterministic model-benchmark tests for the accountable aggregation contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fintick.benchmark import load_corpus, run_benchmark, score_response


CORPUS = Path(__file__).parents[1] / "reference" / "benchmark_burst_28.json"


class BenchmarkScoringTests(unittest.TestCase):
    def test_runner_sends_fixed_short_id_rows_to_injected_provider(self) -> None:
        prompts: list[list[dict[str, str]]] = []

        def model(prompt: str) -> str:
            rows = json.loads(prompt)
            prompts.append(rows)
            return json.dumps({
                "events": [],
                "ignored_posts": [
                    {"id": row["id"], "reason": "benchmark fixture"}
                    for row in rows
                ],
            })

        score = run_benchmark(CORPUS, call_model=model)

        self.assertEqual(len(prompts), 1)
        self.assertEqual(set(prompts[0][0]), {"id", "created_at", "text"})
        self.assertEqual(score["selected"], 28)
        self.assertEqual(score["accounting_rate"], 1.0)

    def test_perfect_response_scores_complete_accounting_and_clustering(self) -> None:
        corpus = load_corpus(CORPUS)
        events = []
        for index, post_ids in enumerate(corpus["expected_clusters"], 1):
            events.append({
                "canonical_headline": f"Expected event {index}",
                "summary": f"Expected event cluster {index}.",
                "importance": 3,
                "instruments": [],
                "facts": [{"label": "cluster", "value": index}],
                "post_ids": post_ids,
            })
        response = json.dumps({
            "events": events,
            "ignored_posts": [
                {"id": post_id, "reason": "expected non-financial item"}
                for post_id in corpus["expected_ignored"]
            ],
        })

        score = score_response(corpus, response, elapsed_seconds=1.25)

        self.assertEqual(score["selected"], 28)
        self.assertEqual(score["accounting_rate"], 1.0)
        self.assertEqual(score["pair_precision"], 1.0)
        self.assertEqual(score["pair_recall"], 1.0)
        self.assertEqual(score["ignore_precision"], 1.0)
        self.assertEqual(score["ignore_recall"], 1.0)
        self.assertEqual(score["parse_errors"], 0)
        self.assertEqual(score["elapsed_seconds"], 1.25)


if __name__ == "__main__":
    unittest.main()
