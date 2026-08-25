"""Aggregate a rolling stream window into distinct financial events."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fintick.storage import V2Event, open_database, upsert_event

DIRECTIONS = {"up", "down", "flat"}
MODEL_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3.8:27b"
WINDOW = timedelta(hours=6)
MAX_POSTS = 200
SYSTEM_PROMPT = """You aggregate posts from ONE fast financial stream into distinct real-world events.
Return JSON with one key, events, an array. Each event must contain:
canonical_headline (concise), summary (one sentence), importance (integer 1-5),
instruments (array of {symbol,name,type,direction}; direction is up/down/flat),
facts (array of {label,value} for concrete claims such as percentage moves, counts, prices, dates), and
stream_post_uris (the exact URI strings supporting that event).
Merge semantic repeats and updates of the same event even when wording, casing, or ticker/name forms
differ. In particular $NVDA and NVIDIA refer to one instrument. A stream post belongs to at most one
event. Do not invent events, symbols, facts, or URIs. Omit chatter that is not a financial event."""


@dataclass(frozen=True, slots=True)
class ParsedAggregation:
    """Validated events plus the count of rejected event objects."""

    events: tuple[V2Event, ...] = ()
    errored: int = 0


@dataclass(frozen=True, slots=True)
class AggregateStats:
    selected: int = 0
    events: int = 0
    created: int = 0
    errored: int = 0


def _json_object(content: str) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model returned empty content")
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        value = None
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", cleaned):
            try:
                candidate, _ = decoder.raw_decode(cleaned[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise ValueError("model response did not contain valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("model JSON must be an object")
    return value


def _parse_event(
    raw: Any,
    *,
    allowed_uris: set[str],
    post_times: dict[str, str],
) -> V2Event:
    if not isinstance(raw, dict):
        raise ValueError("each event must be an object")
    headline = raw.get("canonical_headline")
    summary = raw.get("summary")
    if not isinstance(headline, str) or not headline.strip():
        raise ValueError("each event requires canonical_headline")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("each event requires summary")

    raw_uris = raw.get("stream_post_uris")
    if not isinstance(raw_uris, list) or not raw_uris:
        raise ValueError("each event requires stream_post_uris")
    uris: list[str] = []
    for uri in raw_uris:
        if not isinstance(uri, str) or uri not in allowed_uris:
            raise ValueError("event contains an unknown stream post URI")
        if uri not in uris:
            uris.append(uri)

    raw_instruments = raw.get("instruments")
    if not isinstance(raw_instruments, list):
        raise ValueError("instruments must be an array")
    instruments: list[dict[str, str]] = []
    for item in raw_instruments:
        if not isinstance(item, dict):
            raise ValueError("each instrument must be an object")
        symbol = item.get("symbol")
        name = item.get("name")
        instrument_type = item.get("type")
        direction = str(item.get("direction", "")).lower().strip()
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("each instrument requires a symbol")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each instrument requires a name")
        if not isinstance(instrument_type, str) or not instrument_type.strip():
            raise ValueError("each instrument requires a type")
        if direction not in DIRECTIONS:
            raise ValueError("instrument direction must be up, down, or flat")
        instruments.append({
            "symbol": symbol.strip().lstrip("$").upper(),
            "name": name.strip(),
            "type": instrument_type.strip().lower(),
            "direction": direction,
        })

    raw_facts = raw.get("facts")
    if not isinstance(raw_facts, list):
        raise ValueError("facts must be an array")
    facts: list[dict[str, Any]] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            raise ValueError("each fact must be an object")
        label, value = item.get("label"), item.get("value")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("each fact requires a label")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("each fact requires a scalar value")
        fact: dict[str, Any] = {"label": label.strip(), "value": value}
        unit = item.get("unit")
        if isinstance(unit, str) and unit.strip():
            fact["unit"] = unit.strip()
        facts.append(fact)

    importance_raw = raw.get("importance")
    if isinstance(importance_raw, bool) or not isinstance(importance_raw, (int, str)):
        raise ValueError("importance must be an integer")
    try:
        importance = int(importance_raw)
    except (TypeError, ValueError):
        raise ValueError("importance must be an integer") from None
    if importance < 1 or importance > 5:
        raise ValueError("importance must be between 1 and 5")

    timestamps = [post_times[uri] for uri in uris]
    primary = instruments[0]["symbol"] if instruments else None
    return V2Event.from_key(
        headline.strip(),
        summary.strip(),
        primary_instrument=primary,
        facts=tuple(facts),
        instruments=tuple(instruments),
        importance=importance,
        post_uris=tuple(uris),
        first_seen_at=min(timestamps),
        last_seen_at=max(timestamps),
    )


def parse_aggregation(
    content: str,
    *,
    allowed_uris: set[str],
    post_times: dict[str, str],
) -> ParsedAggregation:
    """Parse model JSON, rejecting malformed events independently."""
    raw = _json_object(content)
    items = raw.get("events")
    if not isinstance(items, list):
        raise ValueError("model response requires an events array")

    events: list[V2Event] = []
    claimed_uris: set[str] = set()
    errored = 0
    for item in items:
        try:
            event = _parse_event(
                item, allowed_uris=allowed_uris, post_times=post_times
            )
            if claimed_uris.intersection(event.post_uris):
                raise ValueError("stream post assigned to more than one event")
            events.append(event)
            claimed_uris.update(event.post_uris)
        except (KeyError, ValueError):
            errored += 1
    return ParsedAggregation(tuple(events), errored)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("post timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _load_window(database: str | Path, limit: int) -> list[dict[str, str]]:
    if limit < 1 or limit > MAX_POSTS:
        raise ValueError(f"limit must be between 1 and {MAX_POSTS}")
    with open_database(database) as connection:
        rows = connection.execute(
            "SELECT uri, created_at, text FROM posts ORDER BY created_at DESC, uri DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return []
    newest = _timestamp(rows[0][1])
    cutoff = newest - WINDOW
    selected = [row for row in rows if _timestamp(row[1]) >= cutoff]
    selected.reverse()
    return [
        {"uri": uri, "created_at": created_at, "text": text}
        for uri, created_at, text in selected
    ]


def call_local_model(
    prompt: str,
    *,
    endpoint: str = MODEL_ENDPOINT,
    model: str = DEFAULT_MODEL,
    timeout: float = 300.0,
) -> str:
    """Make one forced-JSON Ollama call for the complete stream window."""
    payload = json.dumps({
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 32768, "num_predict": 8192},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        content = data["message"]["content"]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"local aggregation request failed: {error}") from error
    if not isinstance(content, str):
        raise RuntimeError("local aggregation model returned non-text content")
    return content


def aggregate_once(
    database: str | Path,
    *,
    limit: int = MAX_POSTS,
    call_model: Callable[[str], str] = call_local_model,
) -> AggregateStats:
    """Aggregate one rolling window with one model call and persist valid events."""
    posts = _load_window(database, limit)
    if not posts:
        return AggregateStats()
    prompt = json.dumps(posts, ensure_ascii=False, separators=(",", ":"))
    allowed_uris = {post["uri"] for post in posts}
    post_times = {post["uri"]: post["created_at"] for post in posts}
    try:
        parsed = parse_aggregation(
            call_model(prompt), allowed_uris=allowed_uris, post_times=post_times
        )
    except Exception:
        return AggregateStats(selected=len(posts), errored=1)

    created = 0
    with open_database(database) as connection:
        for event in parsed.events:
            _, was_created = upsert_event(connection, event)
            created += int(was_created)
    return AggregateStats(
        selected=len(posts),
        events=len(parsed.events),
        created=created,
        errored=parsed.errored,
    )
