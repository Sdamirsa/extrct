# TODO

What is open right now. Longer-lived design rationale lives in
[`docs/system-arch/extraction-stack/`](docs/system-arch/extraction-stack/); what happened when lives in
[`docs/system-arch/log/`](docs/system-arch/log/).

## Next up

- [ ] **Publish `extrct` 0.1.0 to PyPI.** Trusted Publishing is fully linked as of
      2026-08-20 (pending publisher on PyPI + the `pypi` GitHub environment,
      restricted to `v*` tags, no stored secrets). Remaining: green CI on main, a
      release dry run (Actions → release → Run workflow), then the `v0.1.0` tag.
      Afterwards, drop the `git+https` install fallback from both READMEs.
- [ ] **Model registry: finish the baseline.** `extrct.models` (`model-registry/1.0`):
      ollama/gemma4:31b-it-q4_K_M measured 5/5 (2026-08-21, `examples/08_model_probe.py
      --long`). Remaining: the openrouter row (needs `OPENROUTER_API_KEY`), other
      quants if wanted, and vLLM/Cerebras/Fireworks rows as their providers land.
- [ ] **Migrate the Langflow components onto the published library.** They currently
      import the legacy API (`flow_model`, `call_model`, module-level `storage`), which
      the published package reorganised into a provider registry, `xai.*`, and
      class-based storage. Port or shim, re-verify every bundle in-container, then
      retire `integrations/langflow/legacy_extrct/`.
- [ ] **vLLM provider** — input/prompt logprobs and post-mask logprob semantics. The
      registry, the `prompt_logprobs` capability flag, and the sentinel handling in
      `xai.certainty` already anticipate it; see `packages/extrct/docs/providers.md`.
- [ ] **`apps/`** — the workbench: a def-editor first, then a run-monitor over the run
      log, then a canvas that *renders* def documents (never a freeform graph editor).
      Mini apps (NiceGUI) are the proving lane — design:
      [docs/system-arch/workbench/mini-apps.md](docs/system-arch/workbench/mini-apps.md);
      scaffold: [apps/mini/_template/](apps/mini/_template/).
- [ ] **Build `annotate` + `evaluate`** — handshakes are complete and outsourceable
      ([apps/mini/annotate/](apps/mini/annotate/), [apps/mini/evaluate/](apps/mini/evaluate/)).
      Library prerequisites first (in `packages/extrct`, each with offline tests):
      `annotation-def/1.0` + `evaluation-def/1.0` + `rubric-def/1.0` contracts and
      their storage tables; dataset loader (csv/tsv/xlsx/json/jsonl) with
      column-role inference (text/id/llm_output, infer-then-confirm); the
      schema→form-plan module (field descriptors the apps render). Then the two
      UIs per their READMEs; first desktop build on macOS.
- [ ] **Repo-wide licensing.** `packages/extrct/` is Apache-2.0 and the root LICENSE
      matches; confirm the intended license for the docs and deploy configs.

## Backlog

- Cerebras and Fireworks providers, after vLLM proves the registry's second and third
  backends.
- Post-hoc grounding inside the pipeline. `grounding.mode=posthoc` is currently
  recorded as a skipped step with that reason; the standalone lane already works via
  `xai.grounding` + `schema.evidence_schema`.
- A model-gateway component for `deploy/`: one OpenAI-compatible router in front of
  local and hosted backends, doubling as the egress filter.
- Postgres integration tests in CI (needs a service container; the SQLite backend
  covers the shared semantics today).
