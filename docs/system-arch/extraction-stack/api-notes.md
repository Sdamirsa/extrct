# API notes — OpenRouter and Ollama for structured output

*Compiled 2026-08-05 from 6 research agents. Ollama claims were **measured live** against
`qwen3:0.6b` / `qwen3.5:9b-q4_K_M` on Ollama 0.32.5. OpenRouter's public catalogue endpoints were
fetched live (338 models, snapshot 2026-08-05T10:37Z); **authenticated** OpenRouter claims are
documentation-derived — see §11.*

## Framing decisions

| # | Decision | Reason |
|---|---|---|
| **D-a** | **Ollama uses native `POST /api/chat` + `format`, never `/v1`.** `/v1` is a hazard-flagged escape hatch only. | Measured: `/v1` has three silent-disable shapes — `response_format:{type:"json_schema","schema":{…}}` (one level too high), `{"json_schema":{}}`, and `{"type":"grammar"}` each return **HTTP 200 with unconstrained prose** (emitted `PET` against enum `["TTE","CMR"]`). Native `format` never silently drops the constraint — malformed input returns 400/500. `/v1` also cannot reach `options`. |
| **D-b** | **Neither component contains HTTP code.** Both wrap `_core.py` (pure async, no `lfx` import). | The conductor is undecided; welding the call into a component guarantees a second implementation later. See [`concurrency.md`](concurrency.md). |
| **D-c** | **Capability data reaches runtime only as a `value`.** Resolved capabilities are mirrored into a hidden `StrInput(show=False, override_skip=True)` holding JSON. | Langflow re-reads *class-declared* inputs at graph-build time: `self._inputs["model"].options` is `[]` during execution regardless of what was fetched. **Only `value` crosses.** |
| **D-d** | **Never `show=False` on a value-carrying field without `override_skip=True`.** | `show=False` triggers `should_skip_field()`, and `set_attributes()` backfills from the **class default** — measured: a user-set `temperature=1.75` silently became `0.1` after the field was hidden. Gate with `advanced`, `info`, `required`, and option narrowing instead. |
| **D-e** | **Every sampling parameter has a tri-state `auto \| always \| never`, not just a value.** | (1) OpenRouter omits absent params upstream rather than substituting — **omitted ≠ sent-at-default**, and the hash must record which. (2) `require_parameters:true` is evaluated over **every key present**, so always emitting `temperature: 0` excludes every OpenAI/Anthropic reasoning endpoint and the run **fails to route instead of running**. |
| **D-f** | **A client component performs at most one call.** Ladder, retry and batching live in C5 / `call_many`. | Repair must stay a declared, hashed ablation axis. |

## Capability semantics differ, and the contract must not paper over it

| | OpenRouter | Ollama |
|---|---|---|
| Where capability lives | `GET /api/v1/models/{slug}/endpoints` → `endpoints[].supported_parameters`. **Per endpoint.** | Nowhere. `/api/show` `capabilities` is only `completion/vision/tools/thinking` — **no structured-output flag exists.** |
| Model-level list | **UNION over providers** (verified union == catalogue on 12/12 models). `openai/gpt-oss-120b`: 21 advertised, intersection across 19 endpoints = **6**. DigitalOcean, SambaNova and both Bedrock endpoints support neither `response_format` nor `structured_outputs`. | n/a |
| Consequence | **Absence is authoritative** ("can never accept X"). **Presence is not** ("this request will honour X"). | Gate is a no-op; Ollama never errors on an unknown `options` key, so `auto == always`. Recorded as `gate_effective: false` so the two backends' records are not falsely comparable. |
| Enforcement | `strict:true` is a request, never a guarantee — *"some guarantee schema-conforming output, while others treat it as a strong hint"*. | **Measured on qwen3:0.6b.** Enforced: `enum`, `required`, `additionalProperties:false`, nesting, `$defs`/`$ref`. **Not enforced: numeric `minimum`/`maximum`** — `lvef: 250` passed a 0–100 schema. |

`enforcement_verified` is populated by a probe suite, never assumed.

---

## The silent-failure register

