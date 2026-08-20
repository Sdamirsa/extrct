# Synthetic test report and ground truth

*A conformance fixture, not a reasoning test. Every value is stated verbatim in the text, so a
wrong answer indicts the **provider, the schema encoding or the plumbing** — never the model's
clinical judgement. That separation is the whole point: when this fails you know where to look.*

**Fully synthetic. No patient, real or derived. Safe for OpenRouter under .**

---

## The report

```
TRANSTHORACIC ECHOCARDIOGRAM
Study date: 12 March 2026
Record: SYNTH-0042 (synthetic training record, not a real patient)

FINDINGS
Left ventricle is dilated with globally reduced systolic function.
LVEF 47% by biplane Simpson.
Mild mitral regurgitation.
No pericardial effusion.

IMPRESSION
1. Dilated cardiomyopathy - definite, current.
2. Prior myocardial infarction - probable, past.
```

Design notes: the impression lines carry the certainty and timing words **verbatim from the enum
vocabulary**, so no inference is required to map them. There is exactly one explicit negative
(`No pericardial effusion`) and exactly one field the report never mentions (`contrast_agent`) —
those two are the most diagnostic in the whole fixture.

---

## Schema rows

Paste into the Schema Builder's Variables table. `Root Name = extract`, `Encoding = strict_nullable`.

| name | type | is_list | description | parent | options | constraints | required |
|---|---|---|---|---|---|---|---|
| `study` | object | false | The imaging study. | | | | true |
| `modality` | str | false | Imaging modality, as stated in the report header. | `study` | `TTE,CMR,CTA,CXR,TEE,NUC` | | true |
| `study_date` | date | false | Date of the study, as an ISO date. | `study` | | | true |
| `lvef` | float | false | Left ventricular ejection fraction as a percentage. Null if not stated numerically. | `study` | | `{"ge":0,"le":100}` | false |
| `pericardial_effusion` | bool | false | True if an effusion is present, false if explicitly absent, null if not mentioned. | `study` | | | false |
| `contrast_agent` | str | false | Contrast agent used, if any. Null if the report does not mention contrast. | `study` | | | false |
| `findings` | str | true | Each sentence of the FINDINGS section as its own entry. | `study` | | | true |
| `diseases` | object | **true** | One entry per numbered item in the IMPRESSION. | `study` | | | true |
| `disease_name` | str | false | The disease exactly as written, without the certainty or timing words. | `diseases` | | | true |
| `disease_probability` | str | false | The certainty word used in the report. | `diseases` | `definite,probable,possible,unlikely,ruled_out` | | true |
| `disease_temporality` | str | false | The timing word used in the report. | `diseases` | `current,past,resolved,suspected,family_history` | | true |

**11 vars.** Fewer means a row has a blank name and is being skipped.

---

## Ground truth

Under `strict_nullable`:

```json
{
  "study": {
    "modality": "TTE",
    "study_date": "2026-03-12",
    "lvef": 47.0,
    "pericardial_effusion": false,
    "contrast_agent": null,
    "findings": [
      "Left ventricle is dilated with globally reduced systolic function.",
      "LVEF 47% by biplane Simpson.",
      "Mild mitral regurgitation.",
      "No pericardial effusion."
    ],
    "diseases": [
      {
        "disease_name": "Dilated cardiomyopathy",
        "disease_probability": "definite",
        "disease_temporality": "current"
      },
      {
        "disease_name": "Prior myocardial infarction",
        "disease_probability": "probable",
        "disease_temporality": "past"
      }
    ]
  }
}
```

---

## What each field tests, and what its failure means

| Field | Tests | A wrong answer means |
|---|---|---|
| `modality` | **enum enforcement** | The provider is not honouring `enum`. On Ollama the grammar should make a non-member unrepresentable; on OpenRouter, check the pinned endpoint declares `structured_outputs` |
| `study_date` | **format normalisation** | `12 March 2026` → `2026-03-12` is a formatting task, not reasoning. Failure means the `format: date` keyword is being ignored — common, and worth knowing about your endpoint |
| `lvef` | **numeric typing + range** | `"47%"` as a string means type coercion is doing the work rather than the grammar. A value outside 0–100 should come back `invalid`, **never clamped** |
| `pericardial_effusion` | **explicit negative** | `null` here is a real failure: the report *does* state absence. This is the field that catches a model conflating "not mentioned" with "stated as absent" |
| `contrast_agent` | **genuine absence** | Must be `null`. Anything else is fabrication. Under `native_required` the key vanishes entirely, which is why that encoding is the weaker default |
| `findings` | **list of strings** | Count and content matter; exact segmentation does not — see scoring below |
| `diseases` | **list of objects** | Exactly 2. Fewer means nesting collapsed; more means the model split an impression line |
| `disease_probability` / `_temporality` | **enum on a nested repeated field** | The words appear verbatim in the text, so a wrong value indicts the enum path, not comprehension |

---

## Scoring

**Exact-match these nine.** They are deterministic and any deviation is a genuine failure:

`modality` · `study_date` · `lvef` · `pericardial_effusion` · `contrast_agent` ·
`len(diseases) == 2` · both `disease_probability` · both `disease_temporality`

**Do not exact-match `findings`.** Sentence segmentation is a legitimate judgement call — a model
may merge or split lines, or drop the trailing period. Score it as: 4 entries expected, and each
of `dilated`, `47%`, `mitral regurgitation`, `pericardial effusion` appears somewhere in the list.
Holding free text to exact equality manufactures failures that say nothing about the plumbing.

**`disease_name` is near-exact.** Accept `Prior myocardial infarction` and `Myocardial
infarction`; reject anything that drags the certainty or timing words into the name, since the
schema explicitly asks for them to be separated.

---

## Running it

1. **Ollama first.** Inside the wall, free, and it isolates schema problems from provider
   problems. Expect `final_status: ok` with `layers_used: []`.
2. **Then OpenRouter**, `google/gemma-4-26b-a4b-it` pinned to `deepinfra/fp8`
   (`Data Classification = synthetic`). About $0.00002 per call.
3. **Then flip Encoding to `native_required`** and re-run. `contrast_agent` — and probably
   `pericardial_effusion` and `lvef` — should **disappear from the output entirely** rather than
   coming back `null`. That single comparison is the clearest demonstration of why
   `strict_nullable` is the recommended default: an explicit `null` is a claim you can score, a
   missing key is silence you cannot distinguish from absence.

If `layers_used` is non-empty on a report this simple, the model needed repair to produce
conforming output — which is a finding about that endpoint worth recording, not a problem with
the fixture.
