# Contributing

Small library, strong opinions. Read `CLAUDE.md` (the standards table and red lines)
first — PRs are reviewed against it.

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest                      # must pass offline: no model, no key, no network
python examples/00_offline_demo.py # the end-to-end smoke
```

## What a good PR looks like

- **Behaviour lands with its test, in the same change.** The suite is
  behaviour-freezing: if your change breaks a test, either the change is wrong or the
  frozen behaviour is — say which, in the PR.
- **Measured claims only.** A provider behaviour you encode ("X is silently ignored",
  "Y returns 200 on failure") must state how it was measured (version + date) in the
  module docstring. Doc links are not measurements.
- **Never** put credentials in documents/configs/hashes, clamp ranges in repair, edit
  run-log evidence, or add a provider as an if/elif (use the registry — see
  docs/providers.md for the checklist).
- Contracts (`*-def/X.Y`) bump on meaning changes; readers keep accepting old
  versions.
- No real/private text in tests or fixtures — synthetic reproductions only.

## Scope

Providers behind the registry contract, storage backends behind `RunStore`, repair
rungs, chunking logics, and fixes with regression tests are all welcome. New
top-level concepts (a new pipeline step, a new contract) deserve an issue first.

## License

Apache-2.0. By contributing you agree your contribution is licensed under the same
terms.
