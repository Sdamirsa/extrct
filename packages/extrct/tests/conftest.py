"""Shared fixtures: schemas, synthetic provider payloads, and mock transports.

Everything here is offline — the suite proves behaviour against synthetic payloads
shaped exactly like the measured provider responses, so it runs in CI with no model,
no key, and no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from extrct.schema import schema_envelope

ECHO_VARIABLES = [
    {"name": "lvef", "type": "float", "description": "LVEF percent",
     "constraints": {"ge": 0, "le": 100}},
    {"name": "severity", "type": "str", "options": ["mild", "moderate", "severe"],
     "required": False},
]


@pytest.fixture
def echo_envelope() -> dict:
    return schema_envelope(ECHO_VARIABLES, root_name="echo")


def ollama_payload(content: str, *, done_reason: str = "stop", prompt_eval: int = 40,
                   eval_count: int = 20, logprobs: list | None = None) -> dict[str, Any]:
    """A response body shaped like Ollama's native /api/chat answer."""
    body: dict[str, Any] = {
        "model": "test-model",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": prompt_eval,
        "eval_count": eval_count,
        "total_duration": 123456789,
    }
    if logprobs is not None:
        body["logprobs"] = logprobs
    return body


def openrouter_payload(content: str, *, finish_reason: str = "stop",
                       provider: str = "TestServe", cost: float | None = 0.00021,
                       logprobs: dict | None = None) -> dict[str, Any]:
    """A response body shaped like OpenRouter's chat completions answer."""
    choice: dict[str, Any] = {
        "message": {"role": "assistant", "content": content},
        "finish_reason": finish_reason,
        "native_finish_reason": finish_reason,
    }
    if logprobs is not None:
        choice["logprobs"] = logprobs
    usage: dict[str, Any] = {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-123", "model": "test/model", "provider": provider,
            "choices": [choice], "usage": usage}


def tokens_for(pieces: list[tuple[str, float]] | list[str],
               top: dict[str, list[tuple[str, float]]] | None = None) -> list[dict]:
    """Ollama-native logprobs list from (token, logprob) pieces. `top` maps a token
    string to its top_logprobs alternatives."""
    out = []
    for p in pieces:
        tok, lp = (p, -0.05) if isinstance(p, str) else p
        entry: dict[str, Any] = {"token": tok, "logprob": lp}
        alts = (top or {}).get(tok)
        if alts:
            entry["top_logprobs"] = [{"token": t, "logprob": l} for t, l in alts]
        out.append(entry)
    return out


def naive_tokens(content: str, *, size: int = 4, lp: float = -0.05) -> list[dict]:
    """Chop content into fixed-size 'tokens' whose concatenation equals the content —
    any tokenization satisfies the certainty invariant, so this is enough for tests."""
    return [{"token": content[i:i + size], "logprob": lp} for i in range(0, len(content), size)]


def mock_http(handler) -> httpx.AsyncClient:
    """An AsyncClient whose requests never leave the process."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(body: dict, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


@pytest.fixture
def ollama_client_payload() -> dict:
    """The client payload the pipeline consumes, ollama flavour."""
    from extrct.client_model import build_client_payload, client_definition

    defn = client_definition("ollama", {"model": "test-model", "base_url": "http://mock"})
    return build_client_payload(defn)


@pytest.fixture
def openrouter_client_payload() -> dict:
    """OpenRouter client payload with the egress gate satisfied (synthetic data,
    explicit direct route) and an endpoint pin."""
    from extrct.client_model import build_client_payload, client_definition

    defn = client_definition("openrouter", {
        "model": "test/model", "endpoint_tag": "test/model@TestServe",
        "data_classification": "synthetic", "egress_route": "direct",
        "allow_direct_egress": True})
    return build_client_payload(defn)
