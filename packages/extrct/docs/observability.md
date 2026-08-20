# Observability — the run log

Every model call becomes one row in `extraction_run`; every repair attempt one row in
`extraction_attempt`; every chunking one `text_wrapping` (+ `text_chunk` offsets);
every merge one `merge_run`. Two backends, same tables, same semantics:

- **postgres** (`pip install extrct[postgres]`) — the reference: JSONB columns,
  GIN-indexed tags, SQL views. For teams and analysis.
- **sqlite** — stdlib, zero setup. For laptops, notebooks, CI. Moving up later is a
  swap: the `RunStore` protocol is identical.

```yaml
storage: {backend: sqlite, path: runs.sqlite}
# or
storage: {backend: postgres, dsn_env: EXTRCT_PG_DSN}
```

## What a run row holds

Identity (`run_uid`, `request_uid`, `schema_uid` + encoding, provider, model,
endpoint, seed, temperature, mode) · privacy-safe input (`input_sha256`,
`input_chars`; `input_text` NULL unless explicitly enabled) · outcome (`final_status`:
`ok | repaired | invalid | error`, attempts, `layers_used`, `silent_failure_flags`) ·
economics (`total_tokens`, `cost_usd`, `latency_ms`, `http_status`) · evidence
(`request_record` with the REDACTED wire body, `provider_response` VERBATIM,
`extracted`) · joins (`tags`, `run_metadata` with `job_uid` / `batch_uid` /
`input_id` / `chunk_uid`) · annotations (`field_logprobs`, `field_grounding`).

## The write discipline

- `save_run` upserts on `run_uid` with COALESCE semantics: a partial re-save can never
  ERASE a column an earlier complete save populated. `final_status` / `attempts` /
  `ended_at` are the deliberate exceptions — they legitimately progress.
- `annotate_run` is the only after-the-fact writer, and it can only touch the two
  derived-annotation columns. It exists because certainty and grounding are
  deterministic derivations of `provider_response` + schema — convenience, not new
  evidence.
- There is no delete/update API for run rows at all. Evidence is written by the thing
  that produced the run; cleaning up test runs takes raw SQL, and that friction is
  intentional.

## Resume — why hashing pays rent

`run_uid = content_uid({request_uid, input_sha256})`. `find_completed(uids)` returns
which of a planned set already finished (`ok`/`repaired`), and the batch lane skips
them as `skipped_completed` rows — with the extracted object refetched, so downstream
consumers still get complete inputs. Kill a 960-cell benchmark at cell 900 and re-run:
60 calls. Re-labelling inputs (`input_id` lives in `run_metadata`, never in the hash)
re-bills nothing.

## Queries that answer real questions

```sql
-- the clean projection (both backends ship the views)
SELECT * FROM v_run_report ORDER BY started_at DESC LIMIT 20;

-- everything that went wrong, with the reason
SELECT * FROM v_failed;

-- failure rate by schema encoding — the axis the encoding exists for
SELECT schema_encoding, final_status, count(*)
FROM extraction_run GROUP BY 1, 2 ORDER BY 1, 2;

-- which models needed the repair ladder (excluding clean runs)
SELECT model_on_wire, count(*) FILTER (WHERE final_status = 'repaired') AS repaired,
       count(*) AS total
FROM extraction_run GROUP BY 1;

-- cost per batch (Postgres; SQLite: json_extract(run_metadata, '$.batch_uid'))
SELECT run_metadata->>'batch_uid' AS batch, sum(cost_usd), count(*)
FROM extraction_run WHERE run_metadata ? 'batch_uid' GROUP BY 1;

-- tagged runs (Postgres GIN index makes this a lookup, not a scan)
SELECT * FROM extraction_run WHERE tags ? 'pilot2';

-- ungrounded fields across a tag: the human-review queue
SELECT run_uid, field_grounding
FROM extraction_run
WHERE tags ? 'pilot2'
  AND (field_grounding->'summary'->>'unlocated')::int > 0;
```

## Registries (Postgres only)

`PostgresRunStore` additionally stores AUTHORED configuration with full CRUD —
`schema_variable` (variable rows by `schema_set`) and `client_definition` (named
client-def documents). The asymmetry with the run log is the point: configuration is
authored and editable; runs are evidence. Deleting a schema set or a stored client
never touches history — run rows carry the full resolved spec and cite `schema_uid`,
not a registry row.

## Live traces — built-in OpenTelemetry

The run log is the record of truth; traces are the live view (latency waterfalls,
error rates, correlation with a host application). extrct instruments itself with the
OTel API — optional, off by default, zero overhead when unused:

| you install | what happens |
|---|---|
| nothing extra | telemetry is a local no-op |
| `extrct[otel]` (API only) | spans are emitted **iff the host app configured an SDK** — extrct never configures exporters itself |
| `+ opentelemetry-sdk` + an exporter | spans flow wherever you point them |

Span shape: one `extrct.extract` root per document; one `chat <model>` span per model
call, nested — emitted on THE shared request path, so single runs, long-text chunks
and batch cells are traced identically; one `extrct.batch` root per grid. Attributes
follow the GenAI semantic conventions (`gen_ai.provider.name`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`/`output_tokens`) plus `extrct.*` identities. **The privacy
contract holds in traces exactly as in the log**: run_uids, hashes, counts, statuses,
flags — never input text, extracted values, quotes, or keys. `extrct.run_uid` is the
join key from any trace back to its full evidence row.

Console quickstart: `python examples/07_otel_traces.py`. Self-hosted **Langfuse ≥
3.22** ingests OTLP over HTTP (verified 2026-08-20; no gRPC) and maps the `gen_ai.*`
attributes onto its native model/usage fields:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:3000/api/public/otel/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic <base64(public_key:secret_key)>"
```

Two-layer setups (e.g. a Langflow canvas that already traces to Langfuse): the
Langfuse Python SDK v3 is itself OTel-based, so extrct spans nest under the host's
trace through ordinary context propagation — one trace, two layers of detail, and the
`extrct.run_uid` attribute still points at the evidence row. If you prefer a proxy
layer instead (LiteLLM gateway, or any OTel-instrumented router), it plugs into the
existing egress seam (`gateway_url` / `EXTRCT_GATEWAY_URL`) without touching the
library.

## What is deliberately NOT logged

Input text (hash only, unless `store_input_text`), chunk text (offsets + hashes only,
unless `store_text`), API keys in any form (a `sha256[:8]` fingerprint says WHICH key
was used, never the key), and un-redacted message content inside `request_record`
(the verbatim model OUTPUT lives in `provider_response` — outputs are evidence;
inputs are yours).
