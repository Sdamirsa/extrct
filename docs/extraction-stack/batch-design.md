# ExtrCT Batch — design of record (batch-def/1.0)

Benchmark-scale execution on the canvas: N async calls against OpenRouter, and the
full grid **inputs × provider-configs × schemas**. Researched 2026-08-17 (internal
machinery map + OpenRouter primary-source research, both reports in the session log);
this document locks the decisions before implementation.

## Goals

1. **Async N-call runner** — OpenRouter only (user decision): many structured-output
   requests under bounded concurrency, results collected, one row per item, nothing
   lost to a single failure.
2. **The sweep grid** — a LIST of input texts (optional `id` column, table or JSON),
   a LIST of provider configs, a LIST of schemas → every combination executed,
   resumable, capped, and reconstructable from the run log alone.

## Naming (collision avoided)

"Switchboard" in this repo means the parked the factorial sweep agent factorial (32 EHR cells) — the
extraction stack's word for a grid is **Sweep**. This lane is therefore **Batch**:
bundle `extrct_batch`, package module `extrct/batch.py`, components below. Vocabulary
aligned with the switchboard's discipline anyway: `cell` / human `cell_id`
(pipe-delimited) / content-hash uids attached AFTER hashing / `manifest` with counts +
unique uids.

## Decisions (each traceable to a research finding)

**D1 — No native OpenRouter Batch API.** It exists (`/api/beta/batches`, ~50% token
discount) but: beta route outside the v1 OpenAPI spec and its breaking-change
guarantee; provider pinning inside a batch is undocumented (and explicitly
unsupported for embeddings) — collides with the endpoint-tag house rule; Google-served
models require one schema per batch. Revisit ONLY if we ever drop pinning; the
discount is real money, so the revisit criterion is written here on purpose.

**D2 — `httpx` + semaphore via `runner.call_many`, not raw `gather`.** call_many is
the module the concurrency doc built for exactly this (index-safe rows, per-item
isolation, progress hook — the one requirement the current Sweep node drops).
Concurrency default 8, max 16 per pinned endpoint: OpenRouter publishes NO
recommended figure (paid models have no platform request cap; the binding constraint
is per-provider capacity and an undocumented Cloudflare abuse threshold), so this is
engineering judgment and labeled as such.

**D3 — Per-cell request path = `apply_client_overrides` → `pipeline.compose` →
`pipeline.execute_single`.** Never a third inline fork of the request path (the
current sweep_extract predates execute_single). Order matters: compose's upgrade-only
riders read the already-overridden spec, and evidence injection changes schema_uid →
request_uid → run_uid, so resume identity correctly separates grounded cells.

