"""OpenRouter: discover a pinnable endpoint, then extract through it.

    export OPENROUTER_API_KEY=sk-...     # or put it in .env
    python examples/02_openrouter.py

Step 1 (discovery, no key needed) lists every endpoint serving MODEL_ID with what it
can actually do - structured outputs, logprobs, seed, ZDR. Paste the tag you want
into configs/openrouter.yaml. Step 2 runs the extraction through the pin.
"""

import asyncio
import json
from pathlib import Path

import httpx

from extrct import Extractor
from extrct.providers.openrouter import list_endpoints_annotated

# ----------------------------------------------------------------- config
CONFIG_FILE = Path(__file__).parent / "configs" / "openrouter.yaml"
MODEL_ID = "google/gemma-3-27b-it"   # fetch endpoints for the EXACT id you will send
INPUT_TEXT = (
    "Echocardiography performed today. LVEF measured at 55 percent. "
    "Mild mitral regurgitation noted."
)
# -----------------------------------------------------------------


async def discover() -> None:
    async with httpx.AsyncClient() as http:
        endpoints = await list_endpoints_annotated(MODEL_ID, http=http)
    print(f"endpoints for {MODEL_ID}:")
    for e in endpoints:
        marks = "".join((
            "S" if e["structured_outputs"] else "-",
            "L" if "logprobs" in e["supported_parameters"] else "-",
            "s" if e["seed"] else "-",
            "Z" if e["zdr"] else ("?" if e["zdr"] is None else "-"),
        ))
        print(f"  [{marks}] {e['tag']:45s} ctx={e['context_length']}  quant={e['quantization']}")
    print("  S=structured_outputs L=logprobs s=seed Z=zdr - pin a tag with S (and L for certainty)")


async def extract() -> None:
    async with Extractor.from_yaml(str(CONFIG_FILE)) as ex:
        result = await ex.extract(INPUT_TEXT, input_id="or-example-1")
    print(f"status: {result.status}   cost: ${result.cost_usd}   tokens: {result.total_tokens}")
    print(json.dumps(result.data, indent=2))


async def main() -> None:
    await discover()
    import yaml

    pinned = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))["provider"].get("endpoint_tag")
    if not pinned:
        print("\nconfigs/openrouter.yaml has no endpoint_tag yet - paste one from the list "
              "above, then re-run. (Unpinned requests are refused by design: default "
              "routing re-ranks every ~5 minutes.)")
        return
    await extract()


if __name__ == "__main__":
    asyncio.run(main())
