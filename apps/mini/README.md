# apps/mini — the mini-app lane

Small, task-specific NiceGUI UIs over the `extrct` library. The design of record is
[`docs/system-arch/workbench/mini-apps.md`](../../docs/system-arch/workbench/mini-apps.md);
this file is the short form.

## Rules (the design doc is the authority)

- One app per folder, **named by its task verb** (`annotate`, `review`, …) — the
  output contract, not the folder, names the document (`annotation-def/1.0`).
- **NiceGUI only** (MIT), one framework for the whole lane. Chosen over Streamlit
  2026-08-21 for its event-driven model — persistent widgets, two-way binding, no
  rerun/session-state desync — and its first-party native desktop mode.
- **Zero extraction or schema logic in an app.** The library builds and validates
  every document; the app renders.
- **An app's output is an artifact of record**: versioned contract, content uid,
  written through library storage — never the app's own files.
- Hub-and-spoke: apps share state only through the registry and the run log.
- Each app is its **own uv project** (own `pyproject.toml`, own committed
  `uv.lock`) — apps are deployables, so they pin; deliberately *not* a workspace
  with the library, so `extrct`'s dependency tree never entangles with the UI
  framework's.

## Starting a new app

1. Copy [`_template/`](_template/) to `apps/mini/<verb>/` — the folder is
   standalone by construction (entry point, `ui/` package, tests, Dockerfile); its
   README documents the layout.
2. Fill in `README.md` (the handshake: scope, boundaries, definition of done) and
   `data_model.json` (the machine-readable input/output declaration) — **these two
   must not drift**; the README is the prose half, the JSON the typed half, and
   `tests/test_handshake.py` checks both stay well-formed.
3. Keep the app's own knowledge in its `docs/`: dated research notes in
   `docs/research/`, decisions (append-only, with evidence links) in
   `docs/decisions.md` — per the unit standard (`.claude/habits.md`; the
   `unit-keeper` agent scaffolds and audits it).
4. In `pyproject.toml`: set `name`, then `uv lock` and commit the lock.
5. Wire deployment as one compose component folder: `deploy/components/<verb>/`,
   behind a profile, port on `127.0.0.1` (snippet in the template README).

## Apps

| App | Task | Output contract | Status |
|---|---|---|---|
| [`annotate/`](annotate/) | human extraction (blind) and correction of LLM output (review) | `annotation-def/1.0` | handshake complete — ready to build/outsource |
| [`evaluate/`](evaluate/) | rubric-based human judgment of LLM extractions | `evaluation-def/1.0` (rubric: `rubric-def/1.0`) | handshake complete — ready to build/outsource |
