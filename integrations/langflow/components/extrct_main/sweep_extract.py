"""Sweep Extract — run one extraction per config cell, bounded concurrency, resumable.

The executor half of sweeping (generation lives in Prep - Config Sweep). For each cell:
apply client/extract/schema overrides over the base nodes' outputs, build the request,
call, gate, run the deterministic ladder rungs, persist a first-class run row. Cells are
isolated — one failure becomes a row, never a dead sweep.

    [Client] ───────────┐
    [Prep - Schema Builder] ─Schema──> [Run - Sweep Extract] ─> Results (DataFrame)
    text ───────────────┤                                    ─> Sweep Summary (Data)
    [Prep - Config Sweep] ─Configs──>                        ─> Failed Cells (DataFrame)

Execution is a declared configuration: sequential, or parallel with Max Parallel in
1..16 (semaphore-bounded; the runner's measured per-provider defaults are 4 ollama /
8 openrouter). Identity is content-derived per cell, so Skip Completed resumes a sweep
for free (find_completed) — re-running a 60-cell sweep with 50 done costs 10 calls.

Scope, stated: ladder rungs here are the DETERMINISTIC ones (json_repair, coerce);
reprompt/llm_repair belong to the single-run extract. schema.* sweep keys need the
Variables input (rows to rebuild the envelope per cell) — present, or the cell fails
loudly with the reason.
"""

import asyncio
import json

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DataFrameInput, DropdownInput, HandleInput, IntInput, MessageTextInput, Output,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

from extrct import config as flowcfg
from extrct import content_uid, ollama, openrouter, repair
from extrct import schema as S
from extrct import storage
from extrct.runner import call_once


