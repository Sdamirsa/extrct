# Setup 01 — the library (`packages/extrct`)

Pure Python ≥ 3.10, no compiled dependencies, identical on Windows/macOS/Linux.
Models are reached over HTTP only, so the library itself is CPU-only everywhere —
no GPU, CUDA, or local model runtime is ever required to install it.

## Use it in a project

Until the first PyPI release (tracked in [TODO.md](../../../TODO.md)):

```bash
pip install "extrct @ git+https://github.com/sdamirsa/extrct#subdirectory=packages/extrct"
```

Extras, each optional:

| Extra | Adds | For |
|---|---|---|
| `extrct[repair]` | json-repair | the deterministic rung of the repair ladder |
| `extrct[postgres]` | psycopg | the Postgres run-log backend (SQLite needs nothing) |
| `extrct[otel]` | opentelemetry-api | OpenTelemetry spans ([docs/observability.md](../../../packages/extrct/docs/observability.md)) |

## See it run with zero setup

The offline demo runs the full pipeline — wrapping, extraction, grounding,
certainty, the SQLite run log — against a mocked model: no key, no network, no GPU.

```bash
git clone https://github.com/sdamirsa/extrct && cd extrct/packages/extrct
pip install -e ".[dev]"
python examples/00_offline_demo.py
```

## First live run

```bash
ollama pull qwen3:4b-instruct
python examples/01_quickstart_ollama.py
```

Then work through `examples/02` – `07` (OpenRouter, batch, XAI, long text, OTel).

## Developer setup (mirrors CI)

CI (`.github/workflows/ci.yml`) runs this exact shape on a 3-OS × {3.10, 3.13}
matrix — a project venv, never `uv pip install --system` (uv-managed interpreters
refuse it as externally managed):

```bash
cd packages/extrct
uv venv
uv pip install -e ".[dev]"
uv run --no-sync python -m pytest tests/     # 209 offline tests
```

Every library change lands with its test, and the suite stays offline-runnable —
that is what keeps the matrix honest.

## Credentials

Copy [.env.example](../../../packages/extrct/.env.example) to `.env` and fill in what
you use (`OPENROUTER_API_KEY`, optionally `EXTRCT_GATEWAY_URL`, `EXTRCT_PG_DSN`).
Keys resolve from the environment at send time only — they never enter YAML configs,
def documents, or hashes.

## Where the full docs are

| Topic | Document |
|---|---|
| Library guide, quickstart, YAML reference | [packages/extrct/README.md](../../../packages/extrct/README.md) |
| Architecture | [docs/architecture.md](../../../packages/extrct/docs/architecture.md) |
| Configuration & job-def contract | [docs/configuration.md](../../../packages/extrct/docs/configuration.md) |
| Providers (measured behaviour) | [docs/providers.md](../../../packages/extrct/docs/providers.md) |
| XAI: certainty + grounding | [docs/xai.md](../../../packages/extrct/docs/xai.md) |
| Observability / OTel | [docs/observability.md](../../../packages/extrct/docs/observability.md) |
| Contributing | [CONTRIBUTING.md](../../../packages/extrct/CONTRIBUTING.md) |
