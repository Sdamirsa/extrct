"""Postgres-backed run log — the error-analysis substrate.

Deliberately a narrow interface (`ensure_schema`, `save_run`, `save_attempt`,
`find_completed`) so the backend is a swap, not a rewrite.

Postgres rather than Chroma: the questions this log exists to answer are aggregations and
joins - "which schema shapes fail on which models", "failure rate by encoding" - and Chroma
metadata values must be scalars, so `validation_errors` (an array of error objects) would
become a string and every analysis would become string-parsing in Python. A Chroma
collection over variable descriptions is still the right tool for semantic reuse lookup; it
would be a DERIVED index over these rows, rebuildable from them alone.

`input_text` is nullable and defaults to NULL. Only `input_sha256` is required, so
the log stays clean even if a real note is processed by mistake.
"""

from __future__ import annotations

import json
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS schema_variable (
    uid          TEXT PRIMARY KEY,
    schema_set   TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    is_list      BOOLEAN NOT NULL DEFAULT FALSE,
    description  TEXT,
    parent       TEXT,
    options      JSONB,
    constraints  JSONB,
    required     BOOLEAN NOT NULL DEFAULT TRUE,
    ordinal      INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (schema_set, name)
);

-- Stored client definitions (client-def/1.0). AUTHORED CONFIGURATION like schema_variable:
-- named handles over content-addressed documents. Runs are untouched by deletes here -
-- the run row carries the full resolved spec, not a reference.
CREATE TABLE IF NOT EXISTS client_definition (
    name        TEXT PRIMARY KEY,
    client_uid  TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT,
    definition  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extraction_run (
    run_uid       TEXT PRIMARY KEY,
    request_uid   TEXT NOT NULL,
    schema_uid    TEXT NOT NULL,
    schema_encoding TEXT,
    provider      TEXT NOT NULL,
    base_url      TEXT,
    model_on_wire TEXT NOT NULL,
    model_digest  TEXT,
    endpoint_tag  TEXT,
    seed          INT,
    temperature   REAL,
    request_mode  TEXT,
    input_sha256  TEXT NOT NULL,
    input_chars   INT,
    input_text    TEXT,               -- NULL unless store_input_text was explicitly enabled
    data_classification TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    final_status  TEXT NOT NULL,      -- ok | repaired | invalid | error | refused
    attempts      INT NOT NULL DEFAULT 0,
    layers_used   JSONB,
    total_tokens  INT,
    cost_usd      NUMERIC(18, 10),
    latency_ms    INT,
    silent_failure_flags JSONB,
    request_record JSONB,
    provider_response JSONB,           -- the provider body VERBATIM: cost, timing, generation id
    extracted     JSONB,               -- the FINAL object after the ladder (raw text lives in provider_response)
    tags          JSONB,               -- annotation labels; deliberately NOT part of run_uid
    http_status   INT,
    error_class   TEXT,
    error         TEXT,
    run_metadata  JSONB
);

-- Migration-lite for databases created before a column existed. IF NOT EXISTS makes it
-- idempotent, so ensure_schema stays the single entry point with no separate migration step.
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS provider_response JSONB;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS extracted JSONB;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS tags JSONB;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS http_status INT;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS error_class TEXT;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS field_logprobs JSONB;
ALTER TABLE extraction_run ADD COLUMN IF NOT EXISTS field_grounding JSONB;

CREATE INDEX IF NOT EXISTS ix_run_schema   ON extraction_run (schema_uid);
-- GIN so `tags ? 'pilot2'` is an index lookup, not a scan.
CREATE INDEX IF NOT EXISTS ix_run_tags     ON extraction_run USING gin (tags);
CREATE INDEX IF NOT EXISTS ix_run_model    ON extraction_run (model_on_wire);
CREATE INDEX IF NOT EXISTS ix_run_status   ON extraction_run (final_status);
CREATE INDEX IF NOT EXISTS ix_run_encoding ON extraction_run (schema_encoding);

-- Long-text lane (wrap-def/1.0 + merge-def/1.0). Wrappings are re-derivable from the
-- text plus params, so chunk TEXT is stored only on explicit request - offsets and
-- hashes always. A merge_run is evidence of one merge: config, inputs, result, report.
CREATE TABLE IF NOT EXISTS text_wrapping (
    wrap_uid    TEXT PRIMARY KEY,
    text_sha256 TEXT NOT NULL,
    logic       TEXT NOT NULL,
    params      JSONB NOT NULL,
    n_chars     INT,
    n_chunks    INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS text_chunk (
    chunk_uid    TEXT PRIMARY KEY,
    wrap_uid     TEXT NOT NULL REFERENCES text_wrapping(wrap_uid) ON DELETE CASCADE,
    idx          INT NOT NULL,
    char_start   INT NOT NULL,
    char_end     INT NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    text         TEXT,              -- NULL unless store_text was explicitly enabled
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (wrap_uid, idx)
);

CREATE TABLE IF NOT EXISTS merge_run (
    merge_uid       TEXT PRIMARY KEY,
    wrap_uid        TEXT,
    config          JSONB NOT NULL,
    n_inputs        INT NOT NULL,
    run_uids        JSONB,
    merged          JSONB,
    report          JSONB,
    conflicts       INT NOT NULL DEFAULT 0,
    major_conflicts INT NOT NULL DEFAULT 0,
    tags            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extraction_attempt (
    run_uid           TEXT NOT NULL REFERENCES extraction_run(run_uid) ON DELETE CASCADE,
    attempt_no        INT  NOT NULL,
    layer             TEXT NOT NULL,   -- request | json_repair | coerce | reprompt | llm_repair | routing
    valid             BOOLEAN NOT NULL,
    validation_errors JSONB,
    note              TEXT,
    latency_ms        INT,
    tokens            INT,
    PRIMARY KEY (run_uid, attempt_no)
);

-- The five canvas outputs, as tables. extraction_run is the superset row; these views are
-- the "clean and understandable" projections of it, so inspection never needs hand SQL.
CREATE OR REPLACE VIEW v_run_report AS
SELECT run_uid, started_at, provider, model_on_wire, endpoint_tag, schema_uid,
       schema_encoding, final_status, attempts, total_tokens, cost_usd, latency_ms,
       http_status, silent_failure_flags, tags, run_metadata
FROM extraction_run;

CREATE OR REPLACE VIEW v_failed AS
SELECT run_uid, started_at, provider, model_on_wire, final_status, http_status,
       error_class, error, silent_failure_flags, tags
FROM extraction_run
WHERE final_status NOT IN ('ok', 'repaired');
"""

TERMINAL_STATUSES = ("ok", "repaired")


def _dsn(dsn: str | None) -> str:
    import os

    resolved = dsn or os.environ.get("EXTRCT_PG_DSN") or os.environ.get("EXTRCT_PG_DSN", "")
    if not resolved:
        msg = "no DSN: pass dsn= or set EXTRCT_PG_DSN"
        raise ValueError(msg)
    return resolved


async def ensure_schema(*, dsn: str | None = None, seed_builtins: bool = True) -> None:
    """Create the tables, then seed the built-in schema sets.

    Seeding here rather than in a migration keeps `extrct_schema_builder_schema` present on
    any fresh box without a separate setup step - it is the schema an agent needs in order to
    author other schemas, so a registry without it is missing its bootstrap.
    """
    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        await conn.execute(DDL)
        await conn.commit()
    if seed_builtins:
        await seed_builtin_schemas(dsn=dsn)


async def save_run(record: dict[str, Any], *, dsn: str | None = None) -> None:
    """Upsert one run. Idempotent on run_uid so a resumed batch cannot double-count."""
    import psycopg

    cols = (
        "run_uid", "request_uid", "schema_uid", "schema_encoding", "provider", "base_url",
        "model_on_wire", "model_digest", "endpoint_tag", "seed", "temperature", "request_mode",
        "input_sha256", "input_chars", "input_text", "data_classification", "ended_at",
        "final_status", "attempts", "layers_used", "total_tokens", "cost_usd", "latency_ms",
        "silent_failure_flags", "request_record", "provider_response", "extracted", "tags",
        "http_status", "error_class", "error", "run_metadata",
    )
    jsonb = {"layers_used", "silent_failure_flags", "request_record", "provider_response",
             "extracted", "tags", "run_metadata"}
    # None must become SQL NULL, not json.dumps(None) == the JSONB value 'null' — JSONB null
    # is NOT NULL to COALESCE, so it would let a partial re-save erase a populated column,
    # which is exactly what the COALESCE below exists to prevent.
    values = [
        (json.dumps(record.get(c)) if record.get(c) is not None else None) if c in jsonb else record.get(c)
        for c in cols
    ]
    placeholders = ", ".join("%s" for _ in cols)
    # COALESCE, not a plain overwrite: a partial re-save must never ERASE a column that an
    # earlier complete save populated. Caught by test - re-saving with a partial record
    # nulled schema_encoding, which would have silently destroyed the axis we group by.
    # `final_status` and `attempts` are the deliberate exceptions: they legitimately change
    # as a run progresses, so a later write must win.
    always_overwrite = {"final_status", "attempts", "ended_at"}
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" if c in always_overwrite else f"{c} = COALESCE(EXCLUDED.{c}, extraction_run.{c})"
        for c in cols
        if c != "run_uid"
    )
    sql = (
        f"INSERT INTO extraction_run ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (run_uid) DO UPDATE SET {updates}"
    )
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        await conn.execute(sql, values)
        await conn.commit()


async def annotate_run(run_uid: str, *, field_logprobs: dict | None = None,
                       field_grounding: dict | None = None, dsn: str | None = None) -> bool:
    """Attach DERIVED annotations to an existing run row. Returns True if a row matched.

    Deliberately not save_run: that upsert always-overwrites final_status, which an
    annotation must never touch. These two columns are deterministic derivations of
    provider_response + schema - storing them is a query convenience, not new
    evidence, which is why this narrow writer does not violate the runs-are-evidence rule.
    """
    import psycopg

    sets, values = [], []
    if field_logprobs is not None:
        sets.append("field_logprobs = %s")
        values.append(json.dumps(field_logprobs))
    if field_grounding is not None:
        sets.append("field_grounding = %s")
        values.append(json.dumps(field_grounding))
    if not sets:
        return False
    values.append(run_uid)
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        cur = await conn.execute(
            f"UPDATE extraction_run SET {', '.join(sets)} WHERE run_uid = %s", values
        )
        await conn.commit()
        return cur.rowcount > 0


async def save_attempts(run_uid: str, attempts: list[dict], *, dsn: str | None = None) -> None:
    import psycopg

    sql = (
        "INSERT INTO extraction_attempt (run_uid, attempt_no, layer, valid, validation_errors, note, "
        "latency_ms, tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (run_uid, attempt_no) DO UPDATE SET layer=EXCLUDED.layer, valid=EXCLUDED.valid, "
        "validation_errors=EXCLUDED.validation_errors, note=EXCLUDED.note"
    )
    rows = [
        (
            run_uid,
            a.get("attempt_no"),
            a.get("layer"),
            bool(a.get("valid")),
            json.dumps(a.get("validation_errors")),
            a.get("note"),
            a.get("latency_ms"),
            a.get("tokens"),
        )
        for a in attempts
    ]
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
        await conn.commit()


async def find_completed(run_uids: list[str], *, dsn: str | None = None, statuses=TERMINAL_STATUSES) -> set[str]:
    """Resume support. The reason this is cheap is that run_uid is a CONTENT hash.

    If 900 of 960 runs already succeeded, a re-run costs 60 calls. That property exists only
    because run identity is derived from (schema, model, input, config) rather than being a
    sequence number.
    """
    import psycopg

    if not run_uids:
        return set()
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT run_uid FROM extraction_run WHERE run_uid = ANY(%s) AND final_status = ANY(%s)",
                (list(run_uids), list(statuses)),
            )
            return {r[0] for r in await cur.fetchall()}


async def save_variables(schema_set: str, variables: list[dict], *, dsn: str | None = None) -> int:
    """Persist the variable registry. Names are unique within a schema_set."""
    import psycopg

    from .hashing import content_uid

    sql = (
        "INSERT INTO schema_variable (uid, schema_set, name, type, is_list, description, parent, "
        "options, constraints, required, ordinal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (schema_set, name) DO UPDATE SET uid=EXCLUDED.uid, type=EXCLUDED.type, "
        "is_list=EXCLUDED.is_list, description=EXCLUDED.description, parent=EXCLUDED.parent, "
        "options=EXCLUDED.options, constraints=EXCLUDED.constraints, required=EXCLUDED.required, "
        "ordinal=EXCLUDED.ordinal"
    )
    from .schema import Variable

    rows = []
    for raw in variables:
        # Normalise through Variable.from_row FIRST. The table UI supplies `options` as a
        # comma string and `constraints` as JSON TEXT, so dumping them directly double-encodes:
        # json.dumps('{"ge":0}') stores the STRING, and reading it back yields a str, not a
        # dict - which then explodes in _leaf() as "'str' object has no attribute 'items'".
        # Caught by the registry round-trip test.
        v = raw if isinstance(raw, Variable) else Variable.from_row(raw)
        body = {
            "name": v.name, "type": v.type, "is_list": v.is_list, "description": v.description,
            "parent": v.parent, "options": v.options, "constraints": v.constraints, "required": v.required,
        }
        rows.append(
            (
                content_uid({"schema_set": schema_set, **body}),
                schema_set,
                v.name,
                v.type,
                v.is_list,
                v.description,
                v.parent,
                json.dumps(v.options) if v.options is not None else None,
                json.dumps(v.constraints) if v.constraints is not None else None,
                v.required,
                v.ordinal,
            )
        )
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
        await conn.commit()
    return len(rows)


async def load_variables(schema_set: str, *, dsn: str | None = None) -> list[dict]:
    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, type, is_list, description, parent, options, constraints, required, ordinal "
                "FROM schema_variable WHERE schema_set = %s ORDER BY ordinal, name",
                (schema_set,),
            )
            cols = ["name", "type", "is_list", "description", "parent", "options", "constraints", "required", "ordinal"]
            return [dict(zip(cols, row, strict=True)) for row in await cur.fetchall()]


# --- schema_variable write operations -------------------------------------------------
#
# These exist for the REGISTRY only. There is deliberately no equivalent for
# extraction_run / extraction_attempt: a run record is evidence, and it must be written
# only by the thing that produced the run. Cleaning up test runs is possible through raw
# SQL, and the friction of having to write that SQL is the point.


async def copy_schema_set(source: str, target: str, *, dsn: str | None = None, overwrite: bool = False) -> int:
    """Version a schema set. Returns rows copied.

    uid is recomputed because it hashes schema_set, so a copy is a genuinely distinct
    identity rather than a duplicate row wearing the same hash.
    """
    import psycopg

    from .hashing import content_uid

    rows = await load_variables(source, dsn=dsn)
    if not rows:
        msg = f"schema set {source!r} has no variables (or does not exist)"
        raise ValueError(msg)
    existing = await load_variables(target, dsn=dsn)
    if existing and not overwrite:
        msg = f"target {target!r} already has {len(existing)} variables; set overwrite to replace it"
        raise ValueError(msg)

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        if overwrite:
            await conn.execute("DELETE FROM schema_variable WHERE schema_set = %s", (target,))
        async with conn.cursor() as cur:
            for r in rows:
                body = {k: r.get(k) for k in ("name", "type", "is_list", "description", "parent", "options", "constraints", "required")}
                await cur.execute(
                    "INSERT INTO schema_variable (uid, schema_set, name, type, is_list, description, parent, "
                    "options, constraints, required, ordinal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        content_uid({"schema_set": target, **body}), target, r["name"], r["type"],
                        r["is_list"], r["description"], r["parent"],
                        json.dumps(r["options"]) if r["options"] else None,
                        json.dumps(r["constraints"]) if r["constraints"] else None,
                        r["required"], r["ordinal"],
                    ),
                )
        await conn.commit()
    return len(rows)


async def rename_schema_set(source: str, target: str, *, dsn: str | None = None) -> int:
    """Rename in place. uid is recomputed, since schema_set is part of the hash."""
    import psycopg

    from .hashing import content_uid

    rows = await load_variables(source, dsn=dsn)
    if not rows:
        msg = f"schema set {source!r} has no variables (or does not exist)"
        raise ValueError(msg)
    if await load_variables(target, dsn=dsn):
        msg = f"target {target!r} already exists; rename would collide"
        raise ValueError(msg)

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            for r in rows:
                body = {k: r.get(k) for k in ("name", "type", "is_list", "description", "parent", "options", "constraints", "required")}
                await cur.execute(
                    "UPDATE schema_variable SET schema_set = %s, uid = %s WHERE schema_set = %s AND name = %s",
                    (target, content_uid({"schema_set": target, **body}), source, r["name"]),
                )
        await conn.commit()
    return len(rows)


async def delete_schema_set(schema_set: str, *, dsn: str | None = None) -> int:
    """Remove a schema set. Runs that cited its schemas are untouched - they reference a
    schema_uid, not a schema_set, so history survives the deletion of the authoring rows."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM schema_variable WHERE schema_set = %s", (schema_set,))
            n = cur.rowcount
        await conn.commit()
    return n


async def seed_builtin_schemas(*, dsn: str | None = None, overwrite: bool = False) -> dict[str, int]:
    """Seed the built-in schema sets. Idempotent, and NON-DESTRUCTIVE by default.

    A user who has edited `extrct_schema_builder_schema` keeps their version: a builtin is a
    starting point, not a managed resource. Pass overwrite=True to reset it.
    """
    from .builtins import BUILTIN_SCHEMA_SETS

    seeded: dict[str, int] = {}
    for name, variables in BUILTIN_SCHEMA_SETS.items():
        existing = await load_variables(name, dsn=dsn)
        if existing and not overwrite:
            seeded[name] = 0
            continue
        seeded[name] = await save_variables(name, variables, dsn=dsn)
    return seeded


# --- client_definition operations -----------------------------------------------------
#
# Same asymmetry argument as schema_variable: a stored client is AUTHORED CONFIGURATION
# (an agent that drafts clients needs a write path), so full CRUD. The run log stays
# untouched by any of this - a run row carries its full resolved spec, not a reference,
# so history survives every rename and delete here.


async def save_client_definition(name: str, document: dict[str, Any], *,
                                 overwrite: bool = False, dsn: str | None = None) -> dict[str, Any]:
    """Validate + normalize through client_model — the wire is never trusted, the uid is
    recomputed here — then store under `name`. Accepts any parse_document shape (canonical,
    flat shorthand, or a loaded registry row). Existing name + overwrite=False raises,
    same contract as save_variables."""
    import psycopg

    from . import client_model

    handle = (name or "").strip()
    if not handle:
        msg = "save_client_definition needs a non-empty name"
        raise ValueError(msg)
    provider, settings = client_model.parse_document(document)
    doc = client_model.client_definition(provider, settings)
    model = doc["settings"].get("model", "")

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT client_uid FROM client_definition WHERE name = %s", (handle,))
            row = await cur.fetchone()
            if row and not overwrite:
                msg = f"client {handle!r} already exists (uid {row[0]}); set overwrite to replace it"
                raise ValueError(msg)
            await cur.execute(
                "INSERT INTO client_definition (name, client_uid, provider, model, definition) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (name) DO UPDATE SET client_uid=EXCLUDED.client_uid, "
                "provider=EXCLUDED.provider, model=EXCLUDED.model, "
                "definition=EXCLUDED.definition, updated_at=now()",
                (handle, doc["client_uid"], doc["provider"], model, json.dumps(doc)),
            )
        await conn.commit()
    return {"name": handle, "client_uid": doc["client_uid"], "provider": doc["provider"], "model": model}


async def load_client_definition(name: str, *, dsn: str | None = None) -> dict[str, Any]:
    """The stored document, exactly as normalized at save time. Loud when absent."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT definition FROM client_definition WHERE name = %s",
                              ((name or "").strip(),))
            row = await cur.fetchone()
    if not row:
        msg = f"no stored client named {name!r}; see the Registry's list_clients"
        raise ValueError(msg)
    doc = row[0]
    return doc if isinstance(doc, dict) else json.loads(doc)


async def delete_client_definition(name: str, *, dsn: str | None = None) -> int:
    """Remove one stored client. Past runs are unaffected (full spec lives on the run row)."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM client_definition WHERE name = %s", ((name or "").strip(),))
            n = cur.rowcount
        await conn.commit()
    return n


# --- long-text lane (wrappings + merges) ----------------------------------------------


async def save_wrapping(doc: dict[str, Any], *, store_text: bool = False,
                        dsn: str | None = None) -> int:
    """Persist a wrap-def/1.0 document. Idempotent on wrap_uid; chunk text only when
    store_text is explicitly on (a wrapping is re-derivable from text + params)."""
    from .hashing import sha256_text

    import psycopg

    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO text_wrapping (wrap_uid, text_sha256, logic, params, n_chars, n_chunks) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (wrap_uid) DO NOTHING",
                (doc["wrap_uid"], doc["text_sha256"], doc["logic"], json.dumps(doc["params"]),
                 doc.get("n_chars"), doc["n_chunks"]),
            )
            for c in doc["chunks"]:
                await cur.execute(
                    "INSERT INTO text_chunk (chunk_uid, wrap_uid, idx, char_start, char_end, "
                    "chunk_sha256, text) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (chunk_uid) DO UPDATE SET text = COALESCE(EXCLUDED.text, text_chunk.text)",
                    (c["chunk_uid"], doc["wrap_uid"], c["idx"], c["start"], c["end"],
                     sha256_text(c["text"]), c["text"] if store_text else None),
                )
        await conn.commit()
    return len(doc["chunks"])


