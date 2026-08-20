"""SQLite backend — the zero-setup run log. Stdlib only, same tables and semantics
as Postgres (JSON columns become TEXT holding canonical JSON; the COALESCE-upsert and
the always-overwrite exceptions are identical). Good for laptops, notebooks and CI;
move to Postgres when analysis outgrows it — same interface, so it is a swap."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from . import TERMINAL_STATUSES

DDL = """
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
    seed          INTEGER,
    temperature   REAL,
    request_mode  TEXT,
    input_sha256  TEXT NOT NULL,
    input_chars   INTEGER,
    input_text    TEXT,               -- NULL unless store_input_text was explicitly enabled
    data_classification TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,
    final_status  TEXT NOT NULL,      -- ok | repaired | invalid | error | refused
    attempts      INTEGER NOT NULL DEFAULT 0,
    layers_used   TEXT,
    total_tokens  INTEGER,
    cost_usd      TEXT,
    latency_ms    INTEGER,
    silent_failure_flags TEXT,
    request_record TEXT,
    provider_response TEXT,           -- the provider body VERBATIM: cost, timing, generation id
    extracted     TEXT,               -- the FINAL object after the ladder
    tags          TEXT,
    http_status   INTEGER,
    error_class   TEXT,
    error         TEXT,
    run_metadata  TEXT,
    field_logprobs TEXT,
    field_grounding TEXT
);

CREATE INDEX IF NOT EXISTS ix_run_schema   ON extraction_run (schema_uid);
CREATE INDEX IF NOT EXISTS ix_run_model    ON extraction_run (model_on_wire);
CREATE INDEX IF NOT EXISTS ix_run_status   ON extraction_run (final_status);
CREATE INDEX IF NOT EXISTS ix_run_encoding ON extraction_run (schema_encoding);

CREATE TABLE IF NOT EXISTS extraction_attempt (
    run_uid           TEXT NOT NULL REFERENCES extraction_run(run_uid) ON DELETE CASCADE,
    attempt_no        INTEGER NOT NULL,
    layer             TEXT NOT NULL,
    valid             INTEGER NOT NULL,
    validation_errors TEXT,
    note              TEXT,
    latency_ms        INTEGER,
    tokens            INTEGER,
    PRIMARY KEY (run_uid, attempt_no)
);

