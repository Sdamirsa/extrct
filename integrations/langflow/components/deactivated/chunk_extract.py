"""Run - Chunk Extract — one extraction per chunk, bounded concurrency, resumable.

The executor of the long-text lane (wrapping lives in Prep - Text Wrapper, merging in
PostPrep - Extraction Merger):

    [Client] ─────────────────┐
    [Prep - Schema Builder] ──Schema──> [Run - Chunk Extract] ─> Extractions (DataFrame)
    [Prep - Text Wrapper] ─Chunks────>                        ─> Chunk Report (Data)

Same machinery as Run - Sweep Extract, applied across CHUNKS instead of config cells:
per chunk build request -> call -> gate -> deterministic ladder rungs (json_repair,
coerce) -> first-class run row with wrap_uid/chunk_uid/idx/span in run_metadata, so the
run log can always answer "which slice of which document said this". Chunks are isolated
— one failure is one row, never a dead batch. Identity is content-derived per chunk, so
Skip Completed resumes for free — and skipped chunks REFETCH their stored extraction
from Postgres, so the Extractions output is always complete for the Merger.

The Overrides input takes a Flow - Controller thread: extract.* keys apply here,
client keys (ollama.*/openrouter.*/client.*) apply over the wired client's spec.
"""

import asyncio
import json

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DropdownInput, HandleInput, IntInput, MessageTextInput, Output,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

from extrct import config as flowcfg
from extrct import content_uid, ollama, openrouter, repair
from extrct import storage
from extrct.runner import call_once


