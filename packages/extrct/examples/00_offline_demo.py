"""The whole pipeline with NO model, NO key, NO network.

A mocked provider answers every request, so you can watch the complete flow - schema
from YAML variables, evidence injection, logprob riders, repair ladder, grounding,
certainty, and the SQLite run log - before pointing the same config at a real model.

    python examples/00_offline_demo.py
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from extrct import Extractor

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "offline-demo.yaml"
INPUT_TEXT = (
    "Echocardiography performed today. LVEF measured at 55 percent. "
    "Mild mitral regurgitation noted. Plan: follow-up echo in 12 months."
)
# -----------------------------------------------------------------

# The mock model: answers with schema-shaped JSON, evidence quotes, and logprobs -
# the same shape a real Ollama response has.
ANSWER = json.dumps({
    "lvef": 55.0,
    "mitral_regurgitation": "mild",
    "followup_months": 12,
    "_evidence": {
        "lvef": "LVEF measured at 55 percent",
        "mitral_regurgitation": "Mild mitral regurgitation noted",
        "followup_months": "follow-up echo in 12 months",
    },
})


def mock_model(request: httpx.Request) -> httpx.Response:
    logprobs = [{"token": ANSWER[i:i + 4], "logprob": -0.05}
                for i in range(0, len(ANSWER), 4)]
    return httpx.Response(200, json={
        "model": "demo-model",
        "message": {"role": "assistant", "content": ANSWER},
        "done": True, "done_reason": "stop",
        "prompt_eval_count": 120, "eval_count": 60,
        "logprobs": logprobs,
    })


async def main() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(mock_model))
    async with http:
        ex = Extractor.from_yaml(str(CONFIG_FILE), http=http)
        result = await ex.extract(INPUT_TEXT, input_id="demo-note-1")

    print(f"status        : {result.status}")
    print(f"data          : {json.dumps(result.data, indent=2)}")
    print(f"run_uid       : {result.run_uids[0]}  (content hash: same input -> same uid)")
    print(f"job_uid       : {result.job_uid}")

    print("\n--- grounding (every quote located in the source, offsets included) ---")
    for field, r in result.grounding["fields"].items():
        print(f"  {field:24s} {str(r['status']):12s} span={r['start']}-{r['end']}  score={r['score']}")
    print(f"  clean: {result.grounding['summary']['grounding_clean']}")

    print("\n--- certainty (per-field, from token logprobs) ---")
    for field, st in result.certainty["fields"].items():
        if not field.startswith("_evidence"):
            print(f"  {field:24s} mean={st.get('mean')}  min={st.get('min')}  k={st['k']}")
    print(f"  mask_state: {result.certainty['mask_state']} (never pool across states)")

    print("\n--- the run log (SQLite; same tables as the Postgres backend) ---")
    db = ex.job["storage"]["path"]
    with sqlite3.connect(db) as conn:
        for uid, status, tags in conn.execute(
                "SELECT run_uid, final_status, tags FROM v_run_report"):
            print(f"  {uid}  {status}  tags={tags}")
    print(f"  ({db} - input text is NOT in it; only its sha256)")


if __name__ == "__main__":
    asyncio.run(main())
