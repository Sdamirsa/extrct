# Grounding research — mapping extractions back to source spans

Status: RESEARCH RECORD, 2026-08-07. Verified survey (papers + source code) of approaches
for resolving each extracted field to a span in the input document. This is the grounding
enforcement question. Read-level is marked per claim; the could-not-verify list is at the
end. Companion: `certainty-research.md`, `logprobs-design.md`.

## The three findings that decide the design

1. **Nobody asks the model for character offsets.** Every production tool read at source
   (Google langextract, LangChain citation chain, taln) uses the same shape: the model
   emits a **verbatim evidence quote**, and a deterministic post-hoc aligner computes the
   offsets. langextract prompts "Use exact text for extractions. Do not paraphrase" and
   sets `char_interval = None` when a quote cannot be located — the hallucination gate.
   No measured head-to-head of model-emitted offsets vs quote+alignment exists; treat
   "model offsets are unreliable" as a universal design convention, not a citation.

2. **Recall is decided by tokenization granularity, not the alignment algorithm.**
   Measured (BOAT benchmark, bioRxiv 2026.02.06.704502, 35k SQuAD-derived + 5.3k
   biomedical pairs, paper read): word/whitespace tokenization misses **47–58%** of
   contiguous targets (punctuation attaches to words); subword tokenization recovers
   **98–99%** contiguous, ~89–96% non-contiguous. This is exactly the `LVEF 55%`,
   `(CD96+)`, `1.2 x 3.4 cm` regime. langextract dodges the word-level failure with
   character-class tokenization (`CD96+` → `CD`,`96`,`+` — regex read from
   `core/tokenizer.py`), which is structurally the subword arm of that comparison.

3. **SHAP/Shapley attribution cannot satisfy grounding — definitionally, not just practically.**
   ContextCite (NeurIPS 2024) names the distinction: *contributive* attribution finds
   what the model **used**; *corroborative* attribution finds what **supports** the fact.
   grounding is corroborative. A high-Shapley token proves influence (also true of headers and
   boilerplate), not evidence. Additionally impractical today: SHAP-style methods need
   teacher-forced scoring of a fixed completion (`prompt_logprobs`/`echo`), which
   **neither Ollama nor OpenRouter offers** (source/docs read); vLLM has it (M-04).
   SHAP default cost: 500 forward passes per explanation; ContextCite achieves >0.95 rank
   correlation with exact Shapley at ~100 ablations, amortized per document. Keep as a
   future *diagnostic* on vLLM, never as the provenance mechanism.

## The evidence-span vs value split (the core data-model decision)

langextract's shape, read from `core/data.py`: `extraction_text` (verbatim quote, gets
aligned, gets a `CharInterval` + `AlignmentStatus`) is separate from `attributes` (derived
values, never aligned). That answers our "present" ⟵ "there is a small effusion" problem
exactly: the QUOTE is grounded; the VALUE is derived from the quote by the model. Adopt
the shape regardless of implementation.

`AlignmentStatus` vocabulary worth adopting: `MATCH_EXACT | MATCH_GREATER | MATCH_LESSER
| MATCH_FUZZY | None`, with `None` as a hard gate (grounding: unresolvable links are a release
gate — this is that, mechanized).

## Approach comparison (verified)

| # | Approach | Reliability | License | Verdict |
|---|---|---|---|---|
| A | **Quote + vendored langextract aligner** (char-class tokenize; monotonic-occurrence DP exact; LCS fuzzy at 0.75 coverage) | 98–99% localization benchmarked for this tokenization class | Apache-2.0 (verified 3 ways) | **PRIMARY — reimplement/vendor ~700 lines, stdlib+`regex` only** |
| B | `pip install langextract` | same algorithm | Apache-2.0 | Rejected: pulls google-genai/google-cloud-storage for one algorithm; 18 releases/yr churn; open hang bug #277 in legacy fuzzy path |
| C | Subword ordered alignment (taln algorithm) | best non-contiguous recall (~89–96%) but ~8–95 candidate alignments/target need a disambiguation rule | algorithm unencumbered; the package: MIT declared but NO LICENSE file, deps include `anthropic` and `tiktoken` (which fetches its BPE from Azure at first use — an the egress boundary egress event) | Reimplement-only fallback if char-class tokenization fails our gold set |
| D | Embedding sliding-window localization | coarse (window, not chars); published as a filter (removed 14.6% of extractions at cosine 0.65), not a locator | model-dep | Not a locator; possible extra *filter* later |
| E | Span-native model (GLiNER-BioMed) as candidate generator | 59.77 micro-F1 open biomedical NER (abstract-level) — too low to extract, could validate | code Apache-2.0; v1 weights CC-BY-NC (license policy FAIL), v2.1/BioMed `Ihor/*` Apache-2.0 | Deferred |
| F | Constrained span-copy decoding (Copy-as-Decode) | would be a hard guarantee | n/a | **Impossible on both current backends**; revisit on vLLM (M-04) |
| G | SHAP PartitionExplainer | contributive, not corroborative; needs teacher forcing | MIT | Diagnostic-only, vLLM-gated (user-supplied sources reviewed; see §3 above) |
| H | ContextCite | same class as G, 32–256 ablations/document | MIT (repo dormant since 2024-10) | Diagnostic-only, vLLM-gated |
| I | LangChain citation_fuzzy_match | mechanism right, code **unsafe**: quote interpolated into a regex UNESCAPED — `(CD96+)` breaks it; error budget `e<=100` eventually matches anything | MIT | Copy the `{fact, substring_quote[]}` shape, never the code |

