# Mini apps — the Streamlit lane

*Design of record for `apps/`: small, task-specific UIs — the first is a human
extraction form rendered from a variables set — attached to the stack as optional
compose components. Decided 2026-08-21; revisit when the first app graduates.*

## Context

Two kinds of UI are growing around the engine, and they have opposite needs:

| | Main UI (planned workbench) | Mini apps |
|---|---|---|
| Workload | data-heavy: run-monitor over the run log, live notifications | one task, one screen: fill a form, review a record |
| State | long-lived, multi-user, push updates | per-session, single annotator |
| Lifetime | permanent product surface | proving lane; may graduate or be deleted |

Streamlit's whole-script-rerun execution model (no server push, fragile state under
heavy data) is wrong for the first column and nearly ideal for the second. So: **mini
apps are Streamlit; the main UI is not** — and nothing in a mini app may assume it
stays Streamlit.

## Decision

Mini apps live in `apps/<name>/`, are written in Streamlit (Apache-2.0 — passes the
license red line), import `extrct` directly (pure Python, CPU-only, portable), and
attach to the stack as one compose component folder each under `deploy/components/`,
gated by `profiles:`, port bound to `127.0.0.1` — the exact pattern `deploy/` already
uses. Dropping an app from the stack is deleting one `include:` line.

Two rules, in the spirit of the root red lines:

- **A mini app contains zero extraction or schema logic.** It renders documents the
  library builds and validates. Anything not UI-shaped gets pushed DOWN into
  `packages/extrct` — the same rule the Langflow components live under. If a widget
  needs to know what an enum's allowed values are, it asks the compiled schema, never
  a copy.
- **A mini app's output is an artifact of record.** Versioned contract, content uid,
  written through the library's storage — never the app's own files. The Streamlit
  script proves nothing; the document is the record (the same stance as "Flow JSON is
  never an artifact of record").

Hub-and-spoke, never mesh: apps share state only through the registry and the run
log. An app that talks to another app, or to Langflow, has stopped being mini.

The lane's scaffold lives at [`apps/_template/`](../../../apps/_template/): the
handshake `README.md` (scope, boundaries, definition of done), the machine-readable
`data_model.json` (`app-io/1.0` — inputs/outputs as typed contracts), and a per-app
uv project. Each app is a standalone uv project with a committed `uv.lock` — apps
are deployables, so they pin; deliberately not a workspace with the library.

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

## The first app — human extraction form

Renders an entry form for a variables set so a person performs the same extraction
the model does. Requirements:

| Variables column | Widget |
|---|---|
| `options` | selectbox (multiselect when `is_list`) |
| `bool`, required | checkbox |
| `bool`, optional | **three-state** select (yes / no / not stated) — a checkbox cannot distinguish `false` from absent, and that distinction is the whole point of `strict_nullable` |
| `int` / `float` with `ge`/`le` | bounded number input — enforce at entry, never clamp |
| `date` / `datetime` | date picker |
| scalar with `is_list` | `st.data_editor`, one column |
| `object` with `is_list` (e.g. `diseases`) | `st.data_editor`, one column per child field |
| `object`, single | expander / section |

- Every optional field gets an explicit *not stated* affordance and is recorded as
  `null`, never dropped — the form makes the same claim distinction the Encoding axis
  exists for.
- **Known Streamlit pain point, prototype first:** dynamic add-row is not allowed
  inside `st.form`, so nested repeating groups mean `st.data_editor` or manual
  `session_state` bookkeeping. This is the one part of the build with real risk.
- This is the first UI surface that legitimately displays real clinical text — the
  thing Langflow must never see. That raises the stakes, it does not relax them:
  localhost-only, text referenced by hash in the output document, text storage
  strictly per the library's opt-in policy.
- Single annotator, no auth, for now. Multi-annotator means a reverse proxy with auth
  in front — out of scope until someone actually needs it.

## Graduation path

Mini apps are the proving lane for the workbench, the way Langflow was for the
engine. When an app needs multi-user, push notifications, or embedding in the main
UI, it graduates: port the (small) Streamlit layer into the main app; the logic never
moves because it was never in the app. The variables table already compiles to JSON
Schema, so a future web main UI gets its forms nearly free (e.g.
react-jsonschema-form) — nothing built in this lane is a dead end.

## Alternatives considered

- **Gradio** — ML-demo shaped; weaker at structured forms. No.
- **NiceGUI** — websockets/push, i.e. the main UI's needs, not this lane's. If a
  "mini" app wants push, that is the graduation signal, not a framework switch.
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
