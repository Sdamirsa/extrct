# Logprobs design — from token probabilities to per-field confidence

Status: DESIGN, 2026-08-06. The transport layer is built and verified (both clients
request, both readers capture, `provider_response` persists verbatim, Structured Extract
exposes a normalised `Logprobs` output). This note designs what comes next: turning
per-token log-probabilities into a **per-field confidence signal** that analysis can
group, calibrate, and eventually act on.

## 1. Measured foundation (2026-08-06, see api-notes.md)

- Both backends return, per generated token: `token`, `logprob`, `bytes`, and up to 20
  `top_logprobs` alternatives. The `bytes` array is the keystone: cumulative byte offsets
  give an **exact, tokenizer-independent alignment** from tokens to UTF-8 positions in the
  raw output text. No fuzzy matching, ever.
- Ollama: top-level bool + count; MTP models emit logprobs for the first token only
  (unusable — excluded by rule). OpenRouter: per-endpoint capability, gated loudly.
- Under strict `json_schema` the distribution is conditioned on the grammar mask; under
  the GPU host `prompted` path it is the model's unconstrained distribution.

## 2. What the signal is for — three uses, in priority order

1. **Error analysis (current work).** Do wrong extractions carry lower confidence? Per cell of
   the factorial: confidence vs correctness against ground truth. This only needs storage
   and grouping — no thresholds, no product decisions.
2. **Calibration measurement (eval-side).** Reliability diagrams and risk–coverage curves
   per model × mode × encoding. Lives in `extrct-eval`, reads the DB, never the agent.
3. **Selective prediction (later, clinical).** Route low-confidence fields to human
   review (Label Studio). Needs 1 and 2 to be trustworthy first. Explicitly out of scope
   in this phase; noted so the schema anticipates it.

## 3. The core problem: token → field attribution

Logprobs cover the whole emission; confidence must attach to **fields**. Design:

**Byte-offset alignment.** Concatenating each token's `bytes` reconstructs the raw output
byte stream. A position-aware JSON scan of `raw_text` yields, for every field (by JSON
path, e.g. `$.lvef`), the byte span of its **value**. The tokens whose byte ranges
intersect that span are the field's value tokens. Structural tokens (braces, quotes,
keys, commas) attach to no field — under a schema they are near-deterministic and would
only dilute the signal.

Properties worth stating:

- Exact for any UTF-8 content including escapes and multi-byte clinical text — bytes, not
  characters, are the unit.
- Tokenizer-independent: works identically for any model on either backend.
- Computed on the **raw first emission**, before the repair ladder. If `json_repair` or
  `coerce` changed the value, the confidence describes what the model said, not what the
  ladder made of it — `final_status` already separates those worlds, and the pairing
  (repaired value, raw-emission confidence) is itself analytically interesting.
- Attempts beyond the first (reprompt / llm_repair) carry no logprobs in this phase.

## 4. Aggregation per field — store all three, pick primaries later

For a field's value tokens with logprobs `l1..lk`:

| Statistic | Meaning | Failure mode it exposes |
|---|---|---|
| `joint` = exp(Σ li) | probability of the exact value string | long values decay by construction |
| `mean` = exp(Σ li / k) | length-normalised per-token confidence | comparable across value lengths |
| `min` = exp(min li) | the weakest token | one hesitant token inside a confident span |

Plus `k` (token count) so any other statistic is re-derivable. Recommendation: `joint`
as the primary for calibration (it is an actual probability of the emitted value), `min`
as the flag for review-routing later; decide after seeing real distributions in use 1.

## 5. Enum fields get a stronger signal: the option posterior

