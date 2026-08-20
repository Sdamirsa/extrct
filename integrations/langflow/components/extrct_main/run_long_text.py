"""Run - Long Text — the batch runner: wrap FIRST, N single pipelines, merge LAST.

The long-text executor, split out of Run - Structured Extract (user decision
2026-08-14, second round): a batch changing the shape of every output was the reason
the single router design was rejected — here every output is PURPOSE-SHAPED for the
chunked lane and never varies. It consumes the SAME config authors as the single node
(Provider, Schema, XAI - Evidence Grounding, XAI - Certainty Score) plus
Adapter - Wrapper & Merger, and each chunk runs EXACTLY the single pipeline
(pipeline.execute_single — shared core, so single and batch can never drift), with
request composition (riders + evidence injection) applied once for all chunks.

Steps and degradation (the pipeline-run/1.0 record rides the Steps output):
wrap failed = breaking; one failed chunk is one row, never a dead batch; grounding /
certainty failures are recorded per chunk, never lost extractions; merge failure loses
only the merged view. Merge conflicts are labeled with severity; major ones are
counted as needs_manual — the queue a later manual-merge step reads from merge_run.

Two lessons from the 2026-08-14 review wave, both about honesty:
- A FAILED merge over N>1 chunks now returns an EMPTY Merged Extraction, not chunk 0's
  object. The old fallback presented the first ~max_chars of the document as
  document-level truth whenever the merger raised (e.g. a bad merger.scalar_strategy
  override), with no marker in the payload. The single-chunk identity path is unchanged.
- The repair ladder is REAL on this lane: reprompt and llm_repair closures are built per
  chunk exactly as Run - Structured Extract builds them (same prompts, same truncation
  gate on the sub-call). Before, both rungs were advertised and silently no-opped
  because execute_single was called without the callables.

If the Adapter config says enabled=false (e.g. a Flow - Controller thread flipped
wrapper.enabled), this node degrades to ONE identity chunk with an identity merge —
same output shapes, so a flow document can switch the lane without rewiring.

Resume: chunks whose content-derived run_uid already completed refetch their stored
extraction instead of re-billing (Skip Completed).
"""

import asyncio
import json

