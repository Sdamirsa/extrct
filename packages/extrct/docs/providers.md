# Providers

## The contract

A provider is a stateless adapter between the pipeline and one backend
(`providers/base.py`). It owns a frozen spec dataclass (every knob a field), four pure
functions (`build_request`, `request_record`, `read_response`, `target_url`), and the
send-time-only pieces (`headers` resolves credentials, `preflight` runs guards before
any I/O). Everything downstream resolves providers through the registry:

```python
from extrct.providers import register_provider, get_provider, provider_names
```

Registering a provider makes it immediately usable in client definitions, YAML jobs,
config overrides, sweeps, and the pipeline — no core module changes
(`tests/test_registry.py` is the executable proof).

## Built-in: ollama

Native `POST /api/chat` with `format` carrying the full JSON Schema — deliberately NOT
the `/v1` OpenAI layer, which has measured shapes that return 200 with unconstrained
prose and cannot reach `options`.

What the adapter guards (all measured live, encoded in `read_response` and the spec):

- **Truncation** (`done_reason: "length"`) and **context pressure**
  (`prompt_eval_count` ≥ 95% of loaded context) both come back as HTTP 200 —
  flagged, terminal by default.
- **Sampling always emitted**: Ollama ignores unknown option keys and inherits baked
  Modelfile values otherwise; two models "at default" are incomparable without this.
- **`stop` never set**: a stop-truncation reports `done_reason: "stop"`,
  indistinguishable from success.
- **Logprobs**: top-level `logprobs: true` + `top_logprobs: N` (an int `logprobs: 5`
  is a 400; inside `options` both are silently ignored — Ollama 0.30.0). MTP models
  emit first-token-only logprobs; the certainty module names this and refuses.
- **`think` off by default**: it changes the answer and burns `num_predict` before the
  grammar engages; enabling it flags every run (`thinking_enabled`).
- Enforcement honesty: Ollama's grammar enforces enum/required but NOT numeric ranges
  (`enforcement_verified` on every response record) — the local validator is the only
  thing catching a range violation.

## Built-in: openrouter

Chat completions with capability-gated routing. The load-bearing measured facts:

- **`require_parameters: true` genuinely gates**: a `json_schema` request against an
  endpoint without `structured_outputs` 404s loudly instead of silently downgrading.
  Combined with an **endpoint pin** (`provider.only = [tag]`, `allow_fallbacks:
  false`) requests are reproducibly routed; default routing re-ranks every ~5 minutes.
- **`:free` variants have their own endpoint lists** — capability must be fetched for
  the exact id being sent (`fetch_endpoints`, `resolve_capability`,
  `list_endpoints_annotated`).
- **ZDR membership** comes only from `/endpoints/zdr` (`?zdr=true` on the endpoints
  route is silently ignored); a failed fetch reports *unknown*, never "no ZDR".
- **Cost accounting** must be requested (`usage: {include: true}`, on by default) or
  `cost_usd` logs as NULL.
- **Reasoning explicitly off** by default: not sending the field is not a
  no-reasoning baseline, and `reasoning_forced_on` is flagged when reasoning tokens
  appear anyway.
- **The egress guard** (`preflight`): `data_classification` must be declared, and the
  vocabulary has no member that permits confidential text. Gateway routing preferred;
  direct egress requires explicit consent.
- **Credentials resolve at send time** from `OPENROUTER_API_KEY` (or
  `api_key_env_var`); a literal key on the spec must look like one (`sk-…`) — a
  variable NAME never reaches the wire (the measured 401 trap).
- Batch policy (the grid lane): retry {429, 500, 502, 503, 524, 529}, jittered backoff
  capped 60 s, Retry-After honoured when present and never depended on.

## Adding a provider

```python
from dataclasses import dataclass, field
from extrct.providers import register_provider
from extrct.providers.base import Provider

@dataclass(frozen=True)
class VllmSpec:
    base_url: str = "http://localhost:8000"
    model: str = ""
    mode: str = "json_schema"
    seed: int = 42
    temperature: float = 0.0
    max_output_tokens: int = 2048
    logprobs: bool = False
    top_logprobs: int = 0
    prompt_logprobs: int = 0          # the input-logprobs knob, first-class
    timeout_s: int = 600
    api_key: str = ""                 # forbidden in definitions; resolved at send time
    capability: dict = field(default_factory=dict)

class VllmProvider(Provider):
    name = "vllm"
    engine = "vllm"                   # post_mask logprob semantics (see docs/xai.md)
    spec_cls = VllmSpec
    allowed_values = {"mode": ("json_schema", "json_object", "prompted")}
    default_concurrency = 16
    capabilities = frozenset({"structured_outputs", "logprobs", "top_logprobs",
                              "prompt_logprobs", "seed"})
    # implement build_request / request_record / read_response / target_url / headers

register_provider(VllmProvider())
```

The checklist that makes it a *good* provider (walk the built-ins for the pattern):

1. **Spec = every knob, frozen, defaults explicit.** If the backend inherits a value
   when a key is omitted, either always emit it or record the omission.
2. **`build_request` pure**, accepts a bare schema or a `schema_envelope` (unwrap via
   `extrct.schema.unwrap_schema` so the encoding axis rides along).
3. **`read_response` names the silent failures.** Read the backend's source or measure
   it; docs have been wrong before. Truncation MUST be terminal by default.
4. **`request_record` redacts**: message content → sha256 + length, schema → uid
   reference, key → fingerprint at most. `input_text` only behind an explicit flag.
5. **`engine`** set to the real serving engine so certainty's mask-state rules hold;
   a router serving many engines keeps its own name (unknown ≠ assumed).
6. **Vocabularies in `allowed_values`** so authoring mistakes die at definition time.
7. Register, then run your suite against `tests/test_registry.py`-style assertions —
   the registry test shows exactly what a provider must survive.

## Roadmap: vLLM, Cerebras, Fireworks — and input logprobs

The next providers are chosen for what they add measurably:

- **vLLM**: `prompt_logprobs` — per-token logprobs of the INPUT, the basis for
  input-perplexity features; post-mask output logprobs (declared: `engine = "vllm"`).
  The `prompt_logprobs` capability flag and the `-9999.0` sentinel handling in
  `xai.certainty` already anticipate it.
- **Cerebras / Fireworks**: OpenAI-compatible surfaces with their own logprob and
  sampling semantics — each gets its own spec (never a "generic OpenAI" spec with
  silently ignored knobs) and its own measured `read_response`.

Design intent for sampling-based token probabilities (multi-sample agreement as a
certainty signal): that is a CALLER pattern — N runs through the existing pipeline
with seeds varied per run, aggregated over the run log — not a provider feature, so it
needs no new surface. The pieces exist: seeded specs, `call_many`, and the merge
machinery's vote grouping.

Backward compatibility: adding a provider or a capability flag never changes existing
uids or record shapes; record shapes only grow (`record_version` bumps on meaning
changes).

## Which models, though?

Provider behaviour and model behaviour are different axes. This document records the
*provider* measurements; which **models** are measured to support which extrct
features lives in the model registry (`extrct.models`, contract `model-registry/1.0`).
`examples/08_model_probe.py` runs the feature battery against a live endpoint and
prints a paste-ready entry — same rule as here: a claim carries the date and engine
version that justify it.
