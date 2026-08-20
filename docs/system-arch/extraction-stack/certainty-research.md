# Certainty research — implementations, engines, and the numbers (record)

Status: RESEARCH RECORD, 2026-08-07. Implementation-level survey complementing the
2026-08-06 literature review recorded in `logprobs-design.md`. Everything below marked by
read level; could-not-verify at the end. Companion: `grounding-research.md`.

## Lead finding — the engines do not return the same quantity (source-verified)

| Engine | Logprobs are | Evidence |
|---|---|---|
| **Ollama / llama.cpp** (b10242) | **PRE-grammar-mask**, softmax over the full vocab (`get_token_probabilities` reads raw logits; grammar only touches the sampler's separate `cur_p` buffer; `post_sampling_probs` is never set by Ollama and defaults false) | code-read |
| **vLLM** (main 2026-08) | **POST-grammar-mask**: `apply_grammar_bitmask` mutates logits IN PLACE in the model runner *before* the sampler computes "raw" logprobs — "raw" means pre-sampler-processors, not pre-grammar | code-read |

Consequences, now invariants:
- An Ollama field probability is under the **unconstrained** model (can be low even when
  the grammar left no choice); a vLLM one is under the **constrained** model (inflated,
  and exactly the distorted quantity the grammar-aligned-decoding paper describes).
- **Record `mask_state` in every run**: `pre_mask` (ollama+grammar), `post_mask`
  (vllm+schema), `unmasked` (prompted / no schema). Never calibrate or pool across them.
- This answers probe **L4 from source** — no live probe needed for these two engines.

## Second lead — the `bytes` field is NOT trustworthy on self-hosted engines

Ollama discards llama.cpp's byte-exact `bytes` and re-derives from the UTF-8-truncated
token string; vLLM encodes with `errors="replace"` (U+FFFD). A multi-byte character split
across tokens (ä/ö/ü/ß — routine in German clinical text) breaks byte-exactness on BOTH.
Design amendment: align on **decoded token characters**, with a hard runtime invariant
`"".join(tok.token) == content` (and `b"".join(bytes) == content.encode()` whenever bytes
are used) — **fail the run on violation rather than silently misalign**.

## Gaps from the previous review — all filled

**CeRTS** (code-read; paper still paywalled): not a pooling statistic at all — best-first
search over the token tree (~1–3.6 greedy-run equivalents), probability mass marginalized
over sequences yielding the SAME field value, confidence = un-renormalized top-2 delta.
Mean AUROC ~0.74 vs ~0.68 for (a hobbled) sample-consistency baseline, n=60/field. Found
in code: unparseable-JSON mass is silently dropped (up to 78–96% for DeepSeek-R1). Needs
raw prefix continuation — **cannot run behind an OpenAI-compatible router; not shippable
for us**. Its marginalize-over-equal-values idea earns one experiment cell vs our enum
posterior, eval-side.

**LM-Polygraph TACL 2025** (paper-read): output length decides the method family. At 1–2
symbol outputs — our regime — **MSP (the joint!) is the strongest information-based
ranker** (PRR 0.71 vs perplexity 0.62); the ordering inverts for long outputs. Verbalized
and P(True) near-random below ~GPT-4o-mini scale. Normalization: isotonic-regression-based
PCC recommended. **Plan change: joint is promoted from "stored, never scored" to
"measured as a secondary ranker WITHIN field type"** — within a type, lengths are
homogeneous, which is exactly the stratification their result assumes.

**Ollama mask ordering** — filled above (pre-mask), from llama.cpp + ollama source.

## Extraction-specific numbers (2026)

- **ConfBench** (arXiv:2608.01792, VLM invoice extraction): *first-token* consistently
  beats margin and mean-token for logprob confidence (AUROC 0.62/0.61/0.59 on Qwen), ECE
  flat (~0.21) across all three. Domain caveat: VLM/OCR errors are perceptual, first
  token often IS the decision; absolute AUROCs weak. **Plan change: store first-token
  probability as a 4th statistic for ALL fields** — costs nothing now, our own ECARB
  decides later. Dataset is CC-BY-NC (do not ship it).
- **ExtractConf** (arXiv:2606.24420, DocILE invoices): logprob-mean AUC 0.705, verbalized
  0.692, 5-sample self-consistency 0.744 at 5× cost; their 0.928 comes from OCR/layout
  features that DO NOT EXIST in our clean-text pipeline — import the baselines, not the
  pessimism.

## Repo verdicts (all licenses verified from LICENSE files)

| Repo | License | Verdict |
|---|---|---|
| cvs-health/uqlm | Apache-2.0 | Steal two things: the top-k truncation handling (short lists → NaN + nanmean/nanmin, never renormalize against wrong log k) and the 90-line Platt/isotonic calibrator pattern (key it by field-type). Package too heavy (langchain, transformers, optuna) and does NO -inf handling. 0.x churn: pin-and-vendor. |
| IINemo/lm-polygraph | MIT | Reimplement + cite; 47 estimators inventoried; runtime wants HF model objects, not endpoints. No enum posterior exists there (or anywhere) — ours is a reimplementation with no prior art to lift. |
| arena-ai/structured-logprobs | Apache-2.0 | **Closest prior art to our aligner**: lark JSON grammar with propagate_positions → per-value char spans → sum logprobs. Three bugs to fix in any borrowing: excludes the token at end_pos (a value fully inside one token gets confidence 1.0 on a SILENT failure), IndexError on terminal values, no concatenation assertion. Sum-only; ignores top_logprobs. |
| VATBox/llm-confidence | Apache-2.0 | Negative example: joint over KEY+value tokens (doubly length-confounded), token-text heuristics instead of parsing, collects top_logprobs then never uses them. |
| ManiDoraisamy/promptrepo-score | MIT | Negative example: right statistic (geometric mean), wrong token set (substring filter grabs key/whitespace tokens). |
| obielin/llm-extract | MIT | Belongs to grounding survey as a NEGATIVE example: verbalized confidence (near-random family), LLM-emitted "source" snippet never checked against the document, Anthropic-only (the egress boundary fail). |
| kmad.ai post | n/a | Nothing to reuse; independently corroborates per-field-type thresholds; anti-pattern noted (Ġ/▁ stripping breaks alignment). |
| outlines / xgrammar / sglang / guidance | Apache/MIT | Clean if ever needed for vocabulary-prefix machinery. |
| netcal | Apache-2.0 (≥v1.3; was MPL before) | More than needed; its Bayesian calibration gives credible intervals — the honest small-n answer if we want it later. |
| MAPIE | BSD-3 | Conformal sets, not calibrated scalars; needs ~19 calibration points per stratum at α=0.05 — feasible per field-type; fits "route unless singleton" gating later. |

## Engineering rules absorbed into the design

1. **vLLM sentinel**: masked/absent tokens arrive as exactly `-9999.0`, never `-inf` —
   test `lp <= -9998`, never exponentiate it. Ollama path can underflow to `-inf` in
   JSON (invalid JSON, serializer-dependent) — guard both.
2. **Token-concatenation invariant** before any alignment (above).
3. **Top-k truncation**: short top-k lists can occur for reasons besides truncation
   (llama.cpp breaks at p==0); handle as NaN-and-exclude, uqlm-style.
4. **First-token collision precondition** has no prior art: build it per (tokenizer,
   field) by encoding each option IN ITS REAL JSON CONTEXT (`"field": "OPTION"`) and
   taking the first token after the opening quote — leading-space/BPE conventions differ
   across Llama/Qwen/Gemma; the option in isolation tokenizes differently.
5. Ollama forces n_probs = max(top_logprobs, 1) when logprobs on; vLLM allows -1 = full
   vocab (never request it); OpenAI-compatible ceiling 20.

## Could not verify

CeRTS paper text (code is the stronger source but paper-vs-CSV concordance unchecked);
CONSTRUCT (arXiv:2603.18014) beyond its abstract (vendor-adjacent, unread); ExtractConf
§5 tables (abstract-consistent); ConfBench Table 3 exact digits (single-source
transcription; the qualitative ordering is verbatim); vatvenger Medium (unfetchable —
method inferred from its companion repo, which was code-read); whether vLLM
`processed_logprobs` renormalizes at the sampler exactly as assumed.
