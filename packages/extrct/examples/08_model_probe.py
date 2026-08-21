"""Probe a (provider, model) pair against the extrct feature battery and print a
paste-ready model-registry entry (see `extrct.models`, contract model-registry/1.0).

The test suite stays offline; THIS script is the deliberately-live lane:

    # an Ollama host (local, LAN, or tailnet)
    python examples/08_model_probe.py --provider ollama \
        --model gemma4:31b-it-q4_K_M --base-url http://<gpu-host>:11434

    # OpenRouter (reads OPENROUTER_API_KEY from the environment at send time)
    python examples/08_model_probe.py --provider openrouter \
        --model google/gemma-4-31b-it

    # add the long-text lane (a second, chunked extraction)
    python examples/08_model_probe.py ... --long

Every probe input is synthetic text authored in this file - nothing confidential
ever enters a probe. The battery measures, per feature:

    structured_output   hierarchical schema (object + children) comes back valid
    certainty           per-field logprob statistics are available
    enum_posterior      top-k alternatives yield a posterior over an options field
    grounding           quoted evidence aligns against the source text
    long_text           wrap -> N chunks -> merge completes with a merged object

Statuses follow extrct.models: pass / partial / fail are measurements (the printed
entry carries date + engine); anything not probed stays "untested".
"""

import argparse
import asyncio
import datetime as _dt
import json
import sys

import httpx

from extrct import Extractor
from extrct.models import FEATURES

# ------------------------------------------------------------------ synthetic input
TEXT = (
    "Echocardiography performed today. LVEF measured at 55 percent. "
    "The mitral valve shows mild regurgitation; no prolapse seen. "
    "Diagnosis: normal systolic function, mild mitral regurgitation. "
    "Plan: follow-up echo in 12 months."
)

LONG_TEXT = ("\n\n".join(
    [
        "ADMISSION NOTE. Patient admitted for elective evaluation. "
        "Initial echocardiogram: LVEF 55 percent. Started on aspirin 81 mg daily.",
        "HOSPITAL COURSE, DAY 2. Telemetry unremarkable. Metoprolol 25 mg twice "
        "daily added for rate control. LVEF re-read from the same study: 55 percent.",
        "HOSPITAL COURSE, DAY 3. Mild mitral regurgitation noted on review; no "
        "intervention. Atorvastatin 40 mg nightly started.",
        "DISCHARGE SUMMARY. Discharged in stable condition on aspirin 81 mg daily, "
        "metoprolol 25 mg twice daily, atorvastatin 40 mg nightly. "
        "Follow-up echocardiogram in 12 months.",
    ]
))

SCHEMA = {
    "root_name": "echo_probe",
    "variables": [
        {"name": "lvef", "type": "float",
         "description": "Left-ventricular ejection fraction, percent",
         "constraints": {"ge": 0, "le": 100}},
        {"name": "mitral_valve", "type": "object",
         "description": "Findings for the mitral valve"},
        {"name": "regurgitation", "parent": "mitral_valve", "type": "str",
         "options": ["none", "mild", "moderate", "severe"]},
        {"name": "prolapse", "parent": "mitral_valve", "type": "bool",
         "required": False},
        {"name": "diagnoses", "type": "str", "is_list": True,
         "description": "Every diagnosis mentioned, verbatim"},
    ],
}

LONG_SCHEMA = {
    "root_name": "discharge_probe",
    "variables": [
        {"name": "lvef", "type": "float", "required": False},
        {"name": "medications", "type": "str", "is_list": True,
         "description": "Every medication mentioned, with dose when given"},
        {"name": "followup_months", "type": "int", "required": False},
    ],
}


def job_doc(args, *, schema, long_text=False):
    client = {"provider": args.provider, "model": args.model}
    if args.provider == "ollama":
        client.update(base_url=args.base_url, num_ctx=args.num_ctx,
                      num_predict=1024, temperature=0.0, seed=42)
    doc = {
        "provider": client,
        "schema": schema,
        "extract": {"ladder": ["json_repair", "coerce"], "run_tags": "model-probe"},
        "grounding": {"enabled": True},
        "certainty": {"enabled": True},
        "storage": {"backend": "none"},   # a probe measures; it does not log
    }
    if long_text:
        doc["wrapper"] = {"enabled": True, "logic": "paragraph_pack",
                          "max_chars": 400, "overlap_chars": 80, "parallel_calls": 2}
        doc["merger"] = {"scalar_strategy": "majority",
                        "conflict_policy": "label_and_resolve"}
    return doc


