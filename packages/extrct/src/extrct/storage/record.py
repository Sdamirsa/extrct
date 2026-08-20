"""One place that turns a pipeline result into a run row.

`pipeline.execute_single` returns evidence (request record, transport envelope,
response record, ladder log); this module flattens it into the extraction_run row
every backend stores. Shared by the Extractor facade and the batch lane so the two can
never log different shapes for the same event.
"""

from __future__ import annotations

from typing import Any


def _usage_totals(provider: str, response: dict | None) -> tuple[int | None, Any]:
    """(total_tokens, cost_usd) from a response record, per provider shape."""
    usage = (response or {}).get("usage") or {}
    if provider == "ollama":
        pe, ec = usage.get("prompt_eval_count"), usage.get("eval_count")
        total = (pe or 0) + (ec or 0) if (pe is not None or ec is not None) else None
        return total, None
    return usage.get("total_tokens"), usage.get("cost_usd")


def run_row_from_single(single: dict[str, Any], envelope: dict[str, Any], *,
                        tags: list[str] | None = None,
                        store_input_text: bool = False,
                        input_text: str | None = None,
                        run_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """execute_single result -> the extraction_run row dict `RunStore.save_run` takes.

    `input_text` is stored ONLY when `store_input_text` is explicitly on (the privacy
    default is hash-only); passing text without the flag stores nothing.
    """
    rec = single["record"]
    resp = single.get("response")
    lad = single.get("ladder")
    transport = single.get("transport") or {}
    spec = single.get("spec")
    provider = rec["target"]["provider"]
    total_tokens, cost_usd = _usage_totals(provider, resp)

    metadata = dict(run_metadata or {})
    if transport.get("generation_id"):
        metadata.setdefault("generation_id", transport["generation_id"])

    return {
        "run_uid": single["run_uid"],
        "request_uid": rec["request_uid"],
        "schema_uid": rec["schema"].get("schema_uid"),
        "schema_encoding": rec["schema"].get("encoding") or envelope.get("encoding"),
        "provider": provider,
        "base_url": rec["target"].get("base_url"),
        "model_on_wire": rec["target"].get("model_on_wire"),
        "model_digest": rec["target"].get("model_digest"),
        "endpoint_tag": rec["target"].get("endpoint_tag"),
        "seed": getattr(spec, "seed", None),
        "temperature": getattr(spec, "temperature", None),
        "request_mode": rec["schema"].get("mode"),
        "input_sha256": rec["input"]["input_sha256"],
        "input_chars": rec["input"]["input_chars"],
        "input_text": input_text if (store_input_text and input_text) else rec["input"].get("input_text"),
        "data_classification": (rec.get("guard") or {}).get("data_classification"),
        "final_status": single["final_status"],
        "attempts": len(lad["attempts"]) if lad else 1,
        "layers_used": lad["layers_used"] if lad else [],
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "latency_ms": transport.get("latency_ms"),
        "silent_failure_flags": (resp or {}).get("silent_failure_flags", []),
        "request_record": rec,
        "provider_response": transport.get("payload"),
        "extracted": single.get("obj"),
        "tags": list(tags) if tags else None,
        "http_status": transport.get("http_status"),
        "error_class": transport.get("error_class"),
        "error": transport.get("error"),
        "run_metadata": metadata or None,
    }
