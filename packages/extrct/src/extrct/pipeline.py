"""The extraction engine (pipeline-run/1.0) — compose, chunk, post-process, degrade gracefully.

One pipeline:

    wrap -> request (xN chunks, bounded parallel) -> grounding -> certainty -> merge

Design facts this module encodes:
- THE SINGLE-RUN PATH IS THE 1-CHUNK CASE. Wrapper absent/disabled -> one identity
  chunk over the whole text, merge -> skipped-identity. One loop, no second code path.
- REQUEST COMPOSITION derives what actually goes on the wire from the configs:
  certainty-enabled merges logprob riders into the client spec (UPGRADE ONLY — an
  authored stronger setting is never downgraded), grounding inline/auto injects the
  evidence mirror into the schema at request time. What was actually sent is returned
  as `envelope_sent` and recorded.
- GRACEFUL DEGRADATION: grounding and certainty are analysis — their failure is a
  RECORDED step, never a lost extraction. Merge failure loses only the merged view;
  per-chunk extractions survive. Wrap failure is breaking (nothing to extract).
- grounding.mode='posthoc' (a second grounding call) is not wired into this pipeline —
  it is recorded as skipped with that reason, never silently ignored ('auto' == inline
  here, because the pipeline can always inject). Post-hoc grounding of stored runs
  remains available through xai.grounding + schema.evidence_schema.
- Conflicts that need MANUAL resolution are first-class: the merge step entry carries
  `needs_manual` (count of major-severity conflicts) so a later manual-merge step can
  find its queue in merge_run rows.

Providers are resolved through the registry — this module never names a backend.
"""

from __future__ import annotations

import copy
from typing import Any

from . import merging, schema as schema_mod, telemetry, wrapping
from .hashing import content_uid
from .providers import get_provider
from .xai import certainty as certainty_mod, grounding as grounding_mod

PIPELINE_VERSION = "pipeline-run/1.0"

STEPS = ("wrap", "request", "grounding", "certainty", "merge")

# request-time riders certainty needs; enum posterior needs alternatives visible.
CERTAINTY_RIDER_TOP_LOGPROBS = 3


def step_entry(status: str, reason: str = "", **facts: Any) -> dict:
    """One step's record. status: ok | failed | skipped. `reason` says WHY for anything
    that is not a plain ok — absence is not evidence."""
    if status not in ("ok", "failed", "skipped"):
        msg = f"step status {status!r} not in ok|failed|skipped"
        raise ValueError(msg)
    out = {"status": status}
    if reason:
        out["reason"] = reason
    out.update(facts)
    return out


def plan_steps(longtext_cfg: dict | None, grounding_cfg: dict | None,
               certainty_cfg: dict | None) -> dict:
    """The DECLARED plan, before anything runs — compared against executed steps in the
    steps record (declared-vs-executed inside one run)."""
    lt = longtext_cfg or {}
    g = grounding_cfg or {}
    c = certainty_cfg or {}
    wrap_on = bool(lt.get("enabled"))
    return {
        "wrap": {"enabled": wrap_on,
                 "reason": "wrapper.enabled" if wrap_on else
                           ("wrapper.enabled=false" if lt else "no wrapper config wired")},
        "request": {"enabled": True, "reason": "always"},
        "grounding": {"enabled": bool(g) and g.get("enabled", True) is not False,
                      "reason": ("grounding config wired" if g and g.get("enabled", True) is not False
                                 else ("grounding.enabled=false" if g else "no grounding config wired"))},
        "certainty": {"enabled": bool(c) and c.get("enabled", True) is not False,
                      "reason": ("certainty config wired" if c and c.get("enabled", True) is not False
                                 else ("certainty.enabled=false" if c else "no certainty config wired"))},
        "merge": {"enabled": wrap_on, "reason": "follows wrap" if wrap_on else "single chunk -> identity"},
    }