CREATE TABLE IF NOT EXISTS text_wrapping (
    wrap_uid    TEXT PRIMARY KEY,
    text_sha256 TEXT NOT NULL,
    logic       TEXT NOT NULL,
    params      TEXT NOT NULL,
    n_chars     INTEGER,
    n_chunks    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS text_chunk (
    chunk_uid    TEXT PRIMARY KEY,
    wrap_uid     TEXT NOT NULL REFERENCES text_wrapping(wrap_uid) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    text         TEXT,              -- NULL unless store_text was explicitly enabled
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (wrap_uid, idx)
);

CREATE TABLE IF NOT EXISTS merge_run (
    merge_uid       TEXT PRIMARY KEY,
    wrap_uid        TEXT,
    config          TEXT NOT NULL,
    n_inputs        INTEGER NOT NULL,
    run_uids        TEXT,
    merged          TEXT,
    report          TEXT,
    conflicts       INTEGER NOT NULL DEFAULT 0,
    major_conflicts INTEGER NOT NULL DEFAULT 0,
    tags            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS v_run_report AS
SELECT run_uid, started_at, provider, model_on_wire, endpoint_tag, schema_uid,
       schema_encoding, final_status, attempts, total_tokens, cost_usd, latency_ms,
       http_status, silent_failure_flags, tags, run_metadata
FROM extraction_run;

CREATE VIEW IF NOT EXISTS v_failed AS
SELECT run_uid, started_at, provider, model_on_wire, final_status, http_status,
       error_class, error, silent_failure_flags, tags
FROM extraction_run
WHERE final_status NOT IN ('ok', 'repaired');
"""

_RUN_COLS = (
    "run_uid", "request_uid", "schema_uid", "schema_encoding", "provider", "base_url",
    "model_on_wire", "model_digest", "endpoint_tag", "seed", "temperature", "request_mode",
    "input_sha256", "input_chars", "input_text", "data_classification", "ended_at",
    "final_status", "attempts", "layers_used", "total_tokens", "cost_usd", "latency_ms",
    "silent_failure_flags", "request_record", "provider_response", "extracted", "tags",
    "http_status", "error_class", "error", "run_metadata",
)
_JSON_COLS = {"layers_used", "silent_failure_flags", "request_record", "provider_response",
              "extracted", "tags", "run_metadata"}
# `final_status` / `attempts` / `ended_at` legitimately change as a run progresses, so a
# later write must win; everything else COALESCEs so a partial re-save can never ERASE a
# column an earlier complete save populated.
_ALWAYS_OVERWRITE = {"final_status", "attempts", "ended_at"}


class SQLiteRunStore:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def _tx(self):
        """One connection per operation: commit on success, roll back on error, ALWAYS
        close (sqlite3's own context manager commits but never closes)."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    async def ensure_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(DDL)

    async def save_run(self, record: dict[str, Any]) -> None:
        """Upsert one run. Idempotent on run_uid so a resumed batch cannot double-count."""
        values = [
            (json.dumps(record.get(c), ensure_ascii=False) if record.get(c) is not None else None)
            if c in _JSON_COLS else record.get(c)
            for c in _RUN_COLS
        ]
        # cost may arrive as float/str; store as text for lossless round-trip
        placeholders = ", ".join("?" for _ in _RUN_COLS)
        updates = ", ".join(
            f"{c} = excluded.{c}" if c in _ALWAYS_OVERWRITE
            else f"{c} = COALESCE(excluded.{c}, extraction_run.{c})"
            for c in _RUN_COLS if c != "run_uid"
        )
        sql = (f"INSERT INTO extraction_run ({', '.join(_RUN_COLS)}) VALUES ({placeholders}) "
               f"ON CONFLICT (run_uid) DO UPDATE SET {updates}")
        with self._tx() as conn:
            conn.execute(sql, values)

    async def save_attempts(self, run_uid: str, attempts: list[dict]) -> None:
        sql = ("INSERT INTO extraction_attempt (run_uid, attempt_no, layer, valid, "
               "validation_errors, note, latency_ms, tokens) VALUES (?,?,?,?,?,?,?,?) "
               "ON CONFLICT (run_uid, attempt_no) DO UPDATE SET layer=excluded.layer, "
               "valid=excluded.valid, validation_errors=excluded.validation_errors, "
               "note=excluded.note")
        rows = [
            (run_uid, a.get("attempt_no"), a.get("layer"), int(bool(a.get("valid"))),
             json.dumps(a.get("validation_errors"), ensure_ascii=False), a.get("note"),
             a.get("latency_ms"), a.get("tokens"))
            for a in attempts
        ]
        with self._tx() as conn:
            conn.executemany(sql, rows)

    async def annotate_run(self, run_uid: str, *, field_logprobs: dict | None = None,
                           field_grounding: dict | None = None) -> bool:
        """Attach DERIVED annotations to an existing run row. Deliberately not save_run:
        that upsert always-overwrites final_status, which an annotation must never touch."""
        sets, values = [], []
        if field_logprobs is not None:
            sets.append("field_logprobs = ?")
            values.append(json.dumps(field_logprobs, ensure_ascii=False))
        if field_grounding is not None:
            sets.append("field_grounding = ?")
            values.append(json.dumps(field_grounding, ensure_ascii=False))
        if not sets:
            return False
        values.append(run_uid)
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE extraction_run SET {', '.join(sets)} WHERE run_uid = ?", values)
            return cur.rowcount > 0

    async def find_completed(self, run_uids: list[str], statuses=TERMINAL_STATUSES) -> set[str]:
        if not run_uids:
            return set()
        qs_uids = ",".join("?" for _ in run_uids)
        qs_st = ",".join("?" for _ in statuses)
        with self._tx() as conn:
            cur = conn.execute(
                f"SELECT run_uid FROM extraction_run WHERE run_uid IN ({qs_uids}) "
                f"AND final_status IN ({qs_st})",
                [*run_uids, *statuses],
            )
            return {r[0] for r in cur.fetchall()}

    async def fetch_extracted(self, run_uids: list[str]) -> dict[str, Any]:
        if not run_uids:
            return {}
        qs = ",".join("?" for _ in run_uids)
        with self._tx() as conn:
            cur = conn.execute(
                f"SELECT run_uid, extracted FROM extraction_run WHERE run_uid IN ({qs})",
                list(run_uids),
            )
            return {uid: (json.loads(ex) if isinstance(ex, str) else ex)
                    for uid, ex in cur.fetchall()}

    async def save_wrapping(self, doc: dict[str, Any], *, store_text: bool = False) -> int:
        from ..hashing import sha256_text

        with self._tx() as conn:
            conn.execute(
                "INSERT INTO text_wrapping (wrap_uid, text_sha256, logic, params, n_chars, n_chunks) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT (wrap_uid) DO NOTHING",
                (doc["wrap_uid"], doc["text_sha256"], doc["logic"],
                 json.dumps(doc["params"], ensure_ascii=False), doc.get("n_chars"), doc["n_chunks"]),
            )
            for c in doc["chunks"]:
                conn.execute(
                    "INSERT INTO text_chunk (chunk_uid, wrap_uid, idx, char_start, char_end, "
                    "chunk_sha256, text) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT (chunk_uid) DO UPDATE SET text = COALESCE(excluded.text, text_chunk.text)",
                    (c["chunk_uid"], doc["wrap_uid"], c["idx"], c["start"], c["end"],
                     sha256_text(c["text"]), c["text"] if store_text else None),
                )
        return len(doc["chunks"])

    async def save_merge_run(self, result: dict[str, Any], *, wrap_uid: str | None = None,
                             tags: list[str] | None = None) -> None:
        report = {"variables": result["variables"], "conflicts": result["conflicts"],
                  "stats": result["stats"], "schema_uid": result.get("schema_uid")}
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO merge_run (merge_uid, wrap_uid, config, n_inputs, run_uids, merged, "
                "report, conflicts, major_conflicts, tags) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (merge_uid) DO UPDATE SET merged=excluded.merged, report=excluded.report, "
                "conflicts=excluded.conflicts, major_conflicts=excluded.major_conflicts, "
                "tags=COALESCE(excluded.tags, merge_run.tags)",
                (result["merge_uid"], wrap_uid, json.dumps(result["config"], ensure_ascii=False),
                 result["stats"]["inputs"], json.dumps(result.get("run_uids") or []),
                 json.dumps(result["merged"], ensure_ascii=False),
                 json.dumps(report, ensure_ascii=False, default=str),
                 result["stats"]["conflicts"], result["stats"]["major_conflicts"],
                 json.dumps(tags) if tags else None),
            )
