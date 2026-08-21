# annotate — human extraction and correction against an extrct variables set

*This README is the app's **handshake**: scope, boundaries, and definition of done,
agreed before code. [`data_model.json`](data_model.json) is its machine-readable
half — inputs and outputs as typed contracts. The two must not drift: change them in
the same commit or not at all. This document is written to be **outsourceable**: a
developer with no other context, plus the linked authorities, can build and verify
this app.*

Authorities this handshake leans on (read before coding):
[`.claude/habits.md`](../../../.claude/habits.md) (unit standard) ·
[`docs/system-arch/workbench/mini-apps.md`](../../../docs/system-arch/workbench/mini-apps.md)
(lane design: widget mapping, contracts, deployment) ·
[`docs/system-arch/extraction-stack/variables-table.md`](../../../docs/system-arch/extraction-stack/variables-table.md)
(the variables format — the schema this app renders).

## Layout

Standard template layout (see [`_template/README.md`](../_template/README.md) table);
this app adds nothing structural. All non-UI logic named below (dataset loading,
role inference, form plan, contracts, storage) lives in `packages/extrct`, NOT here —
if it is missing from the library, the task includes adding it there with tests.

## Scope

A single-user, localhost NiceGUI app in which a person performs or corrects
structured extraction: it loads a **variables set** (the extrct schema format),
loads **input records** from a file, renders one record at a time as source text
beside a schema-driven form, and writes each completed form as an
`annotation-def/1.0` document through library storage. It starts when the user has
chosen a schema and a dataset; it ends when every record is annotated or skipped
and the results are exported. Two modes, one output contract:

- **blind** — the form starts empty; the person extracts from the text.
- **review** — the form is pre-filled from an LLM extraction the user provides;
  the person corrects it. The document records what it was seeded from, so
  human–LLM deltas stay computable.

## Boundaries

Inherited from the lane (restated so this file stands alone):

- Zero extraction or schema logic here — the library builds and validates every
  document; this app renders. The schema→field walk comes from the library's form
  plan; the app maps plan entries to NiceGUI widgets and nothing more.
- Output is an artifact of record: `annotation-def/1.0`, content uid, written
  through library storage — never this app's own files. `app.storage` holds UI
  conveniences (last schema, window state) only, never records.
- Talks only to the registry and the run log. Never to another app, never to
  Langflow.
- Localhost-only (`127.0.0.1`), single user, no auth.

App-specific boundaries:

- **No LLM calls, ever.** This app consumes LLM output as data; it never produces
  it. No provider import, no network beyond localhost serving.
- **Never clamps or coerces a value silently.** Out-of-range and off-enum entries
  are refused at the widget with the constraint shown; the repair ladder's
  never-clamp stance applies to humans too.
- **Never guesses silently.** Column-role inference (below) always shows its
  conclusion for confirmation before any record is displayed.
- A missing optional value is recorded as explicit `null` (*not stated*), never a
  dropped key — the `strict_nullable` claim distinction, enforced in the UI.
- Source text is displayed, hashed into the document, and stored only per the
  library's opt-in text-storage policy.

## Input / output

The authoritative declaration is [`data_model.json`](data_model.json). Summary:

| Direction | Name | Contract / format | Identified by |
|---|---|---|---|
| in | variables_set | schema variables from the registry, or a schema JSON export | `schema_uid` |
| in | dataset | file: csv, tsv, xlsx, json (array), jsonl — or pasted single text | `input_uid` per record (sha256 of text) + optional user id column |
| in (review mode) | llm_output | JSON values per record, conforming to the schema | `values_sha256`, optional `run_uid` |
| out | annotation | `annotation-def/1.0` (schema embedded in data_model.json) | `annotation_uid` |
| out | export | JSONL / CSV / XLSX projection of stored annotations | — (projection, not a contract) |

**Identity stamping (hard requirement):** every annotation carries `annotator`
(pseudonymous handle, required, no PII), `schema_uid`, `input_text_sha256`
(always computed internally), `input_uid` (the user's id column value when one
exists), and in review mode a `seed` block naming what pre-filled the form.
Timestamps and durations ride the storage row, never the hash.

**Import/export round-trip (hard requirement):** exporting annotations and
re-importing that file must reproduce the same documents (same `annotation_uid`s,
idempotent in storage). Import accepts the app's own export formats plus bare
`annotation-def/1.0` JSON/JSONL.

## Requirements

Numbered and observable — each maps to a Definition-of-done check.

