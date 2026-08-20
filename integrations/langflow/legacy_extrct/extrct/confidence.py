"""Per-field certainty from token logprobs — pure, dual-caller (replayability/independent measurement).

Statistics per field (decisions in docs/system-arch/extraction-stack/logprobs-design.md + amendments):
`mean` (geometric per-token, primary), `joint` (within-field-type ranker), `min`
(secondary OR-gate), `first_token`, `margin` (top-1 minus top-2 at the first value
token), and for enum fields an empirical first-token posterior over the option set.

Alignment is by DECODED CHARACTERS, never the provider `bytes` field — both self-hosted
engines corrupt it (measured; certainty-research.md). The hard invariant is that the
concatenated token strings equal the message content; on violation the result is an
error record, never a silent misalignment.

Engine sentinels handled before any exp(): vLLM emits exactly -9999.0 for masked/absent
(never -inf); the Ollama path can underflow to -inf. Both are excluded from statistics
and counted in `masked_tokens`.
"""

from __future__ import annotations

import math
from typing import Any

SENTINEL_FLOOR = -9998.0  # anything at or below this is a masked/absent marker


# ------------------------------------------------------------------ provider shapes
def normalize_logprobs(payload: dict) -> list[dict] | None:
    """One shape for both providers: [{token, logprob, top: [(text, logprob), ...]}]."""
    raw = payload.get("logprobs")
    if isinstance(raw, dict):  # openrouter nests once more
        raw = raw.get("content")
    if raw is None:
        choices = payload.get("choices") or [{}]
        lp = (choices[0] or {}).get("logprobs")
        raw = (lp or {}).get("content") if isinstance(lp, dict) else lp
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        out.append({
            "token": t.get("token") or "",
            "logprob": t.get("logprob"),
            "top": [(a.get("token") or "", a.get("logprob"))
                    for a in (t.get("top_logprobs") or []) if isinstance(a, dict)],
        })
    return out or None


def content_text(payload: dict) -> str:
    msg = payload.get("message")
    if isinstance(msg, dict):  # ollama native
        return msg.get("content") or ""
    choices = payload.get("choices") or [{}]
    return ((choices[0] or {}).get("message") or {}).get("content") or ""


# ------------------------------------------------------------------ JSON value spans
class _SpanScanner:
    """Position-aware scan of the FIRST JSON document in text.

    Records, per leaf (string/number/bool/null), the character span of its VALUE —
    for strings the content between the quotes, so structural quote characters stay
    structural. Paths are dotted, arrays indexed: study.findings[0].
    """

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.spans: dict[str, tuple[int, int]] = {}

    def scan(self) -> dict[str, tuple[int, int]]:
        start = self.text.find("{")
        if start < 0:
            msg = "no JSON object found in text"
            raise ValueError(msg)
        self._value(start, "")
        return self.spans

    def _ws(self, i: int) -> int:
        while i < self.n and self.text[i] in " \t\r\n":
            i += 1
        return i

    def _value(self, i: int, path: str) -> int:
        i = self._ws(i)
        if i >= self.n:
            msg = f"unexpected end of JSON at {i}"
            raise ValueError(msg)
        c = self.text[i]
        if c == "{":
            return self._object(i, path)
        if c == "[":
            return self._array(i, path)
        if c == '"':
            end = self._string_end(i)
            if path:
                self.spans[path] = (i + 1, end - 1)  # content between the quotes
            return end
        # number / true / false / null
        j = i
        while j < self.n and self.text[j] not in ",}] \t\r\n":
            j += 1
        if path:
            self.spans[path] = (i, j)
        return j

    def _string_end(self, i: int) -> int:
        j = i + 1
        while j < self.n:
            if self.text[j] == "\\":
                j += 2
                continue
            if self.text[j] == '"':
                return j + 1
            j += 1
        msg = f"unterminated string at {i}"
        raise ValueError(msg)

    def _object(self, i: int, path: str) -> int:
        i = self._ws(i + 1)
        if i < self.n and self.text[i] == "}":
            return i + 1
        while True:
            i = self._ws(i)
            # Bounds FIRST: truncated content (num_predict cut mid-object) used to raise
            # IndexError here, escaping field_confidence's ValueError guard and surfacing as
            # "IndexError: string index out of range" instead of the designed error record
            # (measured 2026-08-14). Truncation is a first-class failure mode on this path.
            if i >= self.n:
                msg = f"unexpected end of JSON at {i}"
                raise ValueError(msg)
            if self.text[i] != '"':
                msg = f"expected object key at {i}"
                raise ValueError(msg)
            key_end = self._string_end(i)
            key = self.text[i + 1:key_end - 1]
            i = self._ws(key_end)
            if i >= self.n:
                msg = f"unexpected end of JSON at {i}"
                raise ValueError(msg)
            if self.text[i] != ":":
                msg = f"expected ':' at {i}"
                raise ValueError(msg)
            child = f"{path}.{key}" if path else key
            i = self._value(i + 1, child)
            i = self._ws(i)
            if i < self.n and self.text[i] == ",":
                i += 1
                continue
            if i < self.n and self.text[i] == "}":
                return i + 1
            msg = f"expected ',' or '}}' at {i}"
            raise ValueError(msg)

    def _array(self, i: int, path: str) -> int:
        i = self._ws(i + 1)
        if i < self.n and self.text[i] == "]":
            return i + 1
        idx = 0
        while True:
            i = self._value(i, f"{path}[{idx}]")
            idx += 1
            i = self._ws(i)
            if i < self.n and self.text[i] == ",":
                i += 1
                continue
            if i < self.n and self.text[i] == "]":
                return i + 1
            msg = f"expected ',' or ']' at {i}"
            raise ValueError(msg)


