# extrct

Schema-first structured extraction with LLMs — built for people who need to **trust**
and **replay** their extractions, not just obtain them.

You define *what* to extract as a list of variables (YAML, a table, a database row, or
an agent's output), *how* to call a model as a provider config (Ollama local,
OpenRouter cloud, or your own registered backend), and extrct does the rest:
builds the JSON Schema, makes the async calls, gates the silent failure modes, walks a
logged repair ladder, and writes every run — request hashes, provider response,
attempts, cost — into a content-addressed run log (Postgres or SQLite). Two XAI
modules explain the result after the fact: **per-field certainty** from token logprobs
and **evidence grounding** that verifies every quoted justification against the source
text.

```
   wrap ──► request (×N chunks, bounded parallel) ──► grounding ──► certainty ──► merge
 (long text)      truncation gate · repair ladder      quote        logprob      conflicts
                                                       alignment    statistics   surfaced
```

## Why another extraction library?

Most wrappers optimise for the happy path. This one is organised around the measured
ways extraction **fails quietly**:

- Ollama returns HTTP 200 for truncation and context overflow — extrct gates both
  *before* the repair ladder can brace-balance a truncated body into a plausible wrong
  answer.
- OpenRouter will silently downgrade a `json_schema` request on an endpoint that can't
  enforce it — extrct pins endpoints and sends `require_parameters: true`, turning
  the downgrade into a loud 404.
- Baked model defaults differ per model (temperature 0.6 vs 1.0 in the Modelfile) —
  extrct always emits every sampling knob, so "two models at default" is a real
  comparison.
- Repair inflates success rates invisibly — here repair is an ablation axis: every rung
  is declared, logged per attempt, and `final_status` separates `ok` from `repaired`.
- A quoted "evidence" string the source can't reconstruct is hallucinated evidence —
  the grounding aligner treats an unlocatable quote as a finding, not a formatting
  issue.

## Install

```bash
pip install extrct            # httpx + jsonschema + pyyaml
pip install "extrct[repair]"    # + json-repair (deterministic JSON fixing rung)
pip install "extrct[postgres]"  # + psycopg (reference run-log backend)
```

Until the first PyPI release, install straight from the monorepo:

```bash
pip install "extrct @ git+https://github.com/sdamirsa/extrct#subdirectory=packages/extrct"
```

Python ≥ 3.10. Pure Python, no compiled dependencies — Windows, macOS, and Linux are
all first-class (CI runs the matrix on all three). SQLite logging works with no
extras. GPU work never happens in-process: models are reached over HTTP (Ollama,
vLLM, OpenRouter, …), so extrct itself is CPU-only everywhere.

Optional: `pip install "extrct[otel]"` turns on OpenTelemetry spans (see
[docs/observability.md](docs/observability.md)).

## Quickstart

`configs/echo-report.yaml`:

```yaml
provider:
  provider: ollama
  model: qwen3:4b-instruct
  base_url: http://localhost:11434
  num_ctx: 8192

schema:
  root_name: echo_report
  variables:
    - name: lvef
      type: float
      description: Left-ventricular ejection fraction, percent
      constraints: {ge: 0, le: 100}
    - name: mitral_regurgitation
      type: str
      options: ["none", "mild", "moderate", "severe"]
      required: false

extract:
  ladder: ["json_repair", "coerce"]
  run_tags: quickstart

grounding: {enabled: true}    # ask for verbatim quotes, verify them against the source
certainty: {enabled: true}    # per-field confidence from token logprobs

storage:
  backend: sqlite
  path: runs.sqlite
```

```python
import asyncio
from extrct import Extractor

async def main():
    async with Extractor.from_yaml("configs/echo-report.yaml") as ex:
        result = await ex.extract(
            "Echocardiography performed today. LVEF measured at 55 percent. "
            "Mild mitral regurgitation noted."
        )
        print(result.status)      # "ok"
        print(result.data)        # {"lvef": 55.0, "mitral_regurgitation": "mild", ...}
        print(result.certainty)   # per-field probabilities, enum posterior
        print(result.grounding)   # every quote located (or flagged) in the source

asyncio.run(main())
```

Many documents, bounded concurrency:

```python
results = await ex.extract_many(texts, input_ids=ids, concurrency=4)
```

