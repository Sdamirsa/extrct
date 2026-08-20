"""Run - Batch — the benchmark grid: inputs × configs × schemas, OpenRouter only.

Thin wrapper over extrct.batch (batch-def/1.0; design of record with the D1-D13
decisions: docs/system-arch/extraction-stack/batch-design.md). What the engine guarantees:

- One row per cell, ALWAYS — ok / repaired / failed / config_error /
  skipped_completed / aborted_circuit_breaker. A bad provider config poisons only its
  own column; a failed cell is one row, never a dead batch.
- Cell identity is the existing run_uid formula over (request, input hash) — resume
  (Skip Completed) refetches completed cells instead of re-billing, and the optional
  input id is a LABEL that never enters any hash.
- Pinning is REQUIRED per config (it also disables OpenRouter's sticky routing, which
  would otherwise route the whole benchmark to whichever provider served row one).
- Retries: {429,500,502,503,524,529} with jittered backoff capped 60s — 429s are
  normal operating condition on OpenRouter and are counted, never hidden. A
  non-streaming 200 carrying an in-band provider error is gated as invalid, never
  scored as an answer.
- stop_on_failure_rate circuit breaker: a broken schema burns ~min_sample calls, not
  the whole grid; undispatched cells become aborted rows (absence is not evidence).
- Audit per cell: X-OpenRouter-Metadata rides into the stored provider_response and
  X-Generation-Id into the row + run_metadata — the handle for authoritative post-hoc
  cost via GET /api/v1/generation.

BUDGET RECIPE (documented, user-side): mint a DISPOSABLE OpenRouter key with a spend
limit for the run (openrouter.ai -> Keys; a Management key can set `limit`); put it in
deploy/.env; a blown budget then surfaces as 402 rows and the breaker stops the batch.
"""

import asyncio
import json

import httpx
from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DataInput, FloatInput, HandleInput, IntInput, MessageTextInput, Output,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

from extrct import batch


