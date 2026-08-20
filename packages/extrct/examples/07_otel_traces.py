"""Built-in OpenTelemetry traces, demonstrated offline in 30 seconds.

    pip install "extrct[otel]" opentelemetry-sdk
    python examples/07_otel_traces.py

Prints the spans extrct emits - the extract root, the nested per-call `chat` span
with GenAI attributes - to the console. Point the exporter at your backend instead
(e.g. self-hosted Langfuse >= 3.22) and the SAME spans appear there with model,
tokens and cost mapped automatically; see docs/observability.md for the OTLP setup.

The privacy contract holds in traces exactly as in the run log: identities, hashes,
counts and statuses - never input text, extracted values, or quotes. Look at the
printed attributes and check for yourself.
"""

import asyncio
import json
from pathlib import Path

import httpx

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "offline-demo.yaml"
INPUT_TEXT = ("Echocardiography performed today. LVEF measured at 55 percent. "
              "Mild mitral regurgitation noted. Plan: follow-up echo in 12 months.")
# -----------------------------------------------------------------

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
except ImportError:
    raise SystemExit('this example needs the SDK: pip install "extrct[otel]" opentelemetry-sdk')

# Exporter setup is the HOST's job, never the library's. Console here; for Langfuse:
#   OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:3000/api/public/otel/v1/traces
#   OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic <base64(pk:sk)>"
# and use opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter.
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

from extrct import Extractor  # noqa: E402  (import after provider setup is NOT required; just tidy)

ANSWER = json.dumps({
    "lvef": 55.0, "mitral_regurgitation": "mild", "followup_months": 12,
    "_evidence": {"lvef": "LVEF measured at 55 percent",
                  "mitral_regurgitation": "Mild mitral regurgitation noted",
                  "followup_months": "follow-up echo in 12 months"},
})


def mock_model(request: httpx.Request) -> httpx.Response:
    logprobs = [{"token": ANSWER[i:i + 4], "logprob": -0.05} for i in range(0, len(ANSWER), 4)]
    return httpx.Response(200, json={
        "model": "demo-model", "message": {"role": "assistant", "content": ANSWER},
        "done": True, "done_reason": "stop",
        "prompt_eval_count": 120, "eval_count": 60, "logprobs": logprobs,
    })


async def main() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_model)) as http:
        ex = Extractor.from_yaml(str(CONFIG_FILE), http=http)
        result = await ex.extract(INPUT_TEXT, input_id="otel-demo-1")
    print(f"\nextraction: {result.status}; spans above show the trace "
          f"(join key extrct.run_uid = {result.run_uids[0]})")


if __name__ == "__main__":
    asyncio.run(main())
