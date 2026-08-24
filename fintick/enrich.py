"""Resilient, one-headline-at-a-time enrichment with the local Qwen model."""

from __future__ import annotations

import json
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

from fintick.storage import open_database

MODEL_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3.8:27b"
CATEGORIES = {
    "commodities", "equities", "macro", "central-bank", "geopolitics",
    "fx", "rates", "crypto", "other",
}
SENTIMENTS = {"bullish", "bearish", "neutral"}
DIRECTIONS = {"up", "down", "flat"}
SYSTEM_PROMPT = """You classify one financial headline. Return only a JSON object with:
summary: one plain-English sentence expanding jargon while preserving numbers;
category: one of commodities, equities, macro, central-bank, geopolitics, fx, rates, crypto, other;
importance: integer 1-5 (5 is major and market-moving);
sentiment: bullish, bearish, or neutral for the primary asset;
instruments: array of {symbol,name,type,venue,direction}, where direction is up/down/flat;
entities: array of people, companies, institutions, or countries; regions: array.
Use global tradable symbols: exchange suffixes for non-US equities, futures roots (CL/BZ/NG/GC),
FX pairs (USDJPY), indices (SPX/N225), and crypto (BTC). Never invent a symbol: omit it when unsure.
Use an empty array when none are confidently identified."""


@dataclass(frozen=True, slots=True)
class EnrichStats:
    selected: int = 0
    enriched: int = 0
    errored: int = 0


def _json_object(content: str) -> dict[str, Any]:
    """Decode model JSON despite optional thinking/prose wrappers."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model returned empty content")
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        value = None
        decoder = json.JSONDecoder()
        # Try each opening brace so prose/code fences and nested objects are safe.
        # Keep the first outer object that consumes the remainder except wrappers.
        for match in re.finditer(r"\{", cleaned):
            try:
                candidate, end = decoder.raw_decode(cleaned[match.start():])
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


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def parse_enrichment(content: str) -> dict[str, Any]:
    """Parse, validate, and normalize an untrusted model response."""
    raw = _json_object(content)
    required = {"summary", "category", "importance", "sentiment", "instruments", "entities", "regions"}
    missing = sorted(required - raw.keys())
    if missing:
        raise ValueError(f"model response is missing fields: {', '.join(missing)}")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("model response is missing summary")

    category = str(raw.get("category", "other")).lower().strip()
    if category not in CATEGORIES:
        raise ValueError(f"invalid category: {category}")
    importance_raw = raw["importance"]
    if isinstance(importance_raw, bool) or (
        isinstance(importance_raw, float) and not importance_raw.is_integer()
    ):
        raise ValueError("importance must be an integer") from None
    try:
        importance = max(1, min(5, int(importance_raw)))
    except (TypeError, ValueError):
        raise ValueError("importance must be an integer") from None
    sentiment = str(raw.get("sentiment", "neutral")).lower().strip()
    if sentiment not in SENTIMENTS:
        raise ValueError(f"invalid sentiment: {sentiment}")

    instruments: list[dict[str, str]] = []
    raw_instruments = raw.get("instruments", [])
    if not isinstance(raw_instruments, list):
        raise ValueError("instruments must be an array")
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
            raise ValueError("each instrument direction must be up, down, or flat")
        instruments.append({
            "symbol": symbol.upper().strip(),
            "name": name.strip(),
            "type": instrument_type.lower().strip(),
            "venue": str(item.get("venue", "")).strip(),
            "direction": direction,
        })

    if not isinstance(raw["entities"], list) or not isinstance(raw["regions"], list):
        raise ValueError("entities and regions must be arrays")
    return {
        "summary": summary.strip(),
        "category": category,
        "importance": importance,
        "sentiment": sentiment,
        "instruments": instruments,
        "entities": _strings(raw["entities"]),
        "regions": _strings(raw["regions"]),
    }


def call_local_model(
    headline: str,
    *,
    endpoint: str = MODEL_ENDPOINT,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
) -> str:
    """Call Ollama's native forced-JSON endpoint and return message content."""
    payload = json.dumps({
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 1024},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": headline},
        ],
    }).encode()
    request = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        content = data["message"]["content"]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"local model request failed: {error}") from error
    if not isinstance(content, str):
        raise RuntimeError("local model returned non-text content")
    return content


