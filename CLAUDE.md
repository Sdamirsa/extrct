# extrct monorepo — working context

Schema-first structured extraction: the `extrct` library plus the workbench around it
(a Langflow integration today; a def-editor, run-monitor, and def-rendering canvas
next). Optimise for **re-derivable, auditable artifacts**, not for shipping speed.

## Where things live

| What | Where | Authority for |
|---|---|---|
| The library (canonical, published) | [`packages/extrct/`](packages/extrct/) — its own [CLAUDE.md](packages/extrct/CLAUDE.md) carries the library standards (S1–S5) and red lines | engine behaviour |
| Langflow lane | [`integrations/langflow/`](integrations/langflow/) — components, the test harness, and the FROZEN legacy engine the canvas still runs | canvas behaviour |
| Deployment | [`deploy/`](deploy/) — compose components, one folder per service, composed with `include:`; optionality via `profiles:` | the running stack |
| Open work | [`TODO.md`](TODO.md) | what is open *now* |
| Work log, append-only | [`docs/log/`](docs/log/) | what happened *when* |
| Design of record | [`docs/extraction-stack/`](docs/extraction-stack/) | why things are the way they are, and what was measured to find out |

## Red lines — do not cross without an explicit decision

- **`packages/extrct` never imports Langflow/lfx.** The library is standalone by
  construction; `/verify` greps for it. Components are thin adapters — anything that
  is not UI-shaped gets pushed DOWN into the library.
- **No confidential text in Langflow, ever.** Synthetic and de-identified only.
  Langflow is a code-execution surface with a live CVE record; keeping sensitive data
  out of its blast radius costs nothing.
- **No in-process model execution anywhere.** All model compute lives behind HTTP
  (Ollama, vLLM, OpenRouter, …). That is what keeps the library CPU-only and portable
  across Windows/macOS/Linux and across hardware.
- **Flow JSON is never an artifact of record.** Flows prove ideas; the library and its
  `*-def` documents are the record. The planned canvas *renders* def documents rather
  than owning a graph.
- **The legacy engine (`integrations/langflow/legacy_extrct/`) is frozen** — bug fixes
  only, no new features, until the components migrate to the published package.
- **Credentials never enter documents, configs, or hashes.** Keys resolve from the
  environment at send time; `deploy/.env` is deny-listed from reads.
- **Permissive licenses only.** GPL/AGPL in-process is disqualifying; CI scans the
  dependency tree on every push.

## Conventions

- **UIDs are content hashes**: `sha256(canonical_json(body))[:16]`. Identical inputs
  yield identical uids on any machine; deployment values (gateway URLs, DSNs, keys)
  resolve from the environment AFTER stamping and never enter a hash.
- **Contracts are versioned** (`client-def/1.0`, `job-def/1.0`, `batch-def/1.0`, …):
  additive changes keep the version; anything that changes meaning or hashing bumps
  it, and readers keep accepting the old one.
- **Compose**: one component folder per service, composed with `include:` (not stacked
  `-f`, which resolves relative paths against the *first* file). **Every published
  port binds to `127.0.0.1`.**
- Every library change lands with its test; the suite stays offline-runnable.

## Verification habits that have already caught real bugs

- **Library**: run `/verify` — the offline suite, the offline demo, `uv build` wheel
  hygiene, compose config, and the no-lfx boundary grep.
- **Components**: test INSIDE the container, never by reading docs:
  `docker compose exec -T langflow python -c "..."` (register the module in
  `sys.modules` before instantiating, or `set_class_code` fails). Read the installed
  component source under `/app/.venv/lib/python3.*/site-packages/lfx/components/`
  before assuming behaviour.
- **Providers**: measure, don't trust docs — doc summaries have been wrong here;
  running code has not. Encode the measurement (version + date) in the module
  docstring next to the behaviour it justifies.
- **Langflow reads env at BOOT**: after editing `deploy/.env`, use
  `docker compose --profile prototype up -d`, never `restart`. Component edits need
  `--force-recreate langflow`.
- **Moving a bind-mount source is silent**: Docker resolves host paths at container
  creation, so a moved directory leaves a running container pointing at nothing, with
  no error anywhere. Any path change in `deploy/components/*/compose.yaml` must be
  followed by `--force-recreate` of that service and re-inspected with `docker inspect`.
- **Check telemetry at runtime**, not in YAML — infrastructure images ship their own
  phone-home defaults.
- **PowerShell 5.1**: `curl` aliases `Invoke-WebRequest` — use `curl.exe`; `.ps1`
  files ASCII-only; a generated `.env` must be UTF-8 **without** BOM.

## Current state

Library `packages/extrct` v0.1.0: providers ollama + openrouter (vLLM, Cerebras, and
Fireworks next — see its `docs/providers.md`), XAI (certainty + grounding),
Postgres/SQLite run log, OpenTelemetry built in, 209 offline tests, clean wheel and
sdist. Canvas: components verified in-container against the frozen legacy engine;
migrating them onto the published package is the next milestone.
