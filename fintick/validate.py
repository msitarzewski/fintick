"""Hunt independent news and assign event validation status."""

from __future__ import annotations

import json
import math
import os
import re
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
COMMON_VISION_ENDPOINT = "https://common.vision/api/v1/articles"
SOCIAL_VALIDATION_HOSTS = {
    "bsky.app", "facebook.com", "instagram.com", "threads.net", "twitter.com", "x.com",
}
NUMBER_WORDS = {
    "one": "1", "first": "1", "two": "2", "second": "2", "three": "3", "third": "3",
    "four": "4", "fourth": "4", "five": "5", "fifth": "5", "six": "6", "sixth": "6",
    "seven": "7", "seventh": "7", "eight": "8", "eighth": "8", "nine": "9", "ninth": "9",
    "ten": "10", "tenth": "10",
}
STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it",
    "its", "of", "on", "or", "the", "to", "with", "since",
}
DISPUTE_WORDS = {"deny", "denies", "denied", "dispute", "disputes", "false", "incorrect", "not"}
WEAK_EVIDENCE_WORDS = {
    "action", "announce", "new", "policy", "report", "say", "state", "trump", "us", "warn",
    "remov",
}
COMMON_VISION_SEARCH_NOISE = {
    "announced", "announces", "begins", "clears", "falls", "fell", "launches", "plans",
    "reports", "rises", "rose", "says", "seeks", "starts", "trump", "unveils", "us", "warns",
}
HOST_DOT_EQUIVALENTS = str.maketrans({"。": ".", "．": ".", "｡": "."})


def _normalized_validation_host(host: str) -> str:
    normalized = host
    for _ in range(4):
        decoded = urllib.parse.unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.translate(HOST_DOT_EQUIVALENTS)
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    normalized = normalized.lower().rstrip(".").removeprefix("www.")
    if (
        not normalized
        or not re.fullmatch(r"[a-z0-9.-]+", normalized)
        or ".." in normalized
        or normalized.startswith(".")
    ):
        return ""
    return normalized


def _is_social_validation_host(host: str) -> bool:
    normalized = _normalized_validation_host(host)
    return any(
        normalized == social or normalized.endswith(f".{social}")
        for social in SOCIAL_VALIDATION_HOSTS
    )


