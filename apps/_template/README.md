# <app-verb> — <one-line task statement>

*This README is the app's **handshake**: scope, boundaries, and definition of done,
agreed before code. [`data_model.json`](data_model.json) is its machine-readable
half — inputs and outputs as typed contracts. The two must not drift: change them in
the same commit or not at all.*

## Layout

Standalone by construction — the folder carries everything it needs to run, test,
and containerise, and nothing else:

| Path | Purpose |
|---|---|
| `README.md` | this handshake — the prose half |
| `data_model.json` | the machine-readable half (`app-io/1.0`) |
| `app.py` | entry point (`streamlit run app.py`) — wiring only |
| `ui/` | the app's own modules, all UI-shaped; the single place that talks to `extrct` |
| `tests/` | offline tests; `test_handshake.py` keeps this README and `data_model.json` well-formed |
| `.streamlit/config.toml` | binds `127.0.0.1`, telemetry off — do not weaken |
| `pyproject.toml` + `uv.lock` | standalone uv project; lock committed once the app is real |
| `Dockerfile` | container build for the compose component (buildable once `extrct` is on PyPI — see its header) |
| `.gitignore` | `.venv`, caches, and `.streamlit/secrets.toml` — credentials never enter the repo |

Deliberately absent: `data/`, `output/`, `results/`. An app's output is an artifact
of record in library storage, never files in the app folder. A `pages/` folder
(Streamlit multipage) is allowed but suspect — one task, one screen; wanting pages
is usually wanting a second app.

## Scope

One paragraph: the single task this app performs, for whom, and the moment it starts
and ends. If the paragraph needs the word "and" more than once, it is probably two
apps.

## Boundaries

Inherited from the lane ([design of record](../../docs/system-arch/workbench/mini-apps.md)
— restated here so this file stands alone):

- Zero extraction or schema logic here — the library builds and validates every
  document; this app renders.
- Output is an artifact of record: the contract named in `data_model.json`, content
  uid stamped, written through library storage — never this app's own files.
- Talks only to the registry and the run log. Never to another app, never to Langflow.
- Localhost-only (`127.0.0.1`), single user, no auth. Needing more is the graduation
  signal, not a feature request.

App-specific boundaries (what this app must *never* do, beyond the lane rules):

- <e.g. "never clamps an out-of-range value — refuses it at entry">
- <…>

## Input / output

The authoritative declaration is [`data_model.json`](data_model.json). Summary:

| Direction | Name | Contract | Identified by |
|---|---|---|---|
| in | <variables_set> | <schema variables, from the registry> | <schema_uid> |
| out | <annotation> | <annotation-def/1.0> | <annotation_uid> |

## Requirements

- [ ] <observable behaviour 1>
- [ ] <observable behaviour 2>
- [ ] <…>

## Definition of done

- [ ] Every requirement above demonstrated against a real variables set (name it here).
- [ ] The output document validates through the library's own validator — the same
      one model output goes through — and lands in storage with a stable uid
      (submitting twice yields the same uid, one row).
- [ ] `data_model.json` matches observed behaviour (an agent can read it and predict
      the app's I/O without opening `app.py`).
- [ ] Compose component added under `deploy/components/<app-verb>/`, behind a
      profile, port on `127.0.0.1`, verified with `docker compose config`.
- [ ] `uv.lock` committed; `uv run streamlit run app.py` works from a clean clone.

## App-specific behaviours

Behaviours particular to this app that a reviewer or agent would not guess from the
lane rules. For the first app (`annotate`) this section holds, e.g.: three-state
widgets for optional bools under `strict_nullable` (a checkbox cannot distinguish
`false` from *not stated*); bounds enforced at entry, never clamped; source text
referenced by hash, stored only per the library's opt-in policy.

- <…>

## Run

```bash
uv sync
uv run streamlit run app.py     # http://127.0.0.1:8501
uv run pytest                   # offline tests, including the handshake checks
```

Deployment — one compose component folder, the existing `deploy/` pattern:

```yaml
# deploy/components/<app-verb>/compose.yaml
services:
  <app-verb>:
    build: ../../../apps/<app-verb>
    ports:
      - "127.0.0.1:8601:8501"   # next free 86xx port
    profiles: [apps]
```
