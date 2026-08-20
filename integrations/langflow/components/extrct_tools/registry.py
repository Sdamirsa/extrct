"""DB - Registry — read and write the variable registry; read the run log.

The asymmetry is deliberate and is the whole design:

  schema_variable  is AUTHORED CONFIGURATION. Saving, versioning, renaming and deleting a
                   schema set are normal authoring acts, and an agent that drafts variables
                   needs a write path. Full CRUD.

  client_definition is AUTHORED CONFIGURATION too: named client-def/1.0 documents
                   (save_client validates + recomputes the uid through client_model;
                   load_client wires straight into the Flow - Controller). Full CRUD.

  extraction_run   is EVIDENCE. A run record must be written only by the thing that produced
                   the run, or the log stops being trustworthy. READ ONLY here. Cleaning up
                   test runs is possible through Langflow's SQL Database component, and the
                   friction of having to write that SQL by hand is the point.

For ad-hoc SQL of any kind, use the built-in SQL Database component - this one exists for the
operations worth having typed, and because load_variables feeds straight back into the Schema
Builder, which closes the authoring loop.
"""

import json
import os

from lfx.custom.custom_component.component import Component
from lfx.io import (
    BoolInput, DataFrameInput, DropdownInput, HandleInput, IntInput, MessageTextInput, Output, StrInput,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message

from extrct import storage

READ_OPS: dict[str, tuple[str, str]] = {
    "load_variables": (
        "Variables for one schema set, ready to wire into the Schema Builder.",
        "SELECT name, type, is_list, description, coalesce(parent,'') AS parent, "
        "coalesce(options::text,'') AS options, coalesce(constraints::text,'') AS constraints, "
        "required, ordinal FROM schema_variable WHERE schema_set = %(schema_set)s ORDER BY ordinal, name",
    ),
    "list_schema_sets": (
        "Every schema set with its variable count.",
        "SELECT schema_set, count(*) AS variables, max(created_at) AS last_change "
        "FROM schema_variable GROUP BY schema_set ORDER BY 3 DESC",
    ),
    "load_client": (
        "One stored client definition, wireable straight into the Flow - Controller. Needs Client Name.",
        "SELECT name, provider, model, client_uid, definition::text AS definition "
        "FROM client_definition WHERE name = %(client_name)s",
    ),
    "list_clients": (
        "Every stored client definition, newest change first.",
        "SELECT name, provider, model, client_uid, updated_at "
        "FROM client_definition ORDER BY updated_at DESC",
    ),
    "list_wrappings": (
        "Every stored text wrapping (long-text lane), newest first.",
        "SELECT wrap_uid, logic, params::text AS params, n_chars, n_chunks, "
        "left(text_sha256, 12) AS text_sha, created_at "
        "FROM text_wrapping ORDER BY created_at DESC LIMIT %(limit)s",
    ),
    "list_merges": (
        "Every stored merge run (long-text lane), newest first.",
        "SELECT merge_uid, wrap_uid, n_inputs, conflicts, major_conflicts, "
        "config::text AS config, created_at "
        "FROM merge_run ORDER BY created_at DESC LIMIT %(limit)s",
    ),
    "runs_by_encoding": (
        "Outcome by encoding and model - the aggregate a vector store could not do.",
        "SELECT schema_encoding, provider, model_on_wire, final_status, count(*) AS runs, "
        "round(avg(latency_ms)) AS avg_ms, sum(cost_usd) AS cost "
        "FROM extraction_run GROUP BY 1,2,3,4 ORDER BY 5 DESC",
    ),
    "recent_runs": (
        "Most recent runs, newest first.",
        "SELECT run_uid, started_at, provider, model_on_wire, schema_encoding, final_status, "
        "attempts, layers_used::text, latency_ms, cost_usd, tags::text AS tags "
        "FROM extraction_run ORDER BY started_at DESC LIMIT %(limit)s",
    ),
    "get_run": (
        "ONE run, complete: the report scalars plus extracted, provider_response and "
        "request_record as JSON text. Needs Run UID.",
        "SELECT run_uid, request_uid, schema_uid, schema_encoding, provider, model_on_wire, "
        "endpoint_tag, final_status, attempts, total_tokens, cost_usd, latency_ms, http_status, "
        "error_class, error, silent_failure_flags::text AS silent_failure_flags, tags::text AS tags, "
        "run_metadata::text AS run_metadata, extracted::text AS extracted, "
        "provider_response::text AS provider_response, request_record::text AS request_record, "
        "started_at, ended_at FROM extraction_run WHERE run_uid = %(run_uid)s",
    ),
    "run_attempts": (
        "The repair-ladder attempts for one run, in order. Needs Run UID.",
        "SELECT attempt_no, layer, valid, validation_errors::text AS validation_errors, note, "
        "latency_ms, tokens FROM extraction_attempt WHERE run_uid = %(run_uid)s ORDER BY attempt_no",
    ),
    "field_confidence": (
        "Certainty and grounding annotations for one run: the two-axis audit. Needs Run UID.",
        "SELECT run_uid, field_logprobs::text AS field_logprobs, "
        "field_grounding::text AS field_grounding FROM extraction_run WHERE run_uid = %(run_uid)s",
    ),
    "runs_by_tag": (
        "Runs carrying a tag (exact match against the tags array; GIN-indexed). Needs Tag.",
        "SELECT run_uid, started_at, provider, model_on_wire, final_status, cost_usd, latency_ms, "
        "tags::text AS tags FROM extraction_run WHERE tags ? %(tag)s "
        "ORDER BY started_at DESC LIMIT %(limit)s",
    ),
    "failed_runs": (
        "Runs that did not end ok/repaired, with the error detail that explains why.",
        "SELECT run_uid, started_at, provider, model_on_wire, final_status, http_status, "
        "error_class, left(coalesce(error, ''), 200) AS error, "
        "silent_failure_flags::text AS silent_failure_flags, tags::text AS tags "
        "FROM v_failed ORDER BY started_at DESC LIMIT %(limit)s",
    ),
    "error_taxonomy": (
        "Which ladder rung fired and whether it worked. Accumulates across every run.",
        "SELECT layer, valid, count(*) AS n FROM extraction_attempt GROUP BY 1,2 ORDER BY 3 DESC",
    ),
    "silent_failures": (
        "Runs that returned HTTP 200 but carried a silent-failure flag.",
        "SELECT run_uid, provider, model_on_wire, final_status, silent_failure_flags::text, started_at "
        "FROM extraction_run WHERE silent_failure_flags IS NOT NULL "
        "AND silent_failure_flags::text NOT IN ('[]','null') ORDER BY started_at DESC LIMIT %(limit)s",
    ),
    "schema_usage": (
        "Which schemas have been run, and how they fared.",
        "SELECT schema_uid, schema_encoding, count(*) AS runs, "
        "count(*) FILTER (WHERE final_status='ok') AS ok, "
        "count(*) FILTER (WHERE final_status='repaired') AS repaired, "
        "count(*) FILTER (WHERE final_status NOT IN ('ok','repaired')) AS failed "
        "FROM extraction_run GROUP BY 1,2 ORDER BY 3 DESC",
    ),
    "d022_check": (
        "Must return 0. Any row means raw input text was stored.",
        "SELECT count(*) AS rows_with_raw_input_text FROM extraction_run WHERE input_text IS NOT NULL",
    ),
}

WRITE_OPS: dict[str, str] = {
    "save_variables": "Upsert the connected Variables rows into Schema Set. Needs Variables In.",
    "copy_schema_set": "Version a set: copy Schema Set -> Target Schema Set. uids are recomputed.",
    "rename_schema_set": "Rename Schema Set -> Target Schema Set in place.",
    "delete_schema_set": "Delete every variable in Schema Set. Past runs are unaffected - they cite a schema_uid, not a set.",
    "reset_builtin_schemas": "Restore the builtin sets (extrct_schema_builder_schema, extrct_client_schema) to their shipped definitions, discarding local edits.",
    "save_client": "Validate + store the connected document under Client Name (uid recomputed, never trusted). Needs Definition In.",
    "delete_client": "Delete one stored client definition by Client Name. Past runs are unaffected - they carry their full spec.",
}

ALL_OPS = [*READ_OPS, *WRITE_OPS]

# Which inputs each operation actually reads. update_build_config renders ONLY these, so the
# node never shows a field the selected operation ignores. Every op must have an entry - the
# in-container test asserts this map covers ALL_OPS exactly, so adding an op without deciding
# its fields fails loudly instead of silently showing everything.
OP_FIELDS: dict[str, set[str]] = {
    "load_variables": {"schema_set"},
    "list_schema_sets": set(),
    "runs_by_encoding": set(),
    "recent_runs": {"row_limit"},
    "get_run": {"run_uid"},
    "run_attempts": {"run_uid"},
    "field_confidence": {"run_uid"},
    "runs_by_tag": {"tag", "row_limit"},
    "failed_runs": {"row_limit"},
    "error_taxonomy": set(),
    "silent_failures": {"row_limit"},
    "schema_usage": set(),
    "d022_check": set(),
    "save_variables": {"schema_set", "variables_in", "confirm_write"},
    "copy_schema_set": {"schema_set", "target_schema_set", "overwrite_target", "confirm_write"},
    "rename_schema_set": {"schema_set", "target_schema_set", "confirm_write"},
    "delete_schema_set": {"schema_set", "confirm_write"},
    "reset_builtin_schemas": {"confirm_write"},
    "load_client": {"client_name"},
    "list_clients": set(),
    "list_wrappings": {"row_limit"},
    "list_merges": {"row_limit"},
    "save_client": {"client_name", "definition_in", "overwrite_target", "confirm_write"},
    "delete_client": {"client_name", "confirm_write"},
}
# The fields the map may show or hide. pg_dsn stays out: it applies to every operation.
MANAGED_FIELDS = {"schema_set", "target_schema_set", "run_uid", "tag", "variables_in",
                  "confirm_write", "overwrite_target", "row_limit", "client_name",
                  "definition_in"}
# Shown AND required travel together for these - a visible-but-empty run_uid is just a
# slower version of the error the guard in _read would raise.
REQUIRED_WHEN_SHOWN = {"run_uid", "tag", "target_schema_set", "variables_in", "client_name",
                       "definition_in"}


class ExtrctRegistry(Component):
    display_name: str = "DB - Registry"
    description: str = "Read and write the variable registry; read the run log."
    documentation: str = "docs/extraction-stack/variables-table.md"
    icon: str = "database"
    name: str = "extrct_registry"

    inputs = [
        DropdownInput(
            name="operation",
            display_name="Operation",
            info=(
                "READ: " + ", ".join(READ_OPS) + ".  "
                "WRITE (registry only, needs Confirm Write): " + ", ".join(WRITE_OPS) + ".  "
                "The run log is deliberately read-only here: a run record is evidence and must be "
                "written only by the component that produced the run."
            ),
            options=ALL_OPS,
            value="load_variables",
            real_time_refresh=True,
        ),
        # --- sequencing (read operations only) ---
        BoolInput(
            name="enable_trigger",
            display_name="Wait For Trigger",
            info=(
                "ON adds a Trigger input so this READ can be sequenced behind another "
                "component (typically a Listen node). Langflow runs a node only after ALL "
                "connected inputs resolve — the edge alone provides the ordering."
            ),
            value=False,
            real_time_refresh=True,
            override_skip=True,
        ),
        HandleInput(
            name="trigger",
            display_name="Trigger",
            info=(
                "Sequencing only — the value is accepted and DISCARDED. Wire a Listen output "
                "(or any component's output) here to run this read after it. Mind the "
                "dependency semantics: if the source branch never activates, this node never "
                "runs at all."
            ),
            input_types=["Data", "DataFrame", "Message"],
            required=False,
            show=False,
            # No override_skip: handle inputs (template type "other") must stay skippable,
            # or the flow build dies with "not a valid field type" — measured 2026-08-06.
        ),
        # Initial `show` states MUST match OP_FIELDS[default operation]: update_build_config
        # only fires on change, so a freshly dropped node renders these literals as-is.
        StrInput(name="schema_set", display_name="Schema Set",
                 info="The set being read from, or written to.", value="default",
                 override_skip=True),
        StrInput(name="target_schema_set", display_name="Target Schema Set",
                 info="Destination for copy_schema_set and rename_schema_set.", value="",
                 show=False, override_skip=True),
        MessageTextInput(name="run_uid", display_name="Run UID",
                         info=("For get_run and run_attempts. Shown in Structured Extract's status "
                               "line and carried by every one of its outputs."),
                         value="", show=False, override_skip=True),
        StrInput(name="tag", display_name="Tag",
                 info="For runs_by_tag: one tag, exactly as it was written in Run Tags.", value="",
                 show=False, override_skip=True),
        StrInput(name="client_name", display_name="Client Name",
                 info="The stored client definition being loaded, saved or deleted.", value="",
                 show=False, override_skip=True),
        # NO override_skip here, and none ever on a handle-fed input (Data/DataFrame/Message):
        # their template type is "other", which the param handler must SKIP (edges deliver the
        # value). override_skip forces processing and the build dies with
        # "not a valid field type: other". Measured 2026-08-06; guarded by the render test.
        DataFrameInput(name="variables_in", display_name="Variables In",
                       info="Rows to persist for save_variables. Wire a Schema Builder's Variables output, or an agent that drafts them.",
                       required=False, show=False),
        HandleInput(name="definition_in", display_name="Definition In",
                    info=("The client document to persist for save_client. Wire Client - "
                          "Definition's Definition Doc output, or an agent's Message carrying "
                          "the JSON. Validated and re-hashed before storage - the wire is "
                          "never trusted."),
                    input_types=["Data", "Message"], required=False, show=False),
        BoolInput(
            name="confirm_write",
            display_name="Confirm Write",
            info="Required for every WRITE operation. Off makes writes refuse, so a stray canvas run cannot mutate the registry.",
            value=False, show=False, override_skip=True,
        ),
        BoolInput(name="overwrite_target", display_name="Overwrite Target",
                  info="Allow copy_schema_set to replace a non-empty target, or save_client to replace an existing name.",
                  value=False, advanced=True, show=False, override_skip=True),
        IntInput(name="row_limit", display_name="Limit", value=50, advanced=True,
                 show=False, override_skip=True),
        MessageTextInput(name="pg_dsn", display_name="Postgres DSN",
                         info="Blank uses EXTRCT_PG_DSN from the environment.", value="", advanced=True),
    ]

    outputs = [
        Output(name="rows", display_name="Rows", method="build_rows", group_outputs=True),
        Output(name="summary", display_name="Summary", method="build_summary", group_outputs=True),
    ]

    _cache: list[dict] | None = None

    async def update_build_config(self, build_config, field_value, field_name=None):
        """Render only the fields the selected operation reads.

        Same contract as the clients: mutation is in place (the return value is discarded)
        and every write is membership-guarded, because dotdict.__missing__ turns a typo'd
        key into a silent no-op. `is_refresh` re-applies the state on template rebuilds.
        """
        def put(name, key, value):
            if name in build_config and isinstance(build_config[name], dict):
                build_config[name][key] = value

        if field_name in ("operation", "enable_trigger") or build_config.get("is_refresh"):
            op = field_value if field_name == "operation" else (
                (build_config.get("operation", {}) or {}).get("value") or "load_variables"
            )
            enab = field_value if field_name == "enable_trigger" else (
                (build_config.get("enable_trigger", {}) or {}).get("value")
            )
            # Unknown op (stale frozen node meeting newer options): show everything rather
            # than strand the user with fields they cannot reach.
            used = OP_FIELDS.get(op, MANAGED_FIELDS)
            for f in MANAGED_FIELDS:
                put(f, "show", f in used)
                if f in REQUIRED_WHEN_SHOWN:
                    put(f, "required", f in used)
            # The trigger pair is toggle-AND-mode dependent, so it lives outside OP_FIELDS:
            # the toggle only exists for reads, and the port only exists when toggled on.
            is_read = op in READ_OPS
            put("enable_trigger", "show", is_read)
            put("trigger", "show", is_read and bool(enab))
            if op in WRITE_OPS:
                put("operation", "info", f"WRITE - {WRITE_OPS[op]} Refuses without Confirm Write.")
            elif op in READ_OPS:
                put("operation", "info", f"READ - {READ_OPS[op][0]}")
        return build_config

    def _dsn(self) -> str | None:
        return self.pg_dsn or None

    async def _write(self) -> list[dict]:
        op = self.operation
        if not self.confirm_write:
            msg = f"{op!r} is a write operation. Turn on Confirm Write to proceed."
            raise ValueError(msg)

        src = self.schema_set or "default"
        dst = self.target_schema_set or ""

        if op == "save_variables":
            incoming = getattr(self, "variables_in", None)
            if incoming is None or len(incoming) == 0:
                msg = "save_variables needs rows on Variables In."
                raise ValueError(msg)
            rows = incoming.to_dict(orient="records") if hasattr(incoming, "to_dict") else list(incoming)
            n = await storage.save_variables(src, rows, dsn=self._dsn())
            return [{"operation": op, "schema_set": src, "variables_written": n}]

        if op in ("copy_schema_set", "rename_schema_set"):
            if not dst:
                msg = f"{op} requires Target Schema Set."
                raise ValueError(msg)
            if dst == src:
                msg = "Target Schema Set must differ from Schema Set."
                raise ValueError(msg)
            fn = storage.copy_schema_set if op == "copy_schema_set" else storage.rename_schema_set
            kwargs = {"overwrite": bool(self.overwrite_target)} if op == "copy_schema_set" else {}
            n = await fn(src, dst, dsn=self._dsn(), **kwargs)
            return [{"operation": op, "source": src, "target": dst, "variables": n}]

        if op == "reset_builtin_schemas":
            seeded = await storage.seed_builtin_schemas(dsn=self._dsn(), overwrite=True)
            return [{"operation": op, "schema_set": k, "variables_written": v} for k, v in seeded.items()]

        if op == "delete_schema_set":
            n = await storage.delete_schema_set(src, dsn=self._dsn())
            return [{"operation": op, "schema_set": src, "variables_deleted": n}]

        if op == "save_client":
            handle = (getattr(self, "client_name", "") or "").strip()
            if not handle:
                msg = "save_client requires Client Name."
                raise ValueError(msg)
            raw = getattr(self, "definition_in", None)
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, Message):
                raw = json.loads(str(raw.text or "").strip() or "{}")
            elif isinstance(raw, Data):
                raw = raw.data
            if not isinstance(raw, dict) or not raw:
                msg = ("save_client needs a document on Definition In - wire Client - "
                       "Definition's Definition Doc output, or an agent's JSON Message.")
                raise ValueError(msg)
            res = await storage.save_client_definition(
                handle, raw, overwrite=bool(self.overwrite_target), dsn=self._dsn())
            return [{"operation": op, **res}]

        if op == "delete_client":
            handle = (getattr(self, "client_name", "") or "").strip()
            if not handle:
                msg = "delete_client requires Client Name."
                raise ValueError(msg)
            n = await storage.delete_client_definition(handle, dsn=self._dsn())
            return [{"operation": op, "client_name": handle, "clients_deleted": n}]

        msg = f"unknown write operation {op!r}"
        raise ValueError(msg)

    async def _read(self) -> list[dict]:
        import psycopg

        dsn = self.pg_dsn or os.environ.get("EXTRCT_PG_DSN") or os.environ.get("EXTRCT_PG_DSN", "")
        if not dsn:
            msg = "No DSN. Set EXTRCT_PG_DSN in the environment or fill in Postgres DSN."
            raise ValueError(msg)
        _desc, sql = READ_OPS[self.operation]
        if self.operation in ("get_run", "run_attempts", "field_confidence") and not (self.run_uid or "").strip():
            msg = f"{self.operation} requires Run UID (see Structured Extract's status line or Run Report)."
            raise ValueError(msg)
        if self.operation == "runs_by_tag" and not (self.tag or "").strip():
            msg = "runs_by_tag requires Tag."
            raise ValueError(msg)
        if self.operation == "load_client" and not (getattr(self, "client_name", "") or "").strip():
            msg = "load_client requires Client Name (see list_clients for what is stored)."
            raise ValueError(msg)
        params = {
            "schema_set": self.schema_set or "default",
            "limit": int(self.row_limit or 50),
            "run_uid": (self.run_uid or "").strip(),
            "tag": (self.tag or "").strip(),
            "client_name": (getattr(self, "client_name", "") or "").strip(),
        }
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(storage.DDL)  # idempotent; keeps a fresh box from failing
            await conn.commit()
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def _run(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        if self.operation in WRITE_OPS:
            await storage.ensure_schema(dsn=self._dsn())
            self._cache = await self._write()
        else:
            self._cache = await self._read()
        return self._cache

    async def build_rows(self) -> DataFrame:
        rows = await self._run()
        if self.operation in WRITE_OPS:
            self.status = f"{self.operation}: " + ", ".join(f"{k}={v}" for k, v in rows[0].items() if k != "operation")
        elif self.operation == "d022_check" and rows:
            n = rows[0].get("rows_with_raw_input_text", 0)
            self.status = f" {'OK' if n == 0 else 'VIOLATED'}: {n} row(s) store raw input text"
        else:
            self.status = f"{self.operation}: {len(rows)} row(s)"
        return DataFrame(rows)

    async def build_summary(self) -> Data:
        rows = await self._run()
        is_write = self.operation in WRITE_OPS
        return Data(
            data={
                "operation": self.operation,
                "kind": "write" if is_write else "read",
                "description": WRITE_OPS[self.operation] if is_write else READ_OPS[self.operation][0],
                "row_count": len(rows),
                "schema_set": self.schema_set,
                "rows": rows[:200],
            }
        )
