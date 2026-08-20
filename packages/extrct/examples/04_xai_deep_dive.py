"""The two XAI modules, close up - and why they never touch the extraction.

    python examples/04_xai_deep_dive.py            # part 1 runs offline
    # part 2 needs the ollama-local.yaml model available

Part 1 (offline): the grounding aligner on its own - exact matches, successive
occurrences, fuzzy fallback, and the gate: a quote the source cannot reconstruct is
hallucinated evidence.

Part 2 (live): a full run with certainty on; shows the per-field statistics, the enum
posterior, and re-derives the same numbers from the stored provider response - the
stored value is a deterministic function of stored evidence.
"""

import asyncio
import json
from pathlib import Path

from extrct import Extractor
from extrct.xai import certainty, grounding

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "ollama-local.yaml"
SOURCE = (
    "Echocardiography performed today. LVEF measured at 55 percent. "
    "Mild mitral regurgitation noted. LVEF stable at 55 percent compared to prior. "
    "CD96+ cells absent."
)
# -----------------------------------------------------------------


def part1_grounding_offline() -> None:
    print("=== grounding aligner (offline) ===")
    evidence = {
        "lvef_first": "55 percent",
        "lvef_second": "55 percent",                    # repeated quote -> next occurrence
        "mr": "mild MITRAL regurgitation",              # case differs; offsets stay original
        "marker": "CD96+ cells",                        # symbols survive tokenization
        "invented": "severe aortic stenosis",           # the gate: unlocatable = flagged
    }
    report = grounding.ground_fields(evidence, SOURCE)
    for field, r in report["fields"].items():
        where = f"span {r['start']}-{r['end']}" if r["status"] else r.get("reason", "")
        print(f"  {field:12s} {str(r['status']):12s} {where}")
    print(f"  summary: {report['summary']}")


async def part2_certainty_live() -> None:
    print("\n=== certainty (live model) ===")
    async with Extractor.from_yaml(str(CONFIG_FILE)) as ex:
        result = await ex.extract(SOURCE)
    if not (result.certainty and result.certainty.get("ok")):
        print(f"  (no certainty: {result.certainty and result.certainty.get('error')}; "
              "is the model in configs/ollama-local.yaml pulled?)")
        return
    for field, st in result.certainty["fields"].items():
        if field.startswith("_evidence"):
            continue
        post = st.get("enum_posterior", {})
        extra = f"  posterior={post.get('posterior')}" if post.get("available") else ""
        print(f"  {field:24s} mean={st.get('mean')} joint={st.get('joint')} "
              f"min={st.get('min')} first={st.get('first_token')}{extra}")
    print(f"  engine={result.certainty['engine']}  mask_state={result.certainty['mask_state']}")

    # Re-derivation: the same numbers from stored evidence alone. Anything - a
    # notebook, an eval process, an auditor - can recompute the stored annotation
    # from the run row, without the model and without this library's pipeline.
    import sqlite3

    with sqlite3.connect("runs.sqlite") as conn:
        stored = conn.execute(
            "SELECT provider_response, field_logprobs FROM extraction_run WHERE run_uid = ?",
            (result.run_uids[0],)).fetchone()
    payload, annotation = json.loads(stored[0]), json.loads(stored[1])
    rederived = certainty.field_confidence(payload, ex.job["schema"]["schema"])
    same = {f: st.get("mean") for f, st in rederived["fields"].items()} == \
           {f: st.get("mean") for f, st in annotation["fields"].items()}
    print(f"  re-derived from the stored provider_response alone: identical = {same}")


if __name__ == "__main__":
    part1_grounding_offline()
    asyncio.run(part2_certainty_live())
