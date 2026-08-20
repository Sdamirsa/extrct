"""The open/closed proof: a third-party provider registers and immediately works
everywhere — client definitions, config overrides, the pipeline — with zero core
changes."""

from dataclasses import dataclass, field

import pytest

from extrct import client_model
from extrct.config import apply_client_overrides, client_fields
from extrct.hashing import content_uid, sha256_text
from extrct.providers import (
    Provider,
    get_provider,
    provider_names,
    register_provider,
)


@dataclass(frozen=True)
class EchoSpec:
    base_url: str = "http://echo"
    model: str = ""
    mode: str = "json_schema"
    seed: int = 0
    temperature: float = 0.0
    timeout_s: int = 30
    api_key: str = ""
    capability: dict = field(default_factory=dict)


class EchoProvider(Provider):
    name = "echotest"
    engine = "echotest"
    spec_cls = EchoSpec
    envelope_fields = {}
    allowed_values = {"mode": ("json_schema", "prompted")}
    default_concurrency = 2
    capabilities = frozenset({"structured_outputs", "prompt_logprobs"})

    def build_request(self, spec, text, schema):
        return {"model": spec.model, "messages": [{"role": "user", "content": text}]}, None

    def request_record(self, spec, text, schema, body, resolution=None):
        return {"record_version": "1.0.0",
                "request_uid": content_uid({"provider": self.name, "model": spec.model}),
                "target": {"provider": self.name, "base_url": spec.base_url,
                           "model_on_wire": spec.model},
                "schema": {"schema_uid": None, "encoding": None, "mode": spec.mode},
                "input": {"input_sha256": sha256_text(text), "input_chars": len(text),
                          "input_text": None},
                "capability": {"gate_effective": False}}

    def read_response(self, spec, payload, **kwargs):
        content = payload.get("content", "")
        return {"record_version": "1.0.0", "layer": "request",
                "finish": {}, "usage": {},
                "content": {"raw_sha256": sha256_text(content), "raw_chars": len(content),
                            "raw_text": content},
                "silent_failure_flags": [], "terminal_failure": False,
                "final_status_hint": "ok", "logprobs": payload.get("logprobs")}

    def target_url(self, spec, envelope=None):
        return f"{spec.base_url}/v1/echo"


@pytest.fixture
def echo_registered():
    register_provider(EchoProvider(), replace=True)
    yield
    # no unregister API on purpose (registrations are process-lifetime); replace is enough


def test_unknown_provider_lists_known():
    with pytest.raises(ValueError, match="registered:"):
        get_provider("no-such-backend")


def test_duplicate_registration_is_loud(echo_registered):
    with pytest.raises(ValueError, match="already registered"):
        register_provider(EchoProvider())


def test_nameless_provider_refused():
    class Nameless(EchoProvider):
        name = ""

    with pytest.raises(ValueError, match="has no name"):
        register_provider(Nameless())


def test_registered_provider_visible(echo_registered):
    assert "echotest" in provider_names()
    assert get_provider("echotest").default_concurrency == 2


def test_client_definition_works_for_new_provider(echo_registered):
    doc = client_model.client_definition("echotest", {"model": "echo-1", "seed": 7})
    assert doc["provider"] == "echotest"
    assert doc["settings"]["seed"] == 7
    assert doc["settings"]["temperature"] == 0.0  # defaults filled
    assert "api_key" not in doc["settings"]       # forbidden keys never in the document
    payload = client_model.build_client_payload(doc)
    assert payload["provider"] == "echotest"


def test_new_provider_vocab_enforced(echo_registered):
    with pytest.raises(ValueError, match="not one of"):
        client_model.client_definition("echotest", {"mode": "freeform"})


def test_config_overrides_work_for_new_provider(echo_registered):
    assert "seed" in client_fields("echotest")
    kwargs, applied = apply_client_overrides("echotest", {"model": "echo-1"},
                                             {"echotest.seed": 9, "client.temperature": 0.5})
    assert kwargs["seed"] == 9
    assert kwargs["temperature"] == 0.5
    assert applied == ["seed", "temperature"]


async def test_pipeline_executes_through_new_provider(echo_registered):
    import httpx

    from extrct.pipeline import execute_single

    def handler(req):
        assert req.url.path == "/v1/echo"
        return httpx.Response(200, json={"content": '{"answer": 42}'})

    client = {"provider": "echotest", "spec": {"model": "echo-1", "base_url": "http://echo"}}
    envelope = {"schema": {"type": "object", "properties": {"answer": {"type": "integer"}},
                           "required": ["answer"], "additionalProperties": False}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        out = await execute_single(client, envelope, "question", http=http)
    assert out["final_status"] == "ok"
    assert out["obj"] == {"answer": 42}