## Implementation deviations from upstream langextract (each with a reason)

1. `_normalize_token` strips trailing `s` from tokens >3 chars — English folklore that
   corrupts clinical identifiers and units (`cysts`→`cyst`, mangled IDs). Make opt-in.
2. Use `dp` exact + `lcs` fuzzy only; the `legacy` fuzzy path has an OPEN hang bug
   ([langextract#277](https://github.com/google/langextract/issues/277)).
3. Record `alignment_status` + aligner version in the run record; `char_interval is None`
   → release gate (the grounding enforcement, mechanized).
4. Normalization must be length-preserving or carry an explicit original↔normalized
   offset map — NFKC/unidecode silently shift offsets (BOAT excluded such cases; we
   cannot).

## Evaluation: what "good" looks like

Adopt BOAT's two-metric split (maps directly onto grounding/replayability):
- **Reconstruction accuracy** — can the quote be rebuilt from source tokens at all
  (hallucinated-quote gate).
- **Localization accuracy** — does `[start, end)` match the gold span.

Published levels: 98–99% contiguous localization (subword-class tokenization); ~89%
non-contiguous on biomedical text; clinical VA IE framework reports strict/relaxed F1
0.86/0.90 (zero-shot Llama-3.1-8B) with SME agreement AC1 0.91. Clinical convention to
adopt: report **strict** (exact char match) and **relaxed** (token-overlap F1) side by
side. Cost note for the record: deterministic alignment is sub-ms; LLM-as-judge span
verification costs ~$0.001 and 1–2 s per check — thousands of times more for the same
answer.

## What alignment does NOT solve

- **Negation scope**: alignment locates "no pericardial effusion"; it does not know the
  value should be `absent`. Stays with the extractor + eval.
- **Value correctness**: a perfectly grounded quote can still yield a wrong derived
  value. Grounding and correctness are separate audits; together with per-field logprob
  confidence they form a two-axis signal — *high confidence + no alignment* is the exact
  hallucination profile grounding must reject.

## Could not verify (inherited into any design)

- No measured comparison of model-emitted offsets vs quote+alignment exists at all.
- Whether langextract #386's RapidFuzz replacement actually shipped (issue closed
  2026-04-14; merge not traced).
- "Attribute First, then Generate" (ACL 2024) — abstract only; strongest unexplored lead.
- vLLM `prompt_logprobs` correctness (protocol-present; historical issue #5264 reported
  breakage) — must be tested before designing diagnostics on it.
- GLiNER-BioMed 59.77 F1, Copy-as-Decode, ALCE numbers — abstract/snippet-level.
- `knowledgator/gliner-biomed-*` checkpoints returned no license via HF API; only
  `Ihor/*` confirmed Apache-2.0.

## Primary sources

Source-read: langextract `resolver.py` / `core/tokenizer.py` / `core/data.py` /
LICENSE / pyproject (v1.6.0); LangChain `citation_fuzzy_match.py`; taln pyproject;
tiktoken loaders; ollama `api/types.go`; vLLM v0.11.0 `protocol.py`; shap
`_partition.py`; GLiNER `model.py`.
Papers read: BOAT (bioRxiv 2026.02.06.704502, CC-BY); ContextCite (arXiv:2409.00729);
RAG source attribution (arXiv:2507.04480); VA clinical IE validation (arXiv:2604.06028).
Abstract-level: ACL-2026 attribution survey (arXiv:2508.15396); Attribute-First
(ACL 2024); ALCE (EMNLP 2023); GLiNER/GLiNER-BioMed; Copy-as-Decode (arXiv:2604.18170);
JBI 2025 medication-IE Shapley (doi:10.1016/j.jbi.2025.104898).