def compose(client_payload: dict, envelope: dict, grounding_cfg: dict | None,
            certainty_cfg: dict | None) -> dict:
    """Request composition: what ACTUALLY goes on the wire, derived from the configs.

    Returns {"client": payload (spec adjusted), "envelope_sent": envelope (schema maybe
    evidence-augmented), "composition": record of everything applied}. Pure; inputs are
    not mutated. Idempotent on an already-evidence-requesting envelope."""
    g = grounding_cfg or {}
    c = certainty_cfg or {}
    client = copy.deepcopy(client_payload)
    spec = dict(client.get("spec") or {})
    record: dict[str, Any] = {"riders": {}, "evidence_injected": False}

    if c and c.get("enabled", True) is not False:
        # Upgrade-only: never downgrade an authored stronger setting.
        if not spec.get("logprobs"):
            spec["logprobs"] = True
            record["riders"]["logprobs"] = True
        want_top = int(c.get("top_logprobs", CERTAINTY_RIDER_TOP_LOGPROBS) or 0)
        have_top = int(spec.get("top_logprobs") or 0)
        if want_top > have_top:
            spec["top_logprobs"] = want_top
            record["riders"]["top_logprobs"] = want_top
    client["spec"] = spec

    envelope_sent = envelope
    if g and g.get("enabled", True) is not False and g.get("mode", "auto") in ("inline", "auto"):
        if envelope.get("request_evidence"):
            record["evidence_injected"] = "already_present"
        else:
            schema = envelope.get("schema")
            if isinstance(schema, dict):
                augmented = schema_mod.augment_with_evidence(schema, envelope.get("encoding") or schema_mod.STRICT_NULLABLE)
                envelope_sent = {**envelope, "schema": augmented,
                                 "schema_uid": schema_mod.schema_uid(augmented),
                                 "request_evidence": True}
                record["evidence_injected"] = True
                record["clean_schema_uid"] = envelope.get("schema_uid")
    if record["evidence_injected"] is True:
        record["schema_uid_sent"] = envelope_sent.get("schema_uid")
    return {"client": client, "envelope_sent": envelope_sent, "composition": record}


def make_chunks(text: str, longtext_cfg: dict | None) -> tuple[list[dict], dict | None, dict]:
    """(chunks, wrap_doc | None, wrap step entry). Wrapper off -> ONE identity chunk —
    the single-run path IS the 1-chunk case."""
    lt = longtext_cfg or {}
    if not lt.get("enabled"):
        chunk = {"idx": 0, "text": text, "start": 0, "end": len(text),
                 "chunk_uid": None, "wrap_uid": None}
        reason = "wrapper.enabled=false" if lt else "no wrapper config wired"
        return [chunk], None, step_entry("skipped", reason, chunks=1)
    try:
        doc = wrapping.wrap_text(
            text,
            lt.get("logic", "paragraph_pack"),
            max_chars=int(lt.get("max_chars", 4000)),
            overlap_chars=int(lt.get("overlap_chars", 400)),
        )
    except Exception as exc:  # breaking, but still a recorded entry for the caller to raise on
        return [], None, step_entry("failed", f"{type(exc).__name__}: {exc}")
    chunks = [{**c, "wrap_uid": doc["wrap_uid"]} for c in doc["chunks"]]
    return chunks, doc, step_entry("ok", "", chunks=len(chunks), wrap_uid=doc["wrap_uid"],
                                   logic=doc.get("logic"), coverage_ok=wrapping.coverage_ok(doc))