# ------------------------------------------------------------------ schema lookup
def _enum_options(schema: dict | None, path: str) -> list[str] | None:
    """Follow a dotted/indexed path into the schema; return enum options if declared."""
    if not schema:
        return None
    node = schema
    for seg in path.split("."):
        name, _, idx = seg.partition("[")
        props = node.get("properties") or {}
        if name not in props:
            return None
        node = props[name]
        if idx:  # the segment was indexed (an array element): descend into items
            node = node.get("items") or node
    enum = node.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(o, str) for o in enum):
        return enum
    # strict_nullable wraps in anyOf/type unions; enum usually stays top-level, but check anyOf
    for sub in node.get("anyOf") or []:
        e = sub.get("enum")
        if isinstance(e, list) and e and all(isinstance(o, str) for o in e):
            return e
    return None


# ------------------------------------------------------------------ statistics
def _usable(lp) -> bool:
    return isinstance(lp, (int, float)) and lp > SENTINEL_FLOOR and math.isfinite(lp)


def _enum_posterior(first_tok: dict, prefix_len: int, options: list[str]) -> dict:
    """Empirical posterior over the option set from the first value-token's top-k.

    Tokenizer-free by design: the emission's own alternatives ARE the ground truth about
    what the model could have said here. Each alternative must share the sampled token's
    structural prefix (e.g. the opening quote); its remainder is matched against option
    prefixes. A remainder matching two options is a collision -> posterior refused
    (downgrade to the string statistics), never silently wrong.
    """
    sampled = first_tok["token"]
    prefix = sampled[:prefix_len]
    alts = list(first_tok["top"] or [])
    if not any(t == sampled for t, _ in alts):
        alts.append((sampled, first_tok["logprob"]))
    if len(alts) < 2:
        # With only the sampled token visible, "renormalizing" collapses to 1.0 by
        # construction - first_token in disguise, dangerously readable as certainty.
        return {"available": False,
                "reason": "only the sampled token was visible; set Top Logprobs > 0 "
                          "on the client to get a real posterior over the options"}
    mass: dict[str, float] = {}
    other = 0.0
    for text, lp in alts:
        if not _usable(lp):
            continue
        p = math.exp(lp)
        if not text.startswith(prefix):
            other += p
            continue
        rem = text[prefix_len:]
        if not rem:
            other += p
            continue
        hits = [o for o in options if o.startswith(rem) or rem.startswith(o)]
        if len(hits) > 1:
            return {"available": False, "reason": f"first-token collision: {rem!r} matches {hits}"}
        if len(hits) == 1:
            mass[hits[0]] = mass.get(hits[0], 0.0) + p
        else:
            other += p
    total = sum(mass.values())
    if total <= 0:
        return {"available": False, "reason": "no alternative matched any option"}
    return {
        "available": True,
        "posterior": {o: round(p / total, 6) for o, p in sorted(mass.items(), key=lambda kv: -kv[1])},
        "observed_options": sorted(mass),
        "unmatched_mass": round(other, 6),
        "n_alternatives": len(alts),
    }


