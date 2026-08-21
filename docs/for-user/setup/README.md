# Setup guides

Step-by-step paths into the repo, in reading order. Each guide is thin on purpose:
it sequences the steps and links to the document that owns the details, so there is
one authority per fact and nothing here can silently drift.

| Guide | Gets you | Needs |
|---|---|---|
| [01 — the library](01-library.md) | `extrct` installed, the offline demo running, the 209-test suite passing | Python ≥ 3.10, nothing else |
| [02 — the Langflow stack](02-langflow-stack.md) | the docker compose stack (Postgres, Langfuse, Label Studio, Langflow canvas) | Docker Desktop, Compose v2.20+ |
| [03 — add a GPU host](03-add-gpu-host.md) | a second machine's GPU serving Ollama to your laptop over a LAN cable or an overlay network | a GPU machine, one cable (or Tailscale) |

Quick health check of any checkout, at any time:

```bash
cd packages/extrct && python -m pytest tests/          # 209 offline tests
python examples/00_offline_demo.py                     # end-to-end, no model needed
docker compose --project-directory ../../deploy config --quiet
```
