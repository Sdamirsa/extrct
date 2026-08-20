"""Quickstart against a local Ollama.

    ollama pull qwen3:4b-instruct        # once
    python examples/01_quickstart_ollama.py

Change the model or any knob in the YAML, not here - the config file IS the
experiment; this script only points at it.
"""

import asyncio
import json
from pathlib import Path

from extrct import Extractor

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "ollama-local.yaml"
INPUT_TEXT = (
    "Echocardiography performed today. LVEF measured at 55 percent. "
    "Mild mitral regurgitation noted. Diagnosis: normal systolic function, "
    "mild MR. Plan: follow-up echo in 12 months."
)
# -----------------------------------------------------------------


async def main() -> None:
    async with Extractor.from_yaml(str(CONFIG_FILE)) as ex:
        result = await ex.extract(INPUT_TEXT, input_id="quickstart-1")

    print(f"status: {result.status}   (ok=first parse valid; repaired=a ladder rung fixed it)")
    print(json.dumps(result.data, indent=2))
    if result.grounding:
        s = result.grounding["summary"]
        print(f"grounding: {s['exact']} exact, {s['fuzzy']} fuzzy, {s['unlocated']} unlocated")
    if result.certainty and result.certainty.get("ok"):
        worst = min((st.get("mean", 1.0), f) for f, st in result.certainty["fields"].items()
                    if not f.startswith("_evidence"))
        print(f"least certain field: {worst[1]} (mean p={worst[0]})")
    print(f"run log: runs.sqlite  (query v_run_report; re-running this input is free - same run_uid)")


if __name__ == "__main__":
    asyncio.run(main())
