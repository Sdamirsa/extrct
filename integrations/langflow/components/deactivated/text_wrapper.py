"""Prep - Text Wrapper — wrap a long document into extraction-sized chunks (wrap-def/1.0).

The entry of the long-text lane:

    long text ─> [Prep - Text Wrapper] ─Chunks──> [Run - Chunk Extract] ─> [PostPrep - Extraction Merger]
                                       ─Wrap Doc─> (audit / DB)

Three deterministic logics (extrct.wrapping): fixed_window, paragraph_pack,
sentence_pack — all pure functions of (text, params), so the wrapping is re-derivable
from the text plus the recorded params and every chunk is a (start, end) span into
the ORIGINAL text; offsets are what later map grounding spans and citations back to the
full document. Chunk TEXT lands in Postgres only when Store Chunk Text is explicitly on.

Input stays CLEAN TEXT by design: ingestion (PDF/OCR/HTML — the docling front) belongs
in front of this node, not inside it, so extraction inputs never carry markup.
"""

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import BoolInput, DropdownInput, HandleInput, IntInput, MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

from extrct import storage, wrapping
from extrct.config import owned_subset
from extrct.flow_model import WRAPPER_KEYS


class ExtrctTextWrapper(Component):
    display_name: str = "Prep - Text Wrapper"
    description: str = "Long text -> deterministic overlapping chunks with source offsets (wrap-def/1.0)."
    documentation: str = "docs/extraction-stack/long-text-design.md"
    icon: str = "scissors"
    name: str = "extrct_text_wrapper"

    inputs = [
        MultilineInput(name="source_text", display_name="Source Text", required=True,
                       info="The full document, clean text. Offsets in every chunk index THIS string."),
        DropdownInput(name="logic", display_name="Wrapping Logic",
                      options=list(wrapping.WRAPPER_LOGICS), value="paragraph_pack",
                      info=("fixed_window: fixed character windows, whitespace-snapped. "
                            "paragraph_pack: whole paragraphs packed to Max Chars. "
                            "sentence_pack: sentence units packed to Max Chars. "
                            "All deterministic; overlap carries context across cuts.")),
        IntInput(name="max_chars", display_name="Max Chars", value=4000,
                 info="Upper bound per chunk, in characters. Size to the model's usable context minus the schema."),
        IntInput(name="overlap_chars", display_name="Overlap Chars", value=400,
                 info="Context carried into the next chunk. 10% of Max Chars is a sane start."),
        BoolInput(name="store_text", display_name="Store Chunk Text", value=False,
                  info=("OFF stores only offsets + hashes (the wrapping is re-derivable from "
                        "text + params). ON also stores each chunk's text in Postgres.")),
        BoolInput(name="log_to_db", display_name="Log Wrapping In Postgres", value=True,
                  info="Writes text_wrapping + text_chunk rows keyed by the content-addressed wrap_uid."),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN", value="", advanced=True),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: Flow - Controller's Wrapper Config. Keys wrapper.*: logic, "
                          "max_chars, overlap_chars, store_text, log_to_db. Unwired = this node "
                          "behaves exactly as set.")),
    ]

    outputs = [
        Output(name="chunks", display_name="Chunks", method="build_chunks", group_outputs=True),
        Output(name="wrap_doc", display_name="Wrap Doc", method="build_doc", group_outputs=True),
    ]

    _doc: dict | None = None

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    async def _run(self) -> dict:
        if self._doc is not None:
            return self._doc
        ov = self._unwrap(getattr(self, "overrides", None))
        cfg = owned_subset(ov, "wrapper", WRAPPER_KEYS) if ov else {}
        text = str(self.source_text or "")
        doc = wrapping.wrap_text(
            text,
            str(cfg.get("logic", self.logic)),
            max_chars=int(cfg.get("max_chars", self.max_chars)),
            overlap_chars=int(cfg.get("overlap_chars", self.overlap_chars)),
        )
        if not wrapping.coverage_ok(doc):  # the invariant, checked on every run
            msg = "wrapping lost characters (coverage invariant violated) - this is a bug, report it"
            raise ValueError(msg)
        if cfg.get("config_uid"):
            doc["config_uid"] = cfg["config_uid"]
        if bool(cfg.get("log_to_db", self.log_to_db)):
            try:
                await storage.ensure_schema(dsn=self.pg_dsn or None)
                await storage.save_wrapping(doc, store_text=bool(cfg.get("store_text", self.store_text)),
                                            dsn=self.pg_dsn or None)
            except Exception as exc:  # noqa: BLE001 - logging must never lose the wrapping
                self.log(f"DB logging failed ({type(exc).__name__}: {exc}); outputs unaffected")
        self._doc = doc
        return doc

    async def build_chunks(self) -> DataFrame:
        doc = await self._run()
        sizes = [c["n_chars"] for c in doc["chunks"]]
        self.status = (f"{doc['n_chunks']} chunk(s) via {doc['logic']} | "
                       f"{min(sizes)}-{max(sizes)} chars | wrap_uid={doc['wrap_uid']}")
        return DataFrame([
            {"idx": c["idx"], "chunk_uid": c["chunk_uid"], "wrap_uid": doc["wrap_uid"],
             "start": c["start"], "end": c["end"], "n_chars": c["n_chars"], "text": c["text"]}
            for c in doc["chunks"]
        ])

    async def build_doc(self) -> Data:
        return Data(data=await self._run())
