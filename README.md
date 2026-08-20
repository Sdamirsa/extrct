# extrct

Schema-first structured extraction with LLMs, built for people who need to **trust and
replay** their extractions — plus the workbench growing around it. This monorepo holds:

```
packages/extrct/         THE library — standalone, pip-installable, Langflow-free
                         (providers: ollama, openrouter · XAI: certainty + grounding ·
                         run log: Postgres/SQLite · OpenTelemetry built in)
integrations/langflow/   use extrct from a Langflow canvas: component bundles, the
                         test harness, the frozen engine the canvas currently runs
deploy/                  the docker compose stack (Postgres, Langfuse, Label Studio,
                         Langflow behind a profile) — everything binds to 127.0.0.1
docs/                    design of record: what was measured, and why the code
                         defends against it
```

Planned, in order (see TODO.md): component migration onto the published library ·
`apps/` — def-editor, run-monitor, and a canvas that *renders* def documents ·
`products/` — the miners (PubliMiner, ThemaMiner). Products and apps consume the
library; the library imports nothing above it.

---

## Path 1 — the Python library (no Docker, no models needed to start)

Works on Windows, macOS, and Linux; pure Python ≥ 3.10, no compiled dependencies.

```bash
pip install "extrct @ git+https://github.com/sdamirsa/extrct#subdirectory=packages/extrct"
```

(After the first PyPI release: `pip install extrct`.)

See the whole pipeline run **with zero setup** — a mocked model, so no key, no
network, no GPU:

```bash
git clone https://github.com/sdamirsa/extrct && cd extrct/packages/extrct
pip install -e ".[dev]"
python examples/00_offline_demo.py
```

Then point the same YAML at a real model — [packages/extrct/README.md](packages/extrct/README.md)
is the full library guide (quickstart, YAML reference, providers, XAI, observability),
and `examples/01_quickstart_ollama.py` is the first live run:

```bash
ollama pull qwen3:4b-instruct
python examples/01_quickstart_ollama.py
```

## Path 2 — the Langflow canvas stack

Prereqs: Docker Desktop running, Compose v2.20+. From the repo root:

```powershell
cd deploy; powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1; docker compose up -d
```

That brings up the core (Postgres run log, Langfuse tracing, Label Studio). Add the
canvas:

```bash
docker compose --profile prototype up -d
```

| Service | URL |
|---|---|
| Langflow (canvas + extrct components) | http://localhost:7860 |
| Langfuse (traces) | http://localhost:3000 |
| Label Studio (review) | http://localhost:8080 |
| Postgres (run log) | localhost:5432 |

[deploy/README.md](deploy/README.md) has logins, tracing wiring, and the gotchas that
cost real hours (env is read at container BOOT — after editing `deploy/.env`, run
`up -d`, never `restart`; component edits need `--force-recreate langflow`).
[integrations/langflow/README.md](integrations/langflow/README.md) explains the
component bundles and why the canvas currently runs the frozen legacy engine while
components migrate to the published package.

## The standards (both paths, non-negotiable)

Content-addressed identity (every schema, client, config, and run names itself by its
hash — a re-run of finished work is free) · privacy by default (input text is hashed,
never stored, unless explicitly enabled; the OpenRouter egress guard refuses
undeclared text) · evidence over convenience (redacted wire bodies, verbatim provider
responses, per-attempt repair logs) · loud failure (truncation, silent downgrades, and
config typos are errors, not warnings) · versioned contracts at every boundary.

## Verify your checkout

```bash
cd packages/extrct && python -m pytest tests/        # 209 offline tests — no model, no key, no network
python examples/00_offline_demo.py                   # end-to-end incl. XAI + run log
docker compose --project-directory ../../deploy config --quiet   # stack manifest resolves
```

## License

The library (`packages/extrct/`) is Apache-2.0. Repo-wide licensing for docs and
deploy configs is tracked in TODO.md.
