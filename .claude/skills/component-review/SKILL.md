---
name: component-review
description: Review Langflow components under integrations/langflow/components/ against the ExtrCT component standard (S1-S15). Use when asked to review a component, audit the bundles, or check a new component before it ships.
---

# The ExtrCT component standard (S1–S15)

Every rule below was paid for by a measured failure or decision (dates in the builder
agent doc and `docs/system-arch/extraction-stack/api-notes.md`). Review = run the mechanical script,
then judge the manual items. A component is DONE only when it passes both.

## The standard

- **S1 — Bundle and name.** Folder = sidebar bundle: lowercase snake_case. Display
  name `<Category> - <Name>`, category first, from the closed vocabulary:
  Prep / Provider / Run / PostPrep / XAI / QC / DB / Flow / Adapter. (Client renamed
  Provider 2026-08-14 — user call, display surfaces only; Adapter = a config author
  for things that run before AND after the extraction, e.g. Wrapper & Merger.) No
  ExtrCT suffix — the bundle is the brand.
- **S2 — Frozen identity.** The internal `name` attribute (`extrct_*`), the class name,
  and the package import `extrct` never change. Stability trumps pattern
  (`convert_rich_message` keeps its legacy name; the trigger's capital-W `Webhook` — in
  BOTH class `ExtrctWebhookTrigger` and name attr — is load-bearing: the v1 webhook
  route matches receivers by case-sensitive substring `"Webhook" in node.id`, and
  canvas drags derive the node id from the CLASS
  (`ext:<bundle>:<ClassName>@extra-<sfx>`, measured from a real drag)).
  An import rename is a one-way door: frozen nodes import the old name forever, so it
  would need a permanent compatibility shim that can never be removed.
- **S3 — Thin wrapper.** Logic lives in `extrct`; the component wires, validates
  presence, reports. If a behavior needs a test, it must be testable without `lfx`.
- **S4 — Static template.** `options_metadata` is BANNED (two distinct canvas crashes).
  No network in `update_build_config` (one grandfathered exception: Ollama model list,
  strings only). Discovery is a run-time DataFrame output + a paste field; enforcement
  RAISES in the build method.
- **S5 — Dynamic UI** (only for op/mode dropdowns): OP_FIELDS map covering ALL ops +
  coverage test, `real_time_refresh`, membership-guarded `put()`, initial `show`
  literals matching the default op, `is_refresh` re-apply.
- **S6 — `override_skip` on scalars only.** Never on a handle-fed input (template type
  "other") — the flow build dies with "not a valid field type".
- **S7 — Handle inputs declare `input_types`** and unwrap via the standard `_unwrap`
  (list → first, `.data`, dict guard).
- **S8 — Outputs.** Several typed outputs over one blob; `group_outputs=True` on every
  output of a multi-output node; every `method=` names a real method; return
  annotations drive port types. Data consumed downstream (e.g. by a merger) is never
  truncated for display — truncation belongs to the display path only.
- **S9 — Loud by design.** Bad input/config raises `ValueError` whose message says what
  to DO, not just what failed. A typo must die on the canvas, never no-op. `self.status`
  always carries the load-bearing facts (uids, counts, warnings).
- **S10 — Identity is content.** `sha256(canonical_json(body))[:16]` via
  `hashing.content_uid`; the uid layers (schema / client / config / flow / wrap /
  merge / run) are never conflated.
- **S11 — Overrides thread.** Components that accept flow config apply it via
  `owned_subset` / `apply_client_overrides` (typos raise), report `config_uid`, and are
  **byte-identical in behavior when unwired** (the no-break proof is part of done).
- **S12 — Optional nodes short-circuit first.** `enabled=false` returns disabled
  outputs BEFORE every input guard — a switched-off node never blocks a flow — and the
  disabled output says so with a reason (absence is not evidence).
- **S13 — Two kinds of DB write.** Derived/observability logging (run annotations,
  wrap/merge logs) is guarded — `try/except` + `self.log`, the output survives a dead
  DB. A write that IS the operation (Registry ops, an explicit persist toggle) fails
  LOUDLY. Evidence rows are written only by the thing that produced them. raw
  input/chunk text is never stored by default.
- **S14 — Docstrings carry the measured lessons** (what was observed, when), not
  narration; `documentation` points to the owning doc in `docs/system-arch/extraction-stack/`.
- **S15 — Verified in-container before done.** Template through
  `build_custom_component_template` (the real loader — `to_frontend_node` falsely
  rejects list inputs), then an honest functional run via `set_attributes` with real or
  synthetic inputs. Remember: component edits need a fresh drag (frozen node code);
  package edits need a container restart — and IN-FLOW components run the SERVER's
  cached package import, so exec-based tests can pass against code the server is not
  yet running (measured 2026-08-14). Beware base-class attribute collisions: `_inputs`
  is owned by Component (shadowing it dies at runtime), and an INPUT named
  `session_id` is silently shadowed at flow runtime by the running graph's session
  (measured 2026-08-14 — use a prefixed name like `target_session_id`).

## How to run a review

1. Copy `review_components.py` (beside this file) into the container and run it:
   ```bash
   docker compose --project-directory <repo>/deploy cp .claude/skills/component-review/review_components.py langflow:/tmp/rv.py
   docker compose --project-directory <repo>/deploy exec -T langflow python /tmp/rv.py
   ```
   It checks the mechanical subset (S1, S2, S4, S6, S7, S8, S9-status, S14, S15-loader)
   and flags manual items.
2. Judge the flags:
   - `update_build_config -> manual check`: must be one of the two sanctioned patterns —
     Registry-style field rendering (S5) or the grandfathered Ollama model list. Anything
     else is a violation.
   - `S13 unguarded DB writes`: decide loud-by-design (operation) vs a miss (derived
     logging) per S13.
3. Manual-only rules: S3 (thin wrapper), S11 (no-break proof exists), S12 (short-circuit
   order), S14 (docstring quality). Read the component with those four in mind.

Last full review 2026-08-10: 17/17 clean; both `update_build_config` flags matched the
sanctioned patterns; both S13 notes confirmed loud-by-design (Schema Builder persist
toggle, Registry write ops).