def _flatten_evidence(evidence: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(evidence, dict):
        for k, v in evidence.items():
            out.update(_flatten_evidence(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(evidence, list):
        for i, v in enumerate(evidence):
            out.update(_flatten_evidence(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = evidence
    return out


def try_grounding(extracted: dict | None, source_text: str, cfg: dict | None,
                  *, offset: int = 0) -> tuple[dict | None, dict]:
    """Grounding as a NON-BREAKING step: (report | None, step entry). Never raises —
    analysis failure must not destroy an extraction. `offset` shifts spans to absolute
    document coordinates when the source is a chunk."""
    g = cfg or {}
    if not g or g.get("enabled", True) is False:
        return None, step_entry("skipped", "grounding.enabled=false" if g else "no grounding config wired")
    mode = g.get("mode", "auto")
    if mode == "posthoc":
        return None, step_entry("skipped",
                                "grounding.mode=posthoc (a second grounding call) is not wired "
                                "into this pipeline; use inline, or ground stored runs via "
                                "xai.grounding + schema.evidence_schema")
    if not isinstance(extracted, dict) or extracted is None:
        return None, step_entry("failed", "no extraction to ground")
    evidence = extracted.get("_evidence")
    if not isinstance(evidence, (dict, list)) or not evidence:
        return None, step_entry("failed",
                                "extraction carries no _evidence despite inline injection — "
                                "model ignored the evidence mirror (check request mode/enforcement)")
    try:
        report = grounding_mod.ground_fields(
            _flatten_evidence(evidence), source_text,
            fuzzy_threshold=float(g.get("fuzzy_threshold", 0.75)),
        )
        if offset:
            for field in report.get("fields", {}).values():
                if isinstance(field.get("start"), int):
                    field["start"] += offset
                if isinstance(field.get("end"), int):
                    field["end"] += offset
        report["grounding_mode"] = "inline_evidence"
        s = report.get("summary", {})
        return report, step_entry("ok", "", fields=s.get("fields"), exact=s.get("exact"),
                                  fuzzy=s.get("fuzzy"), unlocated=s.get("unlocated"),
                                  grounding_clean=s.get("grounding_clean"))
    except Exception as exc:  # noqa: BLE001 - non-breaking by contract
        return None, step_entry("failed", f"{type(exc).__name__}: {str(exc)[:200]}")


def try_certainty(provider_payload: dict | None, schema: dict | None, cfg: dict | None,
                  *, engine: str, request_mode: str | None) -> tuple[dict | None, dict]:
    """Certainty as a NON-BREAKING step. Engine and request mode come from the caller's
    own request record — no DB round-trip, never inferred from payload shape."""
    c = cfg or {}
    if not c or c.get("enabled", True) is False:
        return None, step_entry("skipped", "certainty.enabled=false" if c else "no certainty config wired")
    if not isinstance(provider_payload, dict):
        return None, step_entry("failed", "no provider payload to score")
    try:
        report = certainty_mod.field_confidence(
            provider_payload, schema,
            enum_posterior=bool(c.get("enum_posterior", True)),
        )
        report["engine"] = engine
        report["mask_state"] = certainty_mod.mask_state(engine, request_mode)
        if not report.get("ok"):
            return report, step_entry("failed", str(report.get("error"))[:200])
        return report, step_entry("ok", "", fields=len(report.get("fields") or {}),
                                  mask_state=report["mask_state"])
    except Exception as exc:  # noqa: BLE001 - non-breaking by contract
        return None, step_entry("failed", f"{type(exc).__name__}: {str(exc)[:200]}")


def try_merge(inputs: list[dict], merger_cfg: dict | None,
              envelope: dict | None) -> tuple[dict | None, dict]:
    """Merge as the LAST step. `inputs` rows are {chunk_idx, chunk_uid?, run_uid?,
    extracted} (merge-def's own shape). Single input -> skipped-identity. Failure loses
    only the merged view (per-chunk extractions survive upstream). `needs_manual`
    counts major-severity conflicts — the queue a later manual-merge step reads."""
    usable = [d for d in inputs if isinstance(d.get("extracted"), dict)]
    if len(inputs) <= 1:
        return None, step_entry("skipped", "single chunk -> identity")
    if not usable:
        return None, step_entry("failed", "no successful chunk extractions to merge")
    cfg = {k: v for k, v in (merger_cfg or {}).items() if k in merging.MERGE_DEFAULTS}
    try:
        result = merging.merge_extractions(usable, schema=envelope, config=cfg)
        stats = result.get("stats") or {}
        return result, step_entry("ok", "", inputs=len(usable),
                                  dropped=len(inputs) - len(usable),
                                  conflicts=stats.get("conflicts", 0),
                                  needs_manual=stats.get("major_conflicts", 0),
                                  merge_uid=result.get("merge_uid"))
    except Exception as exc:  # noqa: BLE001 - per-chunk results survive upstream
        return None, step_entry("failed", f"{type(exc).__name__}: {str(exc)[:200]}")


async def execute_single(client: dict, envelope: dict, text: str, *, http: Any,
                         ladder: tuple = (), max_reprompts: int = 1, max_retries: int = 2,
                         reprompt: Any = None, llm_repair: Any = None,
                         extra_headers: dict | None = None,
                         retryable_status: Any = None, backoff_cap_s: float = 30.0) -> dict:
    """ONE extraction: build request -> call -> truncation gate -> repair ladder.
    The shared core of every lane (single, long-text per chunk, batch cell), so they
    can never drift. Returns {run_uid, record, transport, response, ladder, obj,
    final_status}. Raises only on config errors (unknown provider / bad spec / failed
    preflight); transport and model failures are DATA in the returned dict."""
    from . import repair
    from .runner import call_once

    provider_name = client.get("provider")
    prov = get_provider(provider_name)  # loud on unknown, lists what is registered
    spec = prov.make_spec(dict(client.get("spec") or {}))
    prov.preflight(spec)  # e.g. the OpenRouter egress gate — raises before any I/O
    body, resolution = prov.build_request(spec, text, envelope)
    record = prov.request_record(spec, text, envelope, body, resolution)
    envelope_settings = {k: client.get(k, d) for k, d in prov.envelope_fields.items()}
    url = prov.target_url(spec, envelope_settings)
    headers = prov.headers(spec)
    timeout = prov.timeout_s(spec)

    run_uid = content_uid({"request_uid": record["request_uid"],
                           "input": record["input"]["input_sha256"]})
    if extra_headers:
        # Additive only (e.g. X-OpenRouter-Metadata for the batch audit trail) —
        # never part of any uid: headers are deployment/observability, not content.
        headers = {**headers, **extra_headers}

    # One span per model call, on THE shared request path — so every lane (single,
    # long-text chunk, batch cell) is traced identically. Identities and outcomes
    # only; never text (telemetry.py states the privacy contract).
    model = getattr(spec, "model", "") or ""
    with telemetry.span(f"chat {model}".strip(), {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider_name,
        "gen_ai.system": provider_name,  # legacy alias deployed backends still read
        "gen_ai.request.model": model,
        "extrct.run_uid": run_uid,
        "extrct.request_uid": record["request_uid"],
        "extrct.schema_uid": record["schema"].get("schema_uid"),
        "extrct.request_mode": record["schema"].get("mode"),
        "extrct.input_sha256": record["input"]["input_sha256"],
        "extrct.input_chars": record["input"]["input_chars"],
    }) as sp:
        transport = await call_once(url, body, http=http, headers=headers, timeout_s=timeout,
                                    max_retries=int(max_retries), retry_on_5xx=False,
                                    retryable_status=retryable_status, backoff_cap_s=backoff_cap_s)
        out = {"run_uid": run_uid, "record": record, "transport": transport,
               "response": None, "ladder": None, "obj": None, "spec": spec}
        telemetry.set_attrs(sp, {"http.response.status_code": transport.get("http_status"),
                                 "extrct.retries": transport.get("retries")})
        if transport.get("payload") is None:
            out["final_status"] = "error"
            telemetry.set_attrs(sp, {"extrct.final_status": "error",
                                     "error.type": transport.get("error_class")})
            telemetry.mark_error(sp, transport.get("error") or "transport failure")
            return out
        response = prov.read_response(spec, transport["payload"])
        out["response"] = response
        usage = response.get("usage") or {}
        telemetry.set_attrs(sp, {
            # missing-key fallback covers both provider shapes; a None value stays skipped
            "gen_ai.usage.input_tokens": usage.get("prompt_tokens", usage.get("prompt_eval_count")),
            "gen_ai.usage.output_tokens": usage.get("completion_tokens", usage.get("eval_count")),
            "gen_ai.response.model": (response.get("served") or {}).get("served_model"),
            "extrct.cost_usd": usage.get("cost_usd"),
            "extrct.silent_failure_flags": response.get("silent_failure_flags") or None,
        })
        if response["terminal_failure"]:
            # Gate BEFORE the ladder: a truncated body is a well-formed prefix, and
            # json_repair would happily brace-balance it into a plausible wrong answer.
            out["final_status"] = "invalid"
            telemetry.set_attrs(sp, {"extrct.final_status": "invalid"})
            return out
        lad = await repair.run_ladder(
            response["content"]["raw_text"], envelope.get("schema"), ladder=tuple(ladder),
            reprompt=reprompt, llm_repair=llm_repair,
            # NOT `or 1`: 0 is a legitimate authored value (keep the reprompt rung selected
            # but forbid re-billing — run_ladder's range(0) handles it).
            max_reprompts=int(1 if max_reprompts is None else max_reprompts))
        out["ladder"] = lad
        out["final_status"] = lad["final_status"]
        out["obj"] = lad["obj"]
        telemetry.set_attrs(sp, {"extrct.final_status": lad["final_status"],
                                 "extrct.repair_layers": lad["layers_used"] or None,
                                 "extrct.attempts": len(lad["attempts"])})
        return out


def assemble_record(plan: dict, steps: dict[str, dict], *, chunk_count: int,
                    run_uids: list[str], composition: dict) -> dict:
    """The pipeline's own record: declared plan, executed steps, identity."""
    breaking_failed = [n for n in ("wrap", "request") if steps.get(n, {}).get("status") == "failed"]
    recorded_failed = [n for n in ("grounding", "certainty", "merge")
                       if steps.get(n, {}).get("status") == "failed"]
    body = {
        "version": PIPELINE_VERSION,
        "plan": plan,
        "steps": {name: steps[name] for name in STEPS if name in steps},
        "chunk_count": chunk_count,
        "run_uids": run_uids,
        "composition": composition,
        "ok": not breaking_failed,
        "failed_breaking": breaking_failed,
        "failed_recorded": recorded_failed,
    }
    body["pipeline_uid"] = content_uid({"plan": plan, "run_uids": run_uids,
                                        "composition": composition})
    return body
