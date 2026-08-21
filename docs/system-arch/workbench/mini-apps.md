# Mini apps — the NiceGUI lane

*Design of record for `apps/mini/`: small, task-specific UIs — the first is a human
extraction form rendered from a variables set — attached to the stack as optional
compose components. Decided 2026-08-21 (originally Streamlit); framework revised to
NiceGUI the same day, before any app code — see "Framework revision" below. Revisit
when the first app graduates.*

## Context

Two kinds of UI are growing around the engine, and they have different needs:

| | Main UI (planned workbench, `apps/workbench/`) | Mini apps (`apps/mini/`) |
|---|---|---|
| Workload | data-heavy: run-monitor over the run log, live notifications | one task, one screen: fill a form, review a record |
| State | long-lived, multi-user, push updates | per-session, single annotator |
| Lifetime | permanent product surface | proving lane; may graduate or be deleted |

The lanes get separate folders because they get separate rules — framework, posture,
and contracts are decided per lane, and the folder boundary matches the rule
boundary. Nothing in a mini app may assume it stays on the lane's framework.

## Decision

Mini apps live in `apps/mini/<verb>/`, named by task verb (`annotate`, `review`, …),
are written in **NiceGUI** (MIT — passes the license red line), import `extrct`
directly (pure Python, CPU-only, portable), and attach to the stack as one compose
component folder each under `deploy/components/`, gated by `profiles:`, port bound
to `127.0.0.1` — the exact pattern `deploy/` already uses. Dropping an app from the
stack is deleting one `include:` line.

Two rules, in the spirit of the root red lines:

- **A mini app contains zero extraction or schema logic.** It renders documents the
  library builds and validates. Anything not UI-shaped gets pushed DOWN into
  `packages/extrct` — the same rule the Langflow components live under. If a widget
  needs to know what an enum's allowed values are, it asks the compiled schema, never
  a copy.
