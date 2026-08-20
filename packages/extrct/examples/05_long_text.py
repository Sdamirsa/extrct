"""Long-document extraction: wrap -> extract per chunk (parallel) -> merge.

    python examples/05_long_text.py

Chunking is deterministic (same text + same params = same wrap_uid) and every chunk is
an exact (start, end) span of the original, so downstream offsets always map back to
the document. The merge NEVER silently resolves a disagreement: conflicting values are
surfaced with candidates, provenance and severity - `needs_manual` is your review
queue.
"""

import asyncio
import json
from pathlib import Path

from extrct import Extractor

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "long-text.yaml"
INPUT_FILE = None   # set a path to use your own document; None uses the demo text
# -----------------------------------------------------------------

DEMO = "\n\n".join([
    "ADMISSION. Patient admitted with dyspnea. Echo on admission: LVEF 40 percent. "
    "Started on furosemide 40mg daily and metoprolol 25mg twice daily.",
    "HOSPITAL COURSE. " + "Progress documented. " * 120,
    "DISCHARGE. Repeat echo: LVEF improved to 45 percent. Furosemide reduced to 20mg. "
    "Follow-up in 3 months.",
])


async def main() -> None:
    text = Path(INPUT_FILE).read_text(encoding="utf-8") if INPUT_FILE else DEMO
    async with Extractor.from_yaml(str(CONFIG_FILE)) as ex:
        result = await ex.extract(text, input_id="long-doc-1")

    print(f"status: {result.status}   chunks: {len(result.chunks)}")
    print("merged:", json.dumps(result.data, indent=2))

    if result.merge:
        stats = result.merge["stats"]
        print(f"\nmerge: {stats['conflicts']} conflict(s), {stats['major_conflicts']} major")
        for c in result.merge["conflicts"]:
            cands = ", ".join(f"{g['value']!r} (chunks {g['chunks']})" for g in c["candidates"])
            print(f"  {c['severity'].upper():5s} {c['path']}: {cands} -> resolved {c['resolved']!r}")
        if stats["major_conflicts"]:
            from extrct.merging import adjudication_task

            print("\nadjudication prompt (run it through another extraction if you want "
                  "an LLM tiebreak - merging itself never calls a model):")
            print(adjudication_task(result.merge))


if __name__ == "__main__":
    asyncio.run(main())
