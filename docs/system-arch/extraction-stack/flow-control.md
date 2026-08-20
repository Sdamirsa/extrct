# Flow control — flow-def/1.0, the Flow Controller, and the rules registry

One document configures a whole extraction flow. Package authority:
`extrct/flow_model.py`; canvas surface: **Flow - Controller** (`extrct_flow`).

## The document

```json
{"provider":  {"provider": "ollama", "model": "gemma4:31b-it-q8_0", "num_ctx": 16384},
 "extract":   {"run_tags": "pilot3"},
 "grounding": {"enabled": true, "mode": "inline"},
 "certainty": {"enabled": true, "enum_posterior": true},
 "wrapper":   {"enabled": true, "parallel_calls": 4}}
```

`"provider"` is the preferred spelling of the client section since 2026-08-14 (internally
it stays `client`, so stored documents and their flow_uids remain valid; authoring both
spellings at once raises). `schema.request_evidence` no longer exists — authoring it
raises a migration message (evidence injection moved into the run nodes; see below).

Any subset of sections. Each section is validated by its component's own data model:
`client` is client-def/1.0 (dataclass-backed, loud); `extract`/`schema` key sets are
**asserted at import time** against `config.EXTRACT_KEYS`/`SCHEMA_KEYS` (drift refuses
to import); `grounding`/`certainty` are defined in `flow_model` and consumed by the XAI
components. A bare client document or a Registry `load_client` row is accepted as
`{"client": ...}` — stored clients wire straight in.

## Threading

The controller emits one output per component, each a **sparse** namespaced override
payload feeding that component's existing Overrides input — only authored keys ride, so
canvas settings the document does not mention stay untouched:

```
[Flow - Controller] ─Provider Overrides─> Provider - Model Server        (ollama.* / openrouter.*)
                    ─Extract Config─────> Run - Structured Extract / Run - Long Text  (extract.*)
                    ─Schema Config──────> Prep - Schema Builder          (schema.*)
                    ─Grounding Config───> XAI - Evidence Grounding       (grounding.* — a CONFIG AUTHOR since 2026-08-14)
                    ─Certainty Config───> XAI - Certainty Score          (certainty.* — a CONFIG AUTHOR)
                    ─Wrapper Config─────> Adapter - Wrapper & Merger     (wrapper.*)
                    ─Merger Config──────> Adapter - Wrapper & Merger     (merger.* — same node, both threads)
                    ─Ready Provider─────> a run node's Provider input    (the READY payload)
                    ─Declared Plan──────> anywhere (audit)
                    ─Flow Doc───────────> anywhere (audit / storage)
```

**The pipeline (2026-08-14, the router wave):** the XAI nodes and the Adapter are
CONFIG AUTHORS — they execute nothing; each emits its validated section as a payload
into the run node's config inputs. **Run - Structured Extract** (single run, output
shapes INVARIANT — unwired configs = byte-identical pre-router behavior, proven)
composes the request from the configs (certainty ⇒ logprob riders, upgrade-only;
grounding inline/auto ⇒ evidence mirror injected into the CLEAN schema at request
time — which is why rules R1–R3 are retired as unrepresentable), executes, then runs
grounding and certainty as NON-BREAKING recorded steps. **Run - Long Text** consumes
the same configs plus the Adapter's and owns the batch lane: wrap first, one full
pipeline per chunk (bounded parallel, resume), merge last with labeled conflicts —
major ones counted as `needs_manual`, the queue for the manual-merge wave. Both nodes
emit a **Steps** record: declared plan next to executed steps (independent measurement inside one node).

**The absorbed gate (2026-08-11, retiring Pipeline Gate):** the **Ready Provider**
output (né "Client") materialises the ready provider payload from the document — one
wire into a run node and the document alone decides Ollama vs OpenRouter; no widget
needed (widget flows keep working via Provider Overrides). The **Declared Plan**
output carries the gate's commitments with a per-item `binding` flag; declared vs
executed stays comparable against the run log.

