"""Deterministic headline normalization for exact duplicate detection.

Legacy v1 mechanism: exact normalized-hash dedup of stream posts. The
``posts`` table still carries the hash columns and the legacy backfill path
still needs these functions, but v2 retires hash-dedup as the merge
mechanism — semantic aggregation (PRD.md F2) replaces it. Do not build new
v2 behavior on top of this module. See STATUS.md "v2 pivot".
"""

from __future__ import annotations

import hashlib
import re

TRAILING_PUNCTUATION = ". ,-–—:;!?"


def normalize_text(text: str) -> str:
    """Normalize casing, whitespace, and trailing headline punctuation."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return normalized.rstrip(TRAILING_PUNCTUATION).rstrip()


def text_hash(text: str) -> str:
    """Return the stable SHA-1 key for a normalized headline."""
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()
