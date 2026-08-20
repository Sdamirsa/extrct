# Design: grounding + certainty in the ExtrCT pipeline

Status: DESIGN, 2026-08-07. Grounded in `grounding-research.md`, `certainty-research.md`,
and the decisions in `logprobs-design.md`. Two features, one architectural spine: both
are **derived, re-derivable views over evidence the run row already stores**, both
computed by pure functions in `extrct` with dual callers (run-time convenience,
offline re-derivation), and both surfaced without touching the extraction value object.

```
                          ┌───────────────────────────────┐
 Schema Builder ──schema──►                               ├─ Extracted        (pure values)
 Client ────────client────►   ExtrCT Structured Extract    ├─ Logprobs         (tokens)
 text ──────────text──────►   (+ evidence request when    ├─ Grounding        (spans)   NEW
                          │    grounding is on)           ├─ Run Report / ... (as today)
                          └───────────────┬───────────────┘
                                          │ Logprobs + Schema (+ run_uid)
                                          ▼
                          ┌───────────────────────────────┐
                          │   ExtrCT Certainty Score  NEW  ├─ Field Certainty  (Data)
                          │   (pure field_confidence())   ├─ Certainty Table  (DataFrame)
                          └───────────────────────────────┘
```

## Feature 1 — Grounding (the grounding path)

**Request side.** A toggle on Structured Extract (and a matching flag hashed into the
schema envelope): `request_evidence`. When on, the schema sent to the model is augmented
mechanically with a parallel top-level object:

```json
{ "lvef": 55, "diagnosis": "...",
  "_evidence": { "lvef": "LVEF measured at 55%",
                 "diagnosis": "normal systolic function" } }
```

- Values object stays PURE — downstream consumers of `Extracted` see no change.
- `_evidence` values are VERBATIM QUOTES (the prompt demands exact text, langextract-style).
  Never offsets: no tool in production asks models for offsets, and quotes + deterministic
  alignment is the universal, benchmarked shape.
- The augmentation changes schema_uid and request_uid — a declared config axis, exactly
  like encoding. Toggle off = byte-identical requests to today (the proven no-break pattern).

**Alignment side.** New module `extrct/grounding.py`: a vendored reimplementation
(~700 lines, stdlib + `regex` only) of langextract's Apache-2.0 alignment path, with the
four deviations recorded in grounding-research.md: no trailing-`s` stemming by default;
`dp` exact + `lcs` fuzzy only (never the hang-prone legacy path); `alignment_status` +
aligner version recorded in the trace; length-preserving normalization only (or explicit
offset maps). Pure: `align(quote, source_text) -> {char_interval, alignment_status}`.

**Surfacing.** Structured Extract gains a `Grounding` output and persists
`field_grounding JSONB` (per field: quote, char_interval, alignment_status). The gate:
`alignment_status = None` (quote not locatable) is the mechanized grounding release check — it
flags in the output now and becomes an eval release gate later.

**Explicitly rejected** (with evidence, see research doc): model-emitted offsets;
SHAP/ContextCite for provenance (contributive ≠ corroborative, and impossible on current
backends anyway); constrained span-copy (impossible on both backends today); LangChain's
fuzzy citation code (unescaped-regex bug); embedding windows (a filter, not a locator).

## Feature 2 — ExtrCT Certainty Score (the new node)

**One pure function**, `extrct/confidence.py::field_confidence(raw_text, logprobs,
schema) -> dict`, per field (by JSON path):

| Statistic | Role |
|---|---|
| `mean` = exp(mean logprob of value tokens) | primary, all field kinds |
| `enum_posterior` (renormalized first-token top-k over options) | primary for enums, gated on the collision precondition |
| `joint` = exp(sum) | secondary ranker measured WITHIN field type (LM-Polygraph: strongest at 1–2-symbol outputs) |
| `min` | secondary OR-gate, measured then kept or dropped |
| `first_token` = exp(logprob of first value token) | 4th stored statistic for ALL fields (ConfBench) |

Alignment of tokens to value spans is by **decoded characters** (not bytes — both
self-hosted engines corrupt `bytes`), guarded by the hard invariant
`"".join(tok.token) == raw_text`, failing the run on violation. Borrow
structured-logprobs' parse-with-positions idea; fix its three catalogued bugs (end-token
off-by-one, terminal-value IndexError, missing assertion). vLLM's `-9999.0` sentinel and
`-inf` underflow are handled before any exp(); short top-k lists become NaN-and-exclude.

**The collision precondition** (no prior art — ours): per (model tokenizer, enum field),
encode each option in its real JSON context (`"field": "OPTION"`) and take the first
token after the opening quote; distinct → posterior valid; collision → downgrade that
field to multi-token scoring. Computed once per schema × model, cached in the trace.

