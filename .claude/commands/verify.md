---
description: Verify the repo is healthy — library suite, offline demo, packaging, compose, contracts
---

Run the checks; do not infer from reading the code. Report a compact table.

**Library (`packages/extrct`)** — run from that directory:

1. **Suite** — `.venv/Scripts/python -m pytest tests/` (or `uv run pytest`). Everything
   must pass offline: no model, no key, no network. A test that needs any of those is a
   finding.
2. **Offline demo** — `python examples/00_offline_demo.py`. Must print status `ok`, a
   clean grounding table, certainty fields, and run-log rows.
3. **Examples compile** — `python -m py_compile examples/*.py`.
4. **Packaging** — `uv build` succeeds; the wheel's top level is `extrct/` +
   dist-info only, `py.typed` included.
5. **Privacy spot-check** — after the demo, confirm `extraction_run.input_text` is NULL
   in the SQLite file and no input-text string appears in `request_record`.
6. **Contract drift** — the `*-def/…` version constants unchanged unless the diff
   deliberately bumps them; `job.py`'s import-time drift guards pass (they run on
   import, so the suite covers them — say so explicitly).

**Repo:**

7. **Compose** — `docker compose --project-directory deploy config --quiet` from the
   repo root. Mount sources under `integrations/langflow/` must exist.
8. **Boundary** — `grep -rn "lfx\|langflow" packages/extrct/src/` returns nothing:
   the library is standalone by construction.

Report only. Do not fix anything without asking — a wrong "fix" to a healthy repo
costs more than the finding. If everything passes, say so in one line plus the table.