No model at hand? `python examples/00_offline_demo.py` runs the entire pipeline —
including the run log and both XAI modules — against a mocked provider.

## The pieces (all importable on their own)

| Module | What it owns |
|---|---|
| `schema` | variable rows → JSON Schema (`strict_nullable` / `native_required` encodings, a declared experimental axis) → Pydantic source; evidence-mirror schemas for grounding |
| `providers` | the registry. `ollama` (native `/api/chat`, silent-failure gates) and `openrouter` (endpoint pinning, capability gating, egress guard, ZDR annotation) ship built in; `register_provider()` adds yours |
| `runner` | async transport: `call_once` (bounded retry, jittered backoff, retries recorded) and `call_many` (order-preserving, failure-isolated concurrency) |
| `repair` | the ladder: `json_repair → coerce → reprompt → llm_repair`, every rung logged, ranges never clamped |
| `pipeline` | wrap → request → grounding → certainty → merge, with request composition (logprob riders, evidence injection) and graceful degradation |
| `xai.certainty` | per-field statistics from logprobs (`mean`, `joint`, `min`, `first_token`, `margin`, enum posterior) with the character-alignment invariant and engine mask-state rules |
| `xai.grounding` | verbatim quote alignment (`ExtrCT-align/1.0`): exact match with successive-occurrence tiebreak, bounded fuzzy fallback, density guard |
| `wrapping` / `merging` | deterministic chunking with offset contracts; vote-based merging with conflicts surfaced, never silently resolved |
| `storage` | the run log behind a narrow `RunStore` protocol — Postgres (reference) and SQLite (zero-setup), same tables, same semantics |
| `telemetry` | built-in OpenTelemetry spans (GenAI semconv + `extrct.*` identities) — optional, no-op by default, content never rides a span; OTLP-ready for self-hosted Langfuse ≥ 3.22 |
| `batch` | the benchmark grid (inputs × configs × schemas): plan-then-run manifest, circuit breaker, hash-based resume that never re-bills completed cells |
| `job` | `job-def/1.0` — the YAML contract the quickstart uses |

## Identity model

Every artifact names itself by its content: `sha256(canonical_json(body))[:16]`.

| uid | names | so that |
|---|---|---|
| `schema_uid` | the JSON Schema actually sent | schema changes are visible as identity changes |
| `client_uid` | the client definition (machine-independent; deployment resolved after stamping) | the same YAML yields the same uid on any machine |
| `config_uid` | one sweep/config cell | grid cells are addressable |
| `run_uid` | one call = request × input | a re-run of 960 cells re-bills only the ones that never finished |

## Standards

1. **Content-addressed identity** — uids are hashes, never sequence numbers.
2. **Privacy by default** — input text is hashed (`input_sha256`), never stored, unless
   `store_input_text` is explicitly enabled. The OpenRouter egress guard refuses to
   send anything whose `data_classification` was not declared.
3. **Evidence over convenience** — every run row carries the redacted wire body, the
   verbatim provider response, and the per-attempt repair log.
4. **Loud failure** — truncation, downgrades, typos in configs, unknown providers:
   errors, not warnings. Absence is never evidence.
5. **Contracts at boundaries** — `job-def/1.0`, `client-def/1.0`, `wrap-def/1.0`,
   `merge-def/1.0`, `batch-def/1.0`: versioned, machine-checkable, swap-testable.

## Roadmap

Provider adapters planned next, in this order: **vLLM** (input/prompt logprobs,
post-mask semantics), **Cerebras**, **Fireworks** — the registry, the
`prompt_logprobs` capability flag, and the mask-state rules already anticipate them.
See [docs/providers.md](docs/providers.md) for the extension guide.

## Docs

- [docs/architecture.md](docs/architecture.md) — layers, data flow, design rules
- [docs/configuration.md](docs/configuration.md) — the full YAML reference
- [docs/providers.md](docs/providers.md) — built-ins in depth + adding a provider
- [docs/observability.md](docs/observability.md) — run-log schema, queries, resume
- [docs/xai.md](docs/xai.md) — certainty and grounding: what the numbers mean

## License

Apache-2.0. The grounding aligner reimplements the alignment approach proven in
Google's [langextract](https://github.com/google/langextract) (Apache-2.0), with
recorded deviations.
