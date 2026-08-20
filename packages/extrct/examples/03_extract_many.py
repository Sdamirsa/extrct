"""Many documents, async, bounded concurrency, total failure isolation.

    python examples/03_extract_many.py

results[i] always corresponds to texts[i]; one document's failure becomes a
status='error' result, never a dead batch. Every run lands in the same log, and
re-running the script re-bills nothing that already succeeded at the provider level
(same input + same config = same run_uid = idempotent upsert).
"""

import asyncio
from pathlib import Path

from extrct import Extractor

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "ollama-local.yaml"
CONCURRENCY = 3   # OUTER bound (documents); chunk-level parallelism nests inside
DOCUMENTS = {
    "note-001": "LVEF measured at 55 percent. Mild mitral regurgitation. On aspirin 100mg.",
    "note-002": "Severely reduced LVEF of 25 percent. Furosemide 40mg started.",
    "note-003": "Normal study. LVEF 62 percent. No valvular disease. Metoprolol continued.",
}
# -----------------------------------------------------------------


async def main() -> None:
    ids, texts = list(DOCUMENTS), list(DOCUMENTS.values())
    async with Extractor.from_yaml(str(CONFIG_FILE)) as ex:
        results = await ex.extract_many(
            texts, input_ids=ids, concurrency=CONCURRENCY,
            on_progress=lambda done, total: print(f"  {done}/{total}"))

    for input_id, r in zip(ids, results):
        line = r.data if r.ok else r.error
        print(f"{input_id}: {r.status:8s} {line}")
    total_cost = sum(r.cost_usd or 0 for r in results)
    print(f"\n{sum(r.ok for r in results)}/{len(results)} ok, total cost ${total_cost}")


if __name__ == "__main__":
    asyncio.run(main())
