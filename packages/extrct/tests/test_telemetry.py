"""OTel instrumentation: spans emitted, correctly nested, and content-free.

Skips cleanly when the otel packages are absent - the rest of the suite then also
exercises the no-op path (every instrumented call site runs without a tracer)."""

import json

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from extrct import Extractor, telemetry  # noqa: E402

from conftest import mock_http, naive_tokens, ollama_payload  # noqa: E402

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)  # once per process; ProxyTracer binds lazily


SECRET_NOTE = ("SENTINEL_PATIENT_TEXT Echocardiography today. LVEF measured at 55 percent. "
               "Mild regurgitation noted.")
CONTENT = json.dumps({
    "lvef": 55.0, "severity": "mild",
    "_evidence": {"lvef": "LVEF measured at 55 percent", "severity": "Mild regurgitation noted"},
})


def _job(tmp_path):
    return {
        "provider": {"provider": "ollama", "model": "test-model", "base_url": "http://mock"},
        "schema": {"root_name": "echo", "variables": [
            {"name": "lvef", "type": "float"},
            {"name": "severity", "type": "str",
             "options": ["mild", "moderate", "severe"], "required": False},
        ]},
        "grounding": {"enabled": True},
        "certainty": {"enabled": True},
        "storage": {"backend": "sqlite", "path": str(tmp_path / "runs.sqlite")},
    }


def _handler(req):
    return httpx.Response(200, json=ollama_payload(CONTENT, logprobs=naive_tokens(CONTENT)))


@pytest.fixture
def spans(tmp_path):
    _EXPORTER.clear()

    async def run():
        async with mock_http(_handler) as http:
            ex = Extractor(_job(tmp_path), http=http)
            return await ex.extract(SECRET_NOTE, input_id="t-1")

    import asyncio

    result = asyncio.run(run())
    return result, _EXPORTER.get_finished_spans()


def test_enabled_reports_api_presence():
    assert telemetry.enabled() is True


def test_extract_root_and_chat_span_emitted(spans):
    result, finished = spans
    names = [s.name for s in finished]
    assert "extrct.extract" in names
    assert "chat test-model" in names


def test_genai_semconv_and_identity_attributes(spans):
    result, finished = spans
    chat = next(s for s in finished if s.name == "chat test-model")
    a = dict(chat.attributes)
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.provider.name"] == "ollama"
    assert a["gen_ai.request.model"] == "test-model"
    assert a["gen_ai.usage.input_tokens"] == 40   # prompt_eval_count mapped
    assert a["gen_ai.usage.output_tokens"] == 20  # eval_count mapped
    assert a["extrct.run_uid"] == result.run_uids[0]  # the trace <-> run-log join key
    assert a["extrct.final_status"] == "ok"
    root = next(s for s in finished if s.name == "extrct.extract")
    assert dict(root.attributes)["extrct.job_uid"] == result.job_uid
    assert dict(root.attributes)["extrct.status"] == "ok"


def test_chat_span_nests_under_extract_root(spans):
    _, finished = spans
    root = next(s for s in finished if s.name == "extrct.extract")
    chat = next(s for s in finished if s.name == "chat test-model")
    assert chat.parent is not None
    assert chat.parent.span_id == root.context.span_id
    assert chat.context.trace_id == root.context.trace_id  # one trace, two layers


def test_no_content_ever_rides_a_span(spans):
    result, finished = spans
    blob = " ".join(f"{s.name} {dict(s.attributes)}" for s in finished)
    assert "SENTINEL_PATIENT_TEXT" not in blob     # input text never leaves as telemetry
    assert "55 percent" not in blob                # nor evidence quotes
    assert "mild" not in blob.replace("test-model", "")  # nor extracted values
    assert result.input_sha256 in blob             # the hash IS allowed - it is the join key


async def test_transport_error_marks_span(tmp_path):
    _EXPORTER.clear()

    async with mock_http(lambda r: httpx.Response(500, json={"error": "kaboom"})) as http:
        ex = Extractor(_job(tmp_path), http=http)
        result = await ex.extract(SECRET_NOTE)
    assert result.status == "error"
    chat = next(s for s in _EXPORTER.get_finished_spans() if s.name == "chat test-model")
    assert dict(chat.attributes)["extrct.final_status"] == "error"
    assert chat.status.status_code.name == "ERROR"
