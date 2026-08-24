"""Command-line entry point for FinTick."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from fintick import __version__
from fintick.enrich import enrich_pending
from fintick.ingest import BlueskyFeedClient, ingest_author_feed, ingest_fixture


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
    enrich = subparsers.add_parser(
        "enrich", help="enrich pending canonical posts with local Qwen"
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print(f"FinTick {__version__}: Python runtime ready")
        return 0

    if args.command == "ingest":
        if args.fixture:
            stats = ingest_fixture(args.fixture, args.database)
        else:
            client = BlueskyFeedClient(args.actor)
            stats = ingest_author_feed(
                client.fetch_page, args.database, max_pages=args.max_pages
            )
        print(
            f"ingest fetched={stats.fetched} new={stats.inserted} "
            f"deduped={stats.deduplicated} pages={stats.pages}"
        )
        return 0

    if args.command == "enrich":
        stats = enrich_pending(
            args.database, limit=args.limit, max_attempts=args.max_attempts
        )
        print(
            f"enrich selected={stats.selected} enriched={stats.enriched} "
            f"errored={stats.errored}"
        )
        return 0

    parser.print_help()
    return 0
