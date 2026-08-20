"""Schema Builder — variable rows to a JSON Schema envelope.

All logic lives in `extrct.schema`; this is a thin wrapper so the same code is
callable from whatever conductor runs the sweep. See docs/system-arch/extraction-stack/.

ONE CLEAN SCHEMA output since 2026-08-14: the Request Evidence toggle and the Grounding
Schema output moved out — evidence injection now happens at request time inside
Run - Structured Extract, driven by XAI - Evidence Grounding's config (the only thing
that ever needed it). The schema this node emits is always the clean one; the schema
actually SENT (possibly evidence-augmented, different schema_uid) is recorded on every
run and on the router's Run Report.

The eight columns are the point: `parent` (nesting) and `options` (enum) are what Langflow's
own four-column table cannot express, and `constraints` + `required` are what make a clinical
schema say what it means.

Review-wave fixes 2026-08-14, all in the same family (a defect must die on EVERY output,
not only on the one that happens to be wired): Pydantic Source now validates the rows
first (it used to drop a field with a mistyped `parent` silently, while the Schema output
raised) and honors the schema.root_name override; a connected-but-EMPTY registry raises
instead of quietly falling back to the demo table; a non-bool
`schema.additional_properties` override raises instead of being truthiness-cast (the
string "false" was becoming True); and table rows are stamped with their row ORDER, which
the schema builder sorts on — without it a hand-arranged clinical schema went out
alphabetically (schema_uid is unaffected: canonical_json sorts keys).
"""

