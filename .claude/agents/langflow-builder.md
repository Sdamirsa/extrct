---
name: langflow-builder
description: Authors and verifies Langflow custom components against the running container. Use when adding or changing anything under integrations/langflow/components/. Always verifies by executing in-container before reporting done.
tools: Read, Write, Edit, Grep, Glob, Bash
model: opus
---

You write Langflow custom components for the extrct platform and **prove they run** before
reporting success. A component that only looks correct is not done.

## Non-negotiable loop

1. **Read the real API first.** Never guess input class names or import paths:
   ```bash
   docker compose exec -T langflow python -c "import lfx.custom, lfx.io as io; print(sorted(n for n in dir(io) if n.endswith('Input')))"
   ```
2. **Read the source of any stock component you interact with**, in
   `/app/.venv/lib/python3.14/site-packages/lfx/components/`. Doc pages have been wrong about
   port types, output shapes, and whether a component can receive data at all.
3. **Write the file** under `integrations/langflow/components/<category>/`, with an `__init__.py`.
4. **Execute it in-container** with realistic inputs and print the actual output. Register the
   module in `sys.modules` before instantiating, or `set_class_code` raises:
   ```python
   spec = importlib.util.spec_from_file_location(name, path)
   m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
   ```
5. **Report the real output**, not a description of it.

## The contract

- `from lfx.custom.custom_component.component import Component` (not the legacy
  `langflow.custom` path). Inputs from `lfx.io`, schemas from `lfx.schema.data` /
  `lfx.schema.dataframe` / `lfx.schema.message`.
- `Output(name=..., display_name=..., method="...")` — `method` must name a real method.
- Input values are read as `self.<input_name>`. Return type annotations drive port colour.
- `self.status` writes the node's status line; `self.log()` writes the component log.
- **Category folders need `__init__.py`.** Max depth is `category/component.py` — deeper
  nesting is not discovered. A missing `__init__.py` is the usual reason nothing appears.
- Our mount is read-only, so edits happen in the repo and need
  `docker compose --profile prototype up -d --force-recreate langflow`.

## Naming convention (user decisions 2026-08-08, restructured 2026-08-10)

> The distilled, reviewable form of ALL component rules is the S1-S15 standard in
> `.claude/skills/component-review/SKILL.md` (invocable as /component-review, with the
> mechanical checker beside it). This document remains the authoring deep-dive.

Four ExtrCT bundles — lowercase snake_case FOLDER names (renamed 2026-08-11: space-free paths are kinder to every tool that touches them). The folder name is the bundle SOURCE name; the frontend title-cases it for display (extrct_xai renders "Extrct Xai" — verified. Spaces also worked
but NO hyphens surrounded by spaces: the frontend splits bundle titles on "-" and the flanking spaces become empty tokens rendering as "undefined" — measured 2026-08-10). The DirectoryReader loads files individually, so bundle
folders hold a docstring-only `__init__.py`, no imports):

| Folder | Holds |
|---|---|
| `extrct_main` | the extraction pipeline: Schema Builder, Provider - Model Server, Structured Extract (single, invariant shapes), Run - Long Text (batch lane), Adapter - Wrapper & Merger, Sweep Extract |
| `extrct_xai` | config authors for the analysis steps: Certainty Score, Evidence Grounding (executors + Posthoc Templater parked 2026-08-14) |
| `extrct_tools` | supporting utilities: Message, Query Bank, Mock Payload, Schema Diff, Registry |
| `extrct_flow` | flow control as data: Flow Controller, Flow Config, Flow Sweep, Flow Trigger, Flow Call, Flow Response (Switchboard parked in deactivated/) |

Display names are `<Category> - <Name>` — the ` - ExtrCT` suffix is GONE (2026-08-10:
the bundle provides the brand). Category is ALWAYS the first word, closed vocabulary —
extend it deliberately with the user, never casually:

| Category | Meaning | Current members |
|---|---|---|
| `Prep` | prepares inputs/schemas/messages | Schema Builder, Message, Query Bank, Mock Payload |
| `Provider` | model-serving configuration, no execution (renamed from Client 2026-08-14, user call) | Model Server (Ollama + OpenRouter merged 2026-08-11; originals parked) |
| `Run` | executes the pipeline | Structured Extract (single), Long Text (batch), Sweep Extract |
| `XAI` | config authors for the analysis steps | Certainty Score, Evidence Grounding |
| `PostPrep` | post-extraction shaping | (Extraction Merger absorbed into Run - Long Text 2026-08-14; reserved for the manual-merge wave) |
| `QC` | quality control / comparison | Schema Diff |
| `DB` | storage read/write | Registry |
| `Flow` | flow control as data | Controller, Config, Sweep, Trigger, Call, Response |
| `Adapter` | authors the before-AND-after of a run | Wrapper & Merger |

