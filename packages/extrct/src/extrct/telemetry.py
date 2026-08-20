"""OpenTelemetry instrumentation - built in, optional, no-op by default.

Division of labour: the RUN LOG is the record of truth (evidence, replayable,
append-only); traces are the LIVE VIEW (waterfalls, error rates, correlation with a
host application). Spans therefore carry identities and outcomes - run_uid,
schema_uid, statuses, token counts, flags - and NEVER content: no input text, no
extracted values, no evidence quotes, no keys. Joining a trace to its full evidence
is one lookup: extrct.run_uid -> extraction_run.

Zero-configuration contract, in increasing order of setup:

1. `opentelemetry-api` not installed          -> every span() here is a local no-op.
2. API installed, no SDK configured           -> OTel's own no-op tracer (same result).
3. SDK + exporter configured (env vars or code) -> spans flow. When extrct runs inside
   a host that already traces with OTel (the Langfuse Python SDK v3 is OTel-based),
   extrct spans nest under the host's trace through ordinary context propagation -
   one trace, two layers, no double counting.

Attribute names follow the OTel GenAI semantic conventions where they apply
(`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` /
`output_tokens`, ...). The conventions are still marked experimental upstream, so the
names are pinned HERE and changed only deliberately; `gen_ai.system` is emitted as a
legacy alias of `gen_ai.provider.name` because deployed backends still read it.
Everything the conventions do not cover uses the `extrct.` prefix.

Verified 2026-08-20: Langfuse >= 3.22 (self-hosted) ingests OTLP over HTTP at
`/api/public/otel` (Basic auth; no gRPC) and maps `gen_ai.request.model` /
`gen_ai.usage.*` onto its native model/usage fields - see docs/observability.md for
the exporter setup.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

try:  # optional by design - the library must not grow a hard dependency for this
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status as _Status, StatusCode as _StatusCode

    _TRACER = _otel_trace.get_tracer("extrct")
except ImportError:  # pragma: no cover - exercised only without the otel extra
    _otel_trace = None
    _Status = None
    _StatusCode = None
    _TRACER = None


class _NoopSpan:
    """Quacks enough like a Span that call sites never branch."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None


_NOOP = _NoopSpan()


def enabled() -> bool:
    """True when the OTel API is importable. NOTE: spans still go nowhere until an
    SDK + exporter are configured - that is the host's call, never this library's."""
    return _TRACER is not None


def set_attrs(span: Any, attributes: dict[str, Any] | None) -> None:
    """Set attributes, skipping None values (an absent fact is absent, not 'None')."""
    for key, value in (attributes or {}).items():
        if value is not None:
            span.set_attribute(key, value)


def mark_error(span: Any, message: str) -> None:
    if _StatusCode is not None and not isinstance(span, _NoopSpan):
        span.set_status(_Status(_StatusCode.ERROR, (message or "")[:200]))


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """One instrumented scope. Yields the (possibly no-op) span so callers can attach
    OUTCOME attributes before leaving; initial attributes are set on entry."""
    if _TRACER is None:
        yield _NOOP
        return
    with _TRACER.start_as_current_span(name) as s:
        set_attrs(s, attributes)
        yield s