def _validation_url(url: str) -> tuple[urllib.parse.ParseResult, str] | None:
    raw_url = url.strip()
    decoded_url = raw_url
    for _ in range(4):
        decoded = urllib.parse.unquote(decoded_url)
        if decoded == decoded_url:
            break
        decoded_url = decoded
    if "\\" in decoded_url:
        return None
    parsed = urllib.parse.urlparse(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = _normalized_validation_host(parsed.hostname or "")
    if not host:
        return None
    return parsed, host


@dataclass(frozen=True, slots=True)
class ValidateStats:
    selected: int = 0
    breaking: int = 0
    confirmed: int = 0
    contradicted: int = 0
    developing: int = 0
    errored: int = 0


@dataclass(frozen=True, slots=True)
class ValidationClaim:
    event_id: int
    query: str
    first_seen_at: str
    previous_status: str
    headline: str
    facts_json: str
    instruments_json: str


def parse_validation_rss(payload: bytes, *, limit: int = 5) -> list[dict[str, str]]:
    """Parse bounded Google News RSS results as unclassified candidate stories."""
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


def _common_vision_retry_delay(error: urllib.error.HTTPError) -> float:
    """Read partner rate-limit delay from the header or OpenAPI JSON body."""
    def valid_delay(value: Any) -> float | None:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return None
        return delay if math.isfinite(delay) else None

    header_delay = valid_delay(error.headers.get("Retry-After"))
    if header_delay is not None:
        return header_delay
    try:
        document = json.loads(error.read(65_536))
    except Exception:
        return 5.0
    body_delay = valid_delay(document.get("retry_after")) if isinstance(document, dict) else None
    return body_delay if body_delay is not None else 5.0


def search_common_vision(
    query: str,
    limit: int = 5,
    *,
    token: str,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[dict[str, str]]:
    """Search the authenticated common.vision partner index for news candidates."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("common.vision partner token is required")
    if limit < 1 or limit > 100:
        raise ValueError("common.vision limit must be between 1 and 100")
    search_terms = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", query)
        if token.lower() not in STOP_WORDS | COMMON_VISION_SEARCH_NOISE
    ]
    compact_query = (" ".join(search_terms[:2]) or query.strip())[:255]
    today = datetime.now(UTC).date()
    params = urllib.parse.urlencode({
        "search": compact_query,
        "from": (today - timedelta(days=7)).isoformat(),
        "to": today.isoformat(),
        "per_page": limit,
    })
    request = urllib.request.Request(
        f"{COMMON_VISION_ENDPOINT}?{params}",
        headers={
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
            "User-Agent": "FinTick/0.2 (common.vision partner validation)",
        },
    )
    payload = b""
    for attempt in range(2):
        try:
            with opener(request, timeout=timeout) as response:
                payload = response.read(2_000_000)
            break
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt == 0:
                delay = _common_vision_retry_delay(error)
                sleep(min(60.0, max(1.0, delay)))
                continue
            raise RuntimeError(f"common.vision search failed: HTTP {error.code}") from error
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"common.vision search failed: {type(error).__name__}") from error
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("common.vision search returned malformed JSON") from error
    items = document.get("data") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("common.vision search response requires a data array")
    stories: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title, url = item.get("title"), item.get("url")
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
            continue
        validated_url = _validation_url(url)
        if validated_url is None:
            continue
        _parsed, host = validated_url
        if _is_social_validation_host(host):
            continue
        metadata = item.get("metadata")
        feed = item.get("feed")
        publisher = metadata.get("source") if isinstance(metadata, dict) else None
        feed_name = feed.get("name") if isinstance(feed, dict) else None
        story = {
            "url": url.strip(),
            "title": title.strip(),
            "publisher": (
                publisher.strip()
                if isinstance(publisher, str) and publisher.strip()
                else host
            ),
        }
        published_at = item.get("published_at")
        if isinstance(published_at, str) and published_at.strip():
            story["published_at"] = published_at.strip()
        if isinstance(feed_name, str) and feed_name.strip():
            story["feed_name"] = feed_name.strip()
        feed_url = feed.get("url") if isinstance(feed, dict) else None
        if isinstance(feed_url, str) and feed_url.strip():
            story["feed_url"] = feed_url.strip()
        feed_type = feed.get("feed_type") if isinstance(feed, dict) else None
        if isinstance(feed_type, str) and feed_type in {"rss", "atom", "json"}:
            story["feed_type"] = feed_type
        stories.append(story)
    return stories


def search_validation_news(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Query Google News AND (when configured) the common.vision partner index, ADDITIVELY.

    Both sources contribute candidates, deduped by URL. Each is independently fault-tolerant:
    if one source is down or rejects the request, the other's results still stand, so a single
    source failure never errors the whole validation (nor does it silence the other source).
    """
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _merge(items: list[dict[str, str]]) -> None:
        for item in items:
            url = item.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                results.append(item)

    # Google News is always queried.
    try:
        _merge(search_external_news(query, limit))
    except Exception:
        pass
    # common.vision is added on top when a partner token is configured.
    token = os.environ.get("FINTICK_COMMON_VISION_TOKEN", "").strip()
    if token:
        try:
            _merge(search_common_vision(query, limit, token=token))
        except Exception:
            pass
    return results[: max(2 * limit, limit)]


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
) -> list[ValidationClaim]:
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
            WHERE validated_at IS NULL OR julianday(validated_at) <= julianday(?)
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
            ValidationClaim(
                event_id=row[0],
                query=_query(row[1], row[2], row[3], row[4]),
                first_seen_at=row[5],
                previous_status=row[6],
                headline=row[1],
                facts_json=row[3],
                instruments_json=row[4],
            )
            for row in rows
        ]
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _stem(token: str) -> str:
    token = NUMBER_WORDS.get(token, token)
    ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", token)
    if ordinal:
        token = ordinal.group(1)
    if not token.isalpha() or len(token) <= 4:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 6:
        return token[:-3]
    if token.endswith("ed") and len(token) > 5:
        return token[:-2]
    if token.endswith("es") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> set[str]:
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOP_WORDS
    }


def _inferred_stance(title: str, claim: ValidationClaim) -> str | None:
    """Classify a search candidate conservatively from title-level evidence."""
    title_tokens = _tokens(title)
    instruments = [item for item in _json_list(claim.instruments_json) if isinstance(item, dict)]
    entity_text = " ".join(
        str(item.get(field, "")) for item in instruments for field in ("symbol", "name")
    )
    entity_tokens = _tokens(entity_text)
    if entity_tokens and not title_tokens.intersection(entity_tokens):
        return None

    facts = [item for item in _json_list(claim.facts_json) if isinstance(item, dict)]
    fact_labels = " ".join(
        f"{item.get('label', '')} {item.get('unit', '')}" for item in facts
    )
    fact_values = " ".join(str(item.get("value", "")) for item in facts)
    value_tokens = _tokens(fact_values)
    context_tokens = _tokens(f"{claim.headline} {fact_labels}") - entity_tokens - value_tokens
    context_matches = title_tokens.intersection(context_tokens)
    value_matches = title_tokens.intersection(value_tokens)
    specific_context = context_matches - WEAK_EVIDENCE_WORDS
    specific_values = value_matches - WEAK_EVIDENCE_WORDS
    specific_matches = specific_context | specific_values
    dispute = bool(title_tokens.intersection({_stem(word) for word in DISPUTE_WORDS}))
    if dispute and specific_values and specific_context:
        return "disputing"
    if specific_values and specific_context and len(specific_matches) >= 3:
        return "corroborating"
    if len(specific_context) >= 2:
        return "partial"
    return None


