"""Prep - Input Bank — the batch's input axis: texts with optional ids, identity by content.

One row per document to benchmark on. The optional `input_id` is a JOIN LABEL (it
rides run_metadata and the results rows) — identity is `input_sha256`, computed here,
so RE-LABELLING A ROW NEVER RE-BILLS IT (batch-def D4). Wire the rows into
Run - Batch's Inputs; a wired DataFrame/Data replaces the hand table (and a
wired-but-EMPTY source raises instead of silently falling back — the Schema Builder
lesson, 2026-08-14).

 as always: synthetic / de-identified text only.
"""

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output, TableInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.table import EditMode

from extrct import batch
from extrct.hashing import content_uid

COLUMNS = [
    {"name": "input_id", "display_name": "ID (optional)", "type": "str", "default": "",
     "description": "Your label for this document — e.g. a synthetic record number. Join label only; never part of identity. Must be unique when set.",
     "edit_mode": EditMode.INLINE},
    {"name": "text", "display_name": "Text", "type": "str", "default": "",
     "description": "The document text. Rows with empty text raise loudly.",
     "edit_mode": EditMode.POPOVER},
]


class ExtrctInputBank(Component):
    display_name = "Prep - Input Bank"
    description = "The batch input axis: texts with optional ids; identity is the content hash, labels never re-bill."
    documentation = "docs/system-arch/extraction-stack/batch-design.md"
    icon = "library"
    name = "extrct_input_bank"

    inputs = [
        TableInput(
            name="input_rows", display_name="Inputs", table_schema=COLUMNS, value=[],
            required=True,
            info="One row per document. Paste texts here, or wire a list below (which replaces this table).",
        ),
        HandleInput(
            name="wired_inputs", display_name="Inputs (wired)", required=False,
            input_types=["Data", "DataFrame"],
            info=("Optional: a DataFrame with input_id/text columns, or a Data payload "
                  "{\"inputs\": [{\"input_id\", \"text\"}, ...]}. When connected it REPLACES the "
                  "table; connected-but-empty raises (no silent fallback)."),
        ),
    ]

    outputs = [
        Output(name="inputs_out", display_name="Inputs", method="build_inputs", group_outputs=True),
        Output(name="manifest", display_name="Manifest", method="build_manifest", group_outputs=True),
    ]

    def _rows(self) -> list[dict]:
        wired = getattr(self, "wired_inputs", None)
        # An unwired DataFrameInput arrives as '' not None (measured 2026-08-14) —
        # detect wiring by shape, then refuse empty-wired loudly.
        has_wired = wired is not None and not (isinstance(wired, str) and not wired)
        if has_wired:
            rows = batch.rows_from(wired, "inputs")
            if not rows:
                msg = ("Inputs (wired) is connected but carries 0 rows — check the upstream "
                       "output; the hand table is deliberately NOT used as a fallback.")
                raise ValueError(msg)
            return batch.normalize_inputs(rows)
        return batch.normalize_inputs([r for r in (self.input_rows or [])
                                       if str(r.get("text") or "").strip()
                                       or str(r.get("input_id") or "").strip()])

    def build_inputs(self) -> DataFrame:
        rows = self._rows()
        labeled = sum(1 for r in rows if r["input_id"])
        self.status = (f"{len(rows)} input(s), {labeled} labeled | "
                       f"bank {content_uid([r['input_sha256'] for r in rows])}")
        return DataFrame([{**r, "chars": len(r["text"])} for r in rows])

    def build_manifest(self) -> Data:
        rows = self._rows()
        return Data(data={
            "count": len(rows),
            "bank_uid": content_uid([r["input_sha256"] for r in rows]),
            "inputs": [{"input_id": r["input_id"], "input_sha256": r["input_sha256"],
                        "chars": len(r["text"])} for r in rows],
        })
