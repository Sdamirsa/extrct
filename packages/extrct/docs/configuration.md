# Configuration reference — job-def/1.0

A job is one YAML document with one section per concern. Only authored keys are kept
(sparse documents read as intent); everything else falls back to spec defaults, which
the normalized client definition records in full. Validation is loud at load time:
unknown sections, unknown keys, type garbage, and closed-vocabulary violations all
raise with the allowed list in the message.

```yaml
provider:        # or `client:` — same section, one spelling per file
  provider: ollama | openrouter | <registered name>
  <flat spec settings>          # or nested under `settings:`

schema:
  root_name: extract
  encoding: strict_nullable | native_required
  additional_properties: false
  variables: [...]              # required — see below

extract:    {...}               # ladder, retries, tags, logging
grounding:  {...}               # evidence quotes + alignment
certainty:  {...}               # logprob confidence
wrapper:    {...}               # long-text chunking
merger:     {...}               # multi-chunk merging
storage:    {...}               # run log backend (deployment: excluded from job_uid)
```

Identity: `job_uid` hashes client + schema + the content sections. `storage` is
deployment — the same job logged to a different database is the same job.

## schema.variables

One row per field. Nesting via `parent`; the parent must be a declared `object` row.

| key | type | default | notes |
|---|---|---|---|
| `name` | str | — | unique across the WHOLE schema, snake_case |
| `type` | str | `str` | `str` `int` `float` `bool` `date` `datetime` `object` |
| `is_list` | bool | false | many of this field |
| `description` | str | "" | sent to the model — this is where extraction guidance lives |
| `parent` | str | — | name of an `object` row |
| `options` | list \| "a,b" | — | enum; prefer over free text whenever values are known |
| `constraints` | map | — | `ge le gt lt pattern min_length max_length min_items max_items` |
| `required` | bool | true | under `strict_nullable`, optional = nullable (null joins the enum too) |
| `ordinal` | int | 0 | display/emission order |

YAML gotcha: quote enum options that YAML would read as booleans or null
(`"yes"`, `"no"`, `"on"`, `"off"`, `"null"`).

**encoding** is an experimental axis, not a style: measured on identical variables,
`native_required` let the model omit an optional field that `strict_nullable` filled.
Pin it per run; the two hash to different `schema_uid`s because they are different
requests.

## provider (client) — common keys

Everything on the spec dataclass is authorable except `api_key` and `capability`,
which raise on sight (credentials resolve from the environment at send time;
capability is fetched, never authored). Full field lists: the `OllamaSpec` /
`OpenRouterSpec` dataclasses are the single source of truth — an unknown key's error
message prints the allowed set.

Ollama essentials: `base_url`, `model`, `mode` (`json_schema`|`json_object`|`prompted`),
`num_ctx`, `num_predict`, sampling (`seed`, `temperature`, …; always sent explicitly),
`think` (off by default: it changes answers and burns `num_predict` before the grammar
engages), `logprobs`/`top_logprobs`.

OpenRouter essentials: `model` (exact id, `:free` suffix included), `endpoint_tag`
(pin it; unpinned requests are refused by default), `data_classification` (required —
see below), `egress_route`/`gateway_url`/`allow_direct_egress`,
`require_parameters` (default true: capability gaps 404 loudly instead of silently
downgrading), `zdr`, `max_output_tokens`, tri-state sampling
(`send_seed`/`send_temperature`/`send_top_p`: `auto`|`always`|`never` — omitted means
the VENDOR default is inherited, and that is recorded), `reasoning_control`
(explicitly off by default — not sending it is not a no-reasoning baseline).

### The egress guard

`data_classification` must be one of `synthetic`, `deidentified_aggregate`, `public`,
`approved_for_egress`. There is deliberately no value that means "confidential but
allow it" — text that must not leave the machine has no legal value here. Routing:
set `gateway_url` (or the `EXTRCT_GATEWAY_URL` env var) to send traffic through a
gateway you control; otherwise author `egress_route: direct` +
`allow_direct_egress: true` explicitly. When all three egress keys are left at their
defaults, the environment decides at payload-build time — AFTER `client_uid` is
stamped, so deployment never leaks into identity.

