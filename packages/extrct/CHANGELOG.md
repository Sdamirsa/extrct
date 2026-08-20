# Changelog

All notable changes to extrct. Format: [Keep a Changelog](https://keepachangelog.com);
versioning: [SemVer](https://semver.org). Contract documents (`*-def/X.Y`) version
independently of the package.

## [0.1.0] — 2026-08-20

First public cut. The engine originates from a private research platform where every
behaviour below was measured against live providers and frozen by tests; this package
is the standalone, provider-extensible port.

### Added

- **Schema from variables**: flat rows → JSON Schema (`strict_nullable` /
  `native_required` encodings as a declared experimental axis) → Pydantic source;
  evidence-mirror schemas for grounding.
- **Provider registry** with two measured built-ins: Ollama (native `/api/chat`,
  truncation/context-pressure gates, explicit sampling, top-level logprobs) and
  OpenRouter (endpoint pinning, capability gating via `require_parameters`, egress
  guard, ZDR annotation, cost accounting). `register_provider()` extends without core
  changes.
- **Async transport**: `call_once` (bounded jittered retry, retries recorded) and
  `call_many` (order-preserving, failure-isolated concurrency).
- **Repair ladder** (`json_repair → coerce → reprompt → llm_repair`): declared,
  per-attempt logged, never range-clamping; `ok` ≠ `repaired`.
- **Pipeline** (`pipeline-run/1.0`): wrap → request → grounding → certainty → merge,
  request composition (upgrade-only logprob riders, request-time evidence injection),
  graceful degradation with declared-vs-executed records.
- **XAI**: per-field certainty from logprobs (mean/joint/min/first_token/margin, enum
  posterior, character-alignment invariant, engine mask states, vLLM/Ollama
  sentinels) and evidence grounding (`ExtrCT-align/1.0` quote aligner).
- **Long-text lane**: deterministic wrapping (`wrap-def/1.0`) + conflict-surfacing
  merge (`merge-def/1.0`).
- **Run log** behind the `RunStore` protocol: Postgres (reference; + authored-config
  registries) and SQLite (zero-setup), COALESCE-upsert semantics, derived-annotation
  writer, hash-based resume.
- **Batch grid** (`batch-def/1.0`): inputs × configs × schemas, plan-then-run
  manifest, OpenRouter-measured retry policy, circuit breaker, resume that never
  re-bills.
- **YAML jobs** (`job-def/1.0`) + `Extractor` facade; examples incl. a fully offline
  demo; offline test suite (200+ tests).
- **Built-in OpenTelemetry** (`telemetry.py`): optional, no-op without the `otel`
  extra; GenAI-semconv `chat <model>` spans on the shared request path (all lanes
  traced identically), `extrct.extract` / `extrct.batch` roots, `extrct.run_uid` as
  the trace↔run-log join key; content never rides a span. OTLP-compatible with
  self-hosted Langfuse ≥ 3.22 (verified 2026-08-20).

### Changed from the private parent (recorded deviations)

- Same import name as the parent (`extrct` — this package is the upstream the parent
  will consume at a pin); providers moved under `extrct.providers.*`; XAI under
  `extrct.xai.*` (`confidence` → `certainty`).
- Grounding summary key `h2_clean` → `grounding_clean`.
- `DATA_CLASSES` extended with `public` and `approved_for_egress` (superset; the
  guard still has no member permitting confidential text).
- Storage is instance-based (`RunStore`) rather than module functions with DSN
  params; SQLite backend is new.
- **Fixed**: under `strict_nullable`, optional enum fields now include `null` in the
  enum — previously a strict validator/provider could force the model to invent an
  option when the source said nothing.
