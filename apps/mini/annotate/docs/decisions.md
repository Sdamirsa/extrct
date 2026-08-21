# Decisions — <app-verb>

Append-only: one dated entry per decision that shaped this unit — corrections are
new entries linking the date they correct, never edits. Repo-wide decisions live in
`docs/system-arch/`; this file holds what is specific to this app. Entries cite the
research note or measurement behind them (`docs/research/`).

Format:

```markdown
## YYYY-MM-DD — <the decision, one line>
**Why:** <the reason that would otherwise be lost>
**Evidence:** <link to docs/research/YYYY-MM-DD-<topic>.md, a measurement, or an issue>
```

## 2026-08-21 — two modes (blind, review) in one app, one output contract
**Why:** correcting an LLM extraction still produces a human-authored annotation;
the claim type is identical, only the starting point differs. One contract
(`annotation-def/1.0`) with a recorded `seed` keeps human-vs-LLM deltas computable
without a second document type. Evaluation (judging an extraction without changing
it) is a different claim and is a separate app (`../evaluate/`).
**Evidence:** lane rule "the output contract names the app" — design doc
(`docs/system-arch/workbench/mini-apps.md`); seed/values_sha256 mechanism in
`data_model.json`.

## 2026-08-21 — column roles are inferred, then confirmed; never silently guessed
**Why:** "outsmart" inference (longest strings → text, unique short values → id,
schema-keyed JSON → llm_output) removes setup friction, but a wrong silent guess
poisons every downstream uid. Infer-then-confirm keeps the ease without the risk,
and remembering the confirmed mapping per dataset shape makes the second file
zero-question.
**Evidence:** repo habit — loud failure over silent repair (root CLAUDE.md red
lines; variables-table.md "never clamps" stance).

## 2026-08-21 — non-UI logic lands in packages/extrct, not in this app
**Why:** dataset loading, role inference, the schema→field form plan, the
annotation-def contract, and storage are library concerns (testable offline,
reusable by ../evaluate/ without app-to-app coupling). This app's ui/ maps form-plan
entries to NiceGUI widgets and nothing else.
**Evidence:** lane rule "zero extraction or schema logic in an app"; hub-and-spoke
(no shared app code) — design doc.