1. **Schema intake**: load a variables set from the extrct registry (by set name /
   `schema_uid`) or from a schema JSON export file; show root name, variable count,
   and `schema_uid` before starting.
2. **Dataset intake**: open csv, tsv, xlsx, json (array of objects), jsonl; also
   accept a single pasted text. Malformed files fail loudly with the row/reason.
3. **Column-role inference, confirm-first**: propose which column is the source
   *text*, which is the record *id*, and (review mode) which holds the *LLM
   output*, from structure and data types — e.g. longest-string column → text;
   unique short values → id; JSON-parseable objects whose keys overlap the schema →
   LLM output. The user confirms or overrides in one dialog before annotation
   starts; the confirmed mapping is remembered per dataset shape and offered as the
   default next time.
4. **Form rendering**: render the schema via the library's form plan using the
   lane's widget mapping (enums → select, optional bool → three-state, bounded
   numbers → bounded inputs refusing out-of-range, list-of-objects → add/remove row
   cards, nested objects → sections). Descriptions from the variables table appear
   as field help.
5. **Record navigation**: next/previous, jump-to-record, progress (done / skipped /
   remaining), and **skip with a reason** (recorded). Keyboard-first: advancing,
   submitting, and enum selection work without the mouse.
6. **Draft safety**: leaving a half-filled record and returning loses nothing;
   drafts persist across an app restart (draft state is a UI convenience, distinct
   from submitted documents).
7. **Submission**: validates through the library's validator (the same one model
   output passes), stamps `annotation_uid`, writes through library storage
   (SQLite default, Postgres when configured). Re-submitting identical content is
   idempotent — same uid, one row.
8. **Review mode**: with an LLM output source confirmed, the form pre-fills;
   changed fields are visually marked; the saved document carries the `seed` block.
   A record whose LLM output fails schema validation is flagged and opens blind,
   with the raw JSON shown read-only.
9. **Export / import**: export stored annotations as JSONL, CSV, or XLSX with all
   identity columns; import per the round-trip requirement above. Resuming a
   half-done dataset (re-open file, already-annotated records shown as done) works.
10. **Session summary**: on finishing, show counts (annotated / skipped / per-mode)
    and offer export in one click.

## Definition of done

- [ ] Every requirement above demonstrated against a real variables set — use the
      `diseases` worked example from `variables-table.md` (8 vars, nested
      list-of-objects) plus a ≥20-record synthetic csv AND xlsx dataset checked
      into `tests/fixtures/` (synthetic only — no real clinical text).
- [ ] Round-trip proven in a test: annotate → export (each format) → import →
      identical `annotation_uid`s, storage row count unchanged.
- [ ] Review-mode delta proven in a test: seed values → corrected values → the
      document's `seed.values_sha256` differs from the final `values` hash and both
      are recoverable.
- [ ] Library pieces (`annotation-def` contract, dataset loader + role inference,
      form plan) live in `packages/extrct` with their own offline tests; this app's
      `ui/` contains no schema walking, no file parsing, no hashing.
- [ ] `data_model.json` matches observed behaviour; `tests/test_handshake.py` still
      passes; app runs from a clean clone with `uv sync && uv run python app.py`.
- [ ] Compose component under `deploy/components/annotate/` behind a profile, port
      on `127.0.0.1`, `docker compose config` clean; `packaging/pack.py` builds a
      desktop app on at least one OS (macOS is the maintainer's target).

## App-specific behaviours

- Optional bools render as three-state (yes / no / not stated); *not stated* saves
  `null`. A required field cannot be skipped silently — skipping the record with a
  reason is the only out.
- Out-of-range numeric entry shows the bound and refuses; it is never clamped
  (an out-of-range value in a dataset is information, not noise).
- Enum matching on import/pre-fill is case-insensitive (mirroring the library's
  `coerce` rung) but saved values are the schema's canonical casing.
- The text panel and the form scroll independently; the current field's description
  is always visible.
- Inference confidence is shown with its evidence ("column `report_text`: mean
  length 1.2k chars → proposed as text"), so the confirm dialog teaches rather
  than interrogates.

## Run

```bash
uv sync
uv run python app.py            # http://127.0.0.1:8080
uv run pytest                   # offline tests, including the handshake checks
```

Desktop build and native preview: see [`packaging/README.md`](packaging/README.md).
Deployment: one compose component folder (`deploy/components/annotate/`), profile-
gated, published on `127.0.0.1` — snippet in the template README.