def _normalized_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _source(item: Any, claim: ValidationClaim) -> dict[str, str | None] | None:
    if not isinstance(item, dict):
        return None
    url, title = item.get("url"), item.get("title")
    if not isinstance(url, str) or not isinstance(title, str):
        return None
    validated_url = _validation_url(url)
    if validated_url is None or not title.strip():
        return None
    _parsed, host = validated_url
    if _is_social_validation_host(host):
        return None
    stance = _inferred_stance(title, claim)
    if stance is None:
        return None
    publisher = item.get("publisher", item.get("source"))
    published_at = _normalized_timestamp(item.get("published_at"))
    return {
        "url": url.strip(),
        "title": title.strip(),
        "publisher": publisher.strip() if isinstance(publisher, str) else host,
        "stance": stance,
        "published_at": published_at,
        "feed_name": item.get("feed_name") if isinstance(item.get("feed_name"), str) else None,
        "feed_url": item.get("feed_url") if isinstance(item.get("feed_url"), str) else None,
        "feed_type": item.get("feed_type") if item.get("feed_type") in {"rss", "atom", "json"} else None,
    }


def _lead_seconds(first_seen_at: str, published_at: str | None) -> int | None:
    if not published_at:
        return None
    try:
        first = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if first.tzinfo is None or published.tzinfo is None:
        return None
    first = first.astimezone(UTC)
    published = published.astimezone(UTC)
    return int((published - first).total_seconds())


def _repair_stored_sources(
    connection: sqlite3.Connection,
    claim: ValidationClaim,
) -> None:
    """Reclassify historical candidates and remove title-level false positives."""
    rows = connection.execute(
        """
        SELECT url, title, publisher, published_at
        FROM event_validations
        WHERE event_id = ?
        """,
        (claim.event_id,),
    ).fetchall()
    for url, title, publisher, published_at in rows:
        source = _source({
            "url": url,
            "title": title,
            "publisher": publisher,
            "published_at": published_at,
        }, claim)
        if source is None:
            connection.execute(
                "DELETE FROM event_validations WHERE event_id = ? AND url = ?",
                (claim.event_id, url),
            )
            continue
        connection.execute(
            """
            UPDATE event_validations
            SET title = ?, publisher = ?, stance = ?, published_at = ?
            WHERE event_id = ? AND url = ?
            """,
            (
                source["title"], source["publisher"], source["stance"],
                source["published_at"], claim.event_id, url,
            ),
        )


def validate_pending(
    database: str | Path,
    *,
    limit: int = 5,
    min_age: float = 900,
    lookup: Callable[[str, int], list[dict[str, str]]] = search_validation_news,
) -> ValidateStats:
    """Re-hunt eligible events; no sources is a successful breaking result."""
    claims = _claim_events(database, limit=limit, min_age=min_age)
    counts = {"breaking": 0, "confirmed": 0, "contradicted": 0, "developing": 0}
    errored = 0
    for claim in claims:
        try:
            sources = [
                source for item in lookup(claim.query, 5)
                if (source := _source(item, claim))
            ]
            with open_database(database) as connection:
                _repair_stored_sources(connection, claim)
                for source in sources:
                    record_validation(
                        connection,
                        claim.event_id,
                        url=str(source["url"]),
                        title=source["title"],
                        publisher=source["publisher"],
                        stance=str(source["stance"]),
                        published_at=source["published_at"],
                        feed_name=source.get("feed_name"),
                        feed_url=source.get("feed_url"),
                        feed_type=source.get("feed_type"),
                    )
                stored = connection.execute(
                    "SELECT stance, published_at FROM event_validations WHERE event_id=?",
                    (claim.event_id,),
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
                published = connection.execute(
                    """
                    SELECT published_at FROM event_validations
                    WHERE event_id=? AND stance='corroborating'
                      AND published_at IS NOT NULL AND julianday(published_at) IS NOT NULL
                    ORDER BY julianday(published_at) ASC LIMIT 1
                    """,
                    (claim.event_id,),
                ).fetchone()
                earliest_published = published[0] if published else None
                set_event_status(
                    connection,
                    claim.event_id,
                    status,
                    lead_seconds=(
                        _lead_seconds(claim.first_seen_at, earliest_published)
                        if status == "confirmed" else None
                    ),
                )
            counts[status] += 1
        except Exception as error:
            with open_database(database) as connection:
                set_event_status(
                    connection,
                    claim.event_id,
                    claim.previous_status,
                    error=f"{type(error).__name__}: {error}"[:1000],
                )
            errored += 1
    return ValidateStats(selected=len(claims), errored=errored, **counts)
