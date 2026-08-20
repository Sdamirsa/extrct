"""The facade — one object that runs the whole pipeline from a job document.

    from extrct import Extractor

    async with Extractor.from_yaml("configs/echo-report.yaml") as ex:
        result = await ex.extract(open("note.txt").read())
        print(result.status, result.data)

Everything the facade does is a composition of the public modules (pipeline, xai,
storage) — nothing here is reachable only through the facade, so scripts that need
finer control drop one level down without rewriting anything. Per-chunk calls run with
bounded concurrency; grounding and certainty run per chunk as non-breaking steps; the
run log receives one row per model call plus the wrapping and the merge, exactly the
rows the batch lane writes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import client_model, pipeline, telemetry
from .hashing import sha256_text
from .job import load_job, parse_job
from .providers import get_provider
from .repair import DEFAULT_LADDER
from .runner import call_many
from .storage import NullRunStore, RunStore, open_store, run_row_from_single


@dataclass
class ExtractionResult:
    """What one `extract()` returns. `data` is the merged object for multi-chunk runs,
    the single extraction otherwise; everything else is the evidence around it."""

    status: str                      # ok | repaired | partial | invalid | error
    data: dict | None
    run_uids: list[str]
    input_sha256: str
    job_uid: str | None
    chunks: list[dict] = field(default_factory=list)   # per-chunk evidence (see extract())
    grounding: dict | None = None    # chunk 0 convenience when single-chunk
    certainty: dict | None = None
    merge: dict | None = None
    record: dict | None = None       # the pipeline-run/1.0 record
    total_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "repaired")


def _aggregate_status(statuses: list[str]) -> str:
    good = [s for s in statuses if s in ("ok", "repaired")]
    if len(good) == len(statuses) and statuses:
        return "ok" if all(s == "ok" for s in statuses) else "repaired"
    if good:
        return "partial"
    if statuses and all(s == "error" for s in statuses):
        return "error"
    return "invalid"


class Extractor:
    """A configured extraction pipeline. Construct from a job document (or YAML file),
    then `extract()` / `extract_many()`. Use as an async context manager unless you
    pass your own `httpx.AsyncClient`."""

    def __init__(self, job: dict[str, Any], *, http: httpx.AsyncClient | None = None,
                 store: RunStore | None = None):
        # Accept a raw authored dict too — normalize through the same validator.
        self.job = job if "job_uid" in job else parse_job(job)
        # Builds the spec and runs the provider preflight (e.g. the OpenRouter egress
        # gate) NOW — a misconfigured job fails at construction, not mid-batch.
        self.client_payload = client_model.build_client_payload(self.job["client"])
        self.provider = get_provider(self.client_payload["provider"])
        self._http = http
        self._own_http = http is None
        self._store: RunStore = store if store is not None else open_store(self.job.get("storage"))
        self._schema_ready = False

    @classmethod
    def from_yaml(cls, path, **kwargs) -> "Extractor":
        """`path` is a str or any PathLike; the file is read as UTF-8 on every OS."""
        return cls(load_job(path), **kwargs)

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> "Extractor":
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _require_http(self) -> httpx.AsyncClient:
        if self._http is None:
            msg = ("no HTTP client: use `async with Extractor(...) as ex:` or pass "
                   "http=httpx.AsyncClient(...)")
            raise RuntimeError(msg)
        return self._http

    async def _ensure_schema_once(self) -> None:
        if not self._schema_ready:
            await self._store.ensure_schema()
            self._schema_ready = True

    # ------------------------------------------------------------------ the lanes
    async def extract(self, text: str, *, input_id: str | None = None,
                      tags: list[str] | None = None) -> ExtractionResult:
        """One document through the full pipeline — see `_extract_impl` for the
        mechanics. This wrapper only opens the trace root: the per-call `chat` spans
        from the pipeline nest under it, and identities (job_uid, run_uids) make the
        trace joinable to the run log. Content never rides a span (telemetry.py)."""
        with telemetry.span("extrct.extract", {
            "extrct.job_uid": self.job.get("job_uid"),
            "extrct.provider": self.client_payload.get("provider"),
            "extrct.input_sha256": sha256_text(text),
            "extrct.input_id": input_id,
        }) as sp:
            result = await self._extract_impl(text, input_id=input_id, tags=tags)
            telemetry.set_attrs(sp, {
                "extrct.status": result.status,
                "extrct.chunks": len(result.chunks),
                "extrct.runs": len(result.run_uids),
                "extrct.total_tokens": result.total_tokens,
                "extrct.cost_usd": result.cost_usd,
                "extrct.grounding_clean": (result.grounding or {}).get("summary", {}).get("grounding_clean")
                                          if result.grounding else None,
            })
            if result.status == "error":
                telemetry.mark_error(sp, result.error or "extraction failed")
            return result

    async def _extract_impl(self, text: str, *, input_id: str | None = None,
                            tags: list[str] | None = None) -> ExtractionResult:
        """One document through the full pipeline:

            wrap -> request (xN chunks, bounded parallel) -> grounding -> certainty -> merge

        Every model call is one run row (idempotent on run_uid); grounding/certainty
        failures are recorded steps, never lost extractions. Raises only on breaking
        config errors (bad wrap parameters, unknown provider); transport and model
        failures land in the result's status and chunk evidence.
        """
        http = self._require_http()
        job = self.job
        extract_cfg = job.get("extract") or {}
        grounding_cfg = job.get("grounding") or {}
        certainty_cfg = job.get("certainty") or {}
        wrapper_cfg = job.get("wrapper") or {}
        merger_cfg = job.get("merger") or {}

        log_runs = bool(extract_cfg.get("log_to_db", True)) and not isinstance(self._store, NullRunStore)
        if log_runs:
            await self._ensure_schema_once()

        run_tags = [t.strip() for t in str(extract_cfg.get("run_tags", "")).split(",") if t.strip()]
        all_tags = [*run_tags, *(tags or [])] or None
        store_input_text = bool(extract_cfg.get("store_input_text", False))
        ladder = tuple(extract_cfg.get("ladder", DEFAULT_LADDER))
        max_retries = int(extract_cfg.get("max_retries", 2))
        max_reprompts = int(extract_cfg.get("max_reprompts", 1))

        plan = pipeline.plan_steps(wrapper_cfg, grounding_cfg, certainty_cfg)
        composed = pipeline.compose(self.client_payload, job["schema"], grounding_cfg, certainty_cfg)
        envelope_sent = composed["envelope_sent"]
        request_mode = (composed["client"].get("spec") or {}).get("mode")

        chunks, wrap_doc, wrap_entry = pipeline.make_chunks(text, wrapper_cfg)
        steps: dict[str, dict] = {"wrap": wrap_entry}
        if not chunks:
            msg = f"wrapping failed: {wrap_entry.get('reason')}"
            raise ValueError(msg)
        if wrap_doc is not None and bool(wrapper_cfg.get("log_to_db", True)) and log_runs:
            await self._store.save_wrapping(wrap_doc, store_text=bool(wrapper_cfg.get("store_text", False)))

        # --- request + per-chunk analysis, bounded parallel, order-preserving ---
        async def worker(chunk: dict) -> dict:
            single = await pipeline.execute_single(
                composed["client"], envelope_sent, chunk["text"], http=http,
                ladder=ladder, max_retries=max_retries, max_reprompts=max_reprompts)
            g_report, g_entry = pipeline.try_grounding(
                single["obj"], chunk["text"], grounding_cfg, offset=int(chunk.get("start") or 0))
            c_report, c_entry = pipeline.try_certainty(
                (single.get("transport") or {}).get("payload"), envelope_sent.get("schema"),
                certainty_cfg, engine=self.provider.engine, request_mode=request_mode)
            return {"chunk": chunk, "single": single, "grounding": g_report,
                    "grounding_entry": g_entry, "certainty": c_report, "certainty_entry": c_entry}

        concurrency = int(wrapper_cfg.get("parallel_calls") or self.provider.default_concurrency)
        rows = await call_many(chunks, worker, concurrency=max(1, concurrency))

        # call_many isolates exceptions into transport-ish dicts; normalize the row shape.
        results: list[dict] = []
        for i, r in enumerate(rows):
            if "single" not in r:
                results.append({"chunk": chunks[i],
                                "single": {"run_uid": None, "record": None, "transport": r,
                                           "response": None, "ladder": None, "obj": None,
                                           "spec": None, "final_status": "error"},
                                "grounding": None,
                                "grounding_entry": pipeline.step_entry("failed", str(r.get("error"))[:200]),
                                "certainty": None,
                                "certainty_entry": pipeline.step_entry("failed", str(r.get("error"))[:200])})
            else:
                results.append(r)

        # --- log every run + annotations (idempotent; logging never loses a result) ---
        log_errors: list[str] = []
        if log_runs:
            for r in results:
                single = r["single"]
                if not single.get("record"):
                    continue
                try:
                    metadata: dict[str, Any] = {"job_uid": job.get("job_uid")}
                    if input_id:
                        metadata["input_id"] = input_id
                    if r["chunk"].get("chunk_uid"):
                        metadata["chunk_uid"] = r["chunk"]["chunk_uid"]
                        metadata["wrap_uid"] = r["chunk"]["wrap_uid"]
                        metadata["chunk_idx"] = r["chunk"]["idx"]
                    await self._store.save_run(run_row_from_single(
                        single, envelope_sent, tags=all_tags,
                        store_input_text=store_input_text, input_text=r["chunk"]["text"],
                        run_metadata=metadata))
                    if single.get("ladder"):
                        await self._store.save_attempts(single["run_uid"], single["ladder"]["attempts"])
                    lp = r["certainty"] if bool(certainty_cfg.get("log_to_db", True)) else None
                    gr = r["grounding"] if bool(grounding_cfg.get("log_to_db", True)) else None
                    if lp is not None or gr is not None:
                        await self._store.annotate_run(single["run_uid"],
                                                       field_logprobs=lp, field_grounding=gr)
                except Exception as exc:  # noqa: BLE001 - logging must never lose a result
                    log_errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")

        # --- step aggregation (worst chunk speaks for the step) ---
        statuses = [r["single"]["final_status"] for r in results]
        request_ok = any(s in ("ok", "repaired") for s in statuses)
        steps["request"] = pipeline.step_entry(
            "ok" if request_ok else "failed",
            "" if request_ok else f"no chunk produced a valid extraction ({statuses})",
            chunks=len(results))
        for step_name, key in (("grounding", "grounding_entry"), ("certainty", "certainty_entry")):
            entries = [r[key] for r in results]
            failed = [e for e in entries if e.get("status") == "failed"]
            skipped = [e for e in entries if e.get("status") == "skipped"]
            if len(skipped) == len(entries):
                steps[step_name] = entries[0]
            elif failed:
                steps[step_name] = pipeline.step_entry(
                    "failed", failed[0].get("reason", ""), failed_chunks=len(failed),
                    ok_chunks=len(entries) - len(failed) - len(skipped))
            else:
                steps[step_name] = pipeline.step_entry("ok", "", chunks=len(entries) - len(skipped))

        # --- merge (identity for a single chunk) ---
        merge_inputs = [{"chunk_idx": r["chunk"]["idx"], "chunk_uid": r["chunk"].get("chunk_uid"),
                         "run_uid": r["single"].get("run_uid"), "extracted": r["single"].get("obj")}
                        for r in results]
        merge_result, merge_entry = pipeline.try_merge(merge_inputs, merger_cfg, envelope_sent)
        steps["merge"] = merge_entry
        if merge_result is not None and bool(merger_cfg.get("log_to_db", True)) and log_runs:
            try:
                await self._store.save_merge_run(
                    merge_result, wrap_uid=wrap_doc["wrap_uid"] if wrap_doc else None, tags=all_tags)
            except Exception as exc:  # noqa: BLE001
                log_errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")

        run_uids = [r["single"]["run_uid"] for r in results if r["single"].get("run_uid")]
        record = pipeline.assemble_record(plan, steps, chunk_count=len(results),
                                          run_uids=run_uids, composition=composed["composition"])
        if log_errors:
            record["log_errors"] = log_errors

        # --- flatten totals ---
        total_tokens = 0
        cost = 0.0
        have_tokens = have_cost = False
        latency = 0
        for r in results:
            row = run_row_from_single(r["single"], envelope_sent) if r["single"].get("record") else {}
            if row.get("total_tokens") is not None:
                total_tokens += row["total_tokens"]
                have_tokens = True
            if row.get("cost_usd") not in (None, ""):
                cost += float(row["cost_usd"])
                have_cost = True
            latency += (r["single"].get("transport") or {}).get("latency_ms") or 0

        single_chunk = len(results) == 1
        data = merge_result["merged"] if merge_result is not None else results[0]["single"].get("obj")
        if isinstance(data, dict) and "_evidence" in data:
            # `data` is uniformly evidence-free (the merge path already strips it);
            # the full object stays in chunks[i].extracted and on the run row.
            data = {k: v for k, v in data.items() if k != "_evidence"}
        first_error = next((str((r["single"].get("transport") or {}).get("error") or "")
                            for r in results if r["single"]["final_status"] == "error"), None)
        return ExtractionResult(
            status=_aggregate_status(statuses),
            data=data,
            run_uids=run_uids,
            input_sha256=sha256_text(text),
            job_uid=job.get("job_uid"),
            chunks=[{"idx": r["chunk"]["idx"], "start": r["chunk"]["start"], "end": r["chunk"]["end"],
                     "chunk_uid": r["chunk"].get("chunk_uid"), "run_uid": r["single"].get("run_uid"),
                     "final_status": r["single"]["final_status"], "extracted": r["single"].get("obj"),
                     "grounding": r["grounding"], "certainty": r["certainty"],
                     "http_status": (r["single"].get("transport") or {}).get("http_status"),
                     "error": (r["single"].get("transport") or {}).get("error")}
                    for r in results],
            grounding=results[0]["grounding"] if single_chunk else None,
            certainty=results[0]["certainty"] if single_chunk else None,
            merge=merge_result,
            record=record,
            total_tokens=total_tokens if have_tokens else None,
            cost_usd=round(cost, 8) if have_cost else None,
            latency_ms=latency or None,
            error=first_error,
        )

    async def extract_many(self, texts: list[str], *, input_ids: list[str] | None = None,
                           tags: list[str] | None = None, concurrency: int = 2,
                           on_progress: Any = None) -> list[ExtractionResult]:
        """`extract()` over many documents with bounded concurrency and total failure
        isolation: results[i] always corresponds to texts[i], and one document's
        exception becomes a status='error' result, never a dead batch. Chunk-level
        parallelism still applies inside each document, so keep this outer bound small.
        """
        if input_ids is not None and len(input_ids) != len(texts):
            msg = f"input_ids has {len(input_ids)} entries for {len(texts)} texts"
            raise ValueError(msg)
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        results: list[ExtractionResult | None] = [None] * len(texts)
        done = 0
        lock = asyncio.Lock()

        async def run(i: int, text: str) -> None:
            nonlocal done
            async with sem:
                try:
                    results[i] = await self.extract(
                        text, input_id=input_ids[i] if input_ids else None, tags=tags)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolation is the point
                    results[i] = ExtractionResult(
                        status="error", data=None, run_uids=[],
                        input_sha256=sha256_text(text), job_uid=self.job.get("job_uid"),
                        error=f"{type(exc).__name__}: {str(exc)[:300]}")
            async with lock:
                done += 1
                if on_progress:
                    on_progress(done, len(texts))

        await asyncio.gather(*(run(i, t) for i, t in enumerate(texts)))
        return [r for r in results if r is not None]