**D4 — Cell identity needs no new scheme.** `run_uid =
content_uid({request_uid, input_sha256})` already distinguishes all three axes (text,
config, schema all feed the request record). The user's optional `id` column is a
JOIN/display label only — it goes in `run_metadata.input_id`, NEVER into any hash
(re-labelling a row must not re-bill it). `batch_uid = content_uid(manifest)` names
the whole grid; every cell's `run_metadata` carries it plus `cell_id =
"{input_id|sha8}|{config_uid}|{schema_uid}"`.

**D5 — Retry policy (from the measured error table).** Retry `429, 500, 502, 503,
524, 529`, connect errors, read timeouts — full-jitter exponential backoff 1s→60s cap,
5 attempts, honoring `Retry-After` when present (it is usually ABSENT on 429; the
schedule must not depend on it). NEVER retry `400/404/413/422` — deterministic; a
pinned-endpoint 404 means the tag or allowlist is wrong. Note this widens
`RETRYABLE_STATUS` relative to `call_once`'s single-run default `{429,502,504}`;
batch passes its own retryable set — single-run behavior unchanged. 429s are "normal
operating condition, not an error state" (concurrency.md) — they are counted, never
hidden.

**D6 — The in-band-error gate.** A non-streaming **200** can carry
`choices[0].finish_reason == "error"` with partial content — a naive scorer grades
truncated garbage as an answer. `openrouter.read_response` already flags
`finish_reason_error_on_200` as terminal; the batch row must surface it as
`final_status="invalid"`, never as ok. (Verified our reader covers this; the batch
tests assert it explicitly.)

**D7 — Circuit breaker: `stop_on_failure_rate`.** Specified in concurrency.md years
ago, never built. Implemented here: after a minimum sample (default 10 completed
cells), if the failure fraction exceeds the threshold (default 0.5), stop dispatching,
mark undispatched cells `aborted_circuit_breaker` (kept as rows — absence is not
evidence), finish in-flight calls. A broken schema burns 10 calls, not 900.

**D8 — Budget guardrails.** Two layers: (a) documented recipe — mint a DISPOSABLE
per-run key with a spend `limit` (`POST /api/v1/keys`, Management key, user-side);
hitting the budget turns into 402s the batch records and the breaker catches;
(b) in-run: poll `GET /api/v1/key` (works with the ordinary inference key; the
`/credits` endpoint needs a Management key and CANNOT be called with an inference
key) between waves and surface `limit_remaining` in progress status. `usage.cost` is
NOT a required field in their schema — treat as nullable, never `float()` blind.
(Also noted: `usage: {include: true}` is now a deprecated no-op — cost is always
returned; our request builder's flag is harmless but obsolete.)

**D9 — Audit trail per cell (replayability/independent measurement).** Send `X-OpenRouter-Metadata: enabled`; persist
`openrouter_metadata` (attempts, selected endpoint, region — the proof a factorial
cell was served by its pinned endpoint) which rides the response body into
`provider_response`, and capture the **`X-Generation-Id` response header** into the
transport envelope (additive `call_once` extension) → run row. `GET
/api/v1/generation?id=` is then the authoritative post-hoc cost/latency
reconciliation for any row.

**D10 — Sticky-routing defense.** OpenRouter routes same-conversation requests to
the same provider by hashing the first system + first user message — a benchmark
reusing one system prompt would silently pin all rows to whichever provider served
first. Our pinning (`provider.order` + `allow_fallbacks: false` +
`require_parameters: true`) disables sticky routing (documented for `order`
specifically) — the batch VALIDATES every provider config carries an endpoint pin and
refuses configs without one (config_error rows for that column, not a dead run).

**D11 — Caps.** `GRID_CAP=500` stays what it is (a single `expand_grid` guard). The
batch product gets its own `BATCH_CAP = 2000` cells, checked at plan time with a loud
message. This deliberately re-scopes concurrency.md's "1000+ belongs to the
conductor" line: the package-first split below is the honest resolution — the same
`extrct.batch` functions ARE the conductor's future import; the canvas node is one
thin caller with the cap. Logged as an explicit decision.

**D12 — Egress and  unchanged.** `check_egress` per distinct provider config at
plan time; a refused config = `config_error` rows for that column only. ZDR remains a
retention control, not an egress control — nothing here moves OpenRouter inside the
the egress boundary; synthetic/de-identified text only.

**D13 — Housekeeping from the map.** `ensure_schema` once per batch (not per cell);
`fetch_extracted` on resume so skipped cells still deliver their extractions; results
DataFrame carries FULL extracted JSON (S8 — merger/QC downstream, no truncation);
`save_run` upserts make re-runs idempotent.

## batch-def/1.0 (the data model)

```
{"version": "batch-def/1.0",
 "inputs":  [{"input_id": "r001"|null, "text": "...", "input_sha256": "<computed>"}, ...],
 "configs": [{"config_uid": "...", "config": {...}}, ...],        # Flow - Sweep grid or authored list
 "schemas": [{"schema_uid": "...", "envelope": {...}}, ...],      # one or many
 "settings": {"concurrency": 8, "max_retries": 5, "stop_on_failure_rate": 0.5,
              "min_sample": 10, "skip_completed": true, "run_tags": "batch"},
 "batch_uid": "<content_uid of the above, uid attached after hashing>"}
```

`plan_batch()` validates loudly (OpenRouter-only, endpoint pins present, cap, egress
per config), returns the manifest (counts, unique uids, per-axis sizes, estimated
cells) BEFORE anything runs — the declared half of independent measurement.

## Component surface (bundle `extrct_batch`, registered in the review checker)

| Component | Category | Job |
|---|---|---|
| **Prep - Input Bank** (`extrct_input_bank`) | Prep | The inputs list: TableInput (id, text) or wired JSON/DataFrame; outputs rows (with computed `input_sha256`), a selected-input Data, and a manifest — the query_bank pattern generalized. |
| **Run - Batch** (`extrct_batch_run`) | Run | The executor: Inputs (DataFrame/Data), Configs (Flow - Sweep grid DataFrame or Data list), Schemas (Data list or single envelope), settings; outputs Results DataFrame (one row/cell, full extracted), Batch Report, Failed Cells, Steps/manifest, Progress via status. |

Schemas-list ingestion accepts: a single Schema Builder envelope, a Data list of
envelopes, or a DataFrame of `{schema_uid, envelope(json)}`. All three list axes go
through one shared reader (the `_configs()` pattern generalized into `extrct/batch.py`).

## Verification plan

Stub-transport matrix in-container: plan validation (pins, egress, cap, unknown
keys), 3-axis grid identity (uid uniqueness across axes; id-relabel does NOT change
run_uid), call_many wiring (progress, index safety), retry classification incl.
Retry-After honored/absent, in-band-200-error → invalid, circuit breaker trips at the
threshold and keeps undispatched rows, resume with fetch_extracted, budget 402
handling, X-Generation-Id captured. Then review 21/21, and a LIVE smoke against a
`:free` OpenRouter model (tiny grid, 4 cells) once the user's rotated key is in
deploy/.env — free-tier limits (20 RPM) make it cheap and also exercise the 429 path
honestly.

## Deferred, explicitly

Native Batch API (revisit criterion in D1); conductor migration (the package
functions are already the import surface); Ollama batch lane (user scoped OpenRouter
only); per-cell certainty/grounding in batch (the composed request supports it —
switch on when benchmarking needs it).
