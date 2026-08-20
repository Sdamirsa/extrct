"""Content addressing. Every identity in this library is a hash of its own content.

Convention: ``sha256(canonical_json(body))[:16]``. Identical inputs must yield identical
uids on any machine (the replayability standard), so canonicalisation is not optional and
must never depend on dict insertion order or float formatting.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

UID_LEN = 16


def canonical_json(body: Any) -> str:
    """Deterministic JSON. Sorted keys, no incidental whitespace, non-ASCII preserved."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_uid(body: Any, *, length: int = UID_LEN) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:length]


def sha256_text(text: str) -> str:
    """Full-length digest. Used for input text, which is hashed but never stored by
    default (the privacy standard: hash, don't keep)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_fingerprint(secret: str) -> str:
    """Short, non-reversible marker so a record can say WHICH key was used, never the key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8] if secret else ""