This table is the reason the components exist. Every row is a case where the API returns
**HTTP 200** and the result is wrong.

| # | Condition | Backend | Failure | Control |
|---|---|---|---|---|
| S1 | Unsupported param on a non-compliant provider | OR | Docs, verbatim: Claude 4.7 Opus / Sonnet 5 *"no longer accept `temperature`, `top_p`, or `top_k`. If you pass these parameters, they will be silently ignored."* No response field lists what was dropped. | `require_parameters: True` (converts to a 503 routing rejection) + `auto` gating from the **endpoint** list + `resolution.would_be_silently_ignored[]` |
| S2 | Model-level `supported_parameters` used as a capability check | OR | It is the union over providers | Gate reads only `/endpoints`; catalogue is for discovery and **negative** filtering only |
| S3 | `default_parameters` mistaken for a capability map | OR | `openai/gpt-5.4-mini` lists `temperature: null` there while `temperature` is **absent** from `supported_parameters`. Wrong for the whole GPT-5 family. | Never consulted for gating; stored verbatim as a snapshot |
| S4 | Omitting a sampling param | OR | Provider default is inherited, not zero. 73/338 ship non-null defaults | Tri-state emission; `vendor_default_inherited` flag |
| S5 | `allow_fallbacks` left at vendor `true` | OR | Different provider, different quantization, still HTTP 200 | Default `False`; divergence computed from served attribution |
| S6 | Slug suffixes `:nitro` `:floor` `:exacto` | OR | Silently rewrite provider preferences, invisible in the `provider` object | Stripped and recorded separately |
| S7 | `~*-latest` alias / `openrouter/*` router | OR | Target moves; 3 routers have `supported_parameters == []` | Resolution **raises** |
| S8 | Default weighted-random routing | OR | Re-ranks every ~5 min | `provider.only` + `allow_fallbacks:false` mandatory |
| S9 | `strict: true` | OR | *"exact compliance is not guaranteed on every endpoint"* | `enforcement_unverified: true` until probed; client-side validation always runs |
| S10 | `response_format` without `structured_outputs` | OR | **30 models** accept the request shape and fall back to unconstrained JSON | `mode.options` narrowed dynamically; `schema_mode_downgraded` |
| S11 | `response-healing` plugin | OR | Server-side JSON repair, **unlogged** — records `ok` where `repaired` is true | Default `False`; keeps `json_repair` an observable rung |
| S12 | `reasoning` omitted | OR | **69/338 reason by default** | `reasoning_control:"off"` **sends** `{enabled:false}` |
| S15 | Per-choice `error` on HTTP 200 | OR | `finish_reason:"error"` | Checked before success |
| S17 | `?supported_parameters=<typo>` | OR | Not validated — returns 321 models instead of an error | Closed vocabulary constant, never free text |
| S19 | `?zdr=true` on `/models/{slug}/endpoints` | OR | **Silently ignored** — identical list including non-ZDR endpoints | ZDR resolved by set membership in `GET /api/v1/endpoints/zdr` |
| **S20** | **`num_ctx` implicit** | **Ollama** | **Measured: a note beginning `CODEWORD IS ZEBRA` answered `ZEBRA` at default ctx and `CODE` at `num_ctx:512`. HTTP 200 both times; `prompt_eval_count` 3641 → 258. ~93% of input discarded with no signal.** For a clinical note this is a confident extraction from a note the model never saw. | Non-advanced field, explicit default 8192, `/api/ps` verification, `fail_on_context_pressure` default `True` |
| S21 | `done_reason == "length"` | Ollama | HTTP 200 with a structurally invalid **prefix**. The grammar guarantees a well-formed prefix, never a complete document. | `fail_on_truncation` default `True`, gated **before** the repair ladder — otherwise `json_repair` brace-balances a truncated record into a plausible wrong answer |
| S22 | `options.stop` | Ollama | Truncation reports `done_reason: "stop"` — looks like success | Not exposed; hardcoded empty |
| S23 | Modelfile baked defaults | Ollama | `qwen3:0.6b` ships `temperature 0.6 / top_k 20 / top_p 0.95`; `qwen3.5:9b` ships `temperature 1 / presence_penalty 1.5`. **Two models "at default" are not comparable.** | Every sampling key always emitted; displaced defaults stored verbatim |
| S24 | `think` default | Ollama | On by default and **changes the answer** — same seeded prompt returned `modality: "CTA"` with think off, `"TTE"` with think on. With `num_predict: 20` a schema that 400s at 500 instead returns **HTTP 200 with empty content**. | Explicit `"false"`; `think` is a declared ablation axis |
| S25 | `keep_alive` inside `options` | Ollama | Silently does nothing (must be top-level) | Builder places it top-level; nested raises |
| S26 | Numeric `minimum`/`maximum` | Ollama | **Not enforced** — `lvef: 250` passed a 0–100 schema | `enforcement_verified.numeric_range: false`; range checks are `coerce`-layer, client-side |
| S27 | `/v1` malformed `response_format` | Ollama | **Three shapes return HTTP 200 with unconstrained prose** | `/api/chat` is the default |
| S28 | Everything else on `/v1` | Ollama | `options`, `num_ctx`, `keep_alive`, `think`, `format`, `top_k`, `n`, and a literal `totally_made_up_param` all returned 200 and were ignored | Same |
| S29 | Cross-path interference | Ollama | A `/v1` call after a native call that set `num_ctx:256` inherits the smaller window — identical requests get different windows depending only on what ran before | `unload_before_run` for sweeps; `loaded_ctx_before/after` recorded |
| **S30** | **`seed`** | **both** | OR: *"Determinism is not guaranteed for some models"*; seed support is per-endpoint (0/21 Anthropic models declare it). **Ollama: 3-sentence free text at temp 0 with a fixed seed gave 2 distinct outputs in 8 runs.** | `content.raw_sha256` on every attempt; **replay equality is a measured rate, never an assumed invariant** |
| S31 | Langflow `show=False` on a value field | Langflow | Value reverts to class default (measured 1.75 → 0.1) | D-d |
| S32 | Typo'd field name in `update_build_config` | Langflow | **Silent no-op** — `dotdict.__missing__` returns a throwaway dict; `"typo" in build_config` stays `False` | Every write guarded by membership; unit test asserts every mutated name exists |