Every thread carries `config_uid = flow_uid` (content-addressed, replayability) — every run this
flow touches joins on one identity in `run_metadata`. The client thread namespaces
under its **provider**; a thread reaching the other provider's client applies nothing
(visible: that client's status shows no overrides).

**XAI on/off:** `grounding.enabled=false` / `certainty.enabled=false` short-circuit the
node — empty outputs, a DISABLED status, and **no error even on inputs the node would
otherwise refuse**. A switched-off analysis layer never blocks the extraction.

## The rules registry

`flow_model.RULES` — cross-component coherence checks no single node can see. A plain
list, concatenated over time; never delete a rule that caught a real failure. Violations
raise ON the controller, all at once, each with its rule id. Rules judge the DOCUMENT:
an unauthored section is out of jurisdiction.

| id | Status |
|---|---|
| R1-inline-grounding-needs-inline-evidence | RETIRED 2026-08-14 — unrepresentable: the run node injects evidence at request time whenever grounding is on |
| R2-enum-posterior-needs-top-logprobs | RETIRED 2026-08-14 — unrepresentable: `certainty.top_logprobs` rides as an upgrade-only request rider |
| R3-certainty-needs-logprobs | RETIRED 2026-08-14 — unrepresentable: certainty-enabled sets the logprobs rider |

All three founding rules were retired the day the router wave made their failure
states impossible to author — the strongest form a rule can reach. Their ids and
lessons stay as tombstones (never delete a rule that caught a real failure). Adding a
rule: one function `sections -> message | None`, one entry in `RULES`, one test that
it fires and one that it passes — and when a refactor makes it unrepresentable,
tombstone it with the date.

## The maximum tables (Flow - Config / Flow - Sweep, 2026-08-11)

Both tables ship pre-populated with `flow_model.variable_catalog()` — every
controllable variable from every data model (103 rows since the router wave), defaults
filled, plus a
**help** column carrying the closed vocabulary or the measured lesson per variable
(parsers ignore it). Semantics: a kept row is AUTHORITATIVE for its key at the
consuming node — **delete rows** to release keys back to widget settings. In the sweep
table every value is a one-candidate JSON array; extend any array to sweep it (single
candidates never multiply the grid). The catalog is generated from the models at
import time and drift-guarded; a dragged node's table refreshes on re-drag.

## Bundle layout (2026-08-11)

`extrct_main` (Schema Builder, Provider - Model Server, Run - Structured Extract,
Run - Long Text, Adapter - Wrapper & Merger, Sweep Extract) · `extrct_xai` (Certainty,
Grounding — config authors) · `extrct_tools` (Message, Query Bank, Mock Payload,
Schema Diff, Registry) · `extrct_flow` (Controller, Config, Sweep, Trigger, Call,
Response — see `flow-calls.md`). Parked in `deactivated/`: Text Wrapper, Extraction
Merger, Chunk Extract, Posthoc Templater (absorbed by the router wave 2026-08-14),
plus the earlier Switchboard and standalone clients. The old extrct bundle is
dissolved; Pipeline Gate is absorbed into the controller (above).

**Canvas migration recipe (pre-wave flows keep running on frozen code; to adopt):**
re-drag Run - Structured Extract; re-drag the XAI nodes and wire their config outputs
into its Grounding/Certainty Config inputs (delete the old post-extract XAI wiring);
re-drag Schema Builder (the Request Evidence toggle is gone — grounding-on injects);
for long documents replace Text Wrapper + Chunk Extract + Extraction Merger with
Adapter - Wrapper & Merger → Run - Long Text. Flow documents: rename any
`schema.request_evidence` authoring to a `grounding` section; prefer `"provider"` as
the client section spelling.

Verified in-container 2026-08-14 (the router wave): 10/10 migration matrix — provider
alias uid-stability, migration message, rule tombstones, unwired single run, NO-BREAK
old-vs-new byte equality, composed run (riders + injection + certainty degradation),
long-text parallel merge with labeled conflicts, failed-chunk isolation, identity
degrade, and every template through the real loader; review 19/19 clean.
