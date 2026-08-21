# extrct — working context

Schema-first structured extraction library. Reused across projects by a small team.
Optimise for **re-derivable, auditable behaviour**, not for shipping speed.

## The standards — five must-haves, each with an enforcement

| # | Must-have | Enforcement |
|---|---|---|
| S1 | Every identity is a content hash: `sha256(canonical_json(body))[:16]`. Identical inputs yield identical uids on any machine | uid-stability tests; deployment (gateways, DSNs, keys) resolves from env AFTER stamping and never enters a hash |
| S2 | Privacy by default: input text is hashed, never stored, unless explicitly enabled | `input_text` nullable, `store_input_text`/`store_text` default false; the OpenRouter egress guard raises without a declared `data_classification` |
| S3 | Evidence over convenience: what was actually sent and received is recorded (redacted wire body, verbatim provider response, per-attempt repair log) | run rows are producer-written, COALESCE-upserted, never edited (only `annotate_run` for derived columns); no delete API |
| S4 | Loud failure: silent downgrades become errors; absence is not evidence | truncation gates before the ladder; `require_parameters`; unknown keys/vocab raise at authoring; skipped steps carry reasons |
| S5 | Contracts at boundaries, swap-testable | versioned `*-def` documents; `RunStore` protocol (postgres⇄sqlite); provider registry (a new backend touches zero core modules — `tests/test_registry.py` is the proof) |

## Red lines — do not cross without an explicit decision

- **Credentials never enter documents, configs, or hashes.** `api_key` / `capability`
  raise on sight everywhere they could be authored. Keys resolve from the environment
  at send time only.
- **Never clamp ranges in repair.** Coercing `lvef: 250` into `100` launders a wrong
  answer. Range violations stay `invalid`.
- **Repair stays an ablation axis**: every rung declared and logged; `final_status`
  keeps `ok` ≠ `repaired`.
- **The run log is append-only evidence.** No update/delete API for `extraction_run`;
  cleaning test runs takes raw SQL on purpose.
- **Certainty numbers never pool across engines/mask states or request modes** — the
  interpretation rules in docs/xai.md are load-bearing, not advice.
- **A provider is never added as an if/elif.** New backends go through
  `providers/base.py` + `register_provider`, with their own spec and their own
  measured `read_response`.

## Where things live

| What | Where |
|---|---|
| Library code | `src/extrct/` (kernel: hashing, runner · engine: schema, repair, pipeline, wrapping, merging, batch, client_model, config, job · adapters: providers/, storage/, xai/ · facade: extractor) |
| Tests (offline, no model/key/network) | `tests/` — behaviour-freezing; mock transports + synthetic payloads shaped like measured responses |
| Examples + YAML configs | `examples/`, `examples/configs/` — config variables at the top of each script, knobs in the YAML |
| Docs | `docs/` — architecture, configuration, providers, observability, xai |
| Work log, append-only | the REPO's `docs/system-arch/log/` (two levels up) — this package has no separate log |

## Conventions

- **Docstrings carry the measured lessons.** Module docstrings state the failure modes
  the code defends against and how they were observed. When you change behaviour,
  update the lesson — a stale lesson is worse than none.
- **Contracts are versioned** (`client-def/1.0`, `job-def/1.0`, `wrap-def/1.0`,
  `merge-def/1.0`, `batch-def/1.0`, `pipeline-run/1.0`). Additive = same version;
  meaning/hash changes = bump, and readers keep accepting the old one.
- **Every new behaviour lands with a test in the same change.** The suite must stay
  runnable offline (`uv run pytest` / `.venv/Scripts/python -m pytest tests/`).
- Line length ≈ 100; comment density and idiom as in the existing modules.

## Verification habits that have already caught real bugs

- **Measure the provider, don't trust the docs.** Doc summaries have been wrong;
  running code has not. New provider behaviours get verified against a live endpoint
  (or the vendor's source) before they are encoded, and the docstring names the
  measurement.
- **Run the offline demo after pipeline changes**:
  `python examples/00_offline_demo.py` exercises schema → riders → evidence injection
  → ladder → grounding → certainty → SQLite log in one shot.
- **Check silent-failure flags in tests, not just statuses** — a change that loses a
  flag loses an audit trail.
- **When a bug is found on real data, reproduce it synthetically** in the test suite;
  never paste private text into tests or fixtures.

## Current state

v0.1.0. Providers: ollama, openrouter. Stores: postgres, sqlite, null. Roadmap
providers (vLLM → Cerebras → Fireworks) documented in docs/providers.md; the
`prompt_logprobs` capability flag and vLLM sentinel/mask-state handling already exist.
Model registry (`extrct.models`, `model-registry/1.0`): gemma-4-31b-it baseline —
ollama/gemma4:31b-it-q4_K_M measured 5/5 by `examples/08_model_probe.py --long`
(2026-08-21); the openrouter row stays `untested` pending OPENROUTER_API_KEY.
