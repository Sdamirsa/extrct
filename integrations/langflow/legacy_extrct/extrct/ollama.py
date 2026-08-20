"""Ollama native chat API — request builder and response reader.

Uses POST /api/chat with `format` carrying a full JSON Schema. NOT /v1 (decision D-a):
/v1 has three measured shapes that return HTTP 200 with unconstrained prose, and it
cannot reach `options` at all. See docs/extraction-stack/api-notes.md.

Every sampling key is emitted explicitly. Ollama never errors on an unknown `options`
key, so always-emit is strictly safer than relying on defaults - and it defeats the
Modelfile inheritance trap, where qwen3:0.6b ships temperature 0.6 and qwen3.5:9b ships
temperature 1.0, making two models "at default" incomparable (S23).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hashing import canonical_json, content_uid, sha256_text

# Sampling keys we always send. Order is irrelevant to the wire but fixed for hashing.
SAMPLING_KEYS = (
    "seed",
    "temperature",
    "top_p",
    "top_k",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "min_p",
    "repeat_last_n",
)


@dataclass(frozen=True)
class OllamaSpec:
    """Everything about a call except the text. Frozen so it can be hashed and reused."""

    base_url: str = "http://host.docker.internal:11434"
    model: str = ""
    model_digest: str | None = None

    # Structured output
    mode: str = "json_schema"  # json_schema | json_object | prompted
    schema_in_prompt: bool = False

    # Context and length - the two silent failures (S20, S21)
    num_ctx: int = 8192
    num_predict: int = 1024

    # Sampling. Defaults deliberately differ from the baked Modelfile values.
    seed: int = 42
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 0
    repeat_penalty: float = 1.0
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    min_p: float = 0.0
    repeat_last_n: int = 64

    # Runtime
    # -1 = AUTO: the key is omitted and the server offloads to GPU when it can. This is
    # the right default now that the GPU host serves over the direct link — sending 0 there
    # would silently force a 27B model onto CPU. 0 remains available for the laptop,
    # whose CUDA backend is broken. Per-host runtime knob, not a sampling knob.
    num_gpu: int = -1
    num_thread: int = 0
    think: str = "false"  # OFF: changes the answer, and burns num_predict pre-grammar (S24)
    keep_alive: str = "30m"  # TOP-LEVEL, never inside options (S25)
    unload_before_run: bool = False

    # Logprobs (measured 2026-08-06 on Ollama 0.30.0): TOP-LEVEL `logprobs` must be a
    # BOOLEAN with `top_logprobs` as the count — an int `logprobs: 5` is a 400, and
    # placing either inside `options` is SILENTLY IGNORED.
    logprobs: bool = False
    top_logprobs: int = 0

    # Guards
    fail_on_truncation: bool = True
    fail_on_context_pressure: bool = True
    verify_loaded_ctx: bool = True

    system_prompt: str = ""
    timeout_s: int = 600
    extra_headers: dict = field(default_factory=dict)


def _options(spec: OllamaSpec) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "num_ctx": int(spec.num_ctx),
        "num_predict": int(spec.num_predict),
    }
    if spec.num_gpu >= 0:  # -1 = auto: omit and let the serving host decide (GPU if able)
        opts["num_gpu"] = int(spec.num_gpu)
    if spec.num_thread:
        opts["num_thread"] = int(spec.num_thread)
    for k in SAMPLING_KEYS:
        opts[k] = getattr(spec, k)
    # `stop` is deliberately never set: a stop-truncation reports done_reason "stop",
    # which is indistinguishable from success (S22).
    return opts


def _think_value(raw: str) -> bool | str:
    if raw in ("false", "", None):
        return False
    if raw == "true":
        return True
    return raw  # "low" | "medium" | "high" | "max"


def build_request(spec: OllamaSpec, text: str, schema: dict | None) -> dict[str, Any]:
    """Return the exact body that will be POSTed. Pure - no I/O, fully testable.

    `schema` accepts a bare JSON Schema or a `schema_envelope()`; the envelope is preferred
    because it carries the encoding axis alongside the schema that produced it.
    """
    from .schema import unwrap_schema

    schema, _encoding = unwrap_schema(schema)
    messages: list[dict[str, str]] = []

    system = spec.system_prompt or ""
    # mode="prompted" IMPLIES the schema goes into the prompt — without this, prompted mode
    # sent the schema NOWHERE (caught live on the GPU host: pure prose back). schema_in_prompt
    # additionally allows belt-and-braces prompting alongside format enforcement.
    if (spec.schema_in_prompt or spec.mode == "prompted") and schema is not None:
        system = (system + "\n\nRespond with JSON matching this schema:\n" + canonical_json(schema)).strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": text})

    body: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
        "stream": False,
        "think": _think_value(spec.think),
        "keep_alive": spec.keep_alive,
        "options": _options(spec),
    }

    if spec.mode == "json_schema":
        if schema is None:
            msg = "mode='json_schema' requires a schema"
            raise ValueError(msg)
        body["format"] = schema
    elif spec.mode == "json_object":
        body["format"] = "json"
    # "prompted" sends no format at all.

    if spec.logprobs:
        body["logprobs"] = True
        if spec.top_logprobs > 0:
            body["top_logprobs"] = min(int(spec.top_logprobs), 20)

    return body


def request_record(spec: OllamaSpec, text: str, schema: dict | None, body: dict) -> dict[str, Any]:
    """The replay unit. Hashes the real body; stores the input only as a digest."""
    from .schema import unwrap_schema

    schema, encoding = unwrap_schema(schema)
    schema_uid = content_uid(schema) if schema is not None else None

    redacted = dict(body)
    redacted["messages"] = [
        {"role": m["role"], "content_sha256": sha256_text(m["content"]), "content_chars": len(m["content"])}
        for m in body["messages"]
    ]
    if "format" in redacted and isinstance(redacted["format"], dict):
        redacted["format"] = {"__ref": f"schema_uid:{schema_uid}"}

    hashable = {
        "target": {"provider": "ollama", "model": spec.model, "model_digest": spec.model_digest},
        "schema_uid": schema_uid,
        "mode": spec.mode,
        "wire": redacted,
    }
    return {
        "record_version": "1.0.0",
        "request_uid": content_uid(hashable),
        "target": {
            "provider": "ollama",
            "base_url": spec.base_url,
            "path": "/api/chat",
            "model_on_wire": spec.model,
            "model_digest": spec.model_digest,
        },
        # `encoding` is a declared axis: identical variable rows under different encodings
        # produce different extraction completeness, so it must be groupable in analysis.
        "schema": {
            "schema_uid": schema_uid,
            "encoding": encoding,
            "mode": spec.mode,
            "schema_in_prompt": spec.schema_in_prompt,
        },
        "input": {"input_sha256": sha256_text(text), "input_chars": len(text), "input_text": None},
        "wire_body_redacted": redacted,
        "wire_body_sha256": sha256_text(canonical_json(body)),
        "capability": {"gate_effective": False},  # Ollama never rejects unknown keys
        "policy": {
            "think": spec.think,
            "num_ctx": spec.num_ctx,
            "num_predict": spec.num_predict,
            "num_gpu": spec.num_gpu,
            "fail_on_truncation": spec.fail_on_truncation,
            "fail_on_context_pressure": spec.fail_on_context_pressure,
        },
    }


def read_response(spec: OllamaSpec, payload: dict, *, loaded_ctx: int | None = None) -> dict[str, Any]:
    """Turn a 200 body into a response record, and decide whether it is actually a failure.

    Ollama returns HTTP 200 for both truncation and context pressure, so the flags below
    are the only thing standing between a truncated record and the repair ladder
    brace-balancing it into a plausible wrong answer (S21).
    """
    content = (payload.get("message") or {}).get("content", "") or ""
    done_reason = payload.get("done_reason")
    prompt_eval = payload.get("prompt_eval_count")
    eval_count = payload.get("eval_count")

    flags: list[str] = []
    if done_reason == "length":
        flags.append("truncated_by_length")
    if loaded_ctx and prompt_eval and prompt_eval >= loaded_ctx * 0.95:
        flags.append("context_pressure")
    if loaded_ctx and loaded_ctx != spec.num_ctx:
        flags.append("context_loaded_mismatch")
    if spec.think not in ("false", "", None):
        flags.append("thinking_enabled")

    terminal = (spec.fail_on_truncation and "truncated_by_length" in flags) or (
        spec.fail_on_context_pressure and "context_pressure" in flags
    )

    return {
        "record_version": "1.0.0",
        "layer": "request",
        "finish": {"done_reason": done_reason},
        "usage": {
            "prompt_eval_count": prompt_eval,
            "eval_count": eval_count,
            "total_duration_ns": payload.get("total_duration"),
            "loaded_context_length": loaded_ctx,
        },
        "content": {
            "raw_sha256": sha256_text(content),
            "raw_chars": len(content),
            "raw_text": content,
        },
        "silent_failure_flags": flags,
        "terminal_failure": terminal,
        "enforcement_verified": {"enum": True, "required": True, "numeric_range": False},
        "final_status_hint": "invalid" if terminal else "ok",
        # Native shape: a top-level list of {token, logprob, bytes, top_logprobs}.
        "logprobs": payload.get("logprobs"),
    }
