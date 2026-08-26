"""Aggregate a rolling stream window into distinct financial events."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fintick.storage import (
    POST_AGGREGATION_MAX_ATTEMPTS,
    V2Event,
    open_database,
    set_post_aggregation_decision,
    upsert_event,
)

DIRECTIONS = {"up", "down", "flat"}
# Inference is a plain OpenAI-compatible endpoint (POST {base_url}/chat/completions).
# Defaults to a free local ollama server; point these at any OpenAI-compatible
# provider for cloud. No Hermes, OAuth, or HOME dependency.
LLM_BASE_URL = os.environ.get("FINTICK_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.environ.get("FINTICK_LLM_API_KEY", "ollama")
LLM_MODEL = os.environ.get("FINTICK_LLM_MODEL", "qwen3.8:27b")
DEFAULT_MODEL = LLM_MODEL  # backward-compat alias
# Reasoning models (GPT-5.x) spend this budget on hidden reasoning BEFORE emitting output, so it
# must leave room for both. 4096 gets fully consumed by reasoning on hard batches -> empty content;
# 16384 leaves ample headroom (observed ~5-7k total). Tunable per-model via the env var.
LLM_MAX_TOKENS = int(os.environ.get("FINTICK_LLM_MAX_TOKENS", "16384"))
WINDOW = timedelta(hours=6)
MAX_POSTS = 200
DEFAULT_BATCH = 50

SYSTEM_PROMPT = """You aggregate posts from ONE fast financial stream into distinct real-world events.
Return JSON with exactly two top-level arrays: events and ignored_posts. Each event must contain:
canonical_headline (concise), summary (one sentence), importance (integer 1-5),
instruments (array of {symbol,name,type,direction}; direction is up/down/flat; symbol is the
ticker when one exists, otherwise leave it "" — indices, government bonds, FX, and commodities
usually have no ticker and are identified by name and type; never invent a ticker),
facts (array of {label,value} for concrete claims such as percentage moves, counts, prices, dates), and
post_ids (the exact short input IDs supporting that event). Each ignored_posts item must be
{"id":"p001","reason":"brief concrete reason"}.
Merge semantic repeats and updates of the same event even when wording, casing, or ticker/name forms
differ. In particular $NVDA and NVIDIA refer to one instrument. A stream post belongs to at most one
event. Every input ID must appear exactly once: in one event or in ignored_posts. Do not invent events,
symbols, facts, or IDs. Treat monetary policy, sanctions, commodity-supply security, and geopolitical
developments with plausible market impact as financial events; ignore only clearly non-financial chatter."""


@dataclass(frozen=True, slots=True)
class ParsedAggregation:
    """Validated events plus the count of rejected event objects."""

    events: tuple[V2Event, ...] = ()
    errored: int = 0


@dataclass(frozen=True, slots=True)
class AccountedAggregation:
    """Validated events plus one durable outcome for every selected post."""

    events: tuple[V2Event, ...] = ()
    ignored: tuple[tuple[str, str], ...] = ()
    errored_uris: tuple[str, ...] = ()
    errored: int = 0


@dataclass(frozen=True, slots=True)
class AggregateStats:
    selected: int = 0
    events: int = 0
    created: int = 0
    ignored: int = 0
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
        # Symbol is OPTIONAL: indices, sovereign bonds, FX, commodities, and many
        # private or foreign issuers have no ticker. Such an instrument is identified
        # by name + type — require those, and normalize a symbol only when one is given.
        if symbol is not None and not isinstance(symbol, str):
            raise ValueError("instrument symbol must be a string when present")
        normalized_symbol = (
            symbol.strip().lstrip("$").upper() if isinstance(symbol, str) else ""
        )
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each instrument requires a name")
        if not isinstance(instrument_type, str) or not instrument_type.strip():
            raise ValueError("each instrument requires a type")
        if direction not in DIRECTIONS:
            raise ValueError("instrument direction must be up, down, or flat")
        instruments.append({
            "symbol": normalized_symbol,
            "name": name.strip(),
            "type": instrument_type.strip().lower(),
            "direction": direction,
        })

    raw_facts = raw.get("facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise ValueError("facts must be a non-empty array")
    facts: list[dict[str, Any]] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            raise ValueError("each fact must be an object")
        label, value = item.get("label"), item.get("value")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("each fact requires a label")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("each fact requires a scalar value")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("fact string values must not be blank")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("fact numeric values must be finite")
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
    # Fall back to the instrument name when it carries no ticker, so events still
    # get a stable, meaningful identity component (indices, bonds, FX, commodities).
    primary = None
    if instruments:
        primary = instruments[0]["symbol"] or instruments[0]["name"]
    return V2Event.from_key(
        headline.strip(),
        summary.strip(),
        primary_instrument=primary,
        facts=tuple(facts),
        instruments=tuple(instruments),
        importance=importance,
        post_uris=tuple(uris),
        first_seen_at=min(timestamps, key=_timestamp),
        last_seen_at=max(timestamps, key=_timestamp),
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


def parse_accounted_aggregation(
    content: str,
    *,
    posts: dict[str, dict[str, str]],
) -> AccountedAggregation:
    """Parse a short-ID response and account for every selected post."""
    raw = _json_object(content)
    items = raw.get("events")
    ignored_items = raw.get("ignored_posts")
    if not isinstance(items, list):
        raise ValueError("model response requires an events array")
    if not isinstance(ignored_items, list):
        raise ValueError("model response requires an ignored_posts array")

    allowed_uris = {post["uri"] for post in posts.values()}
    post_times = {post["uri"]: post["created_at"] for post in posts.values()}
    claimed_ids: set[str] = set()
    events: list[V2Event] = []
    ignored: list[tuple[str, str]] = []
    ignored_ids: set[str] = set()
    contract_errors = 0

    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("each event must be an object")
            post_ids = item.get("post_ids")
            if not isinstance(post_ids, list) or not post_ids:
                raise ValueError("each event requires post_ids")
            if any(not isinstance(post_id, str) for post_id in post_ids):
                raise ValueError("event contains a non-string post id")
            if len(set(post_ids)) != len(post_ids):
                raise ValueError("event repeats a post id")
            normalized_ids: list[str] = []
            for post_id in post_ids:
                if post_id not in posts:
                    raise ValueError("event contains an unknown post id")
                if post_id not in normalized_ids:
                    normalized_ids.append(post_id)
            if claimed_ids.intersection(normalized_ids):
                raise ValueError("post id assigned to more than one event")
            adapted = dict(item)
            adapted["stream_post_uris"] = [posts[post_id]["uri"] for post_id in normalized_ids]
            event = _parse_event(
                adapted, allowed_uris=allowed_uris, post_times=post_times
            )
            events.append(event)
            claimed_ids.update(normalized_ids)
        except (KeyError, ValueError):
            contract_errors += 1

    for item in ignored_items:
        try:
            if not isinstance(item, dict):
                raise ValueError("each ignored post must be an object")
            post_id, reason = item.get("id"), item.get("reason")
            if not isinstance(post_id, str) or post_id not in posts:
                raise ValueError("ignored post contains an unknown id")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("ignored post requires a reason")
            if post_id in claimed_ids or post_id in ignored_ids:
                raise ValueError("post id must have exactly one decision")
            ignored_ids.add(post_id)
            ignored.append((posts[post_id]["uri"], reason.strip()))
        except ValueError:
            contract_errors += 1

    missing_ids = [
        post_id for post_id in posts
        if post_id not in claimed_ids and post_id not in ignored_ids
    ]
    return AccountedAggregation(
        events=tuple(events),
        ignored=tuple(ignored),
        errored_uris=tuple(posts[post_id]["uri"] for post_id in missing_ids),
        errored=max(contract_errors, len(missing_ids)),
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("post timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _load_pending(database: str | Path, limit: int) -> list[dict[str, str]]:
    """Load the oldest posts that still need an aggregation decision."""
    if limit < 1 or limit > MAX_POSTS:
        raise ValueError(f"limit must be between 1 and {MAX_POSTS}")
    with open_database(database) as connection:
        retry = connection.execute(
            """
            SELECT retry_group
            FROM post_aggregation_decisions d
            JOIN posts p ON p.uri = d.post_uri
            WHERE d.state = 'errored' AND d.attempts < ?
            ORDER BY julianday(p.created_at) IS NULL, julianday(p.created_at), p.uri
            LIMIT 1
            """,
            (POST_AGGREGATION_MAX_ATTEMPTS,),
        ).fetchone()
        rows: list[tuple[str, str, str]] = []
        if retry and retry[0] is not None:
            rows = connection.execute(
                """
                SELECT p.uri, p.created_at, p.text
                FROM post_aggregation_decisions d
                JOIN posts p ON p.uri = d.post_uri
                WHERE d.state = 'errored' AND d.attempts < ? AND d.retry_group = ?
                ORDER BY julianday(p.created_at) IS NULL, julianday(p.created_at), p.uri
                """,
                (POST_AGGREGATION_MAX_ATTEMPTS, retry[0]),
            ).fetchall()
        elif retry:
            rows = connection.execute(
                """
                SELECT p.uri, p.created_at, p.text
                FROM post_aggregation_decisions d
                JOIN posts p ON p.uri = d.post_uri
                WHERE d.state = 'errored' AND d.attempts < ? AND d.retry_group IS NULL
                ORDER BY julianday(p.created_at) IS NULL, julianday(p.created_at), p.uri
                LIMIT 1
                """,
                (POST_AGGREGATION_MAX_ATTEMPTS,),
            ).fetchall()
        if not rows and not retry:
            rows = connection.execute(
                """
                SELECT p.uri, p.created_at, p.text
                FROM post_aggregation_decisions d
                JOIN posts p ON p.uri = d.post_uri
                WHERE d.state = 'pending'
                ORDER BY julianday(p.created_at) IS NULL, julianday(p.created_at), p.uri
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [
        {"uri": uri, "created_at": created_at, "text": text}
        for uri, created_at, text in rows
    ]


