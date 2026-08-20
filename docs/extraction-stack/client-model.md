# The client model — payload handshake and client-def/1.0

Two contracts in one page: the **Client payload** (what flows between a client node and
the extract — until now machine-checked but unwritten), and the **client definition**
(`client-def/1.0` — a client authored as pure data, built for agents). Package authority:
`extrct/client_model.py`; the spec dataclasses in `ollama.py` / `openrouter.py`
remain the single source of truth for fields, types, and defaults.

## 1. The Client payload (the wire contract the extract consumes)

```
{ "provider":   "ollama" | "openrouter",     # discriminator, checked with a loud error
  "spec":       { ...spec dataclass fields... },
  "api_path":   "/api/chat",                 # ollama only; silent default if absent
  "key_source": "...",                       # openrouter widget only; informational
  "client_uid": "...",                       # definition-built payloads only
  "config_uid": "..." }                      # widget clients, when Overrides applied
```

**The schema of `spec` IS the dataclass.** Producers serialize `spec.__dict__`; the
consumer reconstructs `OllamaSpec(**kwargs)` / `OpenRouterSpec(**kwargs)`, so an unknown
key is a `TypeError` on the extract node and a missing key falls back to the typed
default. `config.py`'s `CLIENT_FIELDS` derives the override vocabulary from
`dataclasses.fields()` — no parallel list to drift. `check_egress` re-runs on the
consumer side for OpenRouter: the guard survives serialization.

Producers: Client - Model Server (the merged widget, provider dropdown), the Flow - Controller (data),
`client_model.build_client_payload()` (any Python caller). Consumers: Run - Structured
Extract (incl. its repair-client lane), Run - Sweep Extract (rebuilds per cell over the
same spec kwargs). A new provider must ship: the spec dataclass, `build_request` /
`read_response` / `request_record`, a `check_*` guard if it egresses, and a payload with
`provider` + `spec`.

## 2. client-def/1.0 — a client authored as data

The extraction-variable model defines WHAT to extract; this defines HOW to call. An
agent writes JSON; `client_definition()` validates, fills defaults, stamps identity:

```json
{"provider": "ollama", "settings": {"model": "gemma4:31b-it-q8_0", "num_ctx": 16384}}
```

→ normalized document `{version, provider, settings(full), client_uid}` →
`build_client_payload()` → the §1 payload, unchanged downstream. On the canvas the
consumer is the **Flow - Controller** (`extrct_flow`): a bare client document — wired
Data/Message, a Registry `load_client` row, or pasted JSON, flat shorthand included —
becomes the flow document's `client` section and rides its Client Config thread. (The
earlier standalone Client - Definition node was retired 2026-08-10 in favor of the
controller; this model is its foundation and is unchanged.)

**Validation is loud, by design — an agent's typo must die at authoring time:**
- unknown key → raises with the full allowed list
- wrong type → raises (strings are coerced for numeric/bool fields; a number where a
  string belongs raises — not silently stringified)
- closed vocabularies enforced at definition time: `mode`, `egress_route`,
  `data_classification` (membership only — emptiness stays `check_egress`'s call),
  tri-state `send_*`, `max_output_field`, `reasoning_control`
- `api_key` / `capability` raise on sight and never appear in the document, not even
  as defaults — credentials resolve at send time, capability is fetched, never authored

**Identity — three layers, never conflated:**

| uid | Names | Stamped by |
|---|---|---|
| `client_uid` | the definition | `client_model.client_definition` |
| `config_uid` | a config/sweep cell applied on top | `config.py` |
| `run_uid` | one call | request record / storage |

`client_uid` is machine-independent: OpenRouter egress fields left at their
defaults are resolved from `EXTRCT_GATEWAY_URL` **after** the uid is stamped, mirroring
the widget client (`set → gateway; empty → explicit direct`). Same JSON, same uid, any
machine — the resolved route rides the run row. Explicitly authored egress is respected
and does shape the uid: authoring intent is content.

**The capability caveat:** a definition-built OpenRouter payload carries
`capability={}` (the widget client fetches it at build time; a definition is authored
offline). Pin `endpoint_tag` explicitly and keep `require_parameters=True` — the
measured behavior (2026-08-05) turns a capability gap into a loud 404 instead of a
silent downgrade.

**Reverse lane:** `definition_from_payload()` captures a live widget-client payload as
a definition (forbidden keys dropped) — read the current client, mutate, re-emit. For
Ollama this round-trips to the identical uid; a payload with env-resolved egress
captures its deployment explicitly, which is the honest reading of a live client.

## 3. Persistence — named clients in the registry

Stored client definitions are AUTHORED CONFIGURATION (the same asymmetry argument as
`schema_variable`): the `client_definition` table holds named handles over
content-addressed documents, with full CRUD through DB - Registry:

| Operation | Kind | Does |
|---|---|---|
| `save_client` | write | Validate + store the wired document under Client Name. The uid is **recomputed** through `client_model` — the wire is never trusted. Overwrite Target gates replacement. |
| `load_client` | read | One stored definition; the row wires **directly** into the Flow - Controller (its `definition` column is unwrapped by `parse_document`). |
| `list_clients` | read | name, provider, model, client_uid, updated_at — newest change first. |
| `delete_client` | write | Remove one name. Runs are untouched: a run row carries its full resolved spec, not a reference. |

Package lane for non-canvas callers: `storage.save_client_definition` /
`load_client_definition` / `delete_client_definition`.

## 4. extrct_client_schema — the meta-schema for agent authoring

The client analog of `extrct_schema_builder_schema`, seeded by `ensure_schema` and
restorable via `reset_builtin_schemas`: a builtin schema set an agent takes as its
`output_type` to **produce** a client definition (the flat shorthand). Its 15 field
descriptions are load-bearing prompt text teaching the measured traps: family-specific
`format` support, `num_gpu=0` CPU-forcing, silent context truncation, the  egress
gate, `:free`-suffix endpoint identity, endpoint pinning, and the Top-Logprobs-for-
certainty rule. Only the consequential fields appear; any other spec field may ride as
an extra top-level key, and the Flow - Controller rejects unknown keys with the full
allowed list — the schema teaches, the model gates.

The closed authoring loop: Registry `load_variables(extrct_client_schema)` → agent
extraction → flat document → Flow - Controller (validate + uid) → Registry
`save_client` → later, `load_client` → Flow - Controller → Run - Structured Extract.

Verified in-container 2026-08-10: 13-check model matrix (idempotence, key-order
independence, every failure mode's message, env-resolution timing, tuple round-trip),
7-check component matrix through the real loader, and the full persistence loop against
the live database (seed → envelope from DB rows → save/refuse/overwrite/load →
load_client row wired into Client - Definition → `OllamaSpec` reconstruction).
