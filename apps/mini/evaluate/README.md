# evaluate — rubric-based human evaluation of LLM extractions

*This README is the app's **handshake**: scope, boundaries, and definition of done,
agreed before code. [`data_model.json`](data_model.json) is its machine-readable
half — inputs and outputs as typed contracts. The two must not drift: change them in
the same commit or not at all. Written to be **outsourceable**, and deliberately a
sibling of [`../annotate/`](../annotate/README.md): schema intake, dataset intake,
column-role inference, navigation, identity stamping, import/export, and the
library-not-app rule are identical there — this file only restates what differs.
The shared machinery lives in `packages/extrct`, which is exactly why two apps stay
cheap.*

Authorities: [`.claude/habits.md`](../../../.claude/habits.md) ·
[`docs/system-arch/workbench/mini-apps.md`](../../../docs/system-arch/workbench/mini-apps.md) ·
[`docs/system-arch/extraction-stack/variables-table.md`](../../../docs/system-arch/extraction-stack/variables-table.md) ·
[`../annotate/README.md`](../annotate/README.md) (the sibling handshake).

## Layout

Standard template layout; nothing structural added.

## Scope

A single-user, localhost NiceGUI app in which a person **judges** LLM extractions
without changing them: it loads a variables set, records, and the LLM output for
each record (plus, optionally, a reference human annotation), renders source text /
LLM values / reference side by side, and captures per-field verdicts and
rubric scores as an `evaluation-def/1.0` document through library storage. It
starts when schema + dataset + LLM output are confirmed; it ends when every record
is evaluated or skipped and results are exported. Correcting values is out of scope
— that is `../annotate/` review mode.

## Boundaries

All of `../annotate/`'s boundaries apply verbatim (library renders / app displays;
artifact-of-record output; registry-and-run-log only; localhost; **no LLM calls
ever** — rubric evaluation here is human judgment, not LLM-as-judge, which belongs
to the library's XAI lane if it ever lands). Plus:

- **The evaluated values are immutable in this app.** No edit affordance exists on
  LLM output — a tempting "fix it while I'm here" click is the road to corrupted
  evaluations; the UI offers "open in annotate" guidance instead.
- **The rubric is data, not code**: an authored `rubric-def/1.0` JSON document,
  content-hashed to `rubric_uid` — never hardcoded criteria. A default rubric ships
  as a fixture, not as behaviour.

## Input / output

The authoritative declaration is [`data_model.json`](data_model.json). Summary:

| Direction | Name | Contract / format | Identified by |
|---|---|---|---|
| in | variables_set | as annotate | `schema_uid` |
| in | dataset | as annotate (csv, tsv, xlsx, json, jsonl) | `input_uid` + text sha256 |
| in | llm_output | JSON values per record (column or joined file) — **required** | `values_sha256`, optional `run_uid` |
| in | reference | optional: `annotation-def/1.0` documents (gold), joined by input id/hash | `annotation_uid` |
| in | rubric | `rubric-def/1.0` authored JSON (schema in data_model.json) | `rubric_uid` |
| out | evaluation | `evaluation-def/1.0` | `evaluation_uid` |
| out | export | JSONL / CSV / XLSX projection incl. agreement summary | — (projection) |

Identity stamping mirrors annotate: `evaluator` handle (pseudonymous, required),
`schema_uid`, `input_text_sha256` (+ `input_uid` when present), plus `rubric_uid`
and the immutable `target` (what was judged: `values_sha256`, optional `run_uid`).
Same import/export round-trip requirement as annotate.

## Requirements

1. **Intake**: schema, dataset, and LLM output per annotate's requirements 1–3
   (inference proposes the LLM-output column by schema-key overlap; confirm-first).
   Optional reference annotations join by `input_uid` or `input_text_sha256`; join
   misses are listed loudly, not dropped.
2. **Rubric intake**: load a `rubric-def/1.0` file; show its name, criteria, and
   `rubric_uid` before starting. Ship one default rubric fixture (faithfulness /
   completeness / hallucination, 1–5 int scales + a per-field verdict set) that the
   user can copy and edit as data.
3. **Three-pane rendering**: source text | LLM values (read-only, schema-shaped) |
   reference values when present, aligned field by field; fields where LLM and
   reference disagree are pre-highlighted (string-exact after the library's
   case-insensitive enum normalisation — a *hint*, never a verdict).
4. **Per-field verdicts**: one-keystroke verdict per leaf field from the rubric's
   verdict set (default: correct / minor_error / major_error / missing / spurious /
   not_evaluable), optional note per field. List-of-objects fields are judged
   per row.
5. **Per-record rubric scores**: each record-level criterion rendered per its scale
   (enum → select, int range → bounded input); optional overall note.
6. **Navigation, drafts, skip-with-reason, keyboard-first**: as annotate
   requirements 5–6.
7. **Submission**: `evaluation-def/1.0` validated, stamped, written through library
   storage; idempotent on re-submit.
8. **Export / import**: round-trip as annotate requirement 9, plus a summary sheet:
   verdict counts per field, mean rubric scores, and — when references exist —
   simple agreement rates (share of fields judged `correct` vs reference-match
   hint). No statistics beyond counts and means in v1 (kappa etc. is library work
   later, from the stored documents).
9. **Session summary**: counts per verdict, mean scores, one-click export.

## Definition of done

- [ ] Every requirement demonstrated on the `diseases` worked example with a
      ≥20-record synthetic dataset, a synthetic LLM-output file (including at least
      2 records that fail schema validation and 3 join misses), and a reference
      file for half the records — all in `tests/fixtures/`, synthetic only.
- [ ] Round-trip test as annotate; plus: evaluating the same target twice yields
      the same `evaluation_uid` (idempotent), and evaluating a *different* target
      (one changed value) yields a different one.
- [ ] The rubric fixture validates against the `rubric-def/1.0` schema in
      `data_model.json`; a rubric with an unknown scale type is refused loudly.
- [ ] Shared library pieces are imported from `packages/extrct` (no copy-paste from
      `../annotate/`); anything found duplicated is pushed down before done.
- [ ] Handshake tests pass; clean-clone run works; compose component under
      `deploy/components/evaluate/` verified; desktop build on one OS.

## App-specific behaviours

- The disagreement pre-highlight is visually distinct from a human verdict and
  never pre-fills one — the evaluator always chooses.
- A record whose LLM output fails schema validation is still evaluable: verdicts
  apply to the raw JSON rendered read-only, and the document records
  `target.valid: false`.
- `not_evaluable` requires a note; `spurious` is offered for fields present in the
  LLM output but absent from the schema.
- Rubric edits between records are refused — a changed rubric is a new `rubric_uid`
  and a new session (mixed-rubric result sets are not comparable).

## Run

```bash
uv sync
uv run python app.py            # http://127.0.0.1:8080
uv run pytest
```

Desktop build: [`packaging/README.md`](packaging/README.md). Deployment:
`deploy/components/evaluate/`, profile-gated, published on `127.0.0.1`.