def _load_window(database: str | Path, limit: int) -> list[dict[str, str]]:
    if limit < 1 or limit > MAX_POSTS:
        raise ValueError(f"limit must be between 1 and {MAX_POSTS}")
    with open_database(database) as connection:
        rows = connection.execute(
            "SELECT uri, created_at, text FROM posts "
            "ORDER BY julianday(created_at) IS NULL, julianday(created_at) DESC, uri DESC "
            "LIMIT ?",
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


def call_inference(
    prompt: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float = 300.0,
) -> str:
    """Make one forced-JSON call to an OpenAI-compatible chat endpoint.

    Works for a local ollama server (``/v1``) and any cloud provider alike; the
    endpoint, key, and model come from the environment (or the overrides here).
    """
    base_url = (base_url or LLM_BASE_URL).rstrip("/")
    api_key = api_key or LLM_API_KEY
    model = model or LLM_MODEL
    payload = json.dumps({
        "model": model,
        "stream": False,
        # GPT-5.x requires max_completion_tokens (not max_tokens) and rejects any non-default
        # temperature; it also spends this budget on hidden reasoning first, so keep headroom.
        "max_completion_tokens": LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        content = data["choices"][0]["message"]["content"]
    except (
        OSError,
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        IndexError,
    ) as error:
        raise RuntimeError(f"aggregation request failed: {error}") from error
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("aggregation model returned empty or non-text content")
    return content


def aggregate_once(
    database: str | Path,
    *,
    limit: int = DEFAULT_BATCH,
    call_model: Callable[[str], str] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> AggregateStats:
    """Aggregate the oldest pending posts and persist a decision for each."""
    posts = _load_pending(database, limit)
    if not posts:
        return AggregateStats()

    posts_by_id = {
        f"p{index:03d}": post for index, post in enumerate(posts, start=1)
    }
    prompt_rows = [
        {"id": post_id, "created_at": post["created_at"], "text": post["text"]}
        for post_id, post in posts_by_id.items()
    ]
    prompt = json.dumps(prompt_rows, ensure_ascii=False, separators=(",", ":"))
    allowed_uris = {post["uri"] for post in posts}
    post_times = {post["uri"]: post["created_at"] for post in posts}
    retry_group = uuid.uuid4().hex

    try:
        if call_model is None:
            call_model = lambda value: call_inference(
                value, base_url=base_url, api_key=api_key, model=model
            )
        raw_content = call_model(prompt)
        raw_object = _json_object(raw_content)
        raw_events = raw_object.get("events")
        accounted = "ignored_posts" in raw_object or (
            isinstance(raw_events, list)
            and any(isinstance(event, dict) and "post_ids" in event for event in raw_events)
        )
        if accounted:
            parsed_accounted = parse_accounted_aggregation(
                raw_content, posts=posts_by_id
            )
            parsed_legacy = None
        else:
            # Compatibility for captured v2 fixtures; production prompts require
            # short IDs and explicit ignored-post decisions.
            parsed_legacy = parse_aggregation(
                raw_content, allowed_uris=allowed_uris, post_times=post_times
            )
            parsed_accounted = None
    except Exception as error:
        with open_database(database) as connection:
            for post in posts:
                set_post_aggregation_decision(
                    connection,
                    post["uri"],
                    "errored",
                    reason=f"model batch failed: {type(error).__name__}",
                    retry_group=retry_group,
                )
        return AggregateStats(selected=len(posts), errored=len(posts))

    created = 0
    persistence_errors = 0
    persisted_events = 0
    ignored_count = 0
    decided_uris: set[str] = set()
    legacy_omissions = 0
    with open_database(database) as connection:
        events = (
            parsed_accounted.events if parsed_accounted is not None
            else parsed_legacy.events if parsed_legacy is not None
            else ()
        )
        for event in events:
            try:
                event_id, was_created = upsert_event(connection, event)
            except (ValueError, sqlite3.IntegrityError):
                affected_uris = set(event.post_uris)
                persistence_errors += len(affected_uris) or 1
                for post_uri in affected_uris:
                    set_post_aggregation_decision(
                        connection, post_uri, "errored",
                        reason="event persistence rejected",
                        retry_group=retry_group,
                    )
                decided_uris.update(affected_uris)
                continue
            created += int(was_created)
            persisted_events += 1
            for post_uri in event.post_uris:
                set_post_aggregation_decision(
                    connection, post_uri, "assigned", event_id=event_id
                )
            decided_uris.update(event.post_uris)

        if parsed_accounted is not None:
            for post_uri, reason in parsed_accounted.ignored:
                set_post_aggregation_decision(
                    connection, post_uri, "ignored", reason=reason
                )
                decided_uris.add(post_uri)
                ignored_count += 1
            for post_uri in parsed_accounted.errored_uris:
                set_post_aggregation_decision(
                    connection, post_uri, "errored",
                    reason="model omitted or rejected this post id",
                    retry_group=retry_group,
                )
                decided_uris.add(post_uri)
        else:
            omitted_uris = allowed_uris - decided_uris
            legacy_omissions = len(omitted_uris)
            for post_uri in omitted_uris:
                set_post_aggregation_decision(
                    connection, post_uri, "errored",
                    reason="legacy model response omitted this post",
                    retry_group=retry_group,
                )

    parser_errors = (
        parsed_accounted.errored if parsed_accounted is not None
        else parsed_legacy.errored if parsed_legacy is not None
        else 0
    )
    return AggregateStats(
        selected=len(posts),
        events=persisted_events,
        created=created,
        ignored=ignored_count,
        errored=parser_errors + persistence_errors + legacy_omissions,
    )