import httpx
from lfx.custom.custom_component.component import Component
from lfx.io import (
    BoolInput,
    DataInput,
    DropdownInput,
    HandleInput,
    IntInput,
    MessageTextInput,
    MultiselectInput,
    Output,
    TableInput,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message
from lfx.schema.table import EditMode

from extrct import config as flowcfg
from extrct import ollama, openrouter, pipeline, repair, storage


class ExtrctRunLongText(Component):
    display_name: str = "Run - Long Text"
    description: str = "Chunk first, one pipeline per chunk (bounded parallel), merge last with labeled conflicts."
    documentation: str = "docs/extraction-stack/long-text-design.md"
    icon: str = "rows-3"
    name: str = "extrct_long_text"

    inputs = [
        DataInput(name="client", display_name="Provider", required=True,
                  info="From Provider - Model Server or the Flow - Controller's Ready Provider output. One spec serves every chunk."),
        DataInput(name="schema", display_name="Schema", required=True,
                  info="The CLEAN envelope from Prep - Schema Builder. One schema serves every chunk (evidence injected at request time)."),
        MessageTextInput(name="text", display_name="Text", required=True,
                         info="The LONG document. Wrapping happens here — wire the raw text, not chunks."),
        HandleInput(name="longtext_cfg", display_name="Wrapper & Merger Config", required=True, input_types=["Data"],
                    info=("Adapter - Wrapper & Merger's config output — REQUIRED: it authors both the "
                          "wrap (logic, sizes, parallel calls) and the merge (strategy, conflict "
                          "policy). enabled=false degrades to one identity chunk, same shapes.")),
        HandleInput(name="grounding_cfg", display_name="Grounding Config", required=False, input_types=["Data"],
                    info="Optional: XAI - Evidence Grounding's config. Applied PER CHUNK (offsets mapped back to document coordinates)."),
        HandleInput(name="certainty_cfg", display_name="Certainty Config", required=False, input_types=["Data"],
                    info="Optional: XAI - Certainty Score's config. Riders applied once; scoring runs PER CHUNK."),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: a Flow - Config payload. extract.* keys apply to this node; "
                          "provider keys apply over the wired provider's spec.")),
        MessageTextInput(name="run_tags", display_name="Run Tags", value="longtext",
                         info=("Comma-separated tags stamped on EVERY chunk's run row (indexed; the "
                               "Registry's runs_by_tag filters on them). Not part of run identity — "
                               "re-tagging never breaks Skip Completed resume.")),
        MultiselectInput(name="ladder", display_name="Repair Ladder",
                         options=list(repair.LAYERS), value=list(repair.DEFAULT_LADDER),
                         info=("Applied PER CHUNK, always in a FIXED order: json_repair -> coerce -> "
                               "reprompt -> llm_repair (the order you click does not matter). "
                               "json_repair and coerce are deterministic and free. reprompt re-asks "
                               "the same model for THAT chunk (up to Max Reprompts) — on a 20-chunk "
                               "batch every rung is a 20x cost multiplier. llm_repair needs the "
                               "Repair Provider below; without it the rung is skipped and the status "
                               "says so.")),
        DataInput(name="repair_client", display_name="Repair Provider", required=False,
                  info=("Optional, used only by the llm_repair rung. Wire a SECOND provider payload — "
                        "often a cheaper model — so a failed chunk is repaired by something other "
                        "than the model that failed it. Without it llm_repair is skipped. Pick "
                        "patch_only vs rewrite under Repair Strategy (advanced).")),
        DropdownInput(name="repair_strategy", display_name="Repair Strategy",
                      options=["patch_only", "rewrite"], value="patch_only", advanced=True,
                      info=("patch_only shows the repair model ONLY the schema, the bad output and "
                            "the errors — it cannot invent facts. rewrite also passes the CHUNK's "
                            "text: it fixes more, but makes the repair model a second extractor.")),
        IntInput(name="max_reprompts", display_name="Max Reprompts", value=1, advanced=True,
                 info=("Per-chunk cap for the reprompt rung (only used when 'reprompt' is selected). "
                       "0 disables re-asks while keeping the rung declared.")),
        IntInput(name="max_retries", display_name="Transport Retries", value=2, advanced=True,
                 info=("HTTP-level retry cap PER CHUNK for transport failures (timeouts, connection "
                       "errors; 5xx are not retried). Never re-asks the model about content.")),
        BoolInput(name="skip_completed", display_name="Skip Completed", value=True,
                  info=("Resume: chunks whose content-derived run_uid already ended ok/repaired "
                        "refetch their stored extraction so the merge stays complete without re-billing.")),
        BoolInput(name="log_to_db", display_name="Log To Postgres", value=True,
                  info=("Every chunk run lands in extraction_run. Wrap rows (text_wrapping/"
                        "text_chunk) and the merge_run row follow the Adapter - Wrapper & Merger's "
                        "own Log To Postgres, NOT this switch.")),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True,
                         info=("Leave empty to use the stack's default Postgres (EXTRCT_PG_DSN). Set "
                               "it only to log — and resume — against a different database.")),
        BoolInput(name="store_input_text", display_name="Store Chunk Text On Runs", value=False, advanced=True,
                  info=("OFF stores only hashes. ON stores each chunk's text on its "
                        "extraction_run row — separate from the Adapter's 'Store Chunk Text', which "
                        "fills text_chunk. Turning either on is a decision-log event.")),
        TableInput(name="run_metadata", display_name="Run Metadata", advanced=True,
                   info="Joins every chunk run to a registry uid or a factorial cell.",
                   table_schema=[
                       {"name": "key", "display_name": "Key", "type": "str", "default": "cell", "edit_mode": EditMode.INLINE},
                       {"name": "value", "display_name": "Value", "type": "str", "default": "", "edit_mode": EditMode.INLINE},
                   ],
                   value=[]),
    ]

    # Result-shaped outputs first, diagnostics after, identity last. Edges bind by output
    # NAME, so this order only affects fresh drags.
    outputs = [
        Output(name="merged", display_name="Merged Extraction", method="build_merged", group_outputs=True),
        Output(name="chunk_runs", display_name="Chunk Runs", method="build_chunk_runs", group_outputs=True),
        Output(name="merge_report", display_name="Merge Report", method="build_merge_report", group_outputs=True),
        Output(name="batch_report", display_name="Batch Report", method="build_batch_report", group_outputs=True),
        Output(name="steps_out", display_name="Steps", method="build_steps", group_outputs=True),
        Output(name="grounding_out", display_name="Grounding", method="build_grounding", group_outputs=True),
        Output(name="certainty_out", display_name="Certainty", method="build_certainty", group_outputs=True),
        Output(name="pipeline_uid", display_name="Pipeline UID", method="build_pipeline_uid", group_outputs=True),
    ]

    _result: dict | None = None

    @staticmethod
    def _unwrap(value):
        if isinstance(value, list):
            value = value[0] if value else None
        data = getattr(value, "data", value)
        return data if isinstance(data, dict) else {}

    def _config(self, attr: str, section: str) -> dict:
        payload = self._unwrap(getattr(self, attr, None))
        cfg = payload.get(section)
        return cfg if isinstance(cfg, dict) else {}

    # ---- repair ladder, at PARITY with Run - Structured Extract ------------------------
    @staticmethod
    def _spec_from(client: dict):
        provider = client.get("provider")
        kwargs = dict(client.get("spec") or {})
        if provider == "ollama":
            return ollama.OllamaSpec(**kwargs), provider
        if provider == "openrouter":
            spec = openrouter.OpenRouterSpec(**kwargs)
            openrouter.check_egress(spec)
            return spec, provider
        msg = f"unknown provider {provider!r}"
        raise ValueError(msg)

    async def _call(self, provider, spec, client, text, envelope, http) -> str:
        """One call returning raw text. Used by the reprompt and llm_repair rungs.

        Applies the SAME truncation gate execute_single applies to the first call: a
        length-truncated answer is a well-formed prefix, and the next rung would
        brace-balance it into a plausible wrong answer. Gated (or transport-failed)
        sub-calls return "" and the ladder records a failed attempt.
        """
        from extrct.runner import call_once
        if provider == "ollama":
            body = ollama.build_request(spec, text, envelope)
            url = spec.base_url + client.get("api_path", "/api/chat")
            headers, timeout = {}, spec.timeout_s
        else:
            body, _res = openrouter.build_request(spec, text, envelope)
            url, headers, timeout = openrouter.target_url(spec), openrouter.headers(spec), spec.timeout_s
        env = await call_once(url, body, http=http, headers=headers, timeout_s=timeout, max_retries=1)
        if env.get("payload") is None:
            self.log(f"repair sub-call transport failure ({env.get('error_class')}: "
                     f"{str(env.get('error'))[:200]}); rung gets no text")
            return ""
        reader = ollama.read_response if provider == "ollama" else openrouter.read_response
        response = reader(spec, env["payload"])
        if response["terminal_failure"]:
            self.log("repair sub-call returned a TRUNCATED body "
                     f"(flags={response.get('silent_failure_flags')}); discarded — a truncated "
                     "prefix must never enter the ladder")
            return ""
        return response["content"]["raw_text"]

    def _repair_fns(self, text: str, client: dict, envelope: dict, http, selected: tuple):
        """(reprompt, llm_repair) closures for ONE chunk — same prompts as the single node,
        with this chunk's text, so single and batch repair identically."""
        reprompt_fn = None
        repair_fn = None
        if not ("reprompt" in selected or "llm_repair" in selected):
            return None, None
        spec_for_calls, provider = self._spec_from(client)
        if "reprompt" in selected:
            async def reprompt_fn(bad, errors, _t=text):  # noqa: ARG001
                prompt = (_t + "\n\n---\nYour previous answer did not satisfy the schema.\nErrors:\n"
                          + json.dumps(errors, indent=1)[:2000] + "\nReturn the corrected JSON only.")
                return await self._call(provider, spec_for_calls, client, prompt, envelope, http)
        if "llm_repair" in selected:
            rc = self._unwrap(getattr(self, "repair_client", None))
            if rc.get("provider"):
                async def repair_fn(bad, errors, _rc=rc, _t=text):
                    parts = [
                        "Fix this JSON so it satisfies the schema. Return JSON only.",
                        "Schema:\n" + json.dumps(envelope.get("schema"))[:4000],
                        "Invalid output:\n" + str(bad)[:4000],
                        "Errors:\n" + json.dumps(errors, indent=1)[:2000],
                    ]
                    if self.repair_strategy == "rewrite":
                        parts.insert(1, "Source text (this chunk):\n" + _t)
                    rspec, rprov = self._spec_from(_rc)
                    return await self._call(rprov, rspec, _rc, "\n\n".join(parts), envelope, http)
        return reprompt_fn, repair_fn

    async def _one_chunk(self, http, sem, chunk: dict, client: dict, envelope: dict,
                         provider: str, meta_base: dict, tags: list,
                         g_cfg: dict, c_cfg: dict) -> dict:
        """One chunk through the SHARED single pipeline + persist + per-chunk XAI.
        Exceptions become an error row — one failed chunk is never a dead batch."""
        text = str(chunk["text"])
        selected = tuple(self.ladder or ())
        try:
            reprompt_fn, repair_fn = self._repair_fns(text, client, envelope, http, selected)
            async with sem:
                single = await pipeline.execute_single(
                    client, envelope, text, http=http, ladder=selected,
                    # NOT `or 1`: 0 legitimately disables re-asks (the or-default trap).
                    max_reprompts=int(1 if self.max_reprompts is None else self.max_reprompts),
                    max_retries=int(self.max_retries),
                    reprompt=reprompt_fn, llm_repair=repair_fn)
        except Exception as exc:  # noqa: BLE001
            return {"chunk": chunk, "run_uid": None, "final_status": "config_error",
                    "record": None, "response": None, "ladder": None, "obj": None,
                    "transport": {"error": str(exc)[:300], "error_class": type(exc).__name__},
                    "grounding": (None, pipeline.step_entry("skipped", "no run")),
                    "certainty": (None, pipeline.step_entry("skipped", "no run"))}
        single["chunk"] = chunk
        single["tags"] = tags
        if self.log_to_db:
            await self._persist(single, provider, envelope, meta_base)
        offset = int(chunk.get("start") or 0)
        single["grounding"] = pipeline.try_grounding(single["obj"], text, g_cfg, offset=offset)
        single["certainty"] = pipeline.try_certainty(
            single["transport"].get("payload"), envelope.get("schema"), c_cfg,
            engine=provider, request_mode=(single["record"] or {}).get("schema", {}).get("mode"))
        await self._annotate(single, g_cfg, c_cfg)
        return single

    async def _persist(self, r: dict, provider: str, envelope: dict, meta_base: dict) -> None:
        rec, resp, lad, chunk = r["record"], r["response"], r["ladder"], r["chunk"]
        meta = dict(meta_base)
        meta.update({"wrap_uid": chunk.get("wrap_uid"), "chunk_uid": chunk.get("chunk_uid"),
                     "chunk_idx": int(chunk.get("idx", 0)),
                     "chunk_start": chunk.get("start"), "chunk_end": chunk.get("end")})
        try:
            await storage.ensure_schema(dsn=self.pg_dsn or None)
            await storage.save_run(
                {
                    "run_uid": r["run_uid"], "request_uid": rec["request_uid"],
                    "schema_uid": envelope.get("schema_uid"),
                    "schema_encoding": envelope.get("encoding"),
                    "provider": provider,
                    "base_url": rec["target"].get("base_url"),
                    "model_on_wire": rec["target"].get("model_on_wire"),
                    "model_digest": rec["target"].get("model_digest"),
                    "endpoint_tag": rec["target"].get("endpoint_tag"),
                    "seed": (rec.get("policy") or {}).get("seed"),
                    "request_mode": rec["schema"].get("mode"),
                    "input_sha256": rec["input"]["input_sha256"],
                    "input_chars": rec["input"]["input_chars"],
                    "input_text": str(chunk["text"]) if self.store_input_text else None,
                    "data_classification": rec.get("guard", {}).get("data_classification"),
                    "final_status": r["final_status"],
                    "attempts": len(lad["attempts"]) if lad else 1,
                    "layers_used": lad["layers_used"] if lad else [],
                    "total_tokens": (resp or {}).get("usage", {}).get("total_tokens"),
                    "cost_usd": (resp or {}).get("usage", {}).get("cost_usd"),
                    "latency_ms": r["transport"].get("latency_ms"),
                    "silent_failure_flags": (resp or {}).get("silent_failure_flags", []),
                    "request_record": rec,
                    "provider_response": r["transport"].get("payload"),
                    "extracted": r["obj"],
                    "tags": r["tags"] or None,
                    "http_status": r["transport"].get("http_status"),
                    "error_class": r["transport"].get("error_class"),
                    "error": r["transport"].get("error"),
                    "run_metadata": meta,
                },
                dsn=self.pg_dsn or None,
            )
            if lad:
                await storage.save_attempts(r["run_uid"], lad["attempts"], dsn=self.pg_dsn or None)
        except Exception as exc:  # noqa: BLE001 - logging must never lose a chunk
            self.log(f"chunk {chunk.get('idx')}: DB logging failed ({type(exc).__name__}: {exc})")

    async def _annotate(self, r: dict, g_cfg: dict, c_cfg: dict) -> None:
        run_uid = r.get("run_uid")
        if not run_uid:
            return
        g_report, _ = r.get("grounding") or (None, None)
        c_report, _ = r.get("certainty") or (None, None)
        try:
            if g_report and g_cfg.get("log_to_db", True):
                await storage.annotate_run(run_uid, field_grounding=g_report, dsn=self.pg_dsn or None)
            if c_report and c_report.get("ok") and c_cfg.get("log_to_db", True):
                await storage.annotate_run(run_uid, field_logprobs=c_report, dsn=self.pg_dsn or None)
        except Exception as exc:  # noqa: BLE001
            self.log(f"annotation failed ({type(exc).__name__}: {exc}); outputs unaffected")

    async def _run(self) -> dict:
        if self._result is not None:
            return self._result

        client = self._unwrap(self.client)
        envelope_in = self._unwrap(self.schema)
        text = str(self.text or "")
        meta = {r["key"]: r["value"] for r in (self.run_metadata or []) if r.get("key")}

        lt_payload = self._unwrap(self.longtext_cfg)
        lt_cfg = lt_payload.get("wrapper") if isinstance(lt_payload.get("wrapper"), dict) else None
        merger_cfg = lt_payload.get("merger") if isinstance(lt_payload.get("merger"), dict) else {}
        if lt_cfg is None:
            msg = ("Wrapper & Merger Config carries no wrapper section. Wire "
                   "Adapter - Wrapper & Merger's config output.")
            raise ValueError(msg)

        ov = self._unwrap(getattr(self, "overrides", None))
        if ov:
            ex = flowcfg.owned_subset(ov, "extract", flowcfg.EXTRACT_KEYS)
            for k, v in ex.items():
                if k == "ladder" and isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]  # noqa: PLW2901
                setattr(self, k, v)
                meta[f"cfg.extract.{k}"] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
            if ov.get("config_uid"):
                meta["config_uid"] = ov["config_uid"]
            if client:
                kwargs, applied = flowcfg.apply_client_overrides(
                    client.get("provider"), dict(client.get("spec") or {}), ov)
                if applied:
                    client = {**client, "spec": kwargs}

        if not client.get("provider") or not envelope_in.get("schema"):
            msg = "Provider or Schema is missing. Wire a provider payload and a Schema Builder."
            raise ValueError(msg)

        g_cfg = self._config("grounding_cfg", "grounding")
        c_cfg = self._config("certainty_cfg", "certainty")
        plan = pipeline.plan_steps(lt_cfg, g_cfg, c_cfg)
        composed = pipeline.compose(client, envelope_in, g_cfg, c_cfg)
        client_sent, envelope = composed["client"], composed["envelope_sent"]
        provider = client_sent.get("provider")

        steps: dict = {}
        chunks, wrap_doc, steps["wrap"] = pipeline.make_chunks(text, lt_cfg)
        if steps["wrap"]["status"] == "failed":
            msg = f"wrap step failed (breaking): {steps['wrap'].get('reason')}"
            raise ValueError(msg)
        if wrap_doc and lt_cfg.get("log_to_db", True):
            try:
                await storage.ensure_schema(dsn=self.pg_dsn or None)
                await storage.save_wrapping(wrap_doc, store_text=bool(lt_cfg.get("store_text")),
                                            dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001 - derived logging stays guarded
                self.log(f"wrap logging failed ({type(exc).__name__}: {exc})")

        tags = [t.strip() for t in str(self.run_tags or "").split(",") if t.strip()]

        # Armed or not, say so ONCE (not once per chunk): a selected rung that cannot fire
        # is exactly the "absence is not evidence" case.
        llm_repair_off = ("llm_repair" in tuple(self.ladder or ())
                          and not self._unwrap(getattr(self, "repair_client", None)).get("provider"))
        if llm_repair_off:
            self.log("llm_repair selected but no Repair Provider connected; the rung is skipped on every chunk.")

        prefetched: dict[int, dict] = {}
        if self.skip_completed:
            # Identity probe: rebuild each chunk's content-derived run_uid WITHOUT calling
            # anything, then refetch the stored extraction for already-completed ones.
            try:
                # ollama/openrouter are module-level imports now (the repair ladder needs
                # them): a local re-import here would make them function-locals for the
                # WHOLE of _run — the UnboundLocalError trap.
                from extrct.hashing import content_uid as _cu
                uids = {}
                for ch in chunks:
                    ctext = str(ch["text"])
                    if provider == "ollama":
                        spec = ollama.OllamaSpec(**dict(client_sent.get("spec") or {}))
                        body = ollama.build_request(spec, ctext, envelope)
                        rec = ollama.request_record(spec, ctext, envelope, body)
                    else:
                        spec = openrouter.OpenRouterSpec(**dict(client_sent.get("spec") or {}))
                        body, res = openrouter.build_request(spec, ctext, envelope)
                        rec = openrouter.request_record(spec, ctext, envelope, body, res)
                    uids[int(ch["idx"])] = _cu({"request_uid": rec["request_uid"],
                                                "input": rec["input"]["input_sha256"]})
                done = await storage.find_completed(list(uids.values()), dsn=self.pg_dsn or None)
                stored = await storage.fetch_extracted(sorted(done), dsn=self.pg_dsn or None) if done else {}
                for idx, uid in uids.items():
                    if uid in done:
                        prefetched[idx] = {"run_uid": uid, "obj": stored.get(uid)}
            except Exception as exc:  # noqa: BLE001
                self.log(f"resume check failed ({type(exc).__name__}); running all chunks")

        parallel = max(1, min(int(lt_cfg.get("parallel_calls", 4) or 1), 16))
        sem = asyncio.Semaphore(parallel)
        live = [c for c in chunks if int(c.get("idx", 0)) not in prefetched]
        async with httpx.AsyncClient() as http:
            executed = await asyncio.gather(
                *(self._one_chunk(http, sem, c, client_sent, envelope, provider, meta, tags, g_cfg, c_cfg)
                  for c in live),
                return_exceptions=True)

        by_idx: dict[int, dict] = {}
        for ch, res in zip(live, executed, strict=True):
            idx = int(ch.get("idx", 0))
            if isinstance(res, BaseException):
                by_idx[idx] = {"chunk": ch, "run_uid": None, "final_status": "error",
                               "record": None, "response": None, "ladder": None, "obj": None,
                               "transport": {"error": f"{type(res).__name__}: {str(res)[:200]}",
                                             "error_class": type(res).__name__},
                               "grounding": (None, pipeline.step_entry("skipped", "chunk errored")),
                               "certainty": (None, pipeline.step_entry("skipped", "chunk errored"))}
            else:
                by_idx[idx] = res
        for idx, pre in prefetched.items():
            ch = next(c for c in chunks if int(c.get("idx", 0)) == idx)
            note = pipeline.step_entry("skipped", "resumed from stored run")
            by_idx[idx] = {"chunk": ch, "run_uid": pre["run_uid"], "final_status": "skipped_completed",
                           "record": None, "response": None, "ladder": None, "obj": pre["obj"],
                           "transport": {}, "tags": tags, "grounding": (None, note), "certainty": (None, note)}
        chunk_results = [by_idx[i] for i in sorted(by_idx)]

        def rollup(name: str) -> dict:
            entries = [r[name][1] for r in chunk_results]
            if {e["status"] for e in entries} == {"skipped"}:
                return entries[0]
            ok = sum(1 for e in entries if e["status"] == "ok")
            failed = sum(1 for e in entries if e["status"] == "failed")
            reasons = sorted({e.get("reason", "") for e in entries if e.get("reason")})
            return pipeline.step_entry("ok" if failed == 0 else ("failed" if ok == 0 else "ok"),
                                       "; ".join(reasons)[:300], ok=ok, failed=failed, total=len(entries))
        request_ok = sum(1 for r in chunk_results if r["final_status"] in ("ok", "repaired", "skipped_completed"))
        steps["request"] = pipeline.step_entry(
            "ok" if request_ok else "failed", "" if request_ok else "every chunk failed",
            ok=request_ok, failed=len(chunk_results) - request_ok, total=len(chunk_results),
            parallel=parallel)
        steps["grounding"] = rollup("grounding")
        steps["certainty"] = rollup("certainty")

        merge_inputs = [{"chunk_idx": int(r["chunk"].get("idx", 0)),
                         "chunk_uid": r["chunk"].get("chunk_uid"),
                         "run_uid": r["run_uid"], "extracted": r["obj"]} for r in chunk_results]
        merge_result, steps["merge"] = pipeline.try_merge(merge_inputs, merger_cfg, envelope_in)
        if merge_result and merger_cfg.get("log_to_db", True):
            try:
                # wrap_uid joins the merge to its wrap; tags make it filterable — without
                # them the manual-merge queue loses its join (review-wave follow-up).
                await storage.save_merge_run(merge_result,
                                             wrap_uid=(wrap_doc or {}).get("wrap_uid"),
                                             tags=tags or None, dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001
                self.log(f"merge logging failed ({type(exc).__name__}: {exc})")

        record = pipeline.assemble_record(
            plan, steps, chunk_count=len(chunk_results),
            run_uids=[r["run_uid"] for r in chunk_results if r["run_uid"]],
            composition=composed["composition"])
        # The fallback is the SINGLE-CHUNK IDENTITY path only. A failed merge over N>1
        # chunks yields NOTHING here: chunk 0 is the first ~max_chars of the document, and
        # handing it out as the merged view presents a partial answer as document-level
        # truth (review finding 2026-08-14). The per-chunk extractions survive in Chunk Runs.
        merged_obj = (merge_result or {}).get("merged") if merge_result else \
            (chunk_results[0]["obj"] if len(chunk_results) == 1 else None)

        self._result = {"chunks": chunk_results, "merge": merge_result, "steps_record": record,
                        "merged_obj": merged_obj, "tags": tags, "wrap_doc": wrap_doc,
                        "envelope_sent": envelope, "llm_repair_off": llm_repair_off}
        return self._result

    def _batch_status(self, r: dict) -> str:
        """The load-bearing facts of a batch, on every output that sets a status: how many
        chunks made it (steps['request'] is 'ok' if ANY chunk succeeded, so the step chain
        alone hides 19-of-20 failures), how many resumed, and whether the merge died."""
        rec = r["steps_record"]
        req = rec["steps"].get("request", {})
        merge = rec["steps"].get("merge", {})
        resumed = sum(1 for c in r["chunks"] if c["final_status"] == "skipped_completed")
        head = (f"{rec['chunk_count']} chunk(s): {req.get('ok', 0)} ok, {req.get('failed', 0)} failed"
                + (f", {resumed} resumed" if resumed else ""))
        chain = " | ".join(f"{n}:{rec['steps'][n]['status']}" for n in pipeline.STEPS if n in rec["steps"])
        line = f"{head} | {chain}"
        if merge.get("status") == "failed":
            line += (f" | MERGE FAILED ({str(merge.get('reason', ''))[:120]}) — Merged Extraction "
                     "is EMPTY; the per-chunk extractions are in Chunk Runs")
        nm = merge.get("needs_manual", 0)
        if nm:
            line += f" | NEEDS MANUAL: {nm}"
        if r.get("llm_repair_off"):
            line += " | llm_repair OFF (no Repair Provider)"
        return line

    # ---- outputs ---------------------------------------------------------------------

    async def build_merged(self) -> Data:
        """The document-level extraction: the merge result, or the single identity chunk
        when the lane is degraded to one chunk. Same shape either way.

        EMPTY when the merge FAILED over several chunks — a partial first-chunk answer is
        not a document-level result, and the status says MERGE FAILED with the reason."""
        r = await self._run()
        self.status = self._batch_status(r)
        return Data(data=r["merged_obj"] or {})

    async def build_chunk_runs(self) -> DataFrame:
        """One row per chunk. FULL extracted JSON — downstream QC never gets a truncated copy."""
        r = await self._run()
        self.status = self._batch_status(r)
        return DataFrame([
            {"chunk_idx": int(c["chunk"].get("idx", 0)), "chunk_uid": c["chunk"].get("chunk_uid"),
             "wrap_uid": c["chunk"].get("wrap_uid"), "run_uid": c["run_uid"],
             "final_status": c["final_status"],
             "error": (c["transport"] or {}).get("error") or "",
             "extracted": json.dumps(c["obj"], ensure_ascii=False) if c["obj"] else None,
             "grounding": c["grounding"][1]["status"], "certainty": c["certainty"][1]["status"],
             "latency_ms": (c["transport"] or {}).get("latency_ms")}
            for c in r["chunks"]])

    async def build_merge_report(self) -> Data:
        """The merge result with conflicts labeled; major ones are the manual-merge queue."""
        r = await self._run()
        step = r["steps_record"]["steps"]["merge"]
        return Data(data={**(r["merge"] or {}), "step": step})

    async def build_steps(self) -> Data:
        """Declared plan next to executed steps for the WHOLE batch."""
        r = await self._run()
        return Data(data=r["steps_record"])

    async def build_grounding(self) -> Data:
        """Per-chunk grounding reports (spans in DOCUMENT coordinates) + batch h2_clean."""
        r = await self._run()
        reports = [(int(c["chunk"].get("idx", 0)), c["grounding"][0]) for c in r["chunks"]]
        present = [x for x in reports if x[1]]
        return Data(data={
            "h2_clean": all((x[1].get("summary") or {}).get("h2_clean") for x in present) if present else None,
            "per_chunk": [{"chunk_idx": i, **(rep or {})} for i, rep in reports],
            "step": r["steps_record"]["steps"]["grounding"]})

    async def build_certainty(self) -> Data:
        """Per-chunk certainty reports."""
        r = await self._run()
        reports = [(int(c["chunk"].get("idx", 0)), c["certainty"][0]) for c in r["chunks"]]
        return Data(data={
            "per_chunk": [{"chunk_idx": i, **(rep or {})} for i, rep in reports],
            "step": r["steps_record"]["steps"]["certainty"]})

    async def build_batch_report(self) -> Data:
        r = await self._run()
        self.status = self._batch_status(r)
        cost = sum(float((c["response"] or {}).get("usage", {}).get("cost_usd") or 0) for c in r["chunks"])
        by: dict = {}
        for c in r["chunks"]:
            by[c["final_status"]] = by.get(c["final_status"], 0) + 1
        return Data(data={
            "chunks": len(r["chunks"]), "by_status": by, "tags": r["tags"],
            "total_cost_usd": round(cost, 8),
            "wrap_uid": (r["wrap_doc"] or {}).get("wrap_uid"),
            "pipeline_uid": r["steps_record"]["pipeline_uid"],
            "schema_uid_sent": r["envelope_sent"].get("schema_uid"),
            "run_uids": r["steps_record"]["run_uids"],
            "needs_manual": r["steps_record"]["steps"]["merge"].get("needs_manual", 0)})

    async def build_pipeline_uid(self) -> Message:
        """The identity of the whole wrap->merge execution — joins Steps, merge_run, and
        every chunk's run_metadata."""
        r = await self._run()
        return Message(text=r["steps_record"]["pipeline_uid"])
