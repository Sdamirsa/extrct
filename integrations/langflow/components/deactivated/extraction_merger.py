"""PostPrep - Extraction Merger — per-chunk extractions -> one record, conflicts labeled.

The closing node of the long-text lane (merge-def/1.0, extrct.merging):

    [Run - Chunk Extract] ─Extractions──> [PostPrep - Extraction Merger] ─> Merged (clean, schema-shaped)
    [Prep - Schema Builder] ─Schema (opt)─>                        ─> Merge Report (full detail)
                                                                   ─> Conflicts (DataFrame)
                                                                   ─> Adjudication Task (Message)

Votes with abstention (a null chunk did not see the variable), normalized grouping with
numeric tolerance, similarity-clustered list dedup (blocking key optional), and a
conflict is SURFACED, never silently resolved: every disputed variable keeps its full
candidate set, provenance and severity in the report, whatever the strategy chose.

Deterministic, no LLM inside. The Adjudication Task output is the LLM rung: a composed
prompt over the major conflicts — feed it (plus the source text) through a Structured
Extract, the same pattern as post-hoc grounding. Merges persist to merge_run keyed by
the content-addressed merge_uid.
"""

import json

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    DropdownInput, FloatInput, HandleInput, MessageTextInput, Output, BoolInput,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message

from extrct import merging, storage
from extrct.config import owned_subset
from extrct.flow_model import MERGER_KEYS