For a field whose schema declares `enum: [TTE, CTA, CMR]`, the first value token's
`top_logprobs` contains the head-to-head comparison the model actually made. Mapping each
option to its first token (per the emission's own tokenisation) and renormalising over
the options that appear yields a **posterior over the clinical option set** — a much
sharper signal than string-level confidence, and under a grammar mask it is exactly the
posterior over legal options. Truncation caveat: an option absent from top-20 gets a
bound (< p20), not a value; record `observed_options` so the truncation is visible.
The schema envelope already carries enum options through to the extract, so this needs no
new wiring — only the variable's options list at computation time.

## 6. Where the computation lives (replayability/independent measurement/the swap test)

One pure function in `extrct`:

```
field_confidence(raw_text: str, logprobs: list, schema: dict) -> dict
# {"$.lvef": {"value_span": [12, 14], "k": 2, "joint": .., "mean": .., "min": ..,
#             "enum_posterior": {...} | None}, ...}
```

Two callers, one implementation (the swap-test rule):

- **Run time**: Structured Extract computes it when logprobs are present — appended to
  the `Logprobs` output and persisted (§7). Convenience, not authority.
- **Re-derivation**: anything (eval, a notebook, a future component) can recompute it
  from `provider_response` + the schema, both already in the run row. The stored value is
  a deterministic function of stored evidence — replayability-clean, and the eval side never needs
  the agent process.

## 7. Storage

New column `extraction_run.field_logprobs JSONB` (the function's output, NULL when
logprobs were off), added via the established `ADD COLUMN IF NOT EXISTS` migration path.
Registry gains a read op `field_confidence` (run_uid → the per-field table). SQL-side
analysis then groups confidence by every axis the row already carries (model, encoding,
mode, endpoint, tags) without JSON crunching in Python.

## 8. Interpretation rules the analysis must respect

- **Never compare across output modes.** Grammar-masked (`json_schema` strict) and
  unconstrained (`prompted`) probabilities answer different questions. `request_mode` is
  already a column; calibration is per-mode by construction.
- **MTP models are excluded** (first-token-only, measured). The register carries this.
- **Cross-model comparisons are legitimate at the probability level** (probabilities are
  model-agnostic), but only within the same mode and with length effects handled (`mean`
  or per-field-type analysis).
- **top_logprobs truncation** bounds, never estimates, the tail (§5).
- **Reasoning/thinking stays off** on measurement runs — thinking tokens would sit inside
  the byte stream on some backends and are excluded by the alignment only when the
  backend separates them; `thinking_enabled` is already a silent-failure flag.

## 9. Probes before the numbers are trusted (cheap, do first)

- **L1 — request-invariance**: same seed, logprobs on vs off, per backend: content hash
  must not change. If requesting logprobs alters sampling anywhere, every comparison
  inherits a confound. (The run record already hashes responses; two runs each.)
- **L2 — provider sanity on OpenRouter**: all logprobs ≤ 0, exp(joint of full emission)
  in (0, 1], first-token top-k sums ≤ 1 + ε, per endpoint used. Guards against providers
  emitting rounded or fabricated values.
- **L3 — alignment robustness**: unicode, escaped quotes, numbers split across tokens,
  values at object boundaries. Unit tests on stored real payloads from both backends.

## 10. Build order

1. `field_confidence()` + L3 unit tests against the payloads already in Postgres.
2. L1/L2 probes (live, ~6 calls).
3. Structured Extract: extend `Logprobs` output with `fields`; persist `field_logprobs`.
4. Registry `field_confidence` read op.
5. (eval-side, separate package, later) calibration + risk–coverage over the DB.

Out of scope, deliberately: any thresholding/abstention component, prompt phrasing that
asks the model to self-report confidence (a different, weaker signal), and logprobs for
repair-chain attempts.

---

# DECISION — primary statistics (2026-08-06, supersedes the §4 recommendation)

Literature review (26 sources, 2021-2026; verified-vs-abstract status per item in the
research log) inverted the §4 lean. The call, now fixed for the certainty-score component:

## The call

| Field kind | Primary | Kept alongside | Dropped as primary |
|---|---|---|---|
| free / numeric | **`mean` = exp(mean logprob)** (geometric per-token) | `joint` (report per-field only, never pooled), `min` as a secondary OR-gate | `joint` as primary |
| enum | **option posterior** from first value-token top-k | `mean` as fallback | — |

## Why `joint` lost

Within a fixed field, `joint = mean^k` — a monotone transform, so every ranking metric
(AUROC, risk-coverage) is IDENTICAL between them; nothing is gained. Pooled across fields,
`joint` is dominated by token count k, not correctness — and ECE, the one metric that is
not transform-invariant, is computed exactly there. Both clinical extraction papers that
pick a statistic pick the geometric mean (Shrestha & Kim, arXiv:2603.00924, conformal risk
control on FDA/MIMIC-CXR; CeRTS, J Biomed Inform 2025). Nobody publishes joint as primary.

## Why `min` is a gate, not a primary

Head-to-head it ties geometric mean (Bouchard & Chauhan, TMLR 2025) or loses (arXiv:
2602.17431, correlations down 0.10-0.17). But its known failure mode is noise at long
lengths, and our value spans are 1-5 tokens — so keep it as `mean < t_field OR min < t_min`
and MEASURE whether the second clause ever catches anything; drop it if not.

## Why the enum posterior is promoted to primary for enums

The one point where four independent lines converge (ConfBench first-token result,
arXiv:2608.01792; Calibrate Before Use, arXiv:2102.09690; G-Eval score-weighting,
EMNLP 2023; CeRTS abstract). PRECONDITION, checkable statically per model tokenizer: all
options must have DISTINCT first tokens ("mild"/"mildly" collapse into one bucket and the
posterior is silently wrong). On collision, downgrade that field to full multi-token
scoring. Surface-form bias means the posterior should eventually get an affine calibration
per option set; store raw + note.

## New rules absorbed into §8

- **Never pool across ENGINES either** (not just modes): GPU host qwen3.5/3.6 = unconstrained,
  laptop qwen3 = llama.cpp grammar, vLLM (M-04) = post-mask by default (verified in vLLM
  source: "raw" logprobs mean pre-sampler, and the grammar bitmask is applied before the
  sampler). Three different quantities under one column name.
- **Calibrate thresholds per field or field-type, never per model**: Shrestha & Kim
  measured per-section thresholds spanning 0-100% rejection within the SAME corpus.
- **Routing metrics: ECARB (errors caught per review budget) or AUGRC** (arXiv:2407.01032),
  not AUROC alone.
- Value-token-only restriction (§3) is justified by measurement elsewhere: on structured
  JSON, 99.4-100% of ALL-token logprobs saturate >0.999 (VERDI, arXiv:2605.11334) — whole-
  output statistics are meaningless; per-field spans are the signal.

## New probe L4 — is the mask actually there?

Whether laptop-Ollama logprobs are post-grammar-mask is UNVERIFIED upstream (source
unreachable), and on the GPU host the mask is measurably absent (format ignored). Cheap probe:
emit an enum field under json_schema on the laptop path and check the first value-token's
top-20 for grammar-ILLEGAL alternatives. Illegal options present = pre-mask numbers;
absent = post-mask. This decides how the enum posterior is interpreted per engine.

## Noted for eval-side later (not this phase)

CMR-EXTR (arXiv:2605.08045) validates a complementary confidence channel we get for free
from stored fields: cross-field physiologic consistency (e.g. LVEF = LVSV/LVEDV x 100).
Independent of every decoding caveat; belongs in extrct-eval. Verbalized confidence stays
excluded (JMIR 2025: token-prob AUROC 0.71-0.87 vs verbalized 0.51-0.70 across 9 models).

---

# AMENDMENT 2 (2026-08-07) — source-verified corrections from certainty-research.md

- **§3 byte-offset keystone is DEMOTED to character alignment.** Both self-hosted engines
  corrupt the bytes field (Ollama re-derives from truncated strings; vLLM substitutes
  U+FFFD). Align on decoded token characters with the hard invariant
  "".join(tok.token) == raw_text; fail the run on violation. Bytes only with the byte-level
  assertion.
- **Probe L4 is RETIRED for current engines — answered from source.** Ollama/llama.cpp
  logprobs are PRE-mask (grammar never touches them); vLLM logprobs are POST-mask
  ("raw" means pre-sampler, not pre-grammar). Every certainty record now carries
  engine + mask_state (pre_mask | post_mask | unmasked); nothing crosses mask states.
- **first_token is added as a 4th stored statistic for ALL fields** (ConfBench: the only
  extraction-specific head-to-head, first-token beat mean; domain caveats on file).
- **joint is promoted from "stored, never scored" to secondary ranker WITHIN field type**
  (LM-Polygraph TACL 2025: MSP strongest at 1-2-symbol outputs; within-type lengths are
  homogeneous, satisfying the stratification its result assumes).
- Sentinels: vLLM masked tokens arrive as exactly -9999.0 (test <= -9998, never exp);
  Ollama can underflow to -inf inside JSON. Short top-k lists: NaN-and-exclude.

Full evidence chain: certainty-research.md. Integration: grounding-certainty-design.md.