**Trace correctness invariant** (source-verified, both engines read): every certainty
record carries `engine` and `mask_state`:

- `pre_mask` — Ollama/llama.cpp (grammar never touches reported logprobs)
- `post_mask` — vLLM with schema (bitmask applied before the sampler's "raw" logprobs)
- `unmasked` — prompted mode, or vLLM without schema

Calibration, pooling, and thresholds NEVER cross mask states. This retires probe L4 for
these two engines (answered from source); the live probe remains only for future engines.

**The component.** Inputs: `Logprobs` (from Structured Extract), `Schema`, optional
`Extracted`; static template (house rule — no dynamic UI). Outputs: `Field Certainty`
(Data: per-field statistics + flags), `Certainty Table` (DataFrame: one row per field,
ready for Convert Rich Message / registry joins). Persists `field_logprobs JSONB` onto
the run row via run_uid (ADD COLUMN IF NOT EXISTS migration; COALESCE upsert). Registry
gains read op `field_confidence`.

**Two-axis audit** (grounding × certainty), the payoff of doing both: a field with HIGH
certainty and NO alignment is the exact hallucination profile grounding exists to reject; LOW
certainty with EXACT alignment is a transcription-grade fact the model under-trusts.
Both axes land on the same run row; the cross-tab is one SQL query.

## Deferred by design (with reasons on file)

- Calibration fitting (sklearn isotonic/Platt per field-type, uqlm's 90-line pattern) and
  conformal routing sets (MAPIE) — eval-side, after ground-truth accumulation.
- CeRTS-style value-marginalization — one experiment cell vs the enum posterior, eval-side;
  cannot run behind a router.
- ContextCite/SHAP diagnostics and constrained span-copy — gated on vLLM (M-04).
- Confidence-gated review routing (Label Studio) — after calibration is measured.

## Build order

1. `extrct/confidence.py` (`field_confidence` + invariants) — unit-tested against
   the real logprob payloads already in Postgres, both providers. Probes L1–L3 alongside
   (L4 retired for current engines).
2. **ExtrCT Certainty Score** component wrapping it (static template, verified via the
   loader + strict round-trip + live run).
3. `extrct/grounding.py` (aligner) + its own gold-set test (BOAT two metrics:
   reconstruction + localization) on ~200 synthetic clinical targets.
4. Structured Extract: `request_evidence` toggle + `Grounding` output + `field_grounding`
   persistence (no-break proof same as the logprobs toggle: off = byte-identical).
5. Registry read ops; docs; the two-axis cross-tab as a registry query.

Steps 1–2 are independent of 3–4 and can be tested by the user separately.

---

# AMENDMENT (2026-08-07, pre-build) — final component architecture

User decision: keep original components untouched; separate nodes where possible. Design
check against the running components found a cleaner home for the evidence request than
the original plan (which put a toggle on Structured Extract):

- **Schema Builder** gains `request_evidence` (BoolInput, off by default). The envelope
  builder augments the schema with a parallel `_evidence` object (one verbatim-quote
  string per LEAF field, keyed by dotted path). schema_uid changes automatically because
  it hashes the schema - the axis is declared for free. Toggle off = wire-identical
  requests (no-break provable the same way as the logprobs toggle).
- **Structured Extract: ZERO changes.** With an augmented schema its Extracted output
  simply contains `_evidence`; the ladder validates against the augmented schema
  consistently. Downstream purity is restored by the Grounding node''s Clean Values output.
- **NEW node: ExtrCT Certainty Score** - inputs Provider Response + Schema; pure
  field_confidence() in extrct/confidence.py; persists field_logprobs by run_uid.
  Engine/mask_state from the stored run row (fallback: shape inference + "unknown").
  OpenRouter json_schema runs get mask_state "unknown_provider_engine" - honest, since
  OpenRouter''s serving engines are heterogeneous.
- **NEW node: ExtrCT Grounding** - inputs Extracted + Source Text (+ optional Run UID);
  pure aligner in extrct/grounding.py (char-class tokenizer, exact
  successive-occurrence matching, difflib-based fuzzy fallback at 0.75 coverage - a
  compact reimplementation of the langextract approach with the recorded deviations);
  outputs Grounding Table / Grounding Report / Clean Values; persists field_grounding.
- **Statistics finalized** per user review: mean, joint, min, first_token, margin (added),
  enum_posterior (toggle, enums only; empirical in-context first-token matching rather
  than tokenizer-side precondition - we have no model tokenizers client-side, and the
  emission''s own top-k IS the ground truth about what the model could have said).
- Storage: field_logprobs + field_grounding JSONB via annotate_run() (dedicated UPDATE -
  save_run would clobber final_status). Registry read op field_confidence.
- Rejected duplication of Structured Extract (430-line fork = every fix lands twice);
  the separation requirement is met with zero extract changes instead.