class ExtrctExtractionMerger(Component):
    display_name: str = "PostPrep - Extraction Merger"
    description: str = "Merge chunk extractions: vote, dedup lists by similarity, label every conflict."
    documentation: str = "docs/system-arch/extraction-stack/long-text-design.md"
    icon: str = "merge"
    name: str = "extrct_extraction_merger"

    inputs = [
        HandleInput(name="extractions", display_name="Extractions", required=True,
                    input_types=["DataFrame", "Data"],
                    info="Run - Chunk Extract's Extractions output (rows with chunk_idx, run_uid, extracted)."),
        HandleInput(name="schema", display_name="Schema", required=False, input_types=["Data"],
                    info="Optional: the envelope the chunks were extracted with — recorded for audit."),
        DropdownInput(name="scalar_strategy", display_name="Scalar Strategy",
                      options=list(merging.SCALAR_STRATEGIES), value="majority",
                      info=("How a disputed scalar resolves: majority (ties break by document "
                            "order), first/last (document order), longest (most specific "
                            "text), refuse (null + label). The conflict is LABELED regardless.")),
        DropdownInput(name="conflict_policy", display_name="Conflict Policy",
                      options=list(merging.CONFLICT_POLICIES), value="label_and_resolve",
                      info=("label_and_null: a MAJOR conflict (tie, or no candidate above half "
                            "the votes) yields null instead of the strategy's pick — the honest "
                            "answer when the document genuinely disagrees with itself.")),
        FloatInput(name="numeric_tolerance", display_name="Numeric Tolerance", value=0.01,
                   advanced=True,
                   info="Relative tolerance for grouping numbers (0.01 = 1%%): 55 and 55.2 agree, 55 and 45 conflict."),
        FloatInput(name="list_similarity", display_name="List Similarity", value=0.85,
                   range_spec=RangeSpec(min=0.5, max=1.0, step=0.05, step_type="float"),
                   info=("Threshold for clustering list items extracted twice from overlapping "
                         "chunks ('Aspirin 100 mg daily' vs 'aspirin 100mg daily'). Higher = "
                         "stricter = more duplicates survive.")),
        MessageTextInput(name="list_key", display_name="List Key", value="", advanced=True,
                         info=("Optional blocking key for object lists: match items on THIS "
                               "field only (e.g. 'name' for findings). Empty = composite "
                               "similarity over all shared scalar fields.")),
        MessageTextInput(name="run_tags", display_name="Merge Tags", value="",
                         info="Comma-separated tags stored on the merge_run row."),
        BoolInput(name="log_to_db", display_name="Log Merge In Postgres", value=True,
                  info="Writes the merge_run row keyed by the content-addressed merge_uid."),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: Flow - Controller's Merger Config. Keys merger.*: "
                          "scalar_strategy, conflict_policy, numeric_tolerance, list_similarity, "
                          "list_key, log_to_db. Unwired = this node behaves exactly as set.")),
    ]

    outputs = [
        Output(name="merged", display_name="Merged", method="build_merged", group_outputs=True),
        Output(name="merge_report", display_name="Merge Report", method="build_report", group_outputs=True),
        Output(name="conflicts", display_name="Conflicts", method="build_conflicts", group_outputs=True),
        Output(name="adjudication", display_name="Adjudication Task", method="build_adjudication",
               group_outputs=True),
    ]

    _result: dict | None = None

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    # NOT `_inputs`: the Component base class owns that attribute (a dict of the node's
    # inputs) - shadowing it with a method dies with "'dict' object is not callable".
    def _gather(self) -> tuple[list[dict], str | None]:
        raw = self.extractions
        if hasattr(raw, "to_dict") and hasattr(raw, "columns"):
            rows = raw.to_dict(orient="records")
        else:
            rows = list(self._unwrap(raw).get("rows") or [])
        inputs, wrap_uid = [], None
        for r in rows:
            wrap_uid = wrap_uid or r.get("wrap_uid")
            ex = r.get("extracted")
            if isinstance(ex, str) and ex.strip():
                ex = json.loads(ex)
            if isinstance(ex, dict):
                inputs.append({"chunk_idx": int(r.get("chunk_idx", 0)),
                               "chunk_uid": r.get("chunk_uid"), "run_uid": r.get("run_uid"),
                               "extracted": ex})
        if not inputs:
            msg = ("Extractions carries no usable extracted objects. Wire Run - Chunk "
                   "Extract's Extractions output (and check its status for failures).")
            raise ValueError(msg)
        return inputs, wrap_uid

    async def _run(self) -> dict:
        if self._result is not None:
            return self._result
        ov = self._unwrap(getattr(self, "overrides", None))
        cfg = owned_subset(ov, "merger", MERGER_KEYS) if ov else {}
        inputs, wrap_uid = self._gather()
        result = merging.merge_extractions(
            inputs,
            schema=self._unwrap(getattr(self, "schema", None)) or None,
            config={
                "scalar_strategy": str(cfg.get("scalar_strategy", self.scalar_strategy)),
                "conflict_policy": str(cfg.get("conflict_policy", self.conflict_policy)),
                "numeric_tolerance": float(cfg.get("numeric_tolerance", self.numeric_tolerance)),
                "list_similarity": float(cfg.get("list_similarity", self.list_similarity)),
                "list_key": str(cfg.get("list_key", self.list_key) or ""),
            },
        )
        result["wrap_uid"] = wrap_uid
        if ov.get("config_uid"):
            result["config_uid"] = ov["config_uid"]
        if bool(cfg.get("log_to_db", self.log_to_db)):
            tags = [t.strip() for t in str(self.run_tags or "").split(",") if t.strip()]
            try:
                await storage.ensure_schema(dsn=self.pg_dsn or None)
                await storage.save_merge_run(result, wrap_uid=wrap_uid, tags=tags or None,
                                             dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001 - logging must never lose the merge
                self.log(f"DB logging failed ({type(exc).__name__}: {exc}); outputs unaffected")
        self._result = result
        return result

    async def build_merged(self) -> Data:
        r = await self._run()
        s = r["stats"]
        self.status = (f"{s['inputs']} chunk(s) -> {s['variables']} variable(s) | "
                       f"{s['conflicts']} conflict(s), {s['major_conflicts']} major | "
                       f"merge_uid={r['merge_uid']}")
        return Data(data=r["merged"])

    async def build_report(self) -> Data:
        r = await self._run()
        return Data(data={k: r[k] for k in ("version", "merge_uid", "wrap_uid", "variables",
                                            "conflicts", "config", "stats", "run_uids")})

    async def build_conflicts(self) -> DataFrame:
        r = await self._run()
        rows = [{"path": c["path"], "severity": c["severity"],
                 "resolved": json.dumps(c["resolved"], ensure_ascii=False),
                 "candidates": "; ".join(
                     f"{json.dumps(g['value'], ensure_ascii=False)} x{g['count']} (chunks {g['chunks']})"
                     for g in c["candidates"])}
                for c in r["conflicts"]]
        return DataFrame(rows or [{"note": "no conflicts - all chunks agree"}])

    async def build_adjudication(self) -> Message:
        r = await self._run()
        return Message(text=merging.adjudication_task(r))