class ExtrctRunBatch(Component):
    display_name = "Run - Batch"
    description = "The benchmark grid: inputs x configs x schemas — resumable, capped, circuit-broken, fully audited."
    documentation = "docs/system-arch/extraction-stack/batch-design.md"
    icon = "grid-3x3"
    name = "extrct_batch_run"

    inputs = [
        DataInput(name="client", display_name="Provider", required=True,
                  info="An OPENROUTER provider payload (this lane is OpenRouter-only). Each config "
                       "in the grid overrides it; every config must pin an endpoint tag."),
        HandleInput(name="inputs_in", display_name="Inputs", required=True,
                    input_types=["DataFrame", "Data"],
                    info="Prep - Input Bank's Inputs output (or any rows with input_id/text)."),
        HandleInput(name="configs_in", display_name="Configs", required=True,
                    input_types=["DataFrame", "Data"],
                    info="Flow - Sweep's grid output ({config_uid, config}) or an authored list of "
                         "config objects. One column of the grid per config."),
        HandleInput(name="schemas_in", display_name="Schemas", required=True,
                    input_types=["Data", "DataFrame"],
                    info="One Schema Builder envelope, a list of envelopes, or rows of "
                         "{schema_uid, envelope}. One grid axis per schema."),
        BoolInput(name="plan_only", display_name="Plan Only (dry run)", value=False,
                  info="ON: validate everything and emit the Manifest — cell count, bad configs, "
                       "batch_uid — WITHOUT sending a single request. Check this before any "
                       "expensive run."),
        IntInput(name="concurrency", display_name="Concurrency", value=8,
                 range_spec=RangeSpec(min=1, max=16, step=1, step_type="int"),
                 info="In-flight requests. OpenRouter publishes no recommended figure; 8 is our "
                      "engineering default for a pinned endpoint, 16 the ceiling."),
        BoolInput(name="skip_completed", display_name="Skip Completed", value=True,
                  info="Resume: cells whose run_uid already completed refetch their stored "
                       "extraction instead of re-billing. The most valuable property of the design."),
        FloatInput(name="stop_on_failure_rate", display_name="Stop On Failure Rate", value=0.5,
                   advanced=True, range_spec=RangeSpec(min=0.05, max=1.0, step=0.05, step_type="float"),
                   info="Circuit breaker: after Min Sample cells, abort dispatch when the failure "
                        "fraction exceeds this. 1.0 disables the breaker."),
        IntInput(name="min_sample", display_name="Min Sample", value=10, advanced=True,
                 info="Completed cells required before the breaker may judge the failure rate."),
        MessageTextInput(name="run_tags", display_name="Run Tags", value="batch",
                         info="Comma-separated tags on every cell's run row (runs_by_tag filters)."),
        IntInput(name="max_retries", display_name="Transport Retries", value=5, advanced=True,
                 info="Per-call retry cap for {429,500,502,503,524,529} + connect/timeout, jittered "
                      "backoff capped 60s. Deterministic errors (400/404/422) are never retried."),
        BoolInput(name="log_to_db", display_name="Log To Postgres", value=True,
                  info="Every cell lands in extraction_run with batch_uid/cell_id/input_id/"
                       "generation_id in run_metadata — the benchmark's analysis substrate."),
        BoolInput(name="store_input_text", display_name="Store Input Text", value=False, advanced=True,
                  info="OFF keeps only the sha256 per cell."),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True,
                         info="Leave empty to use EXTRCT_PG_DSN from the environment (the normal case)."),
    ]

    outputs = [
        Output(name="results", display_name="Results", method="build_results", group_outputs=True),
        Output(name="batch_report", display_name="Batch Report", method="build_report", group_outputs=True),
        Output(name="failed_cells", display_name="Failed Cells", method="build_failed", group_outputs=True),
        Output(name="manifest_out", display_name="Manifest", method="build_manifest", group_outputs=True),
    ]

    _result: dict | None = None

    @staticmethod
    def _unwrap(value):
        if isinstance(value, list):
            value = value[0] if value else None
        data = getattr(value, "data", value)
        return data if isinstance(data, dict) else {}

    def _axes(self):
        client = self._unwrap(self.client)
        inputs = batch.normalize_inputs(batch.rows_from(self.inputs_in, "inputs"))
        configs = batch.normalize_configs(batch.rows_from(self.configs_in, "configs"))
        schemas = batch.normalize_schemas(self.schemas_in)
        settings = {
            "concurrency": int(self.concurrency or 8),
            "max_retries": int(self.max_retries or 5),
            "stop_on_failure_rate": float(self.stop_on_failure_rate)
            if self.stop_on_failure_rate is not None else 0.5,
            "min_sample": int(self.min_sample or 10),
            "skip_completed": bool(self.skip_completed),
            "run_tags": str(self.run_tags or "batch"),
            "log_to_db": bool(self.log_to_db),
            "store_input_text": bool(self.store_input_text),
        }
        return client, inputs, configs, schemas, settings

    async def _run(self) -> dict:
        if self._result is not None:
            return self._result
        client, inputs, configs, schemas, settings = self._axes()

        if self.plan_only:
            manifest = batch.plan_batch(client, inputs, configs, schemas, settings)
            self._result = {"manifest": manifest, "rows": [], "report": {
                "batch_uid": manifest["batch_uid"], "cells": manifest["cells"],
                "by_status": {"planned": manifest["cells"]}, "total_cost_usd": 0.0,
                "total_retries": 0, "circuit_breaker_tripped": False, "resumed": 0,
                "bad_configs": manifest["bad_configs"], "run_uids": [], "tags": [],
                "plan_only": True}}
            return self._result

        def progress(done, total):
            self.status = f"batch {done}/{total} cells..."

        async with httpx.AsyncClient() as http:
            self._result = await batch.run_batch(
                client, inputs, configs, schemas, settings,
                http=http, dsn=self.pg_dsn or None, on_progress=progress, log=self.log)
        return self._result

    async def build_results(self) -> DataFrame:
        r = await self._run()
        rep = r["report"]
        if rep.get("plan_only"):
            self.status = (f"PLAN ONLY | {rep['cells']} cell(s) | batch {rep['batch_uid']}"
                           + (f" | BAD CONFIGS: {len(rep['bad_configs'])}" if rep["bad_configs"] else ""))
            return DataFrame([])
        parts = [f"{rep['cells']} cell(s)",
                 ", ".join(f"{k}={v}" for k, v in sorted(rep["by_status"].items())),
                 f"cost ${rep['total_cost_usd']}", f"retries {rep['total_retries']}",
                 f"batch {rep['batch_uid']}"]
        if rep["circuit_breaker_tripped"]:
            parts.insert(0, "CIRCUIT BREAKER TRIPPED")
        if rep["bad_configs"]:
            parts.append(f"bad configs: {len(rep['bad_configs'])}")
        if rep["resumed"]:
            parts.append(f"resumed {rep['resumed']}")
        self.status = " | ".join(parts)
        return DataFrame(r["rows"])

    async def build_report(self) -> Data:
        r = await self._run()
        return Data(data=r["report"])

    async def build_failed(self) -> DataFrame:
        """Cells that need attention: everything not ok/repaired/skipped_completed."""
        r = await self._run()
        return DataFrame([row for row in r["rows"]
                          if row["final_status"] not in ("ok", "repaired", "skipped_completed")])

    async def build_manifest(self) -> Data:
        """The DECLARED grid: axes, settings, per-config checks, batch_uid — even
        in Plan Only mode, before anything runs."""
        r = await self._run()
        return Data(data=r["manifest"])
