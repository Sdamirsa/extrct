# <app-verb> — <one-line task statement>

*This README is the app's **handshake**: scope, boundaries, and definition of done,
agreed before code. [`data_model.json`](data_model.json) is its machine-readable
half — inputs and outputs as typed contracts. The two must not drift: change them in
the same commit or not at all.*

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
