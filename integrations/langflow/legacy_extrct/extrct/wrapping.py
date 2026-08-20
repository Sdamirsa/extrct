"""wrap-def/1.0 — deterministic long-text wrapping (chunking) for extraction.

Three logics, all pure functions of (text, params) — no models, no randomness — so a
wrapping is re-derivable from the text plus its recorded params, and chunk storage
is provenance, not necessity:

  fixed_window    fixed character windows with overlap; the cut snaps backward to the
                  nearest whitespace within the last 15% of the window, so words survive.
  paragraph_pack  split on blank lines, pack whole paragraphs up to max_chars; the next
                  window re-opens with the trailing paragraphs up to overlap_chars.
  sentence_pack   the same packing over sentence-ish units (. ! ? and newlines).

Every chunk is a (start, end) span into the ORIGINAL text with `text == source[start:end]`
— offsets are the contract that later lets grounding spans and citations map back to the
full document. Invariant (tested): the union of spans covers every character; packing
units are extended over their separators so nothing falls between chunks. A single unit
longer than max_chars falls back to fixed_window slicing inside itself.

Identity: wrap_uid = content_uid({version, text_sha256, logic, params}) — the PLAN
applied to the TEXT; chunk_uid = content_uid({wrap_uid, idx, start, end}).
"""

from __future__ import annotations

import re
from typing import Any

from .hashing import content_uid, sha256_text

WRAP_MODEL_VERSION = "wrap-def/1.0"
WRAPPER_LOGICS = ("fixed_window", "paragraph_pack", "sentence_pack")

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _windows(text: str, s: int, e: int, max_chars: int, overlap: int) -> list[tuple[int, int]]:
    """Fixed windows over text[s:e], whitespace-snapped, guaranteed forward progress."""
    out = []
    pos = s
    while True:
        end = min(pos + max_chars, e)
        if end < e:
            floor = pos + int(max_chars * 0.85)
            snap = text.rfind(" ", floor, end)
            snap = max(snap, text.rfind("\n", floor, end))
            if snap > floor:
                end = snap + 1  # keep the separator with the left chunk
        out.append((pos, end))
        if end >= e:
            return out
        pos = max(end - overlap, pos + 1)


def _unit_spans(text: str, splitter: re.Pattern) -> list[tuple[int, int]]:
    """Split spans extended over their separators so units TILE the text exactly."""
    spans, last = [], 0
    for m in splitter.finditer(text):
        if m.start() > last:
            spans.append((last, m.end()))  # unit + its separator
            last = m.end()
    if last < len(text):
        spans.append((last, len(text)))
    return spans or [(0, len(text))]


def _pack(text: str, units: list[tuple[int, int]], max_chars: int, overlap: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    i, n = 0, len(units)
    while i < n:
        if units[i][1] - units[i][0] > max_chars:  # oversized single unit
            chunks.extend(_windows(text, units[i][0], units[i][1], max_chars, overlap))
            i += 1
            continue
        j = i
        while j + 1 < n and units[j + 1][1] - units[i][0] <= max_chars \
                and units[j + 1][1] - units[j + 1][0] <= max_chars:
            j += 1
        chunks.append((units[i][0], units[j][1]))
        if j + 1 >= n:
            break
        # next window re-opens with trailing units up to overlap_chars, always advancing
        m = j + 1
        while m > i + 1 and units[j][1] - units[m - 1][0] <= overlap:
            m -= 1
        i = max(m, i + 1)
    return chunks


def wrap_text(text: str, logic: str = "paragraph_pack", *, max_chars: int = 4000,
              overlap_chars: int = 400) -> dict[str, Any]:
    """The full wrap-def/1.0 document. Loud on garbage parameters."""
    if logic not in WRAPPER_LOGICS:
        msg = f"unknown wrapping logic {logic!r}; logics: {WRAPPER_LOGICS}"
        raise ValueError(msg)
    if not text:
        msg = "empty text cannot be wrapped"
        raise ValueError(msg)
    max_chars = int(max_chars)
    overlap = int(overlap_chars)
    if max_chars < 200:
        msg = f"max_chars {max_chars} is below the sane floor (200)"
        raise ValueError(msg)
    if not 0 <= overlap < max_chars:
        msg = f"overlap_chars must be in [0, max_chars); got {overlap} for max_chars={max_chars}"
        raise ValueError(msg)

    if logic == "fixed_window":
        spans = _windows(text, 0, len(text), max_chars, overlap)
    else:
        splitter = _PARA_SPLIT if logic == "paragraph_pack" else _SENT_SPLIT
        spans = _pack(text, _unit_spans(text, splitter), max_chars, overlap)

    params = {"max_chars": max_chars, "overlap_chars": overlap}
    text_sha = sha256_text(text)
    wrap_uid = content_uid({"version": WRAP_MODEL_VERSION, "text_sha256": text_sha,
                            "logic": logic, "params": params})
    chunks = []
    for idx, (s, e) in enumerate(spans):
        chunks.append({
            "chunk_uid": content_uid({"wrap_uid": wrap_uid, "idx": idx, "start": s, "end": e}),
            "idx": idx, "start": s, "end": e, "n_chars": e - s, "text": text[s:e],
        })
    return {"version": WRAP_MODEL_VERSION, "logic": logic, "params": params,
            "text_sha256": text_sha, "n_chars": len(text), "wrap_uid": wrap_uid,
            "n_chunks": len(chunks), "chunks": chunks}


def coverage_ok(doc: dict[str, Any]) -> bool:
    """Every character of the source inside at least one chunk span (the invariant)."""
    spans = sorted((c["start"], c["end"]) for c in doc["chunks"])
    pos = 0
    for s, e in spans:
        if s > pos:
            return False
        pos = max(pos, e)
    return pos >= doc["n_chars"]