def _claim_pending(database: str | Path, limit: int, max_attempts: int) -> list[tuple[str, str, str]]:
    """Atomically lease rows so concurrent workers never process the same post."""
    # Apply schema migrations before taking the explicit write lock.
    with open_database(database):
        pass
    connection = sqlite3.connect(database, isolation_level=None, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        rows = connection.execute(
            """
            SELECT p.uri, p.text FROM posts AS p
            LEFT JOIN enrichments AS e ON e.uri = p.uri
            WHERE p.is_duplicate = 0
              AND (e.uri IS NULL
                   OR (e.status = 'error' AND e.attempts < ?)
                   OR (e.status = 'processing' AND e.attempts < ? AND e.enriched_at < ?))
            ORDER BY p.created_at ASC, p.uri ASC LIMIT ?
            """,
            (max_attempts, max_attempts, cutoff, limit),
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        claims = [(uri, text, uuid.uuid4().hex) for uri, text in rows]
        connection.executemany(
            """
            INSERT INTO enrichments (uri, status, attempts, lease_token, error, enriched_at)
            VALUES (?, 'processing', 1, ?, NULL, ?)
            ON CONFLICT(uri) DO UPDATE SET
                status='processing', attempts=enrichments.attempts + 1,
                lease_token=excluded.lease_token, error=NULL, enriched_at=excluded.enriched_at
            """,
            [(uri, token, now) for uri, _, token in claims],
        )
        connection.commit()
        return claims
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _save_success(
    database: str | Path, uri: str, lease_token: str, result: dict[str, Any]
) -> bool:
    now = datetime.now(UTC).isoformat()
    connection = sqlite3.connect(database, timeout=30)
    try:
        cursor = connection.execute(
            """
            UPDATE enrichments SET
                status='complete', summary=?, category=?, importance=?, sentiment=?,
                instruments_json=?, entities_json=?, regions_json=?,
                lease_token=NULL, error=NULL, enriched_at=?
            WHERE uri=? AND status='processing' AND lease_token=?
            """,
            (result["summary"], result["category"], result["importance"],
             result["sentiment"], json.dumps(result["instruments"], separators=(",", ":")),
             json.dumps(result["entities"], separators=(",", ":")),
             json.dumps(result["regions"], separators=(",", ":")), now, uri, lease_token),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def _save_error(
    database: str | Path, uri: str, lease_token: str, error: BaseException
) -> bool:
    message = f"{type(error).__name__}: {error}"[:1000]
    now = datetime.now(UTC).isoformat()
    connection = sqlite3.connect(database, timeout=30)
    try:
        cursor = connection.execute(
            """
            UPDATE enrichments SET status='error', lease_token=NULL, error=?, enriched_at=?
            WHERE uri=? AND status='processing' AND lease_token=?
            """,
            (message, now, uri, lease_token),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def enrich_pending(
    database: str | Path,
    *,
    limit: int = 10,
    max_attempts: int = 3,
    call_model: Callable[[str], str] = call_local_model,
) -> EnrichStats:
    """Enrich pending canonical posts; isolate and persist every item failure."""
    if limit < 1 or max_attempts < 1:
        raise ValueError("limit and max_attempts must be at least 1")
    rows = _claim_pending(database, limit, max_attempts)
    enriched = errored = 0
    for uri, headline, lease_token in rows:
        try:
            result = parse_enrichment(call_model(headline))
            enriched += _save_success(database, uri, lease_token, result)
        except Exception as error:  # an individual model/output failure must not stop the worker
            errored += _save_error(database, uri, lease_token, error)
    return EnrichStats(selected=len(rows), enriched=enriched, errored=errored)
