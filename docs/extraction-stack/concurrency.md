# Concurrency and scale

*Design note, 2026-08-05. Written before the components, because concurrency decided where the
seams go.*

## The scale we are actually designing for

The pilot alone is 32 configs × 30 queries = **960 runs**, and the factorial sweep's real volume is *"tens of
thousands of invocations across cells"*. At 2 s/call serially, 960 runs is 32 minutes and 50,000
runs is 28 hours. Concurrency is not an optimisation here; it is the difference between a
feasible study and an infeasible one.

## One correction to the framing: nothing should be sync

A "sync component" in Langflow would be a **correctness bug**, not a simplification. Langflow's
graph runs on asyncio and executes independent layers in parallel (`asyncio.gather`,
`graph/base.py:2111`). A blocking `requests.post()` inside a component blocks the **whole event
loop** — every other node in the flow stalls, including unrelated branches.

So: every component is `async def build(...)` and uses `httpx.AsyncClient`, never `requests`.
The distinction you want is **single-item vs batch**, not sync vs async. Both are async
underneath; one just has a batch size of 1.

## Where the concurrency lives — the load-bearing decision

**Not in a Langflow component.** In a plain async Python module the components merely wrap.

```
packages/extrct/           (or, for now, a _core.py beside the components)
  client.py      async def call_once(request: dict) -> Response     # one HTTP call, no Langflow
  batch.py       async def call_many(requests, *, concurrency, on_progress) -> list[Result]
  schema.py      variables -> JSON Schema
  repair.py      the ladder
```

Three reasons this seam matters more than it looks:

1. **The conductor is not chosen yet, which is exactly why this matters.**  named Dagster,
   but that is *not finalised* (2026-08-05). Whatever ends up running the sweep — Dagster,
   Prefect, a cron script, or the Langflow batch node for longer than planned — needs to call the
   same code. Welding the batching into a Langflow component forecloses that choice and
   guarantees a second implementation later, at which point a the factorial sweep result depends on which one
   ran. An undecided conductor makes the seam more valuable, not less.
2. **It is testable without Langflow.** `call_many` against a fake transport is a unit test;
   a Langflow component is not.
3. **Single and batch cannot diverge.** The single-item component is `call_many([one])`. Any
   other arrangement guarantees the two paths eventually disagree about retries or headers.

For the prototype phase this can be one `_core.py` next to the components — pure functions, no
`lfx` imports — so extracting it into `packages/extrct` later is a file move.

## The right concurrency is a property of the backend, not the component

This is the non-obvious part, and getting it wrong wastes hours.

| Backend | Effective ceiling | Why |
|---|---|---|
| **Ollama** | `OLLAMA_NUM_PARALLEL` (commonly 1–4) | Requests beyond it **queue server-side**. Sending 32 concurrent does not go faster — it adds latency variance and makes timeouts fire on requests that never started |
| **vLLM** | high (dozens) | Continuous batching is designed for it; this is the arm that actually benefits from concurrency |
| **OpenRouter** | rate-limit bound, credit-dependent | 429s are normal operating condition, not an error state. Must respect `Retry-After` |

So `concurrency` is an input **on the client component**, defaulting per backend — Ollama 4,
vLLM 16, OpenRouter 8 — and the docs must say why, or someone will set it to 64 for Ollama and
conclude the model is slow.

Worth verifying at build time: query the Ollama host for its actual `OLLAMA_NUM_PARALLEL` rather
than guessing, and surface it as the suggested ceiling.

## Batch runner requirements

Non-negotiable, in rough order of how much pain each prevents:

1. **Bounded concurrency** — `asyncio.Semaphore(n)`. Never unbounded.
2. **Per-item isolation.** One failure must not kill the batch. Every row returns a status; the
   batch always completes. `asyncio.gather(..., return_exceptions=True)`, or per-item try/except.
3. **Deterministic row mapping.** Results carry the input row's id. Never rely on completion
   order — with concurrency the order is nondeterministic by construction, which would silently
   corrupt a 960-row join.
4. **Retry with backoff and jitter**, honouring `Retry-After`. Retries are logged as attempts,
   not hidden — they are part of the run's cost and latency.
5. **Progress.** `self.status` updated as items complete; a 20-minute node showing nothing is
   indistinguishable from a hung one.
6. **Resume.** This one falls out of the logging design for free: `run_uid` is
   `sha256(config_uid + query_uid + schema_uid)`, so before dispatching, skip rows whose `run_uid`
   already has a terminal row in `extraction_run`. If 900 of 960 succeeded, a re-run costs 60
   calls. **This is the single most valuable property of the whole design** and it exists only
   because run identity is content-addressed rather than sequential.
