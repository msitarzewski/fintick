"""Fetch and persist Bluesky author-feed posts."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fintick.storage import insert_post, open_database, set_state


APPVIEW_ENDPOINT = (
    "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
)
DEFAULT_ACTOR = "fintwitter.bsky.social"


def build_feed_url(actor: str, cursor: str | None = None, *, limit: int = 100) -> str:
    """Build a public AppView author-feed request URL."""
    params: dict[str, str | int] = {
        "actor": actor,
        "limit": limit,
        "filter": "posts_no_replies",
    }
    if cursor:
        params["cursor"] = cursor
    return f"{APPVIEW_ENDPOINT}?{urllib.parse.urlencode(params)}"


class BlueskyFeedClient:
    """Small unauthenticated client for Bluesky's public AppView."""

    def __init__(self, actor: str = DEFAULT_ACTOR, *, timeout: float = 20.0) -> None:
        self.actor = actor
        self.timeout = timeout

    def fetch_page(self, cursor: str | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            build_feed_url(self.actor, cursor),
            headers={"User-Agent": "FinTick/0.1 (+local financial tape)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Bluesky feed request failed: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError("Bluesky feed response was not a JSON object")
        return data


@dataclass(frozen=True, slots=True)
class IngestStats:
    fetched: int = 0
    inserted: int = 0
    deduplicated: int = 0
    pages: int = 0


def ingest_page(
    data: dict[str, Any],
    database: str | Path,
    *,
    update_high_water: bool = True,
) -> IngestStats:
    """Persist one AppView author-feed response."""
    feed = data.get("feed")
    if not isinstance(feed, list):
        raise ValueError("feed response must contain a feed list")

    inserted = 0
    deduplicated = 0
    with open_database(database) as connection:
        for item in feed:
            if not isinstance(item, dict) or not isinstance(item.get("post"), dict):
                continue
            result = insert_post(connection, item["post"])
            inserted += result.inserted
            deduplicated += result.deduplicated
        if feed and update_high_water:
            newest = feed[0].get("post", {})
            if newest.get("uri"):
                set_state(connection, "newest_uri", newest["uri"])
            created_at = newest.get("record", {}).get("createdAt")
            if created_at:
                set_state(connection, "newest_created_at", created_at)

    return IngestStats(
        fetched=len(feed),
        inserted=inserted,
        deduplicated=deduplicated,
        pages=1,
    )


def ingest_author_feed(
    fetch_page: Callable[[str | None], dict[str, Any]],
    database: str | Path,
    *,
    max_pages: int = 8,
) -> IngestStats:
    """Walk feed pages until caught up, exhausted, or capped."""
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    fetched = inserted = deduplicated = pages = 0
    cursor: str | None = None
    for _ in range(max_pages):
        data = fetch_page(cursor)
        page = ingest_page(data, database, update_high_water=pages == 0)
        fetched += page.fetched
        inserted += page.inserted
        deduplicated += page.deduplicated
        pages += 1

        # A completely known page is the durable high-water mark: all older
        # pages were traversed by a prior run, so there is nothing more to do.
        if page.fetched == 0 or page.inserted == 0:
            break
        next_cursor = data.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor

    return IngestStats(
        fetched=fetched,
        inserted=inserted,
        deduplicated=deduplicated,
        pages=pages,
    )


def ingest_fixture(fixture: str | Path, database: str | Path) -> IngestStats:
    """Load a captured AppView response without touching the network."""
    try:
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read feed fixture {fixture}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("feed fixture must contain a JSON object")
    return ingest_page(data, database)
