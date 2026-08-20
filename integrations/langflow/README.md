# extrct × Langflow — the canvas integration

Use extrct from a Langflow canvas. This folder holds the component bundles the canvas
loads, the test harness, and the **frozen legacy engine** the components import.

```
components/      Langflow component bundles (extrct_main, extrct_xai, extrct_tools,
                 extrct_flow, extrct_batch; parked nodes in deactivated/)
legacy_extrct/   the frozen engine the components were built against, bind-mounted
                 into the container at /extrct
scripts/         run-tests.ps1 - triggers component test flows over the Langflow run
                 API and harvests the results from extrct-postgres
```

Flow exports are deliberately absent: a flow is never an artifact of record here. Flow
JSON is database-bound, mixes UI coordinates with logic, and does not export
deterministically — so when a flow proves an idea, the idea gets reimplemented in the
library, which is what the run log then cites.

## How it runs

The `prototype` compose profile (see [deploy/](../../deploy)) mounts `components/` at
`/components` and `legacy_extrct/` at `/extrct` inside the Langflow container. Nothing
is installed; both are read-only bind mounts, so component edits need
`docker compose --profile prototype up -d --force-recreate langflow` (Langflow scans
components at startup, and reads env at boot — never `restart` after editing `.env`).

## The two engines, honestly

- **`packages/extrct`** (repo root) is the canonical, published library — standalone,
  Langflow-free by design, with a reorganised API: a provider registry, `xai.*`, and
  class-based storage.
- **`legacy_extrct/`** here is the engine the components were built and
  container-verified against (module-level `ollama`/`openrouter`/`storage`,
  `flow_model`, `call_model`). It is FROZEN: bug fixes only, no new features.

Migrating the components onto the published package is the next milestone for this
folder. Until that lands and is re-verified in-container, the canvas stays pinned to
the legacy copy — so nothing untested is claimed to work.

## Verifying components

Always inside the container, never by reading docs:

```bash
docker compose --project-directory deploy --profile prototype exec -T langflow python -c "import extrct; print(extrct.__version__)"
```

and the flow harness: `integrations/langflow/scripts/run-tests.ps1` (needs
`LANGFLOW_API_KEY` in `deploy/.env`, and flows you have built yourself — the exports
are not shipped, see above).

Red line: **no confidential text in Langflow, ever** — synthetic and de-identified
only. Langflow is a code-execution surface with a live CVE record; the compose file
documents the specific mitigations.