7. **Cancellation.** A cancelled Langflow run must not leave 30 in-flight requests burning
   credits — propagate `asyncio.CancelledError` and close the client.

## Component shapes

**Client components** (`OpenRouter Chat Request`, `Ollama Chat Request`) do **not** take a batch.
They emit a configured client handle plus a request template. Config only, no execution.

**`Structured Extract`** — single item. Text in, validated object out. For designing a schema and
watching one extraction closely.

**`Batch Extract`** — a DataFrame in, a DataFrame out, one row per item, with
`concurrency`, `max_retries`, `skip_completed` (resume), `stop_on_failure_rate` (a circuit
breaker: abort if >X% fail early, rather than burning 900 calls on a broken schema).

Both call the same `call_many`. The single-item one is a batch of one.

**Schema comes from the builder for both**, as a `Data` port — never re-authored per component.

## Storage: Postgres for the log, not Chroma

Verified 2026-08-05 in the Langflow container: **`psycopg` 3.3.4, `psycopg2-binary`, and
`sqlalchemy` 2.0.51 are already installed**, and `extrct-postgres` is already running and empty.
So Postgres costs **zero** new dependencies and zero new containers — the usual reason to reach
for an embedded store does not apply here.

`chromadb` 1.5.9 and `langchain-chroma` 0.2.6 are also present, and Chroma is the right tool for
**semantic search over variable descriptions** — "has someone already defined a variable like
*ejection fraction*?" is a vector question, and it is how the schema-generator agent should find
reuse candidates. Keep it for that.

It is the wrong tool for the registry and the log:

| Need | Chroma |
|---|---|
| "Which schemas use LVEF?" | no joins |
| Failure rate by model × schema shape | no `GROUP BY`, no aggregates |
| `parent_uid` tree for nested variables | no foreign keys, no recursive CTEs |
| Resume: does `run_uid` have a terminal row? | works (`get(ids=[...])`) |

**Decisive detail:** Chroma metadata values must be scalars (str/int/float/bool) — no nested
JSON. `validation_errors` is an array of error objects and is *the* field error analysis runs on.
Stored in Chroma it becomes a string, and every analysis becomes string-parsing in Python instead
of one SQL query. That is precisely where the log was supposed to pay off.

**Rule:** Postgres rows are the source of truth; a Chroma collection, if built, is a derived index
over them and must be rebuildable from Postgres alone.

Regardless of choice, the storage layer sits behind a narrow interface — `save_run`,
`save_attempt`, `find_completed` — in the same plain module as `call_many`, so the backend is a
swap and not a rewrite.

## The line: what must not run in Langflow

Langflow will look capable enough to run the sweep. It is not:

- No content-hash caching of cell results
- No per-partition retry or backfill
- No cost/latency accounting across cells
- A browser tab that must stay open, on a laptop that must stay awake
- Flow JSON is not an artifact of record, so the sweep's *configuration* would be
  unciteable

| Scale | Surface | Purpose |
|---|---|---|
| 1 | `Structured Extract` | design a schema, watch one run closely |
| 10–200 | `Batch Extract` | does this schema hold on a real sample? |
| 1000+, factorial | **a conductor (TBD)** | the study |

The batch component exists so you can iterate on a schema against 50 reports without leaving the
canvas — **not** so the sweep can run there.

The shared `call_many` is what makes that boundary cost nothing: whatever conductor is chosen
imports the same function, so moving from 200 to 50,000 is a change of conductor, not a rewrite.
Until one is chosen, `Batch Extract` plus `skip_completed` is a workable stopgap for hundreds of
runs — it just cannot give you per-cell caching, cost accounting, or an unattended overnight run.

## Open questions for the build

- **Ollama's real `OLLAMA_NUM_PARALLEL`** on this host — query it, do not assume.
- **Does Langflow's own execution impose a concurrency cap** on a single component's internal
  `asyncio.gather`? Nothing suggests it does, but a 200-item batch inside one node is worth
  testing before relying on it.
- **Timeout budget.** A 200-item batch at concurrency 4 with 30 s timeouts is 25 minutes
  worst-case. Does Langflow's HTTP layer or the browser session time out first? If so, batch size
  in the canvas has a hard practical ceiling and the docs must say what it is.
- **Where the log is written from.** If `call_many` writes to Postgres directly, Dagster and
  Langflow share the log for free — but the connection string must then be config, not a
  component input.