## extract

| key | default | notes |
|---|---|---|
| `ladder` | `["json_repair", "coerce"]` | rungs in order; `reprompt`/`llm_repair` need callables (Python API) |
| `max_retries` | 2 | transport retries (429/502/504), recorded |
| `max_reprompts` | 1 | 0 is honoured: rung selected, re-billing forbidden |
| `run_tags` | "" | comma-separated; lands as the queryable `tags` column |
| `store_input_text` | false | the privacy default; flip only deliberately |
| `log_to_db` | true | run rows + attempts (no-op without a storage backend) |

## grounding

| key | default | notes |
|---|---|---|
| `enabled` | — | on: the evidence mirror is injected into the schema AT REQUEST TIME and every quote aligned per chunk |
| `mode` | `auto` | `auto`/`inline` are identical; `posthoc` is recorded as skipped (ground stored runs via `xai.grounding` + `schema.evidence_schema`) |
| `fuzzy_threshold` | 0.75 | in [0.5, 1.0]; quotes below it are UNLOCATED (unverified evidence) |
| `log_to_db` | true | per-field alignment onto the run row (`field_grounding`) |

## certainty

| key | default | notes |
|---|---|---|
| `enabled` | — | on: logprob riders merge into the client spec (UPGRADE ONLY — an authored stronger setting is never lowered) and every variable is scored per chunk |
| `enum_posterior` | true | first-token posterior over the option set; refused with a reason on collisions |
| `top_logprobs` | 3 | minimum alternatives requested; the enum posterior needs ≥ 1 |
| `log_to_db` | true | scores onto the run row (`field_logprobs`) |

## wrapper

| key | default | notes |
|---|---|---|
| `enabled` | — | off = the whole text is one chunk (the single-run path IS the 1-chunk case) |
| `logic` | `paragraph_pack` | `fixed_window` \| `paragraph_pack` \| `sentence_pack` — all deterministic |
| `max_chars` | 4000 | floor 200 |
| `overlap_chars` | 400 | in [0, max_chars) |
| `parallel_calls` | provider default | chunk-level concurrency |
| `store_text` | false | chunk offsets+hashes are always stored; text is opt-in |
| `log_to_db` | true | the wrap-def document |

## merger

| key | default | notes |
|---|---|---|
| `scalar_strategy` | `majority` | `majority` `first` `last` `longest` `refuse` |
| `conflict_policy` | `label_and_resolve` | `label_and_null` nulls MAJOR conflicts regardless of strategy |
| `numeric_tolerance` | 0.01 | relative; groups 55.0 with 55.3 |
| `list_similarity` | 0.85 | clustering threshold for items extracted twice from overlapping chunks |
| `list_key` | "" | blocking key: match object list items on one field only |

Conflicts are always SURFACED (candidates, chunks, severity) whatever the strategy
resolves — `label_and_null` only changes the resolved value, never the report.

## storage

| key | notes |
|---|---|
| `backend` | `none` \| `sqlite` \| `postgres` |
| `path` | sqlite file path |
| `dsn` / `dsn_env` | postgres DSN, or the env var holding it (default `EXTRCT_PG_DSN`) |

## Overrides and sweeps (Python API)

`config.py` applies namespaced keys over any base: `ollama.model`, `openrouter.zdr`,
`client.temperature` (generic; the receiving provider's spec must have the field),
`extract.run_tags`, … Unknown keys within an owned namespace raise. `expand_grid`
turns `{key: [values]}` into config cells, each stamped with its `config_uid` — the
batch lane consumes these directly (`examples/06_batch_grid.py`).

## Environment variables

| var | used by |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter credential (or a custom name via `api_key_env_var`) |
| `EXTRCT_GATEWAY_URL` | egress gateway default for definition-built clients |
| `EXTRCT_PG_DSN` | postgres run log DSN default |