async def save_merge_run(result: dict[str, Any], *, wrap_uid: str | None = None,
                         tags: list[str] | None = None, dsn: str | None = None) -> None:
    """Persist a merge-def/1.0 result. Idempotent on merge_uid."""
    import psycopg

    report = {"variables": result["variables"], "conflicts": result["conflicts"],
              "stats": result["stats"], "schema_uid": result.get("schema_uid")}
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        await conn.execute(
            "INSERT INTO merge_run (merge_uid, wrap_uid, config, n_inputs, run_uids, merged, "
            "report, conflicts, major_conflicts, tags) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (merge_uid) DO UPDATE SET merged=EXCLUDED.merged, report=EXCLUDED.report, "
            "conflicts=EXCLUDED.conflicts, major_conflicts=EXCLUDED.major_conflicts, "
            "tags=COALESCE(EXCLUDED.tags, merge_run.tags)",
            (result["merge_uid"], wrap_uid, json.dumps(result["config"]),
             result["stats"]["inputs"], json.dumps(result.get("run_uids") or []),
             json.dumps(result["merged"], ensure_ascii=False),
             json.dumps(report, ensure_ascii=False, default=str),
             result["stats"]["conflicts"], result["stats"]["major_conflicts"],
             json.dumps(tags) if tags else None),
        )
        await conn.commit()


async def fetch_extracted(run_uids: list[str], *, dsn: str | None = None) -> dict[str, Any]:
    """{run_uid: extracted} for the given runs — lets a resumed chunk sweep hand the
    merger COMPLETE inputs even for calls it skipped."""
    import psycopg

    if not run_uids:
        return {}
    async with await psycopg.AsyncConnection.connect(_dsn(dsn)) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT run_uid, extracted FROM extraction_run WHERE run_uid = ANY(%s)",
                (list(run_uids),),
            )
            out = {}
            for uid, ex in await cur.fetchall():
                out[uid] = ex if isinstance(ex, (dict, type(None))) else json.loads(ex)
            return out
