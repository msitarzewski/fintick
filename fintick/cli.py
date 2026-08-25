"""Command-line entry point for FinTick."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from fintick import __version__
from fintick.aggregate import DEFAULT_BATCH, MAX_POSTS, aggregate_once
from fintick.dashboard import serve_dashboard
from fintick.enrich import enrich_pending
from fintick.ingest import BlueskyFeedClient, ingest_author_feed, ingest_fixture
from fintick.research import research_pending
from fintick.runtime import run_periodically, timestamp
from fintick.validate import validate_pending


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fintick",
        description="Local-first financial intelligence tape",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check the local runtime before starting FinTick",
    )
    ingest = subparsers.add_parser(
        "ingest",
        help="fetch and store the fintwitter author feed",
    )
    ingest.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    ingest.add_argument(
        "--fixture", help="offline AppView JSON fixture (never contacts Bluesky)"
    )
    ingest.add_argument(
        "--max-pages", type=int, default=8, help="maximum live pages per run"
    )
    ingest.add_argument(
        "--actor", default="fintwitter.bsky.social", help="Bluesky handle or DID"
    )
    ingest.add_argument("--watch", action="store_true", help="poll continuously")
    ingest.add_argument(
        "--interval", type=float, default=900, help="watch interval in seconds (default: 900)"
    )
    aggregate = subparsers.add_parser(
        "aggregate", help="drain pending stream posts into distinct accountable events"
    )
    aggregate.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    aggregate.add_argument(
        "--limit", type=int, default=DEFAULT_BATCH, choices=range(1, MAX_POSTS + 1),
        help=f"maximum oldest-pending posts per batch (default: {DEFAULT_BATCH}; cap: {MAX_POSTS})",
    )
    aggregate.add_argument(
        "--model", default=None,
        help="model name override (default: $FINTICK_LLM_MODEL, else qwen3.8:27b)",
    )
    aggregate.add_argument(
        "--base-url", default=None,
        help="OpenAI-compatible endpoint override (default: $FINTICK_LLM_BASE_URL, "
             "else local ollama)",
    )
    aggregate.add_argument(
        "--api-key", default=None,
        help="API key override (default: $FINTICK_LLM_API_KEY)",
    )
    aggregate.add_argument("--watch", action="store_true", help="aggregate continuously")
    aggregate.add_argument(
        "--interval", type=float, default=900, help="watch interval in seconds (default: 900)"
    )
    validate = subparsers.add_parser(
        "validate", help="hunt independent news and update event validation status"
    )
    validate.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    validate.add_argument("--limit", type=int, default=5, help="maximum events per hunt")
    validate.add_argument(
        "--min-age", type=float, default=900,
        help="seconds before rechecking an unconfirmed event (default: 900)",
    )
    validate.add_argument("--watch", action="store_true", help="validate continuously")
    validate.add_argument(
        "--interval", type=float, default=300, help="watch interval in seconds (default: 300)"
    )
    enrich = subparsers.add_parser(
        "enrich", help="RETAINED v1-baseline only (v2 replaces enrich with aggregate): enrich pending canonical posts with local Qwen"
    )
    enrich.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    enrich.add_argument(
        "--limit", type=int, default=10, help="maximum posts to process this run"
    )
    enrich.add_argument(
        "--max-attempts", type=int, default=3, help="retry cap per post"
    )
    enrich.add_argument("--watch", action="store_true", help="process continuously")
    enrich.add_argument(
        "--interval", type=float, default=15, help="watch interval in seconds (default: 15)"
    )
    research = subparsers.add_parser(
        "research", help="RETAINED v1-baseline only (v2 replaces research with validate): find related stories for important enriched posts"
    )
    research.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    research.add_argument(
        "--limit", type=int, default=5, help="maximum posts to research this run"
    )
    research.add_argument(
        "--threshold", type=int, default=3, choices=range(1, 6),
        help="minimum importance score (1-5)",
    )
    research.add_argument(
        "--max-attempts", type=int, default=3, help="retry cap per post"
    )
    research.add_argument("--watch", action="store_true", help="process continuously")
    research.add_argument(
        "--interval", type=float, default=300, help="watch interval in seconds (default: 300)"
    )
    serve = subparsers.add_parser(
        "serve", help="serve the live financial tape dashboard"
    )
    serve.add_argument(
        "--database", default="data/fintick.db", help="SQLite database path"
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="interface to bind (default: loopback)"
    )
    serve.add_argument(
        "--port", type=int, default=8137, help="HTTP port (default: 8137)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print(f"FinTick {__version__}: Python runtime ready")
        return 0

    if args.command == "ingest":
        client = None if args.fixture else BlueskyFeedClient(args.actor)
        def ingest_cycle() -> None:
            if args.fixture:
                stats = ingest_fixture(args.fixture, args.database)
            else:
                assert client is not None
                stats = ingest_author_feed(
                    client.fetch_page, args.database, max_pages=args.max_pages
                )
            print(
                f"{timestamp()} ingest fetched={stats.fetched} new={stats.inserted} "
                f"deduped={stats.deduplicated} pages={stats.pages}", flush=True
            )
        if args.watch:
            run_periodically("ingest", ingest_cycle, args.interval)
        else:
            ingest_cycle()
        return 0

    if args.command == "aggregate":
        def aggregate_cycle() -> None:
            stats = aggregate_once(
                args.database,
                limit=args.limit,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
            )
            print(
                f"{timestamp()} aggregate selected={stats.selected} events={stats.events} "
                f"new={stats.created} ignored={stats.ignored} errored={stats.errored}", flush=True
            )
        if args.watch:
            run_periodically("aggregate", aggregate_cycle, args.interval)
        else:
            aggregate_cycle()
        return 0

    if args.command == "validate":
        def validate_cycle() -> None:
            stats = validate_pending(
                args.database, limit=args.limit, min_age=args.min_age
            )
            print(
                f"{timestamp()} validate selected={stats.selected} "
                f"breaking={stats.breaking} confirmed={stats.confirmed} "
                f"contradicted={stats.contradicted} developing={stats.developing} "
                f"errored={stats.errored}", flush=True
            )
        if args.watch:
            run_periodically("validate", validate_cycle, args.interval)
        else:
            validate_cycle()
        return 0

    if args.command == "enrich":
        def enrich_cycle() -> None:
            stats = enrich_pending(
                args.database,
                # A watch cycle claims one expensive item so a stop signal
                # never has to wait behind an entire model batch.
                limit=1 if args.watch else args.limit,
                max_attempts=args.max_attempts,
            )
            print(
                f"{timestamp()} enrich selected={stats.selected} enriched={stats.enriched} "
                f"errored={stats.errored}", flush=True
            )
        if args.watch:
            run_periodically("enrich", enrich_cycle, args.interval)
        else:
            enrich_cycle()
        return 0

    if args.command == "research":
        def research_cycle() -> None:
            stats = research_pending(
                args.database,
                # Bound graceful-stop latency in continuous mode.
                limit=1 if args.watch else args.limit,
                threshold=args.threshold,
                max_attempts=args.max_attempts,
            )
            print(
                f"{timestamp()} research selected={stats.selected} researched={stats.researched} "
                f"errored={stats.errored}", flush=True
            )
        if args.watch:
            run_periodically("research", research_cycle, args.interval)
        else:
            research_cycle()
        return 0

    if args.command == "serve":
        serve_dashboard(args.database, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 0
