"""Fixed-corpus model benchmarks for FinTick aggregation providers."""

from __future__ import annotations

import argparse
import itertools
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fintick.aggregate import (
    DEFAULT_MODEL,
    call_inference,
    parse_accounted_aggregation,
)


def load_corpus(path: str | Path) -> dict[str, Any]:
    """Load and validate an immutable short-ID benchmark corpus."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("posts"), list):
        raise ValueError("benchmark corpus requires a posts array")
    posts = value["posts"]
    ids: list[str] = []
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get("id"), str):
            raise ValueError("every benchmark post requires a string id")
        ids.append(post["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("benchmark post ids must be unique")
    for post in posts:
        if not isinstance(post.get("created_at"), str) or not isinstance(post.get("text"), str):
            raise ValueError("every benchmark post requires created_at and text")
    clusters = value.get("expected_clusters", [])
    ignored = value.get("expected_ignored", [])
    if not isinstance(clusters, list) or not isinstance(ignored, list):
        raise ValueError("benchmark expectations must be arrays")
    expected_ids = [post_id for cluster in clusters for post_id in cluster] + list(ignored)
    if sorted(expected_ids) != sorted(ids):
        raise ValueError("benchmark expectations must account for every post exactly once")
    return value


def _pairs(clusters: list[list[str]]) -> set[tuple[str, str]]:
    return {
        (min(first, second), max(first, second))
        for cluster in clusters
        for first, second in itertools.combinations(cluster, 2)
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def score_response(
    corpus: dict[str, Any],
    response: str,
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Score one raw model response through FinTick's production parser."""
    source_posts = corpus["posts"]
    posts = {
        post["id"]: {
            "uri": f"benchmark://{post['id']}",
            "created_at": post["created_at"],
            "text": post["text"],
        }
        for post in source_posts
    }
    uri_to_id = {post["uri"]: post_id for post_id, post in posts.items()}
    parsed = parse_accounted_aggregation(response, posts=posts)
    predicted_clusters = [
        [uri_to_id[uri] for uri in event.post_uris]
        for event in parsed.events
    ]
    predicted_ignored = {uri_to_id[uri] for uri, _ in parsed.ignored}
    assigned = {post_id for cluster in predicted_clusters for post_id in cluster}
    expected_pairs = _pairs(corpus["expected_clusters"])
    predicted_pairs = _pairs(predicted_clusters)
    pair_matches = len(expected_pairs & predicted_pairs)
    expected_ignored = set(corpus["expected_ignored"])
    ignored_matches = len(expected_ignored & predicted_ignored)
    selected = len(source_posts)
    return {
        "corpus": corpus.get("name"),
        "selected": selected,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "events": len(parsed.events),
        "ignored": len(parsed.ignored),
        "parse_errors": parsed.errored,
        "accounting_rate": _ratio(len(assigned | predicted_ignored), selected),
        "pair_precision": _ratio(pair_matches, len(predicted_pairs)),
        "pair_recall": _ratio(pair_matches, len(expected_pairs)),
        "ignore_precision": _ratio(ignored_matches, len(predicted_ignored)),
        "ignore_recall": _ratio(ignored_matches, len(expected_ignored)),
        "fact_count": sum(len(event.facts) for event in parsed.events),
        "instrument_count": sum(len(event.instruments) for event in parsed.events),
        "headlines": [event.headline for event in parsed.events],
    }


def run_benchmark(
    corpus_path: str | Path,
    *,
    call_model: Callable[[str], str],
) -> dict[str, Any]:
    """Run one provider against a fixed corpus and return deterministic metrics."""
    corpus = load_corpus(corpus_path)
    prompt = json.dumps(
        corpus["posts"], ensure_ascii=False, separators=(",", ":")
    )
    started = time.monotonic()
    response = call_model(prompt)
    elapsed = time.monotonic() - started
    return score_response(corpus, response, elapsed_seconds=elapsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fintick-benchmark",
        description="Benchmark one aggregation model on a frozen FinTick corpus",
    )
    parser.add_argument(
        "--corpus",
        default="reference/benchmark_burst_28.json",
        help="fixed benchmark corpus JSON",
    )
    parser.add_argument("--model", default=None, help="model name override")
    parser.add_argument(
        "--base-url", default=None,
        help="OpenAI-compatible endpoint override (default: local ollama)",
    )
    parser.add_argument("--api-key", default=None, help="API key override")
    args = parser.parse_args(argv)
    model = args.model or DEFAULT_MODEL
    call_model = lambda prompt: call_inference(
        prompt, base_url=args.base_url, api_key=args.api_key, model=model
    )
    score = run_benchmark(args.corpus, call_model=call_model)
    score["model"] = model
    print(json.dumps(score, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