async def engine_version(args) -> str:
    if args.provider == "ollama":
        try:
            async with httpx.AsyncClient() as http:
                r = await http.get(f"{args.base_url}/api/version", timeout=10)
                return f"ollama/{r.json().get('version', '?')}"
        except Exception:
            return "ollama/?"
    return args.provider


def row(status, note=None, *, date, engine):
    r = {"status": status}
    if status != "untested":
        r["date"] = date
        r["engine"] = engine
    if note:
        r["note"] = note
    return r


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", required=True, choices=["ollama", "openrouter"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="http://localhost:11434",
                    help="ollama only; openrouter resolves its endpoint itself")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--family", default=None,
                    help="provider-neutral family name for the printed entry")
    ap.add_argument("--long", action="store_true", help="also probe the long-text lane")
    args = ap.parse_args()

    today = _dt.date.today().isoformat()
    engine = await engine_version(args)
    feats = {f: {"status": "untested"} for f in FEATURES}
    mk = lambda status, note=None: row(status, note, date=today, engine=engine)

    print(f"probing {args.provider}/{args.model} (engine {engine}) ...", flush=True)
    async with Extractor(job_doc(args, schema=SCHEMA)) as ex:
        result = await ex.extract(TEXT, input_id="probe-single")

    # structured_output - the hierarchical schema came back as a valid object
    if result.ok and isinstance((result.data or {}).get("mitral_valve"), dict) \
            and "lvef" in (result.data or {}):
        note = "repaired" if result.status == "repaired" else None
        feats["structured_output"] = mk("pass", note)
    elif result.ok:
        feats["structured_output"] = mk("partial", f"valid but keys missing: {result.data}")
    else:
        feats["structured_output"] = mk("fail", f"status={result.status} {result.error or ''}".strip())

    # certainty - per-field logprob statistics
    c = result.certainty or {}
    if c.get("ok") and any("mean" in st for st in c.get("fields", {}).values()):
        feats["certainty"] = mk("pass", f"mask_state={c.get('mask_state')}")
    else:
        feats["certainty"] = mk("fail", str(c.get("reason") or c.get("mask_state") or "no statistics"))

    # enum_posterior - a posterior over the options field
    post = None
    for fname, st in c.get("fields", {}).items():
        # skip the _evidence.* mirror rows - quote strings, not enum values
        if not fname.startswith("_evidence") and "regurgitation" in fname:
            post = st.get("enum_posterior")
    if post and post.get("available"):
        feats["enum_posterior"] = mk("pass")
    else:
        feats["enum_posterior"] = mk("fail", str((post or {}).get("reason", "no posterior")))

    # grounding - quoted evidence aligns against the source
    g = result.grounding or {}
    s = g.get("summary") or {}
    if s and not s.get("unlocated"):
        feats["grounding"] = mk("pass", f"{s.get('exact', 0)} exact, {s.get('fuzzy', 0)} fuzzy")
    elif s:
        feats["grounding"] = mk("partial", f"{s.get('unlocated')} unlocated of {sum(v for k, v in s.items() if isinstance(v, int))}")
    else:
        feats["grounding"] = mk("fail", "no grounding result")

    # long_text - wrap -> chunks -> merge (opt-in: a second live run)
    if args.long:
        async with Extractor(job_doc(args, schema=LONG_SCHEMA, long_text=True)) as ex:
            lr = await ex.extract(LONG_TEXT, input_id="probe-long")
        if lr.ok and len(lr.chunks) > 1 and lr.data:
            feats["long_text"] = mk("pass", f"{len(lr.chunks)} chunks merged")
        elif lr.ok:
            feats["long_text"] = mk("partial", f"ok but {len(lr.chunks)} chunk(s) - text too short to wrap?")
        else:
            feats["long_text"] = mk("fail", f"status={lr.status} {lr.error or ''}".strip())

    entry = {
        "provider": args.provider,
        "model": args.model,
        "family": args.family or args.model,
        "features": feats,
        "notes": "measured by examples/08_model_probe.py",
    }
    print("\n--- paste-ready model-registry entry " + "-" * 30)
    print(json.dumps(entry, indent=2))
    bad = [f for f, r in feats.items() if r["status"] == "fail"]
    print(f"\nsummary: {sum(1 for r in feats.values() if r['status'] == 'pass')} pass, "
          f"{sum(1 for r in feats.values() if r['status'] == 'partial')} partial, "
          f"{len(bad)} fail, "
          f"{sum(1 for r in feats.values() if r['status'] == 'untested')} untested"
          + (f"   FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
