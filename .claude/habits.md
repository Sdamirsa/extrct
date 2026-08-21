# Habits — the standalone-unit standard

*How every meaningful unit in this monorepo is shaped, so each can be understood,
run, and audited separately. Authority for unit structure; the root CLAUDE.md and
red lines stay the authority for behaviour. The `unit-keeper` agent
(`.claude/agents/unit-keeper.md`) scaffolds and audits against this file.*

## What is a unit

Any folder that can be **served or shipped on its own**: the library
(`packages/extrct/`), each mini app (`apps/mini/<verb>/`), each future workbench app
(`apps/workbench/`), the Langflow integration (`integrations/langflow/`). Docs
folders, deploy component folders, and scratch are not units.

Existing units are grandfathered — retrofit them with `unit-keeper` when they are
next touched, not in one sweep.

## The shape every unit has

| Piece | What it holds | Required |
|---|---|---|
| `README.md` | the eight standard sections (below) — the unit's handshake | always |
| `docs/` | the unit's own knowledge: `research/` (dated notes) + `decisions.md` (append-only) + architecture notes as needed | always |
| `tests/` | offline-runnable tests; at minimum the structure/handshake checks | always |
| `data_model.json` | machine-readable I/O (`app-io/1.0` style) where the unit has a typed in/out handshake | apps: yes; others: where applicable |
| `pyproject.toml` | **mini apps and the library ONLY.** No other unit gets one — a unit is standalone by its README and docs, not by being a Python project | restricted |
| `packaging/` | desktop-build pipeline (NiceGUI apps only, see below) | NiceGUI apps |

## The eight README sections

Every unit README carries exactly these sections, in this order — `Layout`,
`Scope`, `Boundaries`, `Input / output`, `Requirements`, `Definition of done`,
`Specific behaviours` (apps title it "App-specific behaviours"), `Run`. For a
library, `Run` covers running the tests and building; for an app, running the app.
A unit is **standalone**: its README must be readable in isolation, restating
inherited rules briefly and linking out only to the document that owns a detail.

## Research is stored, not spent

Research that informs a decision is written to the unit's
`docs/research/YYYY-MM-DD-<topic>.md` before the decision lands: verified claims
with source + version + date, unverified claims flagged as such (the repo habit —
measure, don't trust docs). Repo-wide research goes to
`docs/system-arch/extraction-stack/` as before; per-unit research stays with the
unit so the unit explains itself.

## Every unit keeps a decision log

`docs/decisions.md`, append-only: one dated entry per decision that shaped the
unit — the decision in one line, the why, and a link to the research note or
measurement behind it. Corrections are new entries linking the date they correct.
Repo-wide decisions stay in `docs/system-arch/`; the unit log holds what is
specific to the unit. The repo work log (`docs/system-arch/log/`) still records
*when* things happened; the unit decision log records *why this unit is the way it
is*.

## Desktop pipeline (NiceGUI apps)

Every NiceGUI app carries `packaging/` — `pack.py` wrapping `nicegui-pack`
(PyInstaller) plus a README. Two facts the pipeline is built on (measured on
nicegui 3.16.0, 2026-08-21): PyInstaller does **not cross-compile** — the macOS
app is built on a Mac, the Windows exe on Windows; and a packaged app must run
with `reload=False` and pick a free port (`native.find_open_port()`), which the
template's `app.py` does automatically when frozen. Build artifacts (`dist/`,
`build/`, `*.spec`) never enter git.
