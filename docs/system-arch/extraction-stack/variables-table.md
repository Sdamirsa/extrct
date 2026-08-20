# The Variables table — complete reference

*Every column of the ExtrCT Schema Builder, what it does, and where it bites. The eight columns
exist because Langflow's own schema table has four and cannot express nesting, enums,
constraints or optionality.*

## The eight columns

### `name`

Key in the output JSON. **Unique across the whole schema set**, not just within its parent —
because it is also the registry key and the join column for *"which schemas use LVEF?"*.

Use `snake_case`: it becomes a Python attribute on the generated Pydantic class.

### `type`

| Value | JSON Schema | Notes |
|---|---|---|
| `str` | `{"type":"string"}` | |
| `int` | `{"type":"integer"}` | |
| `float` | `{"type":"number"}` | |
| `bool` | `{"type":"boolean"}` | |
| `date` | `{"type":"string","format":"date"}` | |
| `datetime` | `{"type":"string","format":"date-time"}` | |
| `object` | a nested container | **Requires ≥1 child row** or the build raises |

`object` is not a data type you fill in — it is a container. A row with `type: object` and no
children is an error, deliberately: an empty object in a schema is always a mistake.

### `is_list` (As List)

Wraps the field in an array.

On a scalar → `["a","b","c"]`. On an **object** row → a **list of objects**, which is how you say
*many findings per report* or *many diseases per study*:

```json
"diseases": [
  {"disease_name": "...", "disease_probability": "...", "disease_temporality": "..."}
]
```

### `description`

**Sent to the model as prompt text.** This is your main lever on extraction quality — treat it as
prompt engineering, not documentation.

Say what counts and what does not:

- Weak: `"The ejection fraction"`
- Better: `"LV ejection fraction as a percentage. Use the biplane Simpson value if several are reported. Leave null if only a qualitative description is given."`

A blank description on an ambiguous field is the most common cause of inconsistent extraction.

### `parent`

Name of an `object` row. Blank means top level. **This is how nesting works.**

The parent must exist and must have `type: object` — both are checked, and both raise on the
canvas rather than producing a half-valid schema. A cycle raises too.

### `options` (enum)

Comma-separated allowed values → JSON Schema `enum`.

**The single most effective column in the table.** An enum is enforced by the grammar on Ollama
and by strict mode on OpenRouter, and it is the difference between a field you can aggregate and
a field you cannot. Free text gives you `current`, `currently`, `active`, `ongoing` across four
runs of the same report.

Values are matched case-insensitively by the `coerce` rung, so `tte` becomes `TTE` rather than
failing.

### `constraints`

A JSON object. Recognised keys:

| Key | JSON Schema | For |
|---|---|---|
| `ge` / `le` | `minimum` / `maximum` | numeric bounds |
| `gt` / `lt` | `exclusiveMinimum` / `exclusiveMaximum` | strict bounds |
| `pattern` | `pattern` | regex, e.g. ICD-10 `"^[A-Z]\\d{2}"` |
| `min_length` / `max_length` | `minLength` / `maxLength` | strings |
| `min_items` / `max_items` | `minItems` / `maxItems` | arrays |

An unrecognised key raises — silently ignoring a constraint you thought was enforced would be
worse than refusing it.

> ⚠ **Ollama's grammar does not enforce numeric ranges.** Measured: `lvef: 250` passed a 0–100
> schema. The client-side validator catches it, which is why an out-of-range value comes back
> `final_status: invalid` — and why the repair ladder **never clamps**. Clamping 250 to 100 would
> launder a wrong answer into a well-formed one.

### `required`

Off makes the field optional. **How that is encoded depends on the Encoding axis**, and the
difference is not cosmetic:

| Encoding | Optional field behaves as |
|---|---|
| `strict_nullable` | key always present, value `null` when absent |
| `native_required` | **key may be missing entirely** |

Measured on identical rows: `native_required` dropped both optional fields from the output.
A missing key cannot be distinguished from *"absent in the patient"* — which for a negative
finding like `pericardial_effusion: false` is the opposite claim. An explicit `null` is something
you can score; a missing key is not.

---

## Root Name

Names the **schema**, not a field. It sets the JSON Schema `title`, names the generated Pydantic
class (`extract` → `Extract`), and is sent as `response_format.json_schema.name` on OpenRouter.

**It is hashed into `schema_uid`.** Renaming it produces a different schema identity and breaks
replay of earlier runs — settle it before any run cites one.

## Encoding

The declared experimental axis. See `required` above. Default `strict_nullable`, and the
recommendation is to keep it constant unless you are deliberately ablating it.

## Schema Set

Registry namespace. Variable names are unique **within** a set, so `cardiac_echo_v1` and
`cardiac_mri_v1` can each have an `lvef`. Turn on **Persist To Registry** to upsert, then reload
with the ExtrCT Registry component (`operation: load_variables`) wired into **Variables (from
Registry)** — that input *replaces* the table, so a saved set drives the build without retyping.

---

## A worked example — diseases

| name | type | is_list | description | parent | options | constraints | required |
|---|---|---|---|---|---|---|---|
| `study` | object | false | The imaging study | | | | true |
| `modality` | str | false | Imaging modality | `study` | `TTE,CMR,CTA,CXR,TEE,NUC` | | true |
| `lvef` | float | false | LV ejection fraction, percent. Use biplane Simpson if several are given. | `study` | | `{"ge":0,"le":100}` | **false** |
| `findings` | str | true | Each distinct finding as its own string. | `study` | | | true |
| `diseases` | object | **true** | Diseases named or considered in the report. | `study` | | | true |
| `disease_name` | str | false | Disease as written in the report, not a normalised code. | `diseases` | | | true |
| `disease_probability` | str | false | The report's own certainty language. | `diseases` | `definite,probable,possible,unlikely,ruled_out` | | true |
| `disease_temporality` | str | false | Present now, historical, or only suspected? | `diseases` | `current,past,resolved,suspected,family_history` | | true |

Status line should read **8 vars**. Fewer means a row has a blank `name` and is being skipped.

**Why `disease_probability` is an enum, not a float.** A model asked for a numeric probability
invents one — `0.73` looks authoritative and means nothing. What is actually *in* the report is
the radiologist's hedging vocabulary, so extracting that is a grounded claim rather than a
fabricated one. If you do want a number, use `float` with `{"ge":0,"le":1}` and remember the
range is only enforced client-side.

## Failures, and what they mean

| Message | Cause |
|---|---|
| `unknown type 'x'` | `type` is not in the table above |
| `parent 'x' is not a variable in this set` | typo, or the parent row was deleted |
| `parent 'x' has type 'str', must be 'object'` | you nested under a leaf |
| `'x' is type 'object' but has no children` | container with nothing in it |
| `duplicate variable names: [...]` | names must be unique across the whole set |
| `unknown constraint 'x'` | not in the constraints table |
| `cycle in parent chain involving 'x'` | a row is its own ancestor |

All of these raise **before any model call**. A half-valid schema must never reach a model.