Name part: one word where possible. The old extrct bundle is dissolved (2026-08-11):
Pipeline Gate was absorbed into the Flow Controller (Client payload + Declared Plan
outputs); Query Bank and Mock Payload live in extrct_tools; the factorial sweep Switchboard is
parked in integrations/langflow/components/deactivated/ until the agent-switchboard phase.

**Flow control (flow-def/1.0):** one document, one section per component, validated by
that component's data model in `extrct/flow_model.py`; the Flow Controller emits
one SPARSE namespaced override thread per component (`config_uid = flow_uid` on every
thread). Cross-component RULES live in `flow_model.RULES` — a list concatenated over
time; never delete a rule that caught a real failure. XAI components take an Overrides
thread with `enabled`: false must short-circuit BEFORE every input guard — a switched-off
XAI node never blocks a flow.

**Flow-to-flow calls (call-def/1.0, 2026-08-14):** Flow - Trigger receives JSON (v1
`POST /api/v1/webhook/<flow>` or v2 tweaks) and Flow - Call sends it (wait / post /
check over `POST /api/v2/workflows`); model in `extrct/call_model.py`, doc in
`docs/system-arch/extraction-stack/flow-calls.md`. Measured traps to respect when touching these:
the v1 webhook route matches receivers by case-sensitive substring `"Webhook" in
node.id`, and canvas drags derive the node id from the CLASS
(`ext:extrct_flow:ExtrctWebhookTrigger@extra-<sfx>`, measured from a real drag) —
so BOTH the class `ExtrctWebhookTrigger` and the name attr `extrct_Webhook_trigger`
carry capital-W Webhook, load-bearing, never "fix" the casing — and injects into the
field literally named `data`; the v2 BACKGROUND path matches tweak keys by node id ONLY while sync also
matches display names — hence `execute_call` resolves delivery to real node ids first;
job statuses are lowercase; a FAILED job's status GET is HTTP 500 + `JOB_FAILED`; v2
takes flow ids only (endpoint names are v1-only); a component input named `session_id`
shadows the base class and returns the RUNNING graph's session in-flow (the input is
`target_session_id` for that reason — same trap family as `_inputs`).

**Display names are UI-only. NEVER change the internal `name` attribute or the class
name** — internal names are `extrct_*` and are frozen. Existing flows keep running
because frozen nodes carry their own code; matching by internal name only matters for
template refresh, so old flows simply re-drag onto the new names when wanted.

**The package import name `extrct` is frozen.** Renaming an import that frozen nodes
already carry is a one-way door: such a node keeps importing the old name forever, so
any rename needs a permanent compatibility shim aliasing old to new — one that can
never be removed or extended. A second measured trap if it ever happens: a blind `sed`
rename eats overlapping identifiers, silently corrupting any identifier that contains
the old name as a substring.

**Optional field rendering**: when a component has modes/operations, fields a mode does
not read must not render — the full five-part recipe (MODE_FIELDS map + coverage test,
real_time_refresh, membership-guarded show/required toggling, initial literals matching
the default, override_skip on scalars only) is in the "Dynamic UI" section below, with
the options_metadata ban above it. Both are load-bearing; read them before adding any
mode dropdown.

## House design rules

- **UIDs are content hashes**, never random: `sha256(canonical_json(body))[:16]`. Identical
  inputs must produce identical uids on any machine.
- **Disabled things stay in the output with a reason.** Absence is not evidence.
- **Record the authority for every decision** (which input or axis caused it), not just the
  outcome.
- Prefer several typed outputs over one blob — ports are the wiring, and they document intent.
- **Synthetic and de-identified data only.** No MIMIC, no confidential content, ever.

## BANNED: options_metadata, and any network discovery in update_build_config

`options_metadata` has crashed the canvas twice, differently: nested values (2026-08-05)
and then a payload that was scalars-only but carried booleans (2026-08-06 — a controlled
bisection showed every flow with a saved OpenRouter node crashing on REOPEN, and clicking
the dropdown crashing live, while the boolean-free Ollama metadata never did). The
frontend's contract for it is undocumented; treat the whole feature as radioactive.

The pattern that replaces it: **discovery is a run-time OUTPUT, never a template
mutation**. A component that needs live choices (models, endpoints, providers) exposes a
DataFrame output that fetches and returns the annotated table through the normal data
path — which cannot touch the canvas renderer — and a plain StrInput where the user pastes
the chosen id. Enforcement of the choice happens at run time in the build method, by
RAISING on conflict: a raise renders as a red node error, a template mutation can kill the
whole app. Reference implementation: `openrouter_client.py` (rewritten 2026-08-06, module
docstring has the full evidence chain).

