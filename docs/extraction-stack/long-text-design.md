# Long-text extraction — wrap, extract per chunk, merge with labeled conflicts

Three nodes close the long-document gap (a document larger than one useful context
window), each with its own data model, all governed by the Flow Controller:

    long text ─> [Prep - Text Wrapper] ─Chunks─> [Run - Chunk Extract] ─Extractions─> [PostPrep - Extraction Merger]
                  wrap-def/1.0                    one run row per chunk               merge-def/1.0
                                                                                       ├─ Merged (clean)
                                                                                       ├─ Merge Report
                                                                                       ├─ Conflicts
                                                                                       └─ Adjudication Task

Package authority: `wrapping.py`, `merging.py`; storage: `text_wrapping`, `text_chunk`,
`merge_run` (+ Registry `list_wrappings` / `list_merges`). Flow-def sections `wrapper`
and `merger` ride the controller like every other component.

## Wrapping (wrap-def/1.0)

Three deterministic logics — pure functions of (text, params), so a wrapping is
**re-derivable** from the text plus its recorded params and chunk text is stored
only on explicit request:

| Logic | Behavior |
|---|---|
| `fixed_window` | fixed char windows + overlap, cut snapped back to whitespace |
| `paragraph_pack` | whole paragraphs packed to `max_chars`; next window re-opens with trailing paragraphs up to `overlap_chars` |
| `sentence_pack` | the same packing over sentence units |

Every chunk is a `(start, end)` span into the ORIGINAL text with `text == source[start:end]`
— offsets are the contract that later maps grounding spans and citations back to the full
document. Invariants, checked on every run: full coverage (no character outside every
chunk), slice fidelity, oversized units fall back to windowing. Identity:
`wrap_uid = uid(text_sha256 + logic + params)`, `chunk_uid = uid(wrap_uid + idx + span)`.

## Per-chunk execution

Run - Chunk Extract reuses the Sweep Extract machinery across chunks: request → call →
gate → deterministic ladder rungs → a first-class `extraction_run` row per chunk with
`wrap_uid`/`chunk_uid`/`idx`/span in `run_metadata` — the run log always answers "which
slice of which document said this". Chunks are isolated (one failure = one row),
concurrency is semaphore-bounded, and resume is free: completed chunks are skipped AND
their stored extractions are **refetched**, so the Extractions output is always complete
for the Merger.

## Merging (merge-def/1.0)

The design draws on three fields; the composition is ours:

- **Map-reduce long-document IE** → vote-then-merge with **abstention**: a chunk that
  returned null did not see the variable; absence is never a vote.
- **Record linkage / entity resolution** → list items extracted twice from overlapping
  chunks are deduplicated by similarity clustering (difflib ratio on normalized text;
  composite over shared scalar fields for objects; `list_key` acts as a blocking key —
  e.g. match findings on `name` only).
- **Data fusion / truth discovery** → a conflict is **surfaced, never silently
  resolved**: every disputed variable keeps its full candidate set, per-chunk
  provenance, and a severity, whatever the resolution strategy chose.

Scalar pipeline: normalize (strings casefold + collapsed whitespace; numbers grouped
within a RELATIVE `numeric_tolerance` — 55 and 55.2 agree at 1%, 55 and 45 conflict) →
group → resolve by strategy (`majority` | `first` | `last` | `longest` | `refuse`) →
label. Severity `major` = tie or no candidate above half the votes; the
`label_and_null` policy nulls major conflicts regardless of strategy — the honest
answer when the document disagrees with itself. Object lists cluster greedily in
document order, then each cluster's fields merge through the same scalar machinery, so
item-level disputes are labeled exactly like top-level ones (`findings[0].severity`).

Merged output stays **schema-shaped and clean** — provenance, candidates and severity
live in the report and the `merge_run` row, never inside values. `_evidence` is
stripped (chunk-relative offsets do not survive merging; re-ground post-merge on the
full text via the post-hoc lane). `merge_uid` is content-addressed over config + inputs.

**The LLM rung** is deliberately outside the node (the pure-aligner precedent): the
Adjudication Task output composes a prompt over the major conflicts — feed it plus the
source text through a Structured Extract exactly like post-hoc grounding. No model call
ever happens inside the Merger.

## Configuration surface

Everything is controllable per context — node fields, `wrapper.*`/`merger.*` keys on a
Flow - Config payload, or the Flow Controller document:

```json
{"wrapper": {"logic": "paragraph_pack", "max_chars": 4000, "overlap_chars": 400},
 "merger":  {"scalar_strategy": "majority", "conflict_policy": "label_and_null",
             "numeric_tolerance": 0.01, "list_similarity": 0.85, "list_key": "name"}}
```

## The docling call (ingestion)

Decision 2026-08-10, **revised same day after a measured research pass**: the
architecture stands — the Wrapper's input stays CLEAN TEXT and ingestion is a converter
stage in front — but docling's role shrinks from "designated candidate" to **PDF/OCR
candidate only, behind a hard interface**, and the provenance layer is OURS either way:

- **Docling cannot provide char-level provenance into plain text.** Measured on
  2.119.0: HTML items return `prov=[]`; everywhere a `ProvenanceItem` is constructed,
  `charspan` is the span within the item's own text, never an offset into the source.
  The offsets grounding needs come from our own offset-preserving serializer regardless.
