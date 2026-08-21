# Decisions — evaluate

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

## 2026-08-21 — evaluate is its own app, not a mode of annotate
**Why:** the output contract names the app. Annotation (producing/correcting
values → annotation-def/1.0) and evaluation (judging values without changing them
→ evaluation-def/1.0) are different claims with different documents; folding them
into one app invites the "fix it while judging" edit that corrupts evaluations.
Everything they share (dataset loading, role inference, form plan, storage) lives
in packages/extrct, so the second app costs only its UI.
**Evidence:** design doc lane rules; ../annotate/docs/decisions.md 2026-08-21.

## 2026-08-21 — rubric is an authored contract (rubric-def/1.0), never hardcoded
**Why:** criteria change per study; hardcoded criteria would make evaluations
incomparable and unreproducible. As data with a content uid, the rubric rides every
evaluation (rubric_uid), mixed-rubric sessions are refusable, and identical rubrics
hash identically across machines — the house pattern (client-def, job-def).
**Evidence:** repo convention "contracts are versioned, uids are content hashes"
(root CLAUDE.md); rubric-def/1.0 schema in data_model.json.

## 2026-08-21 — v1 statistics stop at counts, means, and simple agreement rates
**Why:** kappa and richer inter-rater statistics need design (who counts as a
rater, per-field vs per-record units) and belong in the library, computed from
stored documents — not improvised in an app's export path.
**Evidence:** lane rule "zero analysis logic in apps beyond projections"; open item
recorded in TODO via the workbench run-monitor lane.

## 2026-08-21 — uv.lock deferred until the build starts
**Why:** as ../annotate/ (2026-08-21): no final dependency set at handshake stage;
the lock is the first act of the build.
**Evidence:** unit-keeper audit 2026-08-21; template README Layout row.
