"""The benchmark grid: inputs x configs x schemas, with resume and a circuit breaker.

    export OPENROUTER_API_KEY=sk-...
    python examples/06_batch_grid.py

Every cell becomes exactly one row - ok, failed, config_error, skipped_completed, or
aborted_circuit_breaker - and every run row lands in the log under the batch_uid.
Because run identity is a content hash, re-running this script re-bills ONLY cells
that never completed. Kill it mid-run and start again: that is the resume story.
"""

import asyncio
from pathlib import Path

import httpx
import yaml

from extrct.batch import normalize_configs, normalize_inputs, normalize_schemas, run_batch
from extrct.client_model import build_client_payload, client_definition
from extrct.config import expand_grid
from extrct.schema import schema_envelope
from extrct.storage import open_store

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "openrouter.yaml"   # base client + schema
SWEEP = {  # the grid axes: every combination becomes a config cell
    "openrouter.temperature": [0.0, 0.7],
    "openrouter.seed": [42, 1337],
}
INPUTS = [
    {"input_id": "n1", "text": "LVEF measured at 55 percent. Mild mitral regurgitation."},
    {"input_id": "n2", "text": "Severely reduced LVEF of 25 percent."},
]
SETTINGS = {"concurrency": 4, "run_tags": "grid-example", "store_input_text": False}
# -----------------------------------------------------------------


async def main() -> None:
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    if not raw["provider"].get("endpoint_tag"):
        print("configs/openrouter.yaml needs an endpoint_tag first - run "
              "examples/02_openrouter.py to discover one. (The batch lane refuses "
              "unpinned configs: pinning is also what disables sticky routing.)")
        return

    client = build_client_payload(client_definition(
        "openrouter", {k: v for k, v in raw["provider"].items() if k != "provider"}))
    schemas = normalize_schemas(schema_envelope(raw["schema"]["variables"],
                                                root_name=raw["schema"].get("root_name", "extract")))
    inputs = normalize_inputs(INPUTS)
    configs = normalize_configs(expand_grid(SWEEP))
    store = open_store({"backend": "sqlite", "path": "runs.sqlite"})

    print(f"{len(inputs)} inputs x {len(configs)} configs x {len(schemas)} schema = "
          f"{len(inputs) * len(configs) * len(schemas)} cells")

    async with httpx.AsyncClient() as http:
        out = await run_batch(client, inputs, configs, schemas, SETTINGS,
                              http=http, store=store, log=print,
                              on_progress=lambda d, t: print(f"  {d}/{t}"))

    report = out["report"]
    print(f"\nbatch {report['batch_uid']}: {report['by_status']}")
    print(f"cost ${report['total_cost_usd']}   retries {report['total_retries']}")
    print("query the grid:  SELECT run_metadata->>'config_uid', final_status, cost_usd "
          "FROM extraction_run WHERE run_metadata->>'batch_uid' = "
          f"'{report['batch_uid']}';  -- (Postgres syntax; SQLite: json_extract)")


if __name__ == "__main__":
    asyncio.run(main())
