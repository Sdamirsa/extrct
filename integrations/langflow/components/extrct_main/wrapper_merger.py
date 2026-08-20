"""Adapter - Wrapper & Merger — CONFIG AUTHOR for the pipeline's before-and-after.

An ADAPTER wraps a function: something runs before (chunking) and something runs after
(merging). This node authors BOTH document sections — wrapper.* and merger.* — as one
config payload for RUN - LONG TEXT's Wrapper & Merger Config input (the dedicated batch
runner; Run - Structured Extract stays single-run with invariant output shapes — user
decision 2026-08-14, second round). Enabled, Run - Long Text chunks the text FIRST,
runs one full pipeline per chunk with bounded parallelism, and merges LAST with every
conflict labeled. enabled=false degrades it to one identity chunk, same shapes.

Born 2026-08-14 by absorbing three executor nodes (Prep - Text Wrapper, Run - Chunk
Extract, PostPrep - Extraction Merger — parked in deactivated/): chunking and merging
are pre/post processing that are only meaningful together, so they are authored
together. The logic is unchanged and lives in extrct.wrapping / extrct.merging /
extrct.pipeline.

Merge conflicts that need MANUAL resolution stay first-class: major-severity conflicts
ride the merge_run row and the router's merge step counts them as needs_manual — the
queue a later manual-merge step reads. The Merge Client input is RESERVED for the
next development wave (LLM-based conflict adjudication; rule-based join/dedup methods
over the full variable anatomy land there too) — it is carried in the payload but not
yet consumed by the router, and the status line says so whenever it is wired.

The Overrides input takes the Flow - Controller's Wrapper Config AND Merger Config
threads (either or both; wrapper.* and merger.* keys apply over the widget values). It is
a LIST handle (fixed 2026-08-14): the controller emits the two sections as two separate
Data outputs, and a single-payload handle silently dropped whichever edge came second —
the emitted config still carried config_uid=flow_uid, so a merge_run row claimed a flow
identity whose merger settings were never in effect. Every wired payload is merged, and
values are validated (flow_model.validate_section), not just key names.
"""

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DropdownInput, FloatInput, HandleInput, IntInput, MessageTextInput, Output,
)
from lfx.schema.data import Data

from extrct.config import owned_subset
from extrct.flow_model import MERGER_KEYS, WRAPPER_KEYS, validate_section
from extrct.hashing import content_uid
from extrct.merging import CONFLICT_POLICIES, SCALAR_STRATEGIES
from extrct.wrapping import WRAPPER_LOGICS