class ExtrctSweepExtract(Component):
    display_name: str = "Run - Sweep Extract"
    description: str = "One extraction per config cell: bounded concurrency, content-addressed, resumable."
    documentation: str = "docs/extraction-stack/grounding-certainty-design.md"
    icon: str = "layers"
    name: str = "extrct_sweep_extract"

    inputs = [
        HandleInput(name="client", display_name="Client", required=True, input_types=["Data"],
                    info="Base client (an ExtrCT client component). Each cell overrides it per its config."),
        HandleInput(name="schema", display_name="Schema", required=True, input_types=["Data"],
                    info="Base envelope from Prep - Schema Builder."),
        MessageTextInput(name="text", display_name="Text", required=True,
                         info="The document to extract from (same input for every cell — the sweep varies CONFIG)."),
        HandleInput(name="configs", display_name="Configs", required=True,
                    input_types=["DataFrame", "Data"],
                    info="Prep - Config Sweep's Configs output (or a Data payload with a 'configs' list)."),
        DataFrameInput(name="schema_variables", display_name="Variables", required=False,
                       info=("Needed ONLY when the sweep carries schema.* keys: the variable rows "
                             "(Schema Builder's Variables output) so the envelope can be rebuilt "
                             "per cell. Without them, schema.* cells fail loudly.")),
        DropdownInput(name="execution", display_name="Execution",
                      options=["sequential", "parallel"], value="sequential",
                      info=("sequential = one call at a time (kind to the GPU host, deterministic "
                            "ordering). parallel = up to Max Parallel in flight (semaphore-"
                            "bounded; suits OpenRouter). Either way cells are isolated: one "
                            "failure is one row.")),
        IntInput(name="max_parallel", display_name="Max Parallel", value=4,
                 range_spec=RangeSpec(min=1, max=16, step=1, step_type="int"),
                 info="Upper bound on in-flight calls when Execution is parallel. Measured sane defaults: 4 ollama, 8 openrouter."),
        BoolInput(name="skip_completed", display_name="Skip Completed", value=True,
                  info=("Resume: cells whose content-derived run_uid already ended ok/repaired "
                        "in Postgres are skipped, not re-billed. Off forces re-runs (same "
                        "run_uid rows are upserted).")),
        MessageTextInput(name="run_tags", display_name="Run Tags", value="sweep",
                         info="Comma-separated tags stamped on every cell's run (plus each cell's config_uid in run_metadata)."),
        BoolInput(name="log_to_db", display_name="Log To Postgres", value=True,
                  info="Every cell lands in extraction_run exactly like a single-run extract."),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True),
        IntInput(name="max_retries", display_name="Transport Retries", value=2, advanced=True),
    ]

    outputs = [
        Output(name="results", display_name="Results", method="build_results", group_outputs=True),
        Output(name="sweep_summary", display_name="Sweep Summary", method="build_summary", group_outputs=True),
        Output(name="failed_cells", display_name="Failed Cells", method="build_failed", group_outputs=True),
    ]

    _rows: list[dict] | None = None

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def _configs(self) -> list[dict]:
        raw = self.configs
        if hasattr(raw, "to_dict") and hasattr(raw, "columns"):  # DataFrame
            cells = []
            for r in raw.to_dict(orient="records"):
                cfg = json.loads(r["config"]) if isinstance(r.get("config"), str) else dict(r.get("config") or {})
                cfg["config_uid"] = r.get("config_uid") or content_uid(cfg)
                cells.append(cfg)
            return cells
        d = self._unwrap(raw)
        cells = list(d.get("configs") or [])
        if not cells:
            msg = "Configs carries no cells. Wire Prep - Config Sweep's Configs output."
            raise ValueError(msg)
        for c in cells:
            c.setdefault("config_uid", content_uid({k: v for k, v in c.items() if k != "config_uid"}))
        return cells

    def _cell_request(self, base_client: dict, base_env: dict, text: str, cfg: dict) -> dict:
        """Pure per-cell build: overridden spec + envelope + wire body + identity."""
        provider = base_client.get("provider")
        # schema.* keys: rebuild the envelope from variable rows, or fail loudly
        schema_over = flowcfg.owned_subset(cfg, "schema", flowcfg.SCHEMA_KEYS)
        env = base_env
        if schema_over:
            rows_in = self.schema_variables
            rows = rows_in.to_dict(orient="records") if hasattr(rows_in, "to_dict") else list(rows_in or [])
            if not rows:
                msg = f"cell {cfg['config_uid']}: schema.* keys need the Variables input to rebuild the envelope"
                raise ValueError(msg)
            eff = {"encoding": base_env.get("encoding"), "request_evidence": bool(base_env.get("request_evidence")),
                   "root_name": base_env.get("root_name") or "extract",
                   "additional_properties": bool(base_env.get("additional_properties"))}
            eff.update(schema_over)
            if eff["encoding"] not in S.ENCODINGS:
                msg = f"cell {cfg['config_uid']}: schema.encoding {eff['encoding']!r} invalid"
                raise ValueError(msg)
            env = S.schema_envelope(rows, root_name=eff["root_name"], encoding=eff["encoding"],
                                    additional_properties=bool(eff["additional_properties"]),
                                    request_evidence=bool(eff["request_evidence"]))
        kwargs, applied = flowcfg.apply_client_overrides(provider, dict(base_client.get("spec") or {}), cfg)
        extract_over = flowcfg.owned_subset(cfg, "extract", flowcfg.EXTRACT_KEYS)

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
        return {"cfg": cfg, "spec": spec, "env": env, "body": body, "record": record,
                "url": url, "headers": headers, "timeout": timeout, "run_uid": run_uid,
                "provider": provider, "applied": applied, "extract_over": extract_over}

    async def _execute_cell(self, http, sem, prep: dict) -> dict:
        async with sem:
            transport = await call_once(prep["url"], prep["body"], http=http,
                                        headers=prep["headers"], timeout_s=prep["timeout"],
                                        max_retries=int(self.max_retries), retry_on_5xx=False)
        cfg, record = prep["cfg"], prep["record"]
        row = {"config_uid": cfg["config_uid"], "run_uid": prep["run_uid"],
               "config": json.dumps({k: v for k, v in cfg.items() if k != "config_uid"}),
               "skipped": False}
        if transport.get("payload") is None:
            row.update({"final_status": "error", "error": str(transport.get("error"))[:200],
                        "extracted": None, "cost_usd": None, "latency_ms": transport.get("latency_ms")})
            final, lad, resp = "error", None, None
        else:
            reader = ollama.read_response if prep["provider"] == "ollama" else openrouter.read_response
            resp = reader(prep["spec"], transport["payload"])
            if resp["terminal_failure"]:
                final, lad = "invalid", None
                row.update({"final_status": final, "error": "terminal: " + ",".join(resp["silent_failure_flags"]),
                            "extracted": None})
            else:
                ladder_cfg = prep["extract_over"].get("ladder", ["json_repair", "coerce"])
                if isinstance(ladder_cfg, str):
                    ladder_cfg = [x.strip() for x in ladder_cfg.split(",") if x.strip()]
                ladder_cfg = [r for r in ladder_cfg if r in ("json_repair", "coerce")]
                lad = await repair.run_ladder(resp["content"]["raw_text"], prep["env"]["schema"],
                                              ladder=tuple(ladder_cfg))
                final = lad["final_status"]
                row.update({"final_status": final, "error": "",
                            "extracted": json.dumps(lad["obj"], ensure_ascii=False)[:500] if lad["obj"] else None})
            row.update({"cost_usd": (resp or {}).get("usage", {}).get("cost_usd"),
                        "latency_ms": transport.get("latency_ms")})

        if self.log_to_db:
            tags = [t.strip() for t in str(self.run_tags or "").split(",") if t.strip()]
            cfg_tag = prep["extract_over"].get("run_tags")
            if cfg_tag:
                tags += [t.strip() for t in str(cfg_tag).split(",") if t.strip()]
            meta = {f"cfg.{k}": (v if isinstance(v, (str, int, float, bool)) else json.dumps(v))
                    for k, v in cfg.items() if k != "config_uid"}
            meta["config_uid"] = cfg["config_uid"]
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
            except Exception as exc:  # noqa: BLE001 - logging must never lose a cell
                self.log(f"cell {cfg['config_uid']}: DB logging failed ({type(exc).__name__}: {exc})")
        return row

    async def _run_sweep(self) -> list[dict]:
        if self._rows is not None:
            return self._rows
        import httpx

        base_client = self._unwrap(self.client)
        base_env = self._unwrap(self.schema)
        text = str(self.text or "")
        if not base_client.get("provider") or not base_env.get("schema") or not text.strip():
            msg = "Client, Schema and Text are all required."
            raise ValueError(msg)
        cells = self._configs()

        preps, rows = [], []
        for cfg in cells:
            try:
                preps.append(self._cell_request(base_client, base_env, text, cfg))
            except Exception as exc:  # noqa: BLE001 - a bad cell is a row, not a dead sweep
                rows.append({"config_uid": cfg.get("config_uid", "?"), "run_uid": None,
                             "config": json.dumps({k: v for k, v in cfg.items() if k != "config_uid"}),
                             "final_status": "config_error", "error": str(exc)[:200],
                             "extracted": None, "cost_usd": None, "latency_ms": None, "skipped": False})

        if self.skip_completed and preps:
            try:
                done = await storage.find_completed([p["run_uid"] for p in preps], dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001
                self.log(f"resume check failed ({type(exc).__name__}); running all cells")
                done = set()
            for p in [p for p in preps if p["run_uid"] in done]:
                rows.append({"config_uid": p["cfg"]["config_uid"], "run_uid": p["run_uid"],
                             "config": json.dumps({k: v for k, v in p["cfg"].items() if k != "config_uid"}),
                             "final_status": "skipped_completed", "error": "", "extracted": None,
                             "cost_usd": None, "latency_ms": None, "skipped": True})
            preps = [p for p in preps if p["run_uid"] not in done]

        limit = 1 if self.execution == "sequential" else max(1, min(int(self.max_parallel or 4), 16))
        sem = asyncio.Semaphore(limit)
        async with httpx.AsyncClient() as http:
            executed = await asyncio.gather(
                *(self._execute_cell(http, sem, p) for p in preps), return_exceptions=True)
        for p, r in zip(preps, executed, strict=True):
            if isinstance(r, BaseException):
                rows.append({"config_uid": p["cfg"]["config_uid"], "run_uid": p["run_uid"],
                             "config": json.dumps({k: v for k, v in p["cfg"].items() if k != "config_uid"}),
                             "final_status": "error", "error": f"{type(r).__name__}: {str(r)[:180]}",
                             "extracted": None, "cost_usd": None, "latency_ms": None, "skipped": False})
            else:
                rows.append(r)
        rows.sort(key=lambda r: r["config_uid"])
        self._rows = rows
        return rows

    async def build_results(self) -> DataFrame:
        rows = await self._run_sweep()
        by = {}
        for r in rows:
            by[r["final_status"]] = by.get(r["final_status"], 0) + 1
        self.status = f"{len(rows)} cell(s): " + ", ".join(f"{k}={v}" for k, v in sorted(by.items()))
        return DataFrame(rows)

    async def build_summary(self) -> Data:
        rows = await self._run_sweep()
        cost = sum(float(r["cost_usd"]) for r in rows if r.get("cost_usd"))
        by = {}
        for r in rows:
            by[r["final_status"]] = by.get(r["final_status"], 0) + 1
        return Data(data={"cells": len(rows), "by_status": by,
                          "total_cost_usd": round(cost, 8),
                          "execution": self.execution,
                          "max_parallel": 1 if self.execution == "sequential" else int(self.max_parallel or 4),
                          "run_uids": [r["run_uid"] for r in rows if r["run_uid"]]})

    async def build_failed(self) -> DataFrame:
        rows = await self._run_sweep()
        bad = [r for r in rows if r["final_status"] not in ("ok", "repaired", "skipped_completed")]
        return DataFrame(bad or [{"note": "no failed cells"}])
