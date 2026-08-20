# Architecture

## The one-sentence version

Pure functions build requests and read responses; one async transport executes them;
every identity is a content hash; every run is evidence in a swappable log; providers,
stores, and XAI plug in at registries and protocols, never at if/elif chains.

## Layers

```
┌────────────────────────────────────────────────────────────────────┐
│ facade      Extractor / ExtractionResult          (extractor.py)   │
│             job-def/1.0 YAML loading              (job.py)         │
├────────────────────────────────────────────────────────────────────┤
│ engine      pipeline-run/1.0: wrap → request → ground → certainty  │
│             → merge                               (pipeline.py)    │
│             schema (variables → JSON Schema)      (schema.py)      │
│             repair ladder                         (repair.py)      │
│             wrap-def/1.0 chunking                 (wrapping.py)    │
│             merge-def/1.0 merging                 (merging.py)     │
│             batch-def/1.0 grid                    (batch.py)       │
│             client-def/1.0 definitions            (client_model.py)│
│             namespaced overrides + sweep grids    (config.py)      │
├────────────────────────────────────────────────────────────────────┤
│ adapters    providers/  registry: ollama, openrouter, yours        │
│             storage/    RunStore: postgres, sqlite, null           │
│             xai/        certainty, grounding (pure analysis)       │
├────────────────────────────────────────────────────────────────────┤
│ kernel      hashing (content addressing)          (hashing.py)     │
│             runner (async transport, call_many)   (runner.py)      │
└────────────────────────────────────────────────────────────────────┘
```

Import rules: the kernel imports nothing local. Adapters import the kernel (and
`schema` for envelope unwrapping). The engine imports kernel + adapters through their
registries. The facade composes everything. Nothing imports the facade.

## Design rules and where they bind

**Pure request/response functions.** `build_request` / `read_response` /
`request_record` do no I/O. Everything on the wire is therefore testable byte-for-byte
offline, and a request can be re-built (and its uid re-derived) years later from the
definition alone.

**One request path.** Single extraction, per-chunk long-text calls, and batch cells
all go through `pipeline.execute_single`. There is deliberately no second code path to
drift: fix a failure mode once and every lane inherits it.

**HTTP success is not extraction success.** Providers decide, in `read_response`,
whether a 200 is actually a failure (truncation, context pressure, downgrade, an error
inside a 200 body) and say so in `silent_failure_flags` / `terminal_failure`. The
truncation gate runs BEFORE the repair ladder, because a truncated body is a
well-formed prefix that `json_repair` would happily brace-balance into a plausible
wrong answer.

**Repair is an ablation axis.** Rungs are declared per run, logged per attempt, and
`final_status` separates `ok` from `repaired` so analysis can exclude repaired runs.
Range clamping does not exist: coercing `lvef: 250` into `100` would launder a wrong
answer into a well-formed one.

**Analysis is non-breaking and re-derivable.** Grounding and certainty run as recorded
steps whose failure never loses an extraction, and both are pure functions of stored
evidence (provider payload + schema + source text) — anything can recompute them from
the run log without the pipeline (the annotation columns are query convenience, not
new evidence, which is why the narrow `annotate_run` writer is allowed to exist).

**Graceful degradation, explicit severity.** Wrap failure is breaking (nothing to
extract). Request failure is per-chunk data. Grounding/certainty failure is a recorded
step. Merge failure loses only the merged view. Every skipped or failed step carries a
reason — absence is not evidence.

**Declared vs executed.** The pipeline record carries the plan (what the configs
declared) next to the executed steps, so "it silently didn't run" is impossible to
miss.

## Identity model

`sha256(canonical_json(body))[:16]`, canonical = sorted keys, no incidental
whitespace, non-ASCII preserved. Identity layers, deliberately distinct:

| uid | hashes | notes |
|---|---|---|
| `schema_uid` | the JSON Schema sent | evidence injection produces a NEW uid (correct: different request); the clean uid is recorded alongside |
| `client_uid` | provider + normalized settings | deployment (gateway URLs) resolves from env AFTER stamping — same YAML, same uid, any machine |
| `config_uid` | one override/sweep cell | |
| `wrap_uid` / `chunk_uid` | chunking plan applied to a text / one span | chunks are re-derivable; storing their text is opt-in provenance |
| `run_uid` | `{request_uid, input_sha256}` | the resume key: completed cells are skipped by hash, never re-billed |
| `job_uid` | client_uid + schema_uid + content sections | the `storage` section is deployment and is excluded |

Rules: credentials and capability can never enter any hashed document (they raise on
sight). Tags and labels (`input_id`) are join metadata, never hashed — re-labelling
must never re-bill.

## Extension points (the SOLID story)

| To add | Implement | Register | Core changes |
|---|---|---|---|
| a provider (vLLM, Cerebras, Fireworks…) | `Provider` subclass + frozen spec dataclass | `register_provider()` | none — definitions, overrides, sweeps, the pipeline and the facade all resolve through the registry |
| a run-log backend | the `RunStore` protocol (8 methods) | `open_store()` branch or pass the instance | none |
| a repair rung | a callable passed to `run_ladder` (`reprompt` / `llm_repair` slots) | — | none for callables; a genuinely new rung extends `LAYERS` |
| a chunking logic | a span function in `wrapping` | add to `WRAPPER_LOGICS` | one module |

Backward compatibility contract: `*-def` documents are versioned
(`client-def/1.0`, `job-def/1.0`, …). Additive fields keep the version; anything that
changes the meaning or hashing of an existing document bumps it, and readers keep
accepting the old version.

## Concurrency model

`runner.call_many` is the only fan-out primitive: order-preserving (results[i] ↔
items[i] — completion order is nondeterministic and never relied on), failure-isolated
(one exception becomes one error row), cancellation-propagating (an aborted run leaves
nothing in flight burning credits). Default concurrency is a property of the BACKEND
(`Provider.default_concurrency`), not the caller: oversubscribing Ollama just queues
server-side and fires timeouts on requests that never started.