- **A mini app's output is an artifact of record.** Versioned contract, content uid,
  written through the library's storage — never the app's own files (NiceGUI's
  `app.storage` is a UI convenience, never a record). The app script proves nothing;
  the document is the record (the same stance as "Flow JSON is never an artifact of
  record").

Hub-and-spoke, never mesh: apps share state only through the registry and the run
log. An app that talks to another app, or to Langflow, has stopped being mini.

The lane's scaffold lives at [`apps/mini/_template/`](../../../apps/mini/_template/):
the handshake `README.md` (scope, boundaries, definition of done), the
machine-readable `data_model.json` (`app-io/1.0` — inputs/outputs as typed
contracts), and a per-app uv project. Each app is a standalone uv project with a
committed `uv.lock` — apps are deployables, so they pin; deliberately not a
workspace with the library.

## Framework revision: Streamlit → NiceGUI (2026-08-21)

The original decision picked Streamlit and named one prototype-first risk: dynamic
nested forms (list-of-objects rows) under the whole-script-rerun model. The risk was
then confirmed by the maintainer's own prior experience before any prototype was
built: session state desyncing across reruns, and form widgets snapping back to
previous values — the classic rerun-model failure class (defaults re-derived on
rerun, positional widget keys shifting when rows are added, one-rerun-behind
writes). Rather than prototype into a known failure, the lane switched frameworks
before the first app.

What NiceGUI changes, mechanically: no rerun — the app holds a persistent element
tree; interactions are events over a websocket; widget state lives in the widget
object (or the app's own model via two-way `bind_value`), so a value changes only
when the user edits it or code assigns it. Dynamic repeating groups are the native
idiom (a handler adds child elements to a container or deletes a row object) rather
than a workaround. Two additional points in its favour: MIT license, and a
first-party **native desktop mode** (`ui.run(native=True)` via pywebview, plus a
documented PyInstaller packaging path) — a real distribution option for
clinician-facing local tools that Docker-averse users can run, aligned with the
library's Windows/macOS/Linux portability goal.

Costs accepted with the switch: event-driven code (handlers, sometimes async) in
`ui/`; a smaller community than Streamlit's (~16k vs ~46k GitHub stars, 2026-08-21);
non-trivial styling leaks Quasar/Tailwind vocabulary into Python; a real major-bump
cadence (3.0.0 in 2025-10 with 13 listed breaking changes — semver with migration
notes, versus Streamlit's rolling deprecations inside 1.x). None touch the failure
mode that forced the change.

Measured facts the lane relies on (NiceGUI 3.16.0, verified 2026-08-21, sources in
the 2026-08 work log entry):

- **No telemetry**: source grep of the installed wheel found no analytics, no
  version phone-home; all frontend assets (Vue/Quasar/Tailwind/Socket.IO) ship in
  the wheel and are served locally. The only remote-connect path is the opt-in
  On Air relay, default off. (Streamlit, measured the same day: usage stats ON by
  default.)
- **`ui.run(host=...)` defaults to `0.0.0.0`** in non-native mode. The template's
  explicit `APP_HOST` default of `127.0.0.1` is therefore the enforcement point of
  the localhost-only rule — never remove it.
- **The shared auto-index page was removed in 3.0** (module-level widgets shared
  state across sessions): all UI builds inside `@ui.page` functions, which
  `nicegui-pack` (the documented PyInstaller path) requires anyway.
- Community record runs one direction: repeated Streamlit→NiceGUI migration
  accounts, each citing session-state pain (the framework's own FAQ opens with it:
  "We like Streamlit but find it does too much magic when it comes to state
  handling"); no NiceGUI→Streamlit migration found.

## The handshake — input and output contracts

The interface between a mini app and the rest of the system is two JSON documents,
following the established contract style (`client-def/1.0`, `job-def/1.0`, …): a
plain JSON document anyone can author or read, **validated by the library against the
real spec** (no parallel field list to drift), stamped with a content uid
`sha256(canonical_json(body))[:16]`. The contract is the document; Pydantic and the
spec dataclasses are how the library enforces it, not a second definition of it.
New handshakes reuse this pattern — no ad-hoc variable lists in app code or docs.

**Input** — a variables set loaded from the registry (or carried by a `job-def/1.0`),
identified by its `schema_uid`. The app renders the schema the library compiles from
it; the eight-column semantics are owned by
[`variables-table.md`](../extraction-stack/variables-table.md), not restated here.

**Output** — proposed `annotation-def/1.0`, one document per completed human
extraction:

```json
{
  "annotation_model_version": "annotation-def/1.0",
  "schema_uid": "1a2b3c4d5e6f7a8b",
  "input_text_sha256": "<full digest — hash, don't keep, as everywhere>",
  "annotator": "<pseudonymous id, never PII>",
  "values": { "study": { "modality": "TTE", "lvef": null, "...": "..." } }
}
```

- `values` must validate against the exact schema `schema_uid` names — the same
  validator the model output goes through. A human annotation and a model run are
  then directly comparable rows, which is the point: **human labels become eval
  ground truth** for the certainty/grounding lane at zero extra cost.
- `annotation_uid = sha256(canonical_json(body))[:16]`. Timestamps ride the storage
  row, not the hash (the deployment-values rule) — so re-submitting an identical
  annotation is idempotent, matching the storage layer's idempotent-on-uid habit.
- `schema_uid` pins the annotation to the schema *as it was*: a renamed Root Name is
  a different schema identity, and annotations must not silently survive it.

## The first app — `annotate`, the human extraction form

Renders an entry form for a variables set so a person performs the same extraction
the model does. Requirements:

| Variables column | Widget |
|---|---|
| `options` | `ui.select` (`multiple=True` when `is_list`) |
| `bool`, required | `ui.checkbox` |
| `bool`, optional | **three-state** `ui.select` (yes / no / not stated) — a checkbox cannot distinguish `false` from absent, and that distinction is the whole point of `strict_nullable` |
| `int` / `float` with `ge`/`le` | `ui.number` with `min`/`max` and `validation=` — enforce at entry, never clamp |
| `date` / `datetime` | `ui.date` / `ui.input` with a date picker |
| scalar with `is_list` | a row container of inputs with add/remove |
| `object` with `is_list` (e.g. `diseases`) | a card per row inside a container — add appends child widgets bound to the row's model; remove deletes the row object; no key bookkeeping |
| `object`, single | `ui.expansion` / section |

- Every optional field gets an explicit *not stated* affordance and is recorded as
  `null`, never dropped — the form makes the same claim distinction the Encoding axis
  exists for.
- Widgets bind to a plain model object (`bind_value`, which supports nested keys —
  a tuple of strings reaches into nested dicts/dataclasses); the submit handler
  passes the model to the library for validation and storage. Values never live in
  the UI layer alone.
- Before hand-rolling the schema→form walk, evaluate `nicecrud` (MIT, small):
  NiceGUI forms generated from Pydantic models, nested models and choice fields
  included. Even if not adopted, it is the reference implementation for the widget
  mapping above.
- This is the first UI surface that legitimately displays real clinical text — the
  thing Langflow must never see. That raises the stakes, it does not relax them:
  localhost-only, text referenced by hash in the output document, text storage
  strictly per the library's opt-in policy.
- Single annotator, no auth, for now. Multi-annotator means a reverse proxy with auth
  in front — out of scope until someone actually needs it.

## Graduation path

Mini apps are the proving lane for the workbench, the way Langflow was for the
engine. When an app needs multi-user, auth, or embedding in the main UI, it
graduates: port the (small) `ui/` layer into the main app; the logic never moves
because it was never in the app. NiceGUI narrows the old capability gap — push
updates over websockets are already native — so graduation is triggered by product
surface (multi-user, auth, permanence), not framework limits. The variables table
already compiles to JSON Schema, so a future web main UI gets its forms nearly free
(e.g. react-jsonschema-form) — nothing built in this lane is a dead end. Native
desktop packaging (PyInstaller) is a distribution option on this path for apps that
must leave the maintainer's machine without Docker — a release-engineering cost paid
per platform, only when someone needs the artifact.

## Alternatives considered

- **Streamlit** — the original pick (Apache-2.0, largest community, fastest first
  screen). Revised out before any code for the reasons in "Framework revision":
  the rerun/session-state model is structurally wrong for dynamic nested forms,
  which is this lane's core workload; no first-party desktop mode.
- **Gradio** — ML-demo shaped; weaker at structured forms. No.
- **FastAPI + templates** — more control, several times the cost per app; that cost
  belongs to the main UI, once.

One framework for the whole lane, deliberately: a second one doubles the maintenance
surface for zero new capability.

## Open questions (for review before the first app lands)

1. Storage: does `annotation-def/1.0` get its own table in the run-log schema, or a
   new document store? (Leaning: a table next to the run rows, same backends.)
2. Annotator identity: what id scheme is pseudonymous enough for the governance-first
   fields while still supporting inter-annotator agreement?
3. Does the `annotation-def` module land in `packages/extrct` with the first app
   (leaning yes — the library owns contracts) or wait for a second consumer?