---

## Langflow dynamic-input contract

`update_build_config` **is `async def`-capable** (`lfx/custom/utils.py:740` awaits coroutines and
runs sync versions in `asyncio.to_thread`), so live HTTP inside it is legitimate. Verified
end-to-end over `POST /api/v1/custom_component/update` against the real Ollama host.

Details that bite:

- **The return value is discarded** (`endpoints.py:1539` calls it without assignment). **Mutate
  in place.** Returning `build_config` is cosmetic.
- **`dotdict.__missing__` returns a throwaway dict.** `build_config["typo"]["show"] = False`
  writes into a temporary and is silently lost. Guard every write with membership.
- Over HTTP `field_name` is never `None` — the `field_name is None` branches copied from in-tree
  components are dead code.
- `options_metadata` is **positionally aligned** with `options`. Never sort one without the other.
- Setting `options = None` on a `list_=True` str field silently turns a multiselect into free
  text. Use `[]`.
- **Raising is the loud channel** — an exception surfaces as HTTP 400 `{"detail": "..."}`. Use it
  for: no model matches the filter, empty endpoint list, alias/meta-router selected, model
  expiring within 30 days, host unreachable.
- Refresh button injects a **top-level** `build_config["is_refresh"] is True` → bypass cache.
- Do **not** copy `a2a_agent.py:383-390`, which reads `self._inputs[…].options_metadata` at run
  time — that always sees `[]`.

---

## Non-vendor defaults, defended

