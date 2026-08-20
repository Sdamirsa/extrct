"""Evidence-quote alignment — the mechanized grounding path. Pure, stdlib-only.

Compact reimplementation of the approach proven in Google langextract (Apache-2.0),
carrying the deviations recorded in docs/extraction-stack/grounding-research.md:

  - Character-class tokenization (letters | digits | single symbols), the granularity
    class that recovers 98-99% of contiguous targets on the BOAT benchmark, where
    word/whitespace tokenization misses 47-58%. `CD96+` -> CD,96,+ ; `55%` -> 55,%.
  - NO trailing-`s` stemming (upstream folklore; corrupts clinical identifiers/units).
  - Exact matching resolves repeated mentions to SUCCESSIVE occurrences in extraction
    order (langextract's `dp` tiebreak); the hang-prone legacy fuzzy path is not ported.
  - Normalization is length-preserving ONLY (casefold per character) — offsets always
    index the ORIGINAL text.
  - `status=None` (quote not locatable) is the grounding gate: a quote the source cannot
    reconstruct is treated as hallucinated evidence.

ALIGNER_VERSION is recorded with every result so a trace can name the algorithm that
produced its offsets.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Recorded in every grounding trace: results are attributable to the exact aligner
# that produced them. Bump on ANY behavioral change to tokenization or matching.
ALIGNER_VERSION = "ExtrCT-align/1.0"

# letters-run | digits-run | any single non-word symbol. Underscore counts as a symbol.
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+|[^\w\s]|_")

MATCH_EXACT = "match_exact"
MATCH_FUZZY = "match_fuzzy"


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """[(casefolded_token, start, end)] over the ORIGINAL text. Length-preserving only."""
    return [(m.group(0).casefold(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def align(quote: str, source: str, *, source_tokens: list | None = None,
          fuzzy_threshold: float = 0.75, used: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    """Locate one verbatim quote in the source. Returns
    {status, start, end, score, matched_text} — status None when unlocatable."""
    none = {"status": None, "start": None, "end": None, "score": 0.0,
            "aligner": ALIGNER_VERSION}
    q = tokenize(quote or "")
    if not q:
        return {**none, "reason": "empty quote"}
    s = source_tokens if source_tokens is not None else tokenize(source)
    q_texts = [t for t, _, _ in q]
    s_texts = [t for t, _, _ in s]
    m = len(q_texts)

    # --- exact: contiguous token-sequence match; successive-occurrence tiebreak ---
    candidates = [i for i in range(len(s_texts) - m + 1) if s_texts[i:i + m] == q_texts]
    if candidates:
        pick = None
        for i in candidates:
            span = (s[i][1], s[i + m - 1][2])
            if not any(span[0] < ue and span[1] > us for us, ue in (used or [])):
                pick = i
                break
        if pick is None:
            pick = candidates[0]
        start, end = s[pick][1], s[pick + m - 1][2]
        return {"status": MATCH_EXACT, "start": start, "end": end, "score": 1.0,
                "matched_text": source[start:end], "aligner": ALIGNER_VERSION}

    # --- fuzzy: token-level matching blocks; coverage against the quote length ---
    sm = SequenceMatcher(None, s_texts, q_texts, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    matched = sum(b.size for b in blocks)
    coverage = matched / m
    if not blocks or coverage < fuzzy_threshold:
        return {**none, "reason": f"coverage {coverage:.2f} below threshold {fuzzy_threshold}"}
    first, last = blocks[0], blocks[-1]
    start = s[first.a][1]
    end = s[last.a + last.size - 1][2]
    # density guard (langextract's min_density idea): a match smeared over a huge span
    # is not a localization.
    span_tokens = (last.a + last.size) - first.a
    if span_tokens > 0 and matched / span_tokens < 1 / 3:
        return {**none, "reason": f"match density {matched}/{span_tokens} too low"}
    return {"status": MATCH_FUZZY, "start": start, "end": end,
            "score": round(coverage, 4), "matched_text": source[start:end],
            "aligner": ALIGNER_VERSION}


def ground_fields(evidence: dict[str, str], source: str, *,
                  fuzzy_threshold: float = 0.75) -> dict[str, Any]:
    """Align every field's evidence quote. Repeated identical quotes map to successive
    occurrences. Returns {field: alignment} plus a summary."""
    source_tokens = tokenize(source)
    used: list[tuple[int, int]] = []
    out: dict[str, Any] = {}
    for field, quote in evidence.items():
        if quote is None or (isinstance(quote, str) and not quote.strip()):
            out[field] = {"status": None, "start": None, "end": None, "score": 0.0,
                          "reason": "no evidence quote emitted", "aligner": ALIGNER_VERSION}
            continue
        res = align(str(quote), source, source_tokens=source_tokens,
                    fuzzy_threshold=fuzzy_threshold, used=used)
        res["quote"] = str(quote)
        if res["status"] is not None:
            used.append((res["start"], res["end"]))
        out[field] = res
    n = len(out)
    exact = sum(1 for r in out.values() if r["status"] == MATCH_EXACT)
    fuzzy = sum(1 for r in out.values() if r["status"] == MATCH_FUZZY)
    return {"fields": out, "summary": {
        "fields": n, "exact": exact, "fuzzy": fuzzy, "unlocated": n - exact - fuzzy,
        "h2_clean": (n - exact - fuzzy) == 0, "aligner": ALIGNER_VERSION,
    }}
