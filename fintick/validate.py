"""Hunt independent news and assign event validation status."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from fintick.storage import open_database, record_validation, set_event_status

NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"
STANCES = {"corroborating", "disputing", "partial"}


@dataclass(frozen=True, slots=True)
class ValidateStats:
    selected: int = 0
    breaking: int = 0
    confirmed: int = 0
    contradicted: int = 0
    developing: int = 0
    errored: int = 0


def parse_validation_rss(payload: bytes, *, limit: int = 5) -> list[dict[str, str]]:
    """Parse bounded Google News RSS results as candidate corroborating stories."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ValueError(f"news search returned malformed XML: {error}") from error
    stories: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        publisher = (item.findtext("source") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if not title or parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
            continue
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -(len(publisher) + 3)].rstrip()
        published_at: str | None = None
        pub_date = (item.findtext("pubDate") or "").strip()
        if pub_date:
            try:
                published_at = parsedate_to_datetime(pub_date).astimezone(UTC).isoformat()
            except (TypeError, ValueError):
                pass
        story = {
            "url": url,
            "title": title,
            "publisher": publisher or parsed.netloc.removeprefix("www."),
            "stance": "corroborating",
        }
        if published_at:
            story["published_at"] = published_at
        stories.append(story)
        seen.add(url)
        if len(stories) >= limit:
            break
    return stories


def search_external_news(
    query: str,
    limit: int = 5,
    *,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[dict[str, str]]:
    """Search a free RSS endpoint with one polite retry on rate limiting."""
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    request = urllib.request.Request(
        f"{NEWS_RSS_ENDPOINT}?{params}",
        headers={"User-Agent": "FinTick/0.2 (personal financial event validator)"},
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
    return parse_validation_rss(payload, limit=limit)


def _json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _query(headline: str, summary: str | None, facts_json: str, instruments_json: str) -> str:
    symbols = [
        item.get("symbol", "") for item in _json_list(instruments_json) if isinstance(item, dict)
    ]
    facts = [
        f"{item.get('label', '')} {item.get('value', '')}"
        for item in _json_list(facts_json)
        if isinstance(item, dict)
    ]
    return " ".join(
        f"{headline} {summary or ''} {' '.join(symbols)} {' '.join(facts)}".split()
    )[:500]


def _claim_events(
    database: str | Path, *, limit: int, min_age: float
) -> list[tuple[int, str, str, str]]:
    if limit < 1 or min_age < 0:
        raise ValueError("limit must be positive and min_age non-negative")
    with open_database(database):
        pass
    connection = sqlite3.connect(database, isolation_level=None, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cutoff = (datetime.now(UTC) - timedelta(seconds=min_age)).isoformat()
        rows = connection.execute(
            """
            SELECT id, headline, summary, facts_json, instruments_json, first_seen_at, status
            FROM events
            WHERE status != 'confirmed'
              AND (validated_at IS NULL OR julianday(validated_at) <= julianday(?))
            ORDER BY importance DESC, first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        connection.executemany(
            "UPDATE events SET validation_attempts=validation_attempts+1, validated_at=?, error=NULL WHERE id=?",
            [(now, row[0]) for row in rows],
        )
        connection.commit()
        return [
            (row[0], _query(row[1], row[2], row[3], row[4]), row[5], row[6])
            for row in rows
        ]
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _source(item: Any) -> dict[str, str | None] | None:
    if not isinstance(item, dict):
        return None
    url, title = item.get("url"), item.get("title")
    if not isinstance(url, str) or not isinstance(title, str):
        return None
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not title.strip():
        return None
    stance = str(item.get("stance", "corroborating")).strip().lower()
    if stance not in STANCES:
        return None
    publisher = item.get("publisher", item.get("source"))
    published_at = item.get("published_at")
    return {
        "url": url.strip(),
        "title": title.strip(),
        "publisher": publisher.strip() if isinstance(publisher, str) else parsed.netloc,
        "stance": stance,
        "published_at": published_at.strip() if isinstance(published_at, str) else None,
    }


def _lead_seconds(first_seen_at: str, published_at: str | None) -> int | None:
    if not published_at:
        return None
    try:
        first = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00")).astimezone(UTC)
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None
    return int((published - first).total_seconds())


def validate_pending(
    database: str | Path,
    *,
    limit: int = 5,
    min_age: float = 900,
    lookup: Callable[[str, int], list[dict[str, str]]] = search_external_news,
) -> ValidateStats:
    """Re-hunt eligible events; no sources is a successful breaking result."""
    claims = _claim_events(database, limit=limit, min_age=min_age)
    counts = {"breaking": 0, "confirmed": 0, "contradicted": 0, "developing": 0}
    errored = 0
    for event_id, query, first_seen_at, previous_status in claims:
        try:
            sources = [source for item in lookup(query, 5) if (source := _source(item))]
            with open_database(database) as connection:
                for source in sources:
                    record_validation(
                        connection,
                        event_id,
                        url=str(source["url"]),
                        title=source["title"],
                        publisher=source["publisher"],
                        stance=str(source["stance"]),
                        published_at=source["published_at"],
                    )
                stored = connection.execute(
                    "SELECT stance, published_at FROM event_validations WHERE event_id=?",
                    (event_id,),
                ).fetchall()
                stances = {row[0] for row in stored}
                if "disputing" in stances:
                    status = "contradicted"
                elif "corroborating" in stances:
                    status = "confirmed"
                elif "partial" in stances:
                    status = "developing"
                else:
                    status = "breaking"
                published = next((row[1] for row in stored if row[0] == "corroborating" and row[1]), None)
                set_event_status(
                    connection,
                    event_id,
                    status,
                    lead_seconds=_lead_seconds(first_seen_at, published) if status == "confirmed" else None,
                )
            counts[status] += 1
        except Exception as error:
            with open_database(database) as connection:
                set_event_status(
                    connection,
                    event_id,
                    previous_status,
                    error=f"{type(error).__name__}: {error}"[:1000],
                )
            errored += 1
    return ValidateStats(selected=len(claims), errored=errored, **counts)
