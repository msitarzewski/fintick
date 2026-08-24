"""Deterministic headline normalization for exact duplicate detection."""

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
