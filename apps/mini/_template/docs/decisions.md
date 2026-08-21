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