from lfx.custom.custom_component.component import Component
from lfx.io import (
    BoolInput, DataFrameInput, DropdownInput, HandleInput, MessageTextInput, Output, StrInput, TableInput,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message
from lfx.schema.table import EditMode

from extrct import config as flowcfg
from extrct import schema as S
from extrct import storage

COLUMNS = [
    # default "" (was "field"): an untouched new row is a DRAFT and is dropped by the
    # blank-name filter in _rows(). With "field" as the default, one untouched row silently
    # injected a str variable literally named 'field' into the schema AND into schema_uid,
    # and a second raised "duplicate variable names: ['field']" — about a name never typed.
    {"name": "name", "display_name": "Name", "type": "str", "default": "",
     "description": "Key in the output JSON. Must be unique across the WHOLE schema set, not just within its parent. snake_case: it becomes a Python attribute in the generated Pydantic class. A row with a blank name is ignored.",
     "edit_mode": EditMode.INLINE},
    {"name": "type", "display_name": "Type", "type": "str", "default": "str",
     "description": "str | int | float | bool | date | datetime | object. Use object for a container - it REQUIRES at least one child row whose Parent is this name. date/datetime emit a string with a format keyword.",
     "edit_mode": EditMode.INLINE},
    {"name": "is_list", "display_name": "As List", "type": "boolean", "default": False,
     "description": "Wrap in an array. On an object row this gives a LIST OF OBJECTS - the way to say many findings, or many diseases, per report.",
     "edit_mode": EditMode.INLINE},
    {"name": "description", "display_name": "Description", "type": "str", "default": "",
     "description": "Sent to the model as prompt text. This is your main lever on extraction quality - treat it as prompt engineering, not documentation. Say what counts and what does not.",
     "edit_mode": EditMode.POPOVER},
    {"name": "parent", "display_name": "Parent", "type": "str", "default": "",
     "description": "Name of an object row. Blank = top level. This is how nesting works. The parent must exist and must have type object, or the build raises.",
     "edit_mode": EditMode.INLINE},
    {"name": "options", "display_name": "Options (enum)", "type": "str", "default": "",
     "description": "Comma-separated allowed values -> JSON Schema enum. Enforced by the grammar on Ollama and by strict mode on OpenRouter. The single most effective way to make a field aggregatable across runs.",
     "edit_mode": EditMode.POPOVER},
    {"name": "constraints", "display_name": "Constraints", "type": "str", "default": "",
     "description": "JSON object. Keys: ge, le, gt, lt, pattern, min_length, max_length, min_items, max_items. WARNING: Ollama grammar does NOT enforce numeric ranges - the client-side validator catches them, so a violation returns invalid rather than being clamped.",
     "edit_mode": EditMode.POPOVER},
    {"name": "required", "display_name": "Required", "type": "boolean", "default": True,
     "description": "Off makes the field optional. HOW that is encoded depends on the Encoding axis: strict_nullable returns an explicit null; native_required may omit the key entirely.",
     "edit_mode": EditMode.INLINE},
]

DEFAULT_ROWS = [
    {"name": "study", "type": "object", "is_list": False, "description": "The imaging study",
     "parent": "", "options": "", "constraints": "", "required": True},
    {"name": "modality", "type": "str", "is_list": False, "description": "Imaging modality",
     "parent": "study", "options": "TTE,CMR,CTA,CXR,TEE,NUC", "constraints": "", "required": True},
    {"name": "lvef", "type": "float", "is_list": False, "description": "LV ejection fraction, percent",
     "parent": "study", "options": "", "constraints": '{"ge":0,"le":100}', "required": False},
    {"name": "findings", "type": "str", "is_list": True, "description": "Free-text findings",
     "parent": "study", "options": "", "constraints": "", "required": True},
]


class ExtrctSchemaBuilder(Component):
    display_name: str = "Prep - Schema Builder"
    description: str = "Variable rows to a JSON Schema envelope, with nesting, enums and constraints."
    documentation: str = "docs/system-arch/extraction-stack/"
    icon: str = "table-2"
    name: str = "extrct_schema_builder"

    inputs = [
        TableInput(
            name="schema_fields",
            display_name="Variables",
            info=("One row per variable to extract, in the order you want the model to answer "
                  "them. Use the Parent column to nest a field under an object row; use "
                  "Options (enum) to restrict answers to a fixed list."),
            table_schema=COLUMNS,
            value=DEFAULT_ROWS,
            required=True,
        ),
        DataFrameInput(
            name="variables_in",
            display_name="Variables (from Registry)",
            info=(
                "Optional. When connected, these rows REPLACE the table above — so a schema set "
                "saved in Postgres can drive the build without being retyped. Wire DB - Registry "
                "with operation=load_variables."
            ),
            required=False,
        ),
        DropdownInput(
            name="encoding",
            display_name="Encoding",
            info=(
                "A DECLARED EXPERIMENTAL AXIS, not a style choice. Measured on identical rows: "
                "strict_nullable returned lvef and pericardial_effusion; native_required omitted "
                "both keys entirely. A missing key cannot be distinguished from 'absent in the "
                "patient', so strict_nullable is the safer default."
            ),
            options=list(S.ENCODINGS),
            value=S.STRICT_NULLABLE,
        ),
        StrInput(
            name="root_name",
            display_name="Root Name",
            info=(
                "Names the schema, not a field. Sets the JSON Schema `title`, names the generated "
                "Pydantic class (extract -> Extract), and is sent as response_format.json_schema.name "
                "on OpenRouter. IT IS HASHED INTO schema_uid, so renaming it produces a different "
                "schema identity and breaks replay of earlier runs - settle it before any run cites one."
            ),
            value="extract",
        ),
        StrInput(
            name="schema_set",
            display_name="Schema Set",
            info=("Registry namespace to WRITE into when Persist To Registry is on — otherwise "
                  "this field is unused. Loading rows back is driven by the Schema Set on "
                  "DB - Registry, not this one. Variable names must be unique within a set."),
            value="default",
        ),
        BoolInput(
            name="additional_properties",
            display_name="Allow Extra Properties",
            info=("Off (default) sets additionalProperties:false, which OpenAI strict mode "
                  "requires. On lets the model return keys not defined in the schema — they "
                  "pass through unvalidated."),
            value=False,
            advanced=True,
        ),
        BoolInput(
            name="persist",
            display_name="Persist To Registry",
            info=("Upsert these rows into Postgres (into the Schema Set above) so they can be "
                  "reused and queried. Runs only when the Schema output is built — wiring only "
                  "Schema UID / Pydantic Source / Variables will not persist."),
            value=False,
        ),
        MessageTextInput(
            name="pg_dsn",
            display_name="Postgres DSN",
            info="Blank uses EXTRCT_PG_DSN from the environment.",
            value="",
            advanced=True,
        ),
        HandleInput(
            name="overrides", display_name="Overrides", required=False, input_types=["Data"],
            info=("Optional: a Prep - Flow Config payload. Keys 'schema.X' override this "
                  "node's settings (encoding, root_name, additional_properties — anything "
                  "else raises). Unwired = unchanged behavior."),
        ),
    ]

    # group_outputs=True: every handle visible at once, no dropdown toggling.
    # Display name "Schema UID" for schema_hash: every other surface (status lines, Run
    # Report, Registry columns, Schema Diff) calls this value schema_uid. Internal name
    # frozen (S2).
    outputs = [
        Output(name="schema", display_name="Schema", method="build_schema_envelope", group_outputs=True),
        Output(name="schema_hash", display_name="Schema UID", method="build_hash", group_outputs=True),
        Output(name="variables_out", display_name="Variables", method="build_variables", group_outputs=True),
        Output(name="pydantic_source", display_name="Pydantic Source", method="build_pydantic", group_outputs=True),
    ]

    def _rows(self) -> list[dict]:
        """A connected Registry wins over the hand-authored table.

        Deliberately not a merge: two sources of truth for a schema silently diverging is
        exactly the failure the content-addressed uid exists to make impossible. Connected
        but EMPTY raises for the same reason — falling back to the table there was the one
        path that could re-introduce the divergence (and, with persist on, write the demo
        rows into the namespace the user was trying to load).

        NOTE: an unwired DataFrameInput arrives as "" (measured), not None — so wiring is
        detected by SHAPE (a frame or a list), never by `is not None`.
        """
        incoming = getattr(self, "variables_in", None)
        wired = hasattr(incoming, "to_dict") or isinstance(incoming, (list, tuple))
        rows: list[dict]
        if wired:
            rows = incoming.to_dict(orient="records") if hasattr(incoming, "to_dict") else list(incoming)
            if not rows:
                msg = ("Variables (from Registry) is connected but returned 0 variables — check "
                       "the schema_set spelling on DB - Registry (operation=load_variables). "
                       "Refusing to silently fall back to the Variables table.")
                raise ValueError(msg)
            self._source = "registry"
        else:
            # Stamp the AUTHORED row order: schema.build sorts children by (ordinal, name),
            # so without this a hand-arranged schema goes out alphabetically. Rows are
            # copied — DEFAULT_ROWS is a shared class-level list.
            rows = [{**r, "ordinal": r.get("ordinal") or i}
                    for i, r in enumerate(list(self.schema_fields or []))]
            self._source = "table"
        return [r for r in rows if str(r.get("name") or "").strip()]

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def _effective(self) -> dict:
        """Node fields, with schema.* flow-config overrides applied (typos raise)."""
        eff = {
            "encoding": self.encoding,
            "root_name": self.root_name or "extract",
            "additional_properties": bool(self.additional_properties),
        }
        ov = self._unwrap(getattr(self, "overrides", None))
        if ov:
            sub = flowcfg.owned_subset(ov, "schema", flowcfg.SCHEMA_KEYS)
            if "encoding" in sub and sub["encoding"] not in S.ENCODINGS:
                msg = f"schema.encoding {sub['encoding']!r} is not one of {list(S.ENCODINGS)}"
                raise ValueError(msg)
            if "additional_properties" in sub and not isinstance(sub["additional_properties"], bool):
                # bool("false") is True: a config row that forgot type=bool used to flip
                # additionalProperties AND the schema_uid with no error anywhere.
                msg = (f"schema.additional_properties must be a boolean, got "
                       f"{sub['additional_properties']!r}. Declare that config row as type=bool "
                       f"(true/false), not a string.")
                raise ValueError(msg)
            eff.update(sub)
            eff["additional_properties"] = bool(eff["additional_properties"])
        return eff

    def _envelope(self) -> dict:
        e = self._effective()
        return S.schema_envelope(
            self._rows(),
            root_name=e["root_name"],
            encoding=e["encoding"],
            additional_properties=e["additional_properties"],
        )

    async def build_schema_envelope(self) -> Data:
        env = self._envelope()
        persisted = ""
        if self.persist:
            n = await storage.save_variables(
                self.schema_set or "default", self._rows(), dsn=self.pg_dsn or None
            )
            self.log(f"persisted {n} variables to schema_set={self.schema_set!r}")
            persisted = f" | persisted {n} -> {self.schema_set or 'default'}"
        self.status = (f"{env['variable_count']} vars ({getattr(self,'_source','table')}) | "
                       f"{env['encoding']} | {env['schema_uid']}{persisted}")
        return Data(data=env)

    def build_hash(self) -> Message:
        env = self._envelope()
        self.status = env["schema_uid"]
        return Message(text=env["schema_uid"])

    def build_pydantic(self) -> Message:
        """The swap test swap surface — so it must carry the SAME identity as the Schema output.

        `_envelope()` runs first and is discarded: to_pydantic_source has none of
        build_schema's checks, so a mistyped `parent` used to emit a valid class with the
        clinical field silently missing. The root name comes from `_effective()`, so a
        schema.root_name override renames the class too."""
        self._envelope()  # validate the rows; a defect must die on every output
        root = (self._effective()["root_name"] or "extract").replace("-", "_")
        cls = "".join(p.capitalize() or "_" for p in root.split("_"))
        src = S.to_pydantic_source(self._rows(), root_name=cls)
        self.status = f"{cls} ({len(src.splitlines())} lines)"
        return Message(text=src)

    def build_variables(self) -> DataFrame:
        rows = self._rows()
        self.status = f"{len(rows)} variables ({getattr(self, '_source', 'table')})"
        return DataFrame(rows)
