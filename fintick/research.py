"""Bounded, cached related-story research using a free news RSS search."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fintick.storage import open_database

NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"


@dataclass(frozen=True, slots=True)
class ResearchStats:
    selected: int = 0
    researched: int = 0
    errored: int = 0


def parse_news_rss(payload: bytes, *, limit: int = 2) -> list[dict[str, str]]:
    """Parse a news RSS response into a small, URL-deduplicated story list."""
    if limit <= 0:
        return []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ValueError(f"news search returned malformed XML: {error}") from error

    stories: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if not title or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if url in seen:
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].rstrip()
        source = source or parsed.netloc.removeprefix("www.")
        stories.append({"title": title, "url": url, "source": source})
        seen.add(url)
        if len(stories) >= limit:
            break
    return stories


def search_news_rss(
    query: str,
    limit: int = 2,
    *,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[dict[str, str]]:
    """Search Google News RSS without authentication or a paid data API."""
    params = urllib.parse.urlencode({
        "q": query,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })
    request = urllib.request.Request(
        f"{NEWS_RSS_ENDPOINT}?{params}",
        headers={"User-Agent": "FinTick/0.1 (personal financial news reader)"},
    )
    payload = b""
    for attempt in range(2):
        try:
            with opener(request, timeout=timeout) as response:
                payload = response.read(2_000_000)
            break
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt == 0:
                try:
                    delay = float(error.headers.get("Retry-After", "5"))
                except (TypeError, ValueError):
                    delay = 5.0
                sleep(min(60.0, max(1.0, delay)))
                continue
            raise RuntimeError(f"news search failed: HTTP {error.code}") from error
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"news search failed: {error}") from error
    return parse_news_rss(payload, limit=limit)


def _build_query(headline: str, summary: str | None, entities_json: str) -> str:
    """Build a focused query from locally generated and source text."""
    try:
        entities = json.loads(entities_json)
    except (TypeError, json.JSONDecodeError):
        entities = []
    if not isinstance(entities, list):
        entities = []
    entity_terms = " ".join(
        item.strip() for item in entities[:3] if isinstance(item, str) and item.strip()
    )
    base = (summary or headline).strip()
    # Keep query URLs bounded while preserving the market-moving claim and key entities.
    return " ".join(f"{base} {entity_terms}".split())[:400]


def _claim_pending(
    database: str | Path, limit: int, threshold: int, max_attempts: int
) -> list[tuple[str, str, str]]:
    with open_database(database):
        pass
    connection = sqlite3.connect(database, isolation_level=None, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        rows = connection.execute(
            """
            SELECT p.uri, p.text, e.summary, e.entities_json
            FROM posts AS p
            JOIN enrichments AS e ON e.uri=p.uri
            LEFT JOIN research AS r ON r.uri=p.uri
            WHERE p.is_duplicate=0 AND e.status='complete' AND e.importance>=?
              AND (r.uri IS NULL
                   OR (r.status='error' AND r.attempts<?)
                   OR (r.status='processing' AND r.attempts<? AND r.researched_at<?))
            ORDER BY e.importance DESC, p.created_at ASC, p.uri ASC LIMIT ?
            """,
            (threshold, max_attempts, max_attempts, cutoff, limit),
        ).fetchall()
        claims: list[tuple[str, str, str]] = []
        now = datetime.now(UTC).isoformat()
        for uri, headline, summary, entities_json in rows:
            query = _build_query(headline, summary, entities_json)
            token = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO research (uri, status, attempts, lease_token, query, error, researched_at)
                VALUES (?, 'processing', 1, ?, ?, NULL, ?)
                ON CONFLICT(uri) DO UPDATE SET status='processing',
                    attempts=research.attempts+1, lease_token=excluded.lease_token,
                    query=excluded.query, error=NULL, researched_at=excluded.researched_at
                """,
                (uri, token, query, now),
            )
            claims.append((uri, query, token))
        connection.commit()
        return claims
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _valid_links(raw: Any, limit: int = 2) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("news lookup must return a list")
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        source = item.get("source")
        if not isinstance(url, str):
            continue
        parsed = urllib.parse.urlparse(url)
        if not isinstance(title, str) or not title.strip():
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
            continue
        clean_source = source.strip() if isinstance(source, str) else ""
        links.append({
            "title": title.strip(),
            "url": url,
            "source": clean_source or parsed.netloc.removeprefix("www."),
        })
        seen.add(url)
        if len(links) >= limit:
            break
    return links


def _save_result(
    database: str | Path,
    uri: str,
    token: str,
    *,
    links: list[dict[str, str]] | None = None,
    error: BaseException | None = None,
) -> bool:
    connection = sqlite3.connect(database, timeout=30)
    try:
        now = datetime.now(UTC).isoformat()
        if error is None:
            cursor = connection.execute(
                """
                UPDATE research SET status='complete', links_json=?, lease_token=NULL,
                    error=NULL, researched_at=?
                WHERE uri=? AND status='processing' AND lease_token=?
                """,
                (json.dumps(links or [], separators=(",", ":")), now, uri, token),
            )
        else:
            message = f"{type(error).__name__}: {error}"[:1000]
            cursor = connection.execute(
                """
                UPDATE research SET status='error', lease_token=NULL, error=?, researched_at=?
                WHERE uri=? AND status='processing' AND lease_token=?
                """,
                (message, now, uri, token),
            )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def research_pending(
    database: str | Path,
    *,
    limit: int = 5,
    threshold: int = 3,
    max_attempts: int = 3,
    lookup: Callable[[str, int], list[dict[str, str]]] = search_news_rss,
) -> ResearchStats:
    """Research eligible enriched items once, isolating lookup failures per item."""
    if limit < 1 or max_attempts < 1 or threshold not in range(1, 6):
        raise ValueError("limit/max_attempts must be positive and threshold must be 1-5")
    claims = _claim_pending(database, limit, threshold, max_attempts)
    researched = errored = 0
    for uri, query, token in claims:
        try:
            links = _valid_links(lookup(query, 2))
            researched += _save_result(database, uri, token, links=links)
        except Exception as error:
            errored += _save_result(database, uri, token, error=error)
    return ResearchStats(selected=len(claims), researched=researched, errored=errored)
