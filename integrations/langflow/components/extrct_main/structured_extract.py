"""Structured Extract — ONE extraction through the composed pipeline: request -> ground -> score.

Config-author inputs (2026-08-14): Provider, Schema, and the two XAI configs. The
router composes the request from them (pipeline.compose: certainty logprob riders
upgrade-only; grounding inline/auto injects the evidence mirror into the CLEAN schema
at request time — killing the old R1-R3 rules at the root), executes ONE extraction
(pipeline.execute_single — the same core Run - Long Text uses per chunk, so single and
batch can never drift), then runs grounding and certainty as NON-BREAKING recorded
steps. The Steps output is the declared-vs-executed record.

OUTPUT SHAPES ARE INVARIANT (user decision 2026-08-14, second round): this node never
chunks and never merges — multiple extractions changing the shape of every output was
the reason the first router design was split. The long-text lane is Run - Long Text,
which consumes the SAME config authors plus Adapter - Wrapper & Merger. With no config
inputs wired this node is byte-identical to its pre-router self (proven old-vs-new).

Measured lesson 2026-08-14 (review wave): the repair rungs' sub-calls bypassed the
truncation gate that execute_single applies to the FIRST call — a length-truncated
reprompt answer is a well-formed prefix, and the next rung would brace-balance it into a
plausible wrong answer with final_status 'repaired'. `_call` now applies the same gate
and discards a truncated sub-call answer.
"""

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