class ExtrctChunkExtract(Component):
    display_name: str = "Run - Chunk Extract"
    description: str = "One extraction per chunk: bounded concurrency, content-addressed, resumable."
    documentation: str = "docs/extraction-stack/long-text-design.md"
    icon: str = "rows-3"
    name: str = "extrct_chunk_extract"

    inputs = [
        HandleInput(name="client", display_name="Client", required=True, input_types=["Data"],
                    info="An ExtrCT client component. One spec serves every chunk (Overrides may adjust it)."),
        HandleInput(name="schema", display_name="Schema", required=True, input_types=["Data"],
                    info="The envelope from Prep - Schema Builder. One schema serves every chunk."),
        HandleInput(name="chunks", display_name="Chunks", required=True,
                    input_types=["DataFrame", "Data"],
                    info="Prep - Text Wrapper's Chunks output (rows with idx, chunk_uid, wrap_uid, text)."),
        DropdownInput(name="execution", display_name="Execution",
                      options=["sequential", "parallel"], value="sequential",
                      info=("sequential = one call at a time (kind to the GPU host, deterministic "
                            "ordering). parallel = up to Max Parallel in flight. Either way "
                            "chunks are isolated: one failure is one row.")),
        IntInput(name="max_parallel", display_name="Max Parallel", value=4,
                 range_spec=RangeSpec(min=1, max=16, step=1, step_type="int"),
                 info="In-flight bound when parallel. Measured sane defaults: 4 ollama, 8 openrouter."),
        BoolInput(name="skip_completed", display_name="Skip Completed", value=True,
                  info=("Resume: chunks whose content-derived run_uid already ended ok/repaired "
                        "are not re-billed — their stored extraction is refetched so the "
                        "Extractions output stays complete for the Merger.")),
        MessageTextInput(name="run_tags", display_name="Run Tags", value="chunked",
                         info="Comma-separated tags stamped on every chunk's run."),
        BoolInput(name="log_to_db", display_name="Log To Postgres", value=True,
                  info="Every chunk lands in extraction_run exactly like a single-run extract."),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True),
        IntInput(name="max_retries", display_name="Transport Retries", value=2, advanced=True),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: a Flow - Controller thread. extract.* keys apply to this "
                          "node; client keys apply over the wired client's spec. Unwired = "
                          "everything behaves exactly as set.")),
    ]

    outputs = [
        Output(name="extractions", display_name="Extractions", method="build_extractions",
               group_outputs=True),
        Output(name="chunk_report", display_name="Chunk Report", method="build_report",
               group_outputs=True),
    ]

    _rows: list[dict] | None = None

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def _chunks(self) -> list[dict]:
        raw = self.chunks
        if hasattr(raw, "to_dict") and hasattr(raw, "columns"):
            rows = raw.to_dict(orient="records")
        else:
            rows = list(self._unwrap(raw).get("chunks") or [])
        if not rows:
            msg = "Chunks carries no rows. Wire Prep - Text Wrapper's Chunks output."
            raise ValueError(msg)
        for r in rows:
            if not str(r.get("text") or "").strip():
                msg = f"chunk idx={r.get('idx')} has no text"
                raise ValueError(msg)
        return sorted(rows, key=lambda r: int(r.get("idx", 0)))

    def _prep(self, base_client: dict, env: dict, chunk: dict, ov: dict) -> dict:
        provider = base_client.get("provider")
        kwargs, applied = flowcfg.apply_client_overrides(provider, dict(base_client.get("spec") or {}), ov)
        text = str(chunk["text"])
        if provider == "ollama":
            spec = ollama.OllamaSpec(**kwargs)
            body = ollama.build_request(spec, text, env)
            record = ollama.request_record(spec, text, env, body)
            url = f"{spec.base_url}{base_client.get('api_path', '/api/chat')}"
            headers, timeout = {}, spec.timeout_s
        elif provider == "openrouter":
            spec = openrouter.OpenRouterSpec(**kwargs)
            openrouter.check_egress(spec)
            body, resolution = openrouter.build_request(spec, text, env)
            record = openrouter.request_record(spec, text, env, body, resolution)
            url = openrouter.target_url(spec)
            headers, timeout = openrouter.headers(spec), spec.timeout_s
        else:
            msg = f"unknown provider {provider!r}"
            raise ValueError(msg)
        run_uid = content_uid({"request_uid": record["request_uid"],
                               "input": record["input"]["input_sha256"]})
        return {"chunk": chunk, "spec": spec, "env": env, "body": body, "record": record,
                "url": url, "headers": headers, "timeout": timeout, "run_uid": run_uid,
                "provider": provider, "applied": applied}

    async def _execute(self, http, sem, prep: dict, extract_over: dict, config_uid) -> dict:
        async with sem:
            transport = await call_once(prep["url"], prep["body"], http=http,
                                        headers=prep["headers"], timeout_s=prep["timeout"],
                                        max_retries=int(self.max_retries), retry_on_5xx=False)
        chunk, record = prep["chunk"], prep["record"]
        row = {"chunk_idx": int(chunk.get("idx", 0)), "chunk_uid": chunk.get("chunk_uid"),
               "wrap_uid": chunk.get("wrap_uid"), "run_uid": prep["run_uid"], "skipped": False}
        if transport.get("payload") is None:
            row.update({"final_status": "error", "error": str(transport.get("error"))[:200],
                        "extracted": None, "cost_usd": None, "latency_ms": transport.get("latency_ms")})
            final, lad, resp = "error", None, None
        else:
            reader = ollama.read_response if prep["provider"] == "ollama" else openrouter.read_response
            resp = reader(prep["spec"], transport["payload"])
            if resp["terminal_failure"]:
                final, lad = "invalid", None
                row.update({"final_status": final,
                            "error": "terminal: " + ",".join(resp["silent_failure_flags"]),
                            "extracted": None})
            else:
                ladder_cfg = extract_over.get("ladder", ["json_repair", "coerce"])
                if isinstance(ladder_cfg, str):
                    ladder_cfg = [x.strip() for x in ladder_cfg.split(",") if x.strip()]
                ladder_cfg = [r for r in ladder_cfg if r in ("json_repair", "coerce")]
                lad = await repair.run_ladder(resp["content"]["raw_text"], prep["env"]["schema"],
                                              ladder=tuple(ladder_cfg))
                final = lad["final_status"]
                # FULL extracted JSON on the wire - the Merger consumes it, never truncate
                row.update({"final_status": final, "error": "",
                            "extracted": json.dumps(lad["obj"], ensure_ascii=False) if lad["obj"] else None})
            row.update({"cost_usd": (resp or {}).get("usage", {}).get("cost_usd"),
                        "latency_ms": transport.get("latency_ms")})

        if self.log_to_db:
            tags = [t.strip() for t in str(self.run_tags or "").split(",") if t.strip()]
            meta = {"wrap_uid": chunk.get("wrap_uid"), "chunk_uid": chunk.get("chunk_uid"),
                    "chunk_idx": int(chunk.get("idx", 0)),
                    "chunk_start": chunk.get("start"), "chunk_end": chunk.get("end")}
            if config_uid:
                meta["config_uid"] = config_uid
            try:
                await storage.ensure_schema(dsn=self.pg_dsn or None)
                await storage.save_run({
                    "run_uid": prep["run_uid"], "request_uid": record["request_uid"],
                    "schema_uid": prep["env"].get("schema_uid"), "schema_encoding": prep["env"].get("encoding"),
                    "provider": prep["provider"], "base_url": record["target"].get("base_url"),
                    "model_on_wire": record["target"].get("model_on_wire"),
                    "endpoint_tag": record["target"].get("endpoint_tag"),
                    "request_mode": record["schema"].get("mode"),
                    "input_sha256": record["input"]["input_sha256"],
                    "input_chars": record["input"]["input_chars"], "input_text": None,
                    "data_classification": record.get("guard", {}).get("data_classification"),
                    "final_status": final, "attempts": len(lad["attempts"]) if lad else 1,
                    "layers_used": lad["layers_used"] if lad else [],
                    "total_tokens": (resp or {}).get("usage", {}).get("total_tokens"),
                    "cost_usd": (resp or {}).get("usage", {}).get("cost_usd"),
                    "latency_ms": transport.get("latency_ms"),
                    "silent_failure_flags": (resp or {}).get("silent_failure_flags", []),
                    "request_record": record,
                    "provider_response": transport.get("payload"),
                    "extracted": (lad or {}).get("obj"),
                    "tags": tags or None, "run_metadata": meta,
                }, dsn=self.pg_dsn or None)
                if lad:
                    await storage.save_attempts(prep["run_uid"], lad["attempts"], dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001 - logging must never lose a chunk
                self.log(f"chunk {row['chunk_idx']}: DB logging failed ({type(exc).__name__}: {exc})")
        return row

    async def _run_batch(self) -> list[dict]:
        if self._rows is not None:
            return self._rows
        import httpx

        base_client = self._unwrap(self.client)
        env = self._unwrap(self.schema)
        if not base_client.get("provider") or not env.get("schema"):
            msg = "Client and Schema are both required."
            raise ValueError(msg)
        ov = self._unwrap(getattr(self, "overrides", None))
        extract_over = flowcfg.owned_subset(ov, "extract", flowcfg.EXTRACT_KEYS) if ov else {}
        config_uid = ov.get("config_uid") if ov else None
        chunks = self._chunks()

        preps, rows = [], []
        for chunk in chunks:
            try:
                preps.append(self._prep(base_client, env, chunk, ov or {}))
            except Exception as exc:  # noqa: BLE001 - a bad chunk is a row, not a dead batch
                rows.append({"chunk_idx": int(chunk.get("idx", 0)), "chunk_uid": chunk.get("chunk_uid"),
                             "wrap_uid": chunk.get("wrap_uid"), "run_uid": None,
                             "final_status": "config_error", "error": str(exc)[:200],
                             "extracted": None, "cost_usd": None, "latency_ms": None, "skipped": False})

        if self.skip_completed and preps:
            try:
                done = await storage.find_completed([p["run_uid"] for p in preps], dsn=self.pg_dsn or None)
                stored = await storage.fetch_extracted(sorted(done), dsn=self.pg_dsn or None) if done else {}
            except Exception as exc:  # noqa: BLE001
                self.log(f"resume check failed ({type(exc).__name__}); running all chunks")
                done, stored = set(), {}
            for p in [p for p in preps if p["run_uid"] in done]:
                ex = stored.get(p["run_uid"])
                rows.append({"chunk_idx": int(p["chunk"].get("idx", 0)),
                             "chunk_uid": p["chunk"].get("chunk_uid"),
                             "wrap_uid": p["chunk"].get("wrap_uid"), "run_uid": p["run_uid"],
                             "final_status": "skipped_completed", "error": "",
                             "extracted": json.dumps(ex, ensure_ascii=False) if ex else None,
                             "cost_usd": None, "latency_ms": None, "skipped": True})
            preps = [p for p in preps if p["run_uid"] not in done]

        limit = 1 if self.execution == "sequential" else max(1, min(int(self.max_parallel or 4), 16))
        sem = asyncio.Semaphore(limit)
        async with httpx.AsyncClient() as http:
            executed = await asyncio.gather(
                *(self._execute(http, sem, p, extract_over, config_uid) for p in preps),
                return_exceptions=True)
        for p, r in zip(preps, executed, strict=True):
            if isinstance(r, BaseException):
                rows.append({"chunk_idx": int(p["chunk"].get("idx", 0)),
                             "chunk_uid": p["chunk"].get("chunk_uid"),
                             "wrap_uid": p["chunk"].get("wrap_uid"), "run_uid": p["run_uid"],
                             "final_status": "error", "error": f"{type(r).__name__}: {str(r)[:180]}",
                             "extracted": None, "cost_usd": None, "latency_ms": None, "skipped": False})
            else:
                rows.append(r)
        rows.sort(key=lambda r: r["chunk_idx"])
        self._rows = rows
        return rows

    async def build_extractions(self) -> DataFrame:
        rows = await self._run_batch()
        by = {}
        for r in rows:
            by[r["final_status"]] = by.get(r["final_status"], 0) + 1
        self.status = f"{len(rows)} chunk(s): " + ", ".join(f"{k}={v}" for k, v in sorted(by.items()))
        return DataFrame(rows)

    async def build_report(self) -> Data:
        rows = await self._run_batch()
        cost = sum(float(r["cost_usd"]) for r in rows if r.get("cost_usd"))
        by = {}
        for r in rows:
            by[r["final_status"]] = by.get(r["final_status"], 0) + 1
        return Data(data={"chunks": len(rows), "by_status": by,
                          "total_cost_usd": round(cost, 8),
                          "wrap_uid": rows[0].get("wrap_uid") if rows else None,
                          "run_uids": [r["run_uid"] for r in rows if r["run_uid"]]})
