# XAI — certainty and grounding

Both modules are pure functions of stored evidence. They run inside the pipeline as
non-breaking steps (a failed analysis is a recorded step, never a lost extraction) and
can be re-run by anything — a notebook, an eval process, an auditor — from the run log
alone. Neither ever changes what was extracted.

---

## Certainty (`xai.certainty`) — per-field confidence from token logprobs

### How fields get their tokens

A position-aware scan of the raw emission locates the character span of every JSON
VALUE (string content between quotes; structural tokens attach to nothing — under a
schema they are near-deterministic and only dilute the signal). Tokens overlapping a
span are that field's tokens.

Alignment is by DECODED CHARACTERS with a hard invariant: the concatenated token
strings must equal the message content. On violation the result is an ERROR RECORD,
never a silent misalignment. Why not the provider `bytes` field: both measured
self-hosted engines corrupt it (Ollama re-derives bytes from truncated strings; vLLM
substitutes U+FFFD). Two tolerated deviations, both named in the output: a token
PREFIX beyond the content (thinking text — aligned on the concatenation), and tokens
covering only a content prefix — the MTP signature (multi-token-prediction models emit
first-token-only logprobs; refused with that explanation).

### The statistics, and why these

| statistic | role | rationale |
|---|---|---|
| `mean` = exp(mean logprob) | PRIMARY for free/numeric fields | geometric per-token; length-comparable. Within a fixed field `joint = mean^k` is a monotone transform (identical rankings), but pooled across fields `joint` is dominated by token count — and the calibration metrics that matter are computed exactly there |
| `enum_posterior` | PRIMARY for enum fields | the first value-token's own top-k alternatives ARE the head-to-head the model ran over the options; renormalising over option-matching alternatives gives a posterior over the answer set |
| `min` | secondary OR-gate | catches one hesitant token inside a confident span; noisy at length, fine on 1–5-token values |
| `joint` | within-field-type ranker | legitimate where lengths are homogeneous |
| `first_token`, `margin` | kept per the extraction-specific literature | margin = top-1 − top-2 at the first value token |
| `k`, `masked_tokens` | re-derivability + sentinel accounting | |

Engine sentinels are excluded BEFORE any exp(): vLLM emits exactly −9999.0 for
masked/absent (test ≤ −9998, never exp), the Ollama path can underflow to −inf.

The enum posterior refuses rather than guesses: with only the sampled token visible
it says "set top_logprobs > 0" (renormalising one token is 1.0 by construction —
first_token disguised as certainty), and when an alternative's remainder matches two
options ("mild"/"mildly") it reports the collision.

### Interpretation rules (each ignores at your peril)

1. **Never pool across mask states.** Grammar-constrained decoding may or may not be
   reflected in reported logprobs: llama.cpp/Ollama reports PRE-mask numbers, vLLM
   POST-mask ("raw" means pre-sampler, not pre-grammar), a router serves heterogeneous
   engines (unknown). Every report carries `engine` + `mask_state`; numbers from
   different states answer different questions.
2. **Never compare across request modes** (`json_schema` vs `prompted`): conditioned
   vs unconstrained distributions. `request_mode` is a run-log column for exactly this.
3. **Calibrate thresholds per field or field type, never per model** — measured
   per-section thresholds span 0–100% rejection within one corpus.
4. **Whole-output statistics are meaningless** on structured JSON (~99% of all-token
   logprobs saturate >0.999); per-field value spans are the signal.
5. `top_logprobs` truncation BOUNDS the tail, it never estimates it
   (`unmatched_mass` is reported).
6. Confidence describes the raw FIRST emission — what the model said, before any
   repair. `final_status` separates those worlds; the pairing (repaired value,
   raw-emission confidence) is itself informative.

---

## Grounding (`xai.grounding`) — evidence quotes, verified

The extraction is asked (via a schema mirror, injected at request time when
`grounding.enabled`) to return, per value, the VERBATIM source passage that justifies
it — and every quote is then aligned against the source. **A quote the source cannot
reconstruct is hallucinated evidence**, reported as `status: null`, and
`summary.grounding_clean` goes false. That is the gate.

The aligner (`ExtrCT-align/1.0` — versioned in every result, bumped on any behavioral
change) reimplements the approach proven in Google langextract, with recorded
deviations:

- **Character-class tokenization** (letter-runs | digit-runs | single symbols):
  `CD96+` → `CD·96·+`, `55%` → `55·%`. This granularity class recovers 98–99% of
  contiguous targets on the BOAT benchmark where word/whitespace tokenization misses
  47–58%.
- **No trailing-`s` stemming** (upstream folklore; corrupts identifiers and units).
- **Exact first**: contiguous token-sequence match, with repeated identical quotes
  resolved to SUCCESSIVE occurrences in extraction order.
- **Bounded fuzzy fallback**: token-level matching blocks; coverage ≥ threshold
  (default 0.75, floor 0.5), plus a density guard — a match smeared across a huge span
  is not a localization.
- **Length-preserving normalization only** (casefold per character): offsets always
  index the ORIGINAL text — on chunked runs the pipeline shifts them to absolute
  document coordinates, so citations survive chunking.

Why quotes, not model-emitted offsets: models reliably copy text and unreliably count
characters. Why the evidence mirror is a PARALLEL `_evidence` object: values purity —
the value fields stay exactly as they would have been without grounding (and
`ExtractionResult.data` strips `_evidence`; the full object stays in the chunk
evidence and the run row).

The quote-description prompt in the mirror encodes a measured failure: models echo
INFERRED values ("current") instead of quoting the sentence they inferred from — so it
demands the source passage, never the value itself.

Post-hoc lane: for runs extracted WITHOUT evidence, `schema.evidence_schema()` builds
a standalone quote-only schema (a second call that cannot perturb the original run),
and `ground_fields` aligns the result — total values purity.

### Reading a grounding report

```
fields.<name>.status   match_exact | match_fuzzy | None
fields.<name>.start/end  offsets into the ORIGINAL document
fields.<name>.score    1.0 exact; token coverage for fuzzy
summary                {fields, exact, fuzzy, unlocated, grounding_clean, aligner}
```

Route on `unlocated > 0` (the run-log query in docs/observability.md builds the
review queue); treat `match_fuzzy` scores near the threshold as "verify by eye".