class ExtrctStructuredExtract(Component):
    display_name: str = "Run - Structured Extract"
    description: str = "ONE extraction (never chunked) through the composed pipeline: request, ground, score — with a per-step record."
    documentation: str = "docs/extraction-stack/flow-control.md"
    icon: str = "scan-text"
    name: str = "extrct_structured_extract"

    inputs = [
        DataInput(name="client", display_name="Provider", info="From Provider - Model Server or the Flow - Controller's Ready Provider output.", required=True),
        DataInput(name="schema", display_name="Schema", info="The CLEAN envelope from Prep - Schema Builder (evidence is injected here, at request time).", required=True),
        MessageTextInput(name="text", display_name="Text", required=True,
                         info=("The document to extract from. This node runs ONE pass and never "
                               "chunks — for a document longer than the model's context, use "
                               "Run - Long Text.")),
        HandleInput(
            name="grounding_cfg", display_name="Grounding Config", required=False, input_types=["Data"],
            info=("Optional: XAI - Evidence Grounding's config output. Enabled -> the evidence "
                  "mirror is injected into the schema at request time and the extraction is "
                  "aligned. Unwired = no grounding, schema untouched."),
        ),
        HandleInput(
            name="certainty_cfg", display_name="Certainty Config", required=False, input_types=["Data"],
            info=("Optional: XAI - Certainty Score's config output. Enabled -> logprob riders are "
                  "merged into the provider spec (upgrade-only) and per-variable certainty is "
                  "computed. Unwired = no certainty, provider spec untouched."),
        ),
        HandleInput(
            name="overrides", display_name="Overrides", required=False, input_types=["Data"],
            info=("Optional: a Flow - Config payload. Keys 'extract.X' override this node's "
                  "settings; provider keys apply over the wired provider's spec. config_uid and "
                  "applied values are recorded in run_metadata. Unwired = unchanged behavior."),
        ),
        MessageTextInput(
            name="run_tags",
            display_name="Run Tags",
            info=(
                "Comma-separated labels stored with the run, e.g. 'pilot2, prompt-v3'. They ride "
                "the Provider Response and Run Report outputs and are filterable through the "
                "Registry's runs_by_tag. Tags never change a run's identity, so re-tagging cannot "
                "break resume."
            ),
            value="",
        ),
        MultiselectInput(
            name="ladder",
            display_name="Repair Ladder",
            info=(
                "Rungs always run in a FIXED order: json_repair -> coerce -> reprompt -> "
                "llm_repair. Selecting one turns it on; the order you click does not matter. "
                "json_repair and coerce are deterministic and free. reprompt re-asks the SAME "
                "model with its validation errors (up to Max Reprompts — extra calls, extra "
                "cost). llm_repair needs the Repair Provider below; without it the rung is "
                "skipped and the status says so. coerce never clamps ranges - turning 250 into "
                "100 would launder a wrong answer, and Ollama's grammar does not enforce ranges "
                "at all."
            ),
            options=list(repair.LAYERS),
            value=list(repair.DEFAULT_LADDER),
        ),
        DataInput(
            name="repair_client",
            display_name="Repair Provider",
            info=(
                "Optional, used only by the llm_repair rung. Wire a SECOND provider payload - "
                "often a different or cheaper model - so repair is done by something other than "
                "the model that failed. Without it, llm_repair is skipped and the status says so. "
                "Pick patch_only vs rewrite under Repair Strategy (advanced)."
            ),
            required=False,
        ),
        DropdownInput(
            name="repair_strategy",
            display_name="Repair Strategy",
            info=(
                "patch_only shows the repair model ONLY the schema, the bad output and the errors. "
                "It never sees the source text, so it can reshape what is already there but cannot "
                "invent facts. rewrite also passes the source: it fixes more, but makes the repair "
                "model a second extractor and a second thing to attribute a result to."
            ),
            options=["patch_only", "rewrite"],
            value="patch_only",
            advanced=True,
        ),
        IntInput(name="max_reprompts", display_name="Max Reprompts", value=1, advanced=True,
                 info=("How many times the reprompt rung re-asks the extraction model with its "
                       "validation errors. Only used when 'reprompt' is selected in the Repair "
                       "Ladder. 0 disables re-asks while keeping the rung declared.")),
        IntInput(name="max_retries", display_name="Transport Retries", value=2, advanced=True,
                 info=("Network-level retries per HTTP call (timeouts, connection errors). Never "
                       "re-asks the model about content — that is the Repair Ladder's job.")),
        BoolInput(
            name="log_to_db",
            display_name="Log To Postgres",
            info=("Writes extraction_run + extraction_attempt. This is the error-analysis "
                  "substrate. OFF: nothing is persisted; the canvas outputs still work."),
            value=True,
        ),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True,
                         info=("Leave empty to use the stack's EXTRCT_PG_DSN environment variable "
                               "(the normal case). Set it only to log to a different database.")),
        BoolInput(
            name="store_input_text",
            display_name="Store Input Text",
            info="OFF keeps only a sha256 of the input. Turning this on is a decision-log event.",
            value=False,
            advanced=True,
        ),
        TableInput(
            name="run_metadata",
            display_name="Run Metadata",
            info="Joins this run to a registry uid or a factorial cell.",
            table_schema=[
                {"name": "key", "display_name": "Key", "type": "str", "default": "cell", "edit_mode": EditMode.INLINE},
                {"name": "value", "display_name": "Value", "type": "str", "default": "", "edit_mode": EditMode.INLINE},
            ],
            value=[],
            advanced=True,
        ),
    ]

    # group_outputs=True shows every handle at once. All outputs share one cached
    # _run(); wiring several costs one extraction, not several.
    outputs = [
        Output(name="extracted", display_name="Extracted", method="build_extracted", group_outputs=True),
        Output(name="provider_response", display_name="Provider Response", method="build_provider_response",
               group_outputs=True),
        Output(name="run_report", display_name="Run Report", method="build_report", group_outputs=True),
        Output(name="attempts", display_name="Attempts", method="build_attempts", group_outputs=True),
        Output(name="failed", display_name="Failed", method="build_failed", group_outputs=True),
        Output(name="run_uid", display_name="Run UID", method="build_run_uid", group_outputs=True),
        Output(name="steps_out", display_name="Steps", method="build_steps", group_outputs=True),
        Output(name="grounding_out", display_name="Grounding", method="build_grounding", group_outputs=True),
        Output(name="certainty_out", display_name="Certainty", method="build_certainty", group_outputs=True),
        # Logprobs last: it is the raw material behind Certainty. Edges bind by output
        # NAME, so reordering only affects fresh drags.
        Output(name="logprobs_out", display_name="Logprobs", method="build_logprobs", group_outputs=True),
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

    async def _call(self, provider, spec, client, text, envelope, http) -> str:
        """One call returning raw text. Used by the reprompt and llm_repair rungs.

        Applies the SAME truncation gate execute_single applies to the first call: a
        length-truncated answer is a well-formed prefix, and the next rung would
        brace-balance it into a plausible wrong answer. A gated (or transport-failed)
        sub-call returns "" — the ladder records the rung as a failed attempt.
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
                     f"{str(env.get('error'))[:200]}, http={env.get('http_status')}); rung gets no text")
            return ""
        reader = ollama.read_response if provider == "ollama" else openrouter.read_response
        response = reader(spec, env["payload"])
        if response["terminal_failure"]:
            self.log("repair sub-call returned a TRUNCATED body "
                     f"(flags={response.get('silent_failure_flags')}); discarded — a truncated "
                     "prefix must never enter the ladder (same gate as the first call)")
            return ""
        return response["content"]["raw_text"]

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

    async def _run(self) -> dict:
        if self._result is not None:
            return self._result

        client = self._unwrap(self.client)
        envelope_in = self._unwrap(self.schema)
        text = str(self.text or "")
        meta = {r["key"]: r["value"] for r in (self.run_metadata or []) if r.get("key")}

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
        plan = {k: v for k, v in pipeline.plan_steps(None, g_cfg, c_cfg).items()
                if k in ("request", "grounding", "certainty")}
        composed = pipeline.compose(client, envelope_in, g_cfg, c_cfg)
        client_sent, envelope = composed["client"], composed["envelope_sent"]
        provider = client_sent.get("provider")

        async with httpx.AsyncClient() as http:
            reprompt_fn = None
            repair_fn = None
            llm_repair_off = False
            selected = tuple(self.ladder or ())
            if "reprompt" in selected or "llm_repair" in selected:
                spec_for_calls, _ = self._spec_from(client_sent)
                if "reprompt" in selected:
                    async def reprompt_fn(bad, errors, _t=text):  # noqa: ARG001
                        prompt = (_t + "\n\n---\nYour previous answer did not satisfy the schema.\nErrors:\n"
                                  + json.dumps(errors, indent=1)[:2000] + "\nReturn the corrected JSON only.")
                        return await self._call(provider, spec_for_calls, client_sent, prompt, envelope, http)
                if "llm_repair" in selected:
                    rc = self._unwrap(self.repair_client)
                    if rc.get("provider"):
                        async def repair_fn(bad, errors, _rc=rc, _t=text):
                            parts = [
                                "Fix this JSON so it satisfies the schema. Return JSON only.",
                                "Schema:\n" + json.dumps(envelope.get("schema"))[:4000],
                                "Invalid output:\n" + str(bad)[:4000],
                                "Errors:\n" + json.dumps(errors, indent=1)[:2000],
                            ]
                            if self.repair_strategy == "rewrite":
                                parts.insert(1, "Source text:\n" + _t)
                            rspec, rprov = self._spec_from(_rc)
                            return await self._call(rprov, rspec, _rc, "\n\n".join(parts), envelope, http)
                    else:
                        llm_repair_off = True
                        self.log("llm_repair selected but no Repair Provider connected; rung skipped.")

            single = await pipeline.execute_single(
                client_sent, envelope, text, http=http, ladder=selected,
                # NOT `or 1`: 0 legitimately disables re-asks (the or-default trap).
                max_reprompts=int(1 if self.max_reprompts is None else self.max_reprompts),
                max_retries=int(self.max_retries),
                reprompt=reprompt_fn, llm_repair=repair_fn)

        single["tags"] = [t.strip() for t in str(self.run_tags or "").split(",") if t.strip()]
        single["meta"] = meta
        single["llm_repair_off"] = llm_repair_off
        single["db_log_error"] = None
        if self.log_to_db:
            single["db_log_error"] = await self._persist(single, provider, envelope, text)

        steps = {"request": pipeline.step_entry(
            "ok" if single["final_status"] in ("ok", "repaired") else "failed",
            "" if single["final_status"] in ("ok", "repaired") else single["final_status"])}
        g_report, steps["grounding"] = pipeline.try_grounding(single["obj"], text, g_cfg)
        c_report, steps["certainty"] = pipeline.try_certainty(
            single["transport"].get("payload"), envelope.get("schema"), c_cfg,
            engine=provider, request_mode=(single["record"] or {}).get("schema", {}).get("mode"))
        ann_err = await self._annotate(single["run_uid"], g_report, c_report, g_cfg, c_cfg)
        single["db_log_error"] = single["db_log_error"] or ann_err

        record = pipeline.assemble_record(plan, steps, chunk_count=1,
                                          run_uids=[single["run_uid"]],
                                          composition=composed["composition"])
        self._result = {**single, "steps_record": record, "grounding_report": g_report,
                        "certainty_report": c_report, "envelope_sent": envelope}
        return self._result

    async def _persist(self, r: dict, provider: str, envelope: dict, text: str) -> str | None:
        """Guarded (S13) — a dead DB never loses an extraction. Returns the failure tag so
        the status line can shout it: Log To Postgres defaults ON and its rows are the
        error-analysis substrate, so a silent write failure is the most expensive thing
        this node can hide."""
        rec, resp, lad = r["record"], r["response"], r["ladder"]
        spec_policy = (rec or {}).get("policy", {})
        try:
            await storage.ensure_schema(dsn=self.pg_dsn or None)
            await storage.save_run(
                {
                    "run_uid": r["run_uid"],
                    "request_uid": rec["request_uid"],
                    "schema_uid": envelope.get("schema_uid"),
                    "schema_encoding": envelope.get("encoding"),
                    "provider": provider,
                    "base_url": rec["target"].get("base_url"),
                    "model_on_wire": rec["target"].get("model_on_wire"),
                    "model_digest": rec["target"].get("model_digest"),
                    "endpoint_tag": rec["target"].get("endpoint_tag"),
                    "seed": spec_policy.get("seed"),
                    "request_mode": rec["schema"].get("mode"),
                    "input_sha256": rec["input"]["input_sha256"],
                    "input_chars": rec["input"]["input_chars"],
                    "input_text": text if self.store_input_text else None,
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
                    "run_metadata": r["meta"],
                },
                dsn=self.pg_dsn or None,
            )
            if lad:
                await storage.save_attempts(r["run_uid"], lad["attempts"], dsn=self.pg_dsn or None)
        except Exception as exc:  # noqa: BLE001 - logging must never lose an extraction
            self.log(f"DB logging failed ({type(exc).__name__}: {exc}); extraction result is unaffected")
            return type(exc).__name__
        return None

    async def _annotate(self, run_uid, g_report, c_report, g_cfg, c_cfg) -> str | None:
        """Derived annotations onto the run row — guarded, never load-bearing (S13).
        Returns the failure tag for the status line."""
        if not run_uid:
            return None
        try:
            if g_report and g_cfg.get("log_to_db", True):
                await storage.annotate_run(run_uid, field_grounding=g_report, dsn=self.pg_dsn or None)
            if c_report and c_report.get("ok") and c_cfg.get("log_to_db", True):
                await storage.annotate_run(run_uid, field_logprobs=c_report, dsn=self.pg_dsn or None)
        except Exception as exc:  # noqa: BLE001
            self.log(f"annotation failed ({type(exc).__name__}: {exc}); outputs unaffected")
            return f"annotate/{type(exc).__name__}"
        return None

    # ---- outputs ---------------------------------------------------------------------

    async def build_extracted(self) -> Data:
        r = await self._run()
        flags = (r["response"] or {}).get("silent_failure_flags", [])
        lad = r["ladder"] or {}
        head = r["final_status"]
        if head == "repaired":
            # WHICH rung rescued the output is load-bearing for anyone who must exclude
            # repaired runs — it used to require opening the Attempts DataFrame.
            used = ",".join(lad.get("layers_used") or []) or "?"
            head = f"repaired via {used} ({len(lad.get('attempts') or [])} attempts)"
        line = f"{head} | {r['run_uid']}"
        if flags:
            line += f" | flags={flags}"
        if r.get("llm_repair_off"):
            line += " | llm_repair OFF (no Repair Provider)"
        if r.get("db_log_error"):
            line += f" | DB LOG FAILED ({r['db_log_error']})"
        self.status = line
        return Data(data=r["obj"] or {})

    @staticmethod
    def _normalize_logprobs(raw) -> list[dict] | None:
        """One shape for both providers: Ollama returns a top-level list, OpenRouter an
        OpenAI-style {"content": [...]}. Each entry becomes {token, logprob, prob, top}."""
        import math

        if isinstance(raw, dict):
            raw = raw.get("content")
        if not isinstance(raw, list) or not raw:
            return None

        def one(t: dict) -> dict:
            lp = t.get("logprob")
            return {
                "token": t.get("token"),
                "logprob": lp,
                "prob": round(math.exp(lp), 6) if isinstance(lp, (int, float)) else None,
            }

        out = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            entry = one(t)
            entry["top"] = [one(a) for a in (t.get("top_logprobs") or []) if isinstance(a, dict)]
            out.append(entry)
        return out or None

    async def build_logprobs(self) -> Data:
        """Token probabilities, when requested. Joined by run_uid; the verbatim provider
        shape is preserved in provider_response in Postgres."""
        r = await self._run()
        norm = self._normalize_logprobs((r["response"] or {}).get("logprobs"))
        if not norm:
            return Data(data={
                "run_uid": r["run_uid"], "available": False,
                "note": ("No logprobs in the response. Wire a Certainty Config (the rider "
                         "requests them), or turn on Token Logprobs on the provider."),
            })
        lps = [t["logprob"] for t in norm if isinstance(t.get("logprob"), (int, float))]
        return Data(data={
            "run_uid": r["run_uid"],
            "available": True,
            "tokens": len(norm),
            "mean_logprob": round(sum(lps) / len(lps), 4) if lps else None,
            "min_logprob": round(min(lps), 4) if lps else None,
            "content": norm,
        })

    async def build_run_uid(self) -> Message:
        """The run's identity alone, as text — wires straight into the Registry's Run UID
        field (get_run / run_attempts), or anything else that joins on the run."""
        r = await self._run()
        return Message(text=r["run_uid"])

    async def build_provider_response(self) -> Data:
        """The provider body VERBATIM — cost, native timings, generation id, finish reasons.

        `Extracted` stays pure schema output on purpose: mixing bookkeeping into it would
        contaminate every downstream comparison. This output is the bookkeeping, joined to
        the extraction by run_uid.
        """
        r = await self._run()
        return Data(
            data={
                "run_uid": r["run_uid"],
                "tags": r["tags"],
                "http_status": r["transport"].get("http_status"),
                "latency_ms": r["transport"].get("latency_ms"),
                "response": r["transport"].get("payload"),
            }
        )

    async def build_report(self) -> Data:
        r = await self._run()
        resp = r["response"] or {}
        return Data(
            data={
                "run_uid": r["run_uid"],
                "tags": r["tags"],
                "request_uid": r["record"]["request_uid"],
                "served": resp.get("served", {}),
                "schema_uid": r["record"]["schema"].get("schema_uid"),
                "schema_encoding": r["record"]["schema"].get("encoding"),
                "final_status": r["final_status"],
                "layers_used": (r["ladder"] or {}).get("layers_used", []),
                "attempts": len((r["ladder"] or {}).get("attempts", [])) or 1,
                "silent_failure_flags": resp.get("silent_failure_flags", []),
                "usage": resp.get("usage", {}),
                "latency_ms": r["transport"].get("latency_ms"),
                "http_status": r["transport"].get("http_status"),
            }
        )

    async def build_attempts(self) -> DataFrame:
        r = await self._run()
        rows = (r["ladder"] or {}).get("attempts", [])
        return DataFrame(
            [
                {
                    "attempt_no": a["attempt_no"], "layer": a["layer"], "valid": a["valid"],
                    "errors": len(a.get("validation_errors") or []),
                    "first_error": (a.get("validation_errors") or [{}])[0].get("msg", "") if a.get("validation_errors") else "",
                    "note": a.get("note") or "",
                }
                for a in rows
            ]
        )

    async def build_failed(self) -> Data:
        """Empty unless terminal — lets a flow branch on failure instead of dying."""
        r = await self._run()
        if r["final_status"] in ("ok", "repaired"):
            return Data(data={})
        return Data(
            data={
                "run_uid": r["run_uid"],
                "final_status": r["final_status"],
                "http_status": r["transport"].get("http_status"),
                "error_class": r["transport"].get("error_class"),
                "error": r["transport"].get("error"),
                "silent_failure_flags": (r["response"] or {}).get("silent_failure_flags", []),
                "validation_errors": ((r["ladder"] or {}).get("attempts") or [{}])[-1].get("validation_errors", []),
            }
        )

    async def build_steps(self) -> Data:
        """Declared plan next to executed steps — this run's independent measurement record."""
        r = await self._run()
        rec = r["steps_record"]
        line = " | ".join(f"{n}:{rec['steps'][n]['status']}" for n in ("request", "grounding", "certainty"))
        if r.get("db_log_error"):
            line += f" | DB LOG FAILED ({r['db_log_error']})"
        self.status = line
        return Data(data=rec)

    async def build_grounding(self) -> Data:
        """The grounding report for THIS run (empty shell with the step entry when the
        step did not produce one). Shape never varies."""
        r = await self._run()
        report = r["grounding_report"] or {}
        return Data(data={**report, "step": r["steps_record"]["steps"]["grounding"]})

    async def build_certainty(self) -> Data:
        """The certainty report for THIS run (empty shell with the step entry when the
        step did not produce one). Shape never varies."""
        r = await self._run()
        report = r["certainty_report"] or {}
        return Data(data={**report, "step": r["steps_record"]["steps"]["certainty"]})