- **`pip install docling` is 5.5 GB** (torch/CUDA stack), and the slim path is broken
  at import in 2.119.0 (the converter drags in ASR + OCR at module scope). A torch-free
  HTML backend exists (~280 MB) but only via a private attribute.
- **replayability hazards**: layout model specs pin `revision="main"` — mutable weights; and
  `origin.binary_hash` is the LOW 64 bits of the sha256 while our uid convention takes
  the high bits — never treat it as a uid. First-party telemetry: none found in the
  sdist (transitive deps unaudited; assume huggingface.co egress on first model fetch).
- For HTML/markdown ingestion, `beautifulsoup4` + our own serializer beats docling on
  the only axis we care about (provenance) at 1/20th the weight. Docling earns its 5.5 GB
  only when scanned-PDF OCR actually arrives — via a derived image, models pinned to
  commit SHAs, wrapped in our interface (its API had two structural repackagings).

## Addendum — prior-art findings (research pass, 2026-08-10)

- **The abstention rule is the load-bearing prior art.** LLM×MapReduce (ACL 2025) names
  inter-chunk conflict explicitly and keeps silent chunks out of the vote with a
  `NO INFORMATION` sentinel; its ablation shows the structured per-chunk protocol
  carries most of the gain. The truth-discovery literature adds the warning our design
  already encodes: chunks are *disjoint evidence, not independent observers* — majority
  voting across chunks is a category error unless absence abstains. Source-reliability
  weighting becomes legitimate only with multiple observers of the SAME chunk
  (multi-model/multi-pass merges — a future axis).
- **Conflict-as-first-class-object is a gap we fill, not a pattern we cite.** In the
  Bleiholder–Naumann data-fusion taxonomy: LangChain's chunk merge was a bare
  `.extend()` (conflict-ignoring; the how-to was deleted upstream 2025-10), Google
  LangExtract resolves by first-pass-wins (conflict-avoiding — and its ungrounded
  extractions bypass dedup entirely, a duplicate multiplier), LLM×MapReduce resolves
  inside a reduce prompt. None surfaces candidates + provenance + severity. Ours does.
- **LangExtract independently validates the aligner design**: verbatim spans + a
  deterministic difflib aligner at a 0.75 fuzzy threshold — the same numbers
  ExtrCT-align/1.0 already uses.
- **Clinical practice deduplicates by terminology normalization, not string
  similarity** (RxNorm/UMLS via a normalizer; 2019 n2c2 Track 3). Designed v2 rung: let
  `list_key` reference a normalized code column once a normalizer stage exists; string
  similarity then becomes the within-block tiebreaker. Also transferable: i2b2-2008's
  textual-vs-intuitive split — make "said vs inferred" part of the schema, not hidden
  post-processing.
- **No validated similarity thresholds exist for short clinical phrases.** Winkler's
  numbers are 1990 census person-names; the popular 0.85/0.9 are tutorial folklore.
  Our thresholds are configurable precisely because they must be tuned on labeled
  pairs from the actual context (the grounding gold-set can serve double duty).
- **License watch**: `fuzzywuzzy` and `python-Levenshtein`/`Levenshtein` are GPL —
  red-list candidates for the  CI scan (realistic accidental transitive pulls);
  `rapidfuzz`, `thefuzz` (current), `jellyfish`, `splink` are MIT. Splink (MIT,
  Fellegi–Sunter, DuckDB backend we already run) is the reference implementation if
  entity resolution ever needs to outgrow difflib. LlamaExtract is sales-gated
  self-hosting (Enterprise + license key + Kubernetes) — fails license policy and one-operator bootstrap; never on the
  shippable path.

Verified in-container 2026-08-10 on synthetic data (a -safe cardiology note with a
deliberate LVEF contradiction, near-duplicate medication strings, and repeated findings):
wrapping determinism + coverage + slice fidelity across all three logics; the full merge
matrix (tolerance grouping, abstention, clustering with blocking key, tie→major,
label_and_null, refuse, adjudication composition, mixed-kind coercion, uid stability);
the chunk executor end-to-end with a stubbed transport — real request build, real
response reader, real repair ladder, real run rows with chunk provenance, resume with
refetch — and the merger over those rows, logged to `merge_run` and cleaned up.

---

## Addendum 2026-08-14 — the router wave supersedes the three-node lane

Prep - Text Wrapper, Run - Chunk Extract, and PostPrep - Extraction Merger are parked
in `deactivated/`. The lane is now: **Adapter - Wrapper & Merger** (config author for
wrapper.* AND merger.*, plus a reserved Merge Client input for the LLM-adjudication
wave) feeding **Run - Long Text** (wrap first, one FULL pipeline per chunk — the
complete repair ladder, per-chunk grounding with document-coordinate spans, per-chunk
certainty — bounded parallel, resume, merge last). Merge conflicts keep merge-def/1.0
semantics; major-severity conflicts are counted as `needs_manual` on the merge step
and in merge_run — the queue the future manual-merge step reads. wrap-def/1.0 and
merge-def/1.0 are unchanged; everything in this document about wrapping logics,
spans, and merge strategies still holds. See flow-control.md for the full pipeline
story and the migration recipe.