def field_confidence(payload: dict, schema: dict | None = None, *,
                     enum_posterior: bool = True) -> dict[str, Any]:
    """Per-field certainty statistics from a provider response payload. Pure.

    Returns {"ok": bool, "fields": {path: stats}, "diagnostics": {...}} — or an error
    record when the token-concatenation invariant fails (never a silent misalignment).
    """
    toks = normalize_logprobs(payload)
    if not toks:
        return {"ok": False, "error": "no logprobs in payload (turn on Token Logprobs on the client)"}
    content = content_text(payload)
    concat = "".join(t["token"] for t in toks)

    # The invariant. Some engines emit content with thinking/whitespace trimmed; tolerate
    # ONLY an exact prefix/suffix relationship, else refuse.
    if concat != content:
        if content and concat.endswith(content):
            offset = len(concat) - len(content)  # tokens include a prefix (e.g. thinking) — align on concat
            base = concat
        elif content and content.startswith(concat):
            # Tokens cover only a PREFIX of the content: the MTP signature (first-token-only
            # logprobs, measured in the register). Refusing is correct; naming it saves an hour.
            return {"ok": False, "error": f"logprobs cover only the first {len(concat)} of "
                    f"{len(content)} chars — MTP model? MTP variants emit first-token-only "
                    "logprobs (api-notes.md); use a non-MTP model for certainty work.",
                    "concat_chars": len(concat), "content_chars": len(content)}
        else:
            return {"ok": False, "error": "token_concat_mismatch: joined tokens != message content; "
                    "refusing to align (see certainty-research.md invariant)",
                    "concat_chars": len(concat), "content_chars": len(content)}
    else:
        base, offset = content, 0

    # char -> token index map over `base`
    bounds = []  # (start, end) per token
    pos = 0
    for t in toks:
        bounds.append((pos, pos + len(t["token"])))
        pos += len(t["token"])

    try:
        json_start = base.index("{", offset if offset else 0)
        spans = _SpanScanner(base[json_start:]).scan()
        spans = {p: (s + json_start, e + json_start) for p, (s, e) in spans.items()}
    except (ValueError, IndexError) as exc:  # IndexError: belt to the scanner's braces
        return {"ok": False, "error": f"could not scan JSON for value spans: {exc}"}

    fields: dict[str, Any] = {}
    for path, (s, e) in spans.items():
        overlapping = [i for i, (ts, te) in enumerate(bounds) if ts < e and te > s]
        if not overlapping:
            fields[path] = {"k": 0, "error": "no tokens overlap value span"}
            continue
        lps = [toks[i]["logprob"] for i in overlapping]
        usable = [lp for lp in lps if _usable(lp)]
        masked = len(lps) - len(usable)
        st: dict[str, Any] = {"k": len(overlapping), "masked_tokens": masked,
                              "span": [s, e], "value_text": base[s:e]}
        if usable:
            st["mean"] = round(math.exp(sum(usable) / len(usable)), 6)
            st["joint"] = round(math.exp(sum(usable)), 6)
            st["min"] = round(math.exp(min(usable)), 6)
        first = toks[overlapping[0]]
        if _usable(first["logprob"]):
            st["first_token"] = round(math.exp(first["logprob"]), 6)
        top_probs = sorted((math.exp(lp) for t, lp in (first["top"] or []) if _usable(lp)), reverse=True)
        if len(top_probs) >= 2:
            st["margin"] = round(top_probs[0] - top_probs[1], 6)
        if enum_posterior:
            options = _enum_options(schema, path)
            if options:
                prefix_len = s - bounds[overlapping[0]][0]
                st["enum_posterior"] = _enum_posterior(first, prefix_len, options)
        fields[path] = st

    return {"ok": True, "fields": fields,
            "diagnostics": {"tokens": len(toks), "invariant": "exact" if offset == 0 else f"suffix(+{offset})",
                            "json_start": json_start}}


def mask_state(engine: str, mode: str | None) -> str:
    """Source-verified engine semantics (certainty-research.md). Never pool across states."""
    if mode == "prompted" or mode is None:
        return "unmasked"
    if engine == "ollama":
        return "pre_mask"       # llama.cpp: grammar never touches reported logprobs
    if engine == "vllm":
        return "post_mask"      # bitmask applied before the sampler's "raw" logprobs
    return "unknown_provider_engine"  # openrouter: heterogeneous serving engines