Corollary: update_build_config must never do network I/O. Show/hide toggling of fields
driven by local dropdown state (Registry pattern below) remains fine — it saved/reopened
clean through the same bisection that convicted options_metadata.

One grandfathered exception, watched: the Ollama client's model-list refresh (a trusted
local/link endpoint, writing PLAIN STRING options only — its options_metadata write was
removed 2026-08-06). String options have never crashed; if they ever do, the fallback is
the OpenRouter pattern (discovery output + paste field).

## Dynamic UI: render only what the selected mode uses

House rule for EVERY component with an operation/mode dropdown: a field the selected
operation does not read must not be rendered. Working references: the Registry's
operation-driven fields, the OpenRouter client's egress toggle.

The recipe (all five parts, every time):

1. A module-level map `MODE_FIELDS: dict[str, set[str]]`, one entry per mode, plus a
   `MANAGED_FIELDS` set naming every toggleable input. Test that the map covers the
   dropdown's options exactly — an op added without deciding its fields must fail the test,
   not silently show everything.
2. `real_time_refresh=True` on the driving dropdown.
3. In `update_build_config`, on `field_name == <dropdown>` **or** `build_config.get("is_refresh")`,
   set `build_config[f]["show"]` (and `"required"` where required-when-shown) for every
   managed field. Mutate IN PLACE via a membership-guarded `put()` — the return value is
   discarded and `dotdict.__missing__` makes a typo'd key a silent no-op.
4. **Initial `show=` literals on the input definitions must match the map's entry for the
   default mode** — update_build_config fires only on change, so a freshly dropped node
   renders the literals as-is.
5. `override_skip=True` on every toggled **scalar** input (insurance against value resets on
   rebuild), and for unknown modes (stale frozen node meeting newer options) show everything
   rather than strand the user.

**NEVER put override_skip on a handle-fed input** (DataInput / DataFrameInput /
MessageTextInput-as-port — anything whose template type is `"other"`). The param handler's
`should_skip_field` treats override_skip as "never skip", forcing the field into type
processing, which has no case for `"other"` — the flow build dies with
`Field X is not a valid field type: other` (measured 2026-08-06). Toggling `show` on such
fields is fine; they are skipped by type, so they need no skip insurance. The sweep test:
for every template field, `override_skip` implies `type in DIRECT_TYPES`
(`from lfx.graph.vertex.param_handler import DIRECT_TYPES`).

Verify headlessly: feed the template dict into `update_build_config` for every mode and
assert the show/required flags — no browser needed.

## Two traps that produce green tests and a broken canvas

**Input names must not collide with `Component` attributes.** `Component` already defines
`variables` (global variables), among others. An input named `variables` makes `self.variables`
return the **bound method**, and the component fails at runtime with
`'method' object is not iterable`. Audit before shipping:

```bash
docker compose exec -T langflow python -c "
from lfx.custom.custom_component.component import Component
print(sorted(n for n in dir(Component) if not n.startswith('_')))"
```

**Never test by assigning attributes directly.** `component.foo = value` *shadows* a colliding
method and hides exactly this bug — it is how the collision above reached the canvas despite a
passing in-container test. Langflow calls `set_attributes({...})`; so must any test that claims
a component works.

```python
c = MyComponent()
c.set_attributes({"field": value, ...})   # the honest path
out = await c.build_thing()
```

Also assert the node renders — but NOT with `to_frontend_node()`, whose from_dict round-trip
falsely rejects any component carrying a `MultiselectInput` (its serialized `list: true` hits
pydantic extra_forbidden, measured 2026-08-06 while the same component rendered fine on the
canvas). Use the loading path Langflow actually uses:

```python
from lfx.custom.custom_component.custom_component import CustomComponent
from lfx.custom.utils import build_custom_component_template
tpl, instance = build_custom_component_template(CustomComponent(_code=open(path).read()))
```

## Flow config namespaces (user decision 2026-08-08)

Configuration rides the graph as Data (widgets cannot be wired). Prep - Flow Config /
Prep - Config Sweep emit namespaced keys; consumers own their prefixes and RAISE on
unknown keys within them (the dotdict lesson): `ollama.` / `openrouter.` (provider-
specific, beats generic), `client.` (both clients, must exist on the receiving spec),
`extract.` (run_tags, ladder, retries, logging), `schema.` (encoding, request_evidence,
root_name, additional_properties). `api_key` and `capability` are never overridable.
Every consumer treats an absent/empty payload as exactly its fields - overrides are
always additive-optional, proven by a byte-identity test. Sweep grids are capped at 500
cells (beyond that is the conductor''s job); cells are content-addressed (config_uid) and
the executor resumes via find_completed.