class ExtrctWrapperMerger(Component):
    display_name: str = "Adapter - Wrapper & Merger"
    description: str = "Author the long-text lane: chunk first, parallel per-chunk runs, merge last with labeled conflicts."
    documentation: str = "docs/system-arch/extraction-stack/long-text-design.md"
    icon: str = "combine"
    name: str = "extrct_wrapper_merger"

    inputs = [
        BoolInput(name="enabled", display_name="Enabled", value=True,
                  info=("ON: Run - Long Text chunks the text first and merges last. OFF: the whole "
                        "text runs as one piece; this node then only declares the lane is off.")),
        # --- wrapper (before) ---
        DropdownInput(name="logic", display_name="Wrapping Logic", options=list(WRAPPER_LOGICS),
                      value="paragraph_pack",
                      info=("paragraph_pack keeps paragraphs whole (best default for clinical "
                            "notes); sentence_pack when the note is one wall of text; fixed_window "
                            "cuts at exact character counts. Spans are recorded and coverage is "
                            "checked either way.")),
        IntInput(name="max_chars", display_name="Max Chars / Chunk", value=4000,
                 info=("Chunk size in characters (~4 characters per token). Lower it if items late "
                       "in a chunk get missed; raise it only for large-context models.")),
        IntInput(name="overlap_chars", display_name="Overlap Chars", value=400,
                 info="Context carried across cuts — roughly 10% of Max Chars."),
        IntInput(name="parallel_calls", display_name="Parallel Calls", value=4,
                 range_spec=RangeSpec(min=1, max=16, step=1, step_type="int"),
                 info="In-flight bound across chunks. Measured sane defaults: 4 ollama, 8 openrouter."),
        BoolInput(name="store_text", display_name="Store Chunk Text", value=False, advanced=True,
                  info=("OFF (default): only hashes and character spans are stored in "
                        "text_wrapping/text_chunk. ON writes each chunk's raw text into Postgres — "
                        "synthetic or de-identified text only.")),
        # --- merger (after) ---
        DropdownInput(name="scalar_strategy", display_name="Scalar Strategy",
                      options=list(SCALAR_STRATEGIES), value="majority",
                      info=("majority: the most common value wins. first/last: chunk order decides. "
                            "longest: keep the fullest answer. refuse: any disagreement becomes a "
                            "conflict for review. Abstentions (null) never vote.")),
        DropdownInput(name="conflict_policy", display_name="Conflict Policy",
                      options=list(CONFLICT_POLICIES), value="label_and_resolve",
                      info=("label_and_resolve: resolve by strategy AND label the conflict. "
                            "label_and_null: refuse a value, keep the conflict for review. "
                            "Major conflicts are counted as needs_manual either way.")),
        FloatInput(name="numeric_tolerance", display_name="Numeric Tolerance", value=0.01,
                   info=("Relative: 0.01 groups 55 with 55.2 and splits 55 from 45 — it decides "
                         "whether two chunks' numbers agree or become a conflict.")),
        FloatInput(name="list_similarity", display_name="List Similarity", value=0.85, advanced=True,
                   range_spec=RangeSpec(min=0.5, max=1.0, step=0.05, step_type="float"),
                   info="Clustering threshold for list items across chunks (dedup)."),
        MessageTextInput(name="list_key", display_name="List Key", value="", advanced=True,
                         info=("Field that identifies the same item across chunks — e.g. name or "
                               "code for a medication list. Empty: items match by overall "
                               "similarity.")),
        BoolInput(name="log_to_db", display_name="Log To Postgres", value=True,
                  info="text_wrapping/text_chunk rows for the wrap; a merge_run row (conflicts included) for the merge."),
        # --- plumbing last: wire targets grouped at the bottom ---
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    is_list=True,
                    info=("Optional: Flow - Controller's Wrapper Config and/or Merger Config "
                          "threads — wire BOTH if you author both sections (this handle takes "
                          "several edges). wrapper.* and merger.* keys apply over the widget "
                          "values; unknown keys and bad values raise. Unwired = as set here.")),
        HandleInput(name="merge_client", display_name="Merge Client", required=False, input_types=["Data"],
                    info=("RESERVED (next wave): a client payload for LLM-based conflict "
                          "adjudication. Carried in the config; the router does not consume it "
                          "yet — rule-based strategies apply and major conflicts are labeled "
                          "needs_manual for the manual-merge step.")),
    ]

    outputs = [
        Output(name="longtext_config", display_name="Wrapper & Merger Config", method="build_config"),
    ]

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    @staticmethod
    def _unwrap_all(value) -> list[dict]:
        """EVERY wired payload, not just the first — the Wrapper Config and Merger Config
        threads arrive as two separate edges on one handle."""
        items = value if isinstance(value, list) else [value]
        out = []
        for item in items:
            d = getattr(item, "data", item)
            if isinstance(d, dict) and d:
                out.append(d)
        return out

    def _overrides(self) -> tuple[dict, dict, str | None]:
        """(wrapper overrides, merger overrides, config_uid) merged over all wired threads.

        Loud on: unknown keys (owned_subset), bad VALUES (validate_section — owned_subset
        checks names only), the same key wired twice with different values, and threads
        from two different flow documents (their config_uids disagree, so the emitted
        identity would describe neither)."""
        wrapper: dict = {}
        merger: dict = {}
        uids: list[str] = []
        for payload in self._unwrap_all(getattr(self, "overrides", None)):
            for section, bucket, keys in (("wrapper", wrapper, WRAPPER_KEYS),
                                          ("merger", merger, MERGER_KEYS)):
                sub = validate_section(section, owned_subset(payload, section, keys))
                for k, v in sub.items():
                    if k in bucket and bucket[k] != v:
                        msg = (f"two Overrides threads set {section}.{k} differently "
                               f"({bucket[k]!r} vs {v!r}). Wire the threads of ONE flow document.")
                        raise ValueError(msg)
                    bucket[k] = v
            uid = payload.get("config_uid")
            if uid and uid not in uids:
                uids.append(uid)
        if len(uids) > 1:
            msg = (f"Overrides threads carry different config_uids {uids}. They must come from "
                   "ONE Flow - Controller, else the emitted config claims an identity that "
                   "describes neither flow.")
            raise ValueError(msg)
        return wrapper, merger, (uids[0] if uids else None)

    def build_config(self) -> Data:
        wrapper = {
            "enabled": bool(self.enabled),
            "logic": self.logic or "paragraph_pack",
            "max_chars": int(self.max_chars or 4000),
            # NOT `or 400`: zero overlap is a legitimate authored value (caught by test 2026-08-14)
            "overlap_chars": int(self.overlap_chars) if self.overlap_chars is not None else 400,
            "parallel_calls": int(self.parallel_calls or 4),
            "store_text": bool(self.store_text),
            "log_to_db": bool(self.log_to_db),
        }
        merger = {
            "scalar_strategy": self.scalar_strategy or "majority",
            "conflict_policy": self.conflict_policy or "label_and_resolve",
            # NOT `or 0.01`: zero tolerance (exact numeric match) is a legitimate value
            "numeric_tolerance": float(self.numeric_tolerance) if self.numeric_tolerance is not None else 0.01,
            "list_similarity": float(self.list_similarity or 0.85),
            "list_key": str(self.list_key or ""),
            "log_to_db": bool(self.log_to_db),
        }
        w_ov, m_ov, config_uid = self._overrides()
        wrapper.update(w_ov)
        merger.update(m_ov)
        applied = sorted([f"wrapper.{k}" for k in w_ov] + [f"merger.{k}" for k in m_ov])

        merge_client = self._unwrap(getattr(self, "merge_client", None)) or None
        # Identity covers the AUTHORED lane only; the reserved client is deployment-ish
        # and rides beside the uid, not inside it.
        payload = {"wrapper": wrapper, "merger": merger,
                   "config_uid": config_uid or content_uid({"wrapper": wrapper, "merger": merger})}
        if merge_client:
            payload["merge_client"] = merge_client

        note = " | merge_client wired (RESERVED — not consumed until the LLM-adjudication wave)" if merge_client else ""
        # With overrides wired the widgets no longer show the effective config; say so.
        ovnote = f" | OVERRIDDEN by Flow Config: {','.join(applied)}" if applied else ""
        if wrapper["enabled"]:
            head = (f"lane ON | {wrapper['logic']} {wrapper['max_chars']}/{wrapper['overlap_chars']} "
                    f"x{wrapper['parallel_calls']}")
        else:
            # The chunk sizes describe nothing that will happen; printing them next to OFF
            # reads as "it will still chunk at 4000".
            head = "lane OFF (single identity chunk)"
        self.status = (f"{head} | merge {merger['scalar_strategy']}/"
                       f"{merger['conflict_policy']} | {payload['config_uid']}{ovnote}{note}")
        return Data(data=payload)