| Knob | Ours | Vendor | Defence |
|---|---|---|---|
| `require_parameters` | `True` | `false` | The only switch turning "silently ignored" into a routing refusal. Its 503 is the **desired** loud failure |
| `allow_fallbacks` | `False` | `true` | Vendor default lets a pinned run execute elsewhere at a different quantization and still return 200 — hostile to replayability |
| `data_collection` | `"deny"` | `"allow"` | ; vendor default permits training on inputs |
| `zdr` | `True` | unset | Retention. Strictly stronger than `deny`, which covers **training only** |
| `send_top_p` | `"never"` | — | Inert at temperature 0, and every extra key invisibly prunes the routing pool |
| `send_temperature` / `send_seed` | `"auto"` | always | Prevents the `require_parameters` trap on **51/338** models that do not declare `temperature` |
| `reasoning_control` | `"off"`, sent explicitly | on for 69/338 | "Not sending `reasoning`" is not a no-reasoning baseline |
| `response_healing` | `False` | off | Keeps `json_repair` an observable rung |
| `retry_on_5xx` | `False` | — | 503 is a **routing rejection**, not a transient error. Blind retry is how you get a silent fallback |
| Ollama `api_path` | `/api/chat` | — | `/v1` has three verified silent-disable shapes |
| Ollama `think` | `"false"` | on | Changes the answer; consumes `num_predict` before the grammar engages |
| Ollama `num_ctx` | `8192`, **non-advanced** | implicit | Silent input truncation is the worst failure in the stack |
| Ollama `temperature`/`top_k`/`top_p` | `0.0` / `0` / `1.0` | 0.6 / 20 / 0.95 baked | Modelfile inheritance makes two models "at default" incomparable |
| Ollama `num_gpu` | `0` | server-chosen | **Host-specific, with an expiry condition**: both models currently fail GPU load (`CUDA error: device kernel image is invalid`, HTTP 500). A default that reliably 500s is worse than a slow one. Flip and re-run the determinism battery once the host is fixed |
| `store_input_text` | `False` | — |  |

---

##  / the egress boundary hazard register — OpenRouter only

> **OpenRouter is outside the wall.** Synthetic or de-identified/aggregate content only, and in
> production it sits behind the  gateway — never called directly from a flow.

| Hazard | Control |
|---|---|
| Note text in `messages` | `data_classification` gate **raises** on empty; `egress_route` defaults `gateway`; `allow_direct_egress` defaults `False`. Two gates on a red line is deliberate |
| Logging the prompt | `store_input_text` default `False`; only `input_sha256` + `input_chars` |
| `user` field | **Not exposed at any level** — ships a stable identifier across the wall |
| `HTTP-Referer` / `X-OpenRouter-Title` | Default `""`; these make the app publicly discoverable on OpenRouter's Apps ranking |
| `data_collection` covers **training only** | *"OpenRouter does not have routing rules that change based on data retention policies of providers"* — hence `zdr` as well |
| `zdr` ORs with **account** settings | Two byte-identical payloads can have different enforcement depending on account state — a pinned body is an **incomplete** audit record. `account_privacy_snapshot` is hashed into the record at send time |
| **Plugins escape ZDR** | *"ZDR enforcement only applies to provider routing… It does not apply to plugins and tools you choose to enable"* |
| OpenRouter's own storage | Input/Output Logging retains *"a minimum of 3 months, and may be retained beyond… at OpenRouter's discretion"*; there is a 1% discount for permitting use of inputs/outputs. **Both must be verified OFF and recorded before any call, even synthetic** |
| No per-provider retention field | `/api/v1/providers` is `{name, slug, privacy_policy_url, terms_of_service_url, status_page_url, headquarters, datacenters}` — `datacenters` was **null for every major provider checked** |
| Unknown policy | *"we take a conservative stance and assume that the endpoint both retains and trains on data"* — excluded by ZDR set membership, not by trust |
| `is_moderated` (83/338) | Can refuse or alter clinical content — a silent confound in a the factorial sweep cell. Recorded per run |

---

## Must be measured before OpenRouter ships

**No OpenRouter API key exists in this environment.** Auth is checked before body validation, so
every authenticated claim is documentation-derived. These are resolvable with **six calls against
a `:free` model**:

| # | Question | Blocks |
|---|---|---|
| **O1** | Does `require_parameters:true` match `response_format:{type:"json_schema"}` against the **`structured_outputs`** capability, or only the literal `response_format` key? Pin an endpoint declaring `response_format` but *not* `structured_outputs`, send a prose-demanding prompt. **404 → it protects us. 200 with prose → it does not, and we must gate ourselves.** | Whether `require_parameters` can be trusted alone |
| **O2** | Does OpenRouter **strip** unsupported params or forward them? OpenAI 400s on `temperature` for o-series; forwarded means a hard error, not a silent ignore. `debug:{echo_upstream_body:true}` answers it. | Whether `always` is ever safe |
| **O3** | Does `response_format: json_schema` actually **constrain** decoding per endpoint, or merely prompt for JSON? | Whether ladder rungs 2–5 are needed at all |
| O7 | Is `canonical_slug` accepted as the request `model` value? Currently assumed. | replayability pinning strategy |
| O8/O9 | Does `GET /api/v1/generation` expose the endpoint **tag**, not just a display name? Is `X-Provider-Name` a slug or a display name? Insufficient attribution cannot distinguish `google-vertex` from `google-vertex/us-central1` — which report **different** `structured_outputs`. | Served-endpoint fidelity |
| O10 | Endpoint `status` vocabulary: 0 (627), −2 (63), −5 (18) observed across 708 endpoints. Undocumented; unknown whether negative excludes or deprioritises. | Severity of `endpoint_status_negative` |

Ollama-side, still open: enforcement of `type: ["number","null"]` (the optional-field union our
schema builder's `required` column depends on), `minItems`/`maxItems`, `pattern`, `oneOf`/`anyOf`,
recursive `$ref`; and whether tail non-determinism at temp 0 is thread-count related
(`options.num_thread`, N=32).

## Langflow API Request cannot send these calls - measured, do not retry

The split "ExtrCT prepares -> Langflow API Request sends" was built and then removed
(components `request_builder.py` / `response_parser.py`, deleted 2026-08-06; recoverable
from git). Two blocking facts, both established by test against Langflow 1.11:

- `curl_input` is parsed inside `update_build_config`, i.e. only when a human PASTES into
  the field. A *connected* cURL is never parsed, so `url_input` stays empty and the build
  fails with "URL cannot be empty".
- `body` is a TableInput. It rejects a JSON string outright and coerces a dict into a
  one-row table, silently dropping `messages` - the provider answers 400 "Input required:
  specify prompt or messages". `_process_body()` looks like it would handle nesting, but
  `make_api_request` reads `self.body` raw, after TableInput has already flattened it.

A nested `response_format` carrying a JSON Schema cannot survive that component. Sending
stays with ExtrCT Structured Extract.

## The 401 register - exact wording per failure mode (measured 2026-08-06)

| Authorization header | OpenRouter answer |
|---|---|
| absent | 401 `No cookie auth credentials found` |
| `Bearer ` (empty token) | never leaves the machine - httpx raises `LocalProtocolError: Illegal header value` |
| `Bearer sk-or-v1-<garbage>` | 401 `User not found.` |
| `Bearer OPENROUTER_API_KEY` (a variable NAME as the token) | 401 `Missing Authentication header` |
| non-Bearer scheme (`Basic ...`, bare token) | 401 `Missing Authentication header` |

So "Missing Authentication header" means the header VALUE did not parse as Bearer
credentials - in practice, a node sent a placeholder or variable name as the key. It does
NOT mean the key is wrong (`User not found.`) or the header absent (`No cookie auth
credentials found`).

Design consequence (2026-08-06): the key is resolved from the container environment at SEND
time inside `extrct.openrouter.resolve_key()` - the single choke point every caller
shares. The component carries only the env var NAME (`api_key_env_var`, default
`OPENROUTER_API_KEY`); the secret never rides the canvas as node data and never enters a
flow export. A literal `sk-` key on the spec still wins, for scripted use outside Langflow;
anything else falls through to the environment, which heals stale frozen nodes at the
moment of sending.

## GPU host serving path: format is silently ignored (measured 2026-08-06)

On a dedicated GPU host (Ollama 0.30.0, direct link, GPU-resident confirmed via /api/ps
size_vram == size), BOTH `format: <json schema>` and `format: "json"` are SILENTLY
IGNORED for `qwen3.6:27b-mtp-q8_0` and `qwen3.5:35b-a3b-q8_0` - HTTP 200 with markdown
prose, or JSON in an invented shape. The same request pattern enforced correctly on the
laptop against the `qwen3` family. Working hypothesis: these `qwen35`/`qwen35moe`
families run on Ollama''s newer engine, which does not wire grammar-constrained decoding;
the laptop family runs the llama.cpp path, which does. Family-dependent, engine-level,
invisible at the HTTP layer: the client-side validator is the ONLY thing that caught it.

Measured mitigation until an Ollama upgrade fixes the engine: `mode="prompted"` plus the
repair ladder. qwen3.6:27b -> ok (clean JSON, no repair); qwen3.5:35b -> repaired
(json_repair stripped fences). Numeric/enum constraints are of course unenforced by a
prompt, so the validator and reprompt rungs are load-bearing on this path.

Fixed while measuring this: `mode="prompted"` previously sent the schema NOWHERE unless
the separate `schema_in_prompt` flag was also on - the mode now implies it, in both the
Ollama and OpenRouter builders.

Also changed for the GPU host: `num_gpu` default is now -1 = AUTO (key omitted, server
offloads to GPU when it can). The old always-emitted 0 would have silently forced a 27B
model onto CPU on the GPU host. 0 remains an explicit choice for the laptop''s broken CUDA.

## Logprobs (measured 2026-08-06, live on both backends)

Both backends can return per-token log-probabilities with top-N alternatives; both clients
now expose `Token Logprobs` + `Top Logprobs` (0-20), and Structured Extract has a
`Logprobs` output (normalised across providers: {token, logprob, prob, top}). The verbatim
provider shape always lands in `extraction_run.provider_response`.

Wire facts, all measured:

- **Ollama native /api/chat**: TOP-LEVEL `"logprobs": true` + `"top_logprobs": N`. An int
  `logprobs: 5` is HTTP 400; either key inside `options` is SILENTLY IGNORED. Response
  carries a top-level `logprobs` list.
- **MTP models return logprobs for the FIRST token only**: qwen3.6:27b-mtp gave 1 entry
  for 50 generated tokens; qwen3.5:35b (non-MTP) gave 35/36. Multi-token prediction and
  per-token logprobs do not mix - use a non-MTP model when logprobs matter.
- **OpenRouter**: OpenAI shape (`choices[0].logprobs.content[]`), ENDPOINT-dependent:
  7 of 9 gemma-4-26b endpoints declare logprobs, but NOT deepinfra/fp8 (our usual pin) or
  siliconflow/fp8. The Endpoints output has a logprobs column; build_client raises on a
  conflicting pin; require_parameters turns the residual into a routing refusal.
- **Interpretation caveat**: under strict json_schema the probabilities are conditioned on
  the grammar mask ("confident among the legal options"). On the GPU host prompted path they
  are unconstrained model probabilities - the cleaner per-field confidence signal.

## Correction (measured 2026-08-10): the GPU host format trap is FAMILY-specific

gemma4:31b-it-q8_0 on the GPU host HONORS `format` fully in json_schema mode - plain schema
returned exactly the schema keys; the evidence-augmented schema returned `_evidence` too.
The silently-ignored-format behaviour is confined to the qwen3.5/qwen3.6 (new-engine)
families as originally measured. Rule update: per-MODEL verification, not per-host - one
tiny json_schema probe against the exact model before trusting either mode. Credit: the
user challenged the earlier host-level generalisation and was right.

## Langflow engine defect (measured 2026-08-10): multi-output -> one list input collapses

ComponentVertex._get_result resolves a pulled value by scanning edges to the requester and
BREAKING at the first match on (source, target field) - so several edges from ONE node
into the SAME list input all deliver the first edge''s output. Caught live: three XAI
outputs (clean_values, grounding_table, grounding_report) wired into Prep - Message all
rendered as clean_values. Component-side immunisation shipped: Prep - Message re-resolves
each payload from its edge''s declared output via the source vertex''s results, in edge
order (titles and contents provably aligned); falls back to engine delivery on any miss.
Any future component with a list input consuming multiple outputs of one node needs the
same pattern. Candidate for an upstream Langflow issue.
