# Testing the ExtrCT components on the canvas

*A walkthrough, in dependency order. The happy path is already proven in-container — the
tests that earn their time are the **deliberate breaks** in §4, because those prove the guards
fire. A guard nobody has seen fire is a guard nobody should trust.*

**Before starting:** `docker compose --profile prototype up -d` · Langflow http://localhost:7860 ·
components appear under **ExtrCT** in the sidebar.

---

## 1 · ExtrCT Schema Builder (alone)

Drop it on the canvas. It ships four default rows, so it works untouched.

| Run this output | Expect |
|---|---|
| **Schema** | `Data` with `schema`, `schema_uid`, `encoding`, `variable_count: 4` |
| **Schema Hash** | 16 hex chars |
| **Pydantic Source** | two classes, `Study` then `Extract`, children first |
| **Variables** | 4-row table |

**Status line** should read `4 vars | strict_nullable | <uid>`.

**Then check the property that matters:** run it twice. **The `Schema Hash` must be identical.**
Now reorder the rows in the table and run again — **still identical**, because ordering is not
part of identity. If either changes, content addressing is broken and nothing downstream can be
replayed.

**Then flip `Encoding` to `native_required`** and look at the hash again. It **must change** —
they are different requests. Keep this window open for §3.

---

## 2 · ExtrCT Ollama Request (alone)

| Field | Set to |
|---|---|
| Base URL | `http://host.docker.internal:11434` (or your GPU host) |
| Model | `qwen3:0.6b` |
| Context Window | `8192` |

| Run this output | Expect |
|---|---|
| **Client** | `Data` with `provider: "ollama"` and the full spec |
| **Capability Report** | `enforcement_verified` showing `numeric_range: false` |

That `numeric_range: false` is not a bug — Ollama's grammar genuinely does not enforce
`minimum`/`maximum`. It is there so you never assume the model is policing your ranges.

This component makes **no network call**. If it errors, the problem is config, not connectivity.

---

## 3 · The chain

```
[ExtrCT Schema Builder] --Schema--┐
                                  ├--> [ExtrCT Structured Extract] --> Extracted
[ExtrCT Ollama Request] --Client--┘         (paste a report into `Text`)
```

Use a report with a **negative finding** — it matters in a moment:

> Transthoracic echocardiogram. LVEF 47% by biplane Simpson. Mild global hypokinesis.
> Mild mitral regurgitation. No pericardial effusion.

| Output | Expect |
|---|---|
| **Extracted** | `{"study": {"modality": "TTE", "lvef": 47, "findings": [...]}}` |
| **Run Report** | `final_status: "ok"`, `layers_used: []` |
| **Attempts** | 1 row, `layer: request`, `valid: true` |
| **Failed** | **empty** `{}` |

`final_status: "ok"` with `layers_used: []` means no repair rung fired — the model got it right
first time. Anything else is `repaired`, and that distinction is what stops repair inflating your
success rate later.

### The encoding comparison — do this one

Flip the Schema Builder's `Encoding` to `native_required` and re-run the chain.

**`lvef` disappears from the output.** Same rows, same prompt, same model.

That is the finding worth internalising: a missing key cannot be distinguished from *"absent in
the patient"*. With `strict_nullable` an absent value comes back as an explicit `null`, which is
a claim you can score. This is why the recommended default is `strict_nullable`.

---

## 4 · Deliberate breaks — the tests that matter

Each of these **should fail**, in a specific way. If any silently succeeds, tell me.

### 4a Schema Builder rejects malformed input

Edit the Variables table and run **Schema**:

| Break | Expected error |
|---|---|
| `type` = `blob` | `unknown type 'blob'` |
| `parent` = `nope` | `parent 'nope' is not a variable in this set` |
| set `parent` of `lvef` to `modality` | `parent 'modality' has type 'str', must be 'object'` |
| two rows both named `lvef` | `duplicate variable names` |
| `constraints` = `{"nope":1}` | `unknown constraint 'nope'` |
| delete all children of `study` | `'study' is type 'object' but has no children` |

Every one raises **on the canvas**, before any model call. A half-valid schema must never reach a
model.

### 4b Ollama silently truncates — see it happen

Paste a **long** report (repeat the text ~40 times), then set **Context Window = `512`**.

You should get `final_status: invalid` and a `context_pressure` flag. Now set
**Fail On Context Pressure = off** and re-run: you get a **confident answer** — extracted from
text the model largely never saw. That is the single worst failure mode in the stack, and it
returns HTTP 200 either way. Turn the guard back on.

### 4c Truncation produces a valid-looking prefix

Set **Max Output Tokens = `20`**, restore Context Window to `8192`.

Expect `final_status: invalid`, flag `truncated_by_length`, and **`Attempts` showing the ladder
did not run**. The gate fires first on purpose — a truncated body is a well-formed *prefix*, and
`json_repair` would brace-balance it into a plausible wrong answer.

### 4d Range violations are not laundered

Add a `constraints` of `{"ge":0,"le":50}` to `lvef` and re-run on the 47% report — passes. Change
to `{"ge":0,"le":40}` — now `47` violates it.

Expect `final_status: invalid` with `47 is greater than the maximum of 40`. **It must not clamp
to 40.** Coercion fixes *types*, never *values*; clamping would launder a wrong answer into a
well-formed one.

### 4e Thinking changes the answer

Set **Thinking = `true`**, keep everything else identical, run twice.

Expect a *different* extraction from the same seed. This is why `think` defaults off — it is an
experimental axis, not a quality setting.

### 4f The ladder is genuinely switchable

Clear **Repair Ladder** to empty and set a prompt that provokes fenced output (add
`Wrap your answer in a markdown code block.` to the System Prompt).

Expect `final_status: invalid`. Re-add `json_repair` → `repaired`, with `layers_used: ["json_repair"]`.
That is the ladder behaving as an axis you control rather than a hidden fallback.

---

## 5 · ExtrCT OpenRouter Request

**Outside the wall. Synthetic content only.**

### 5a The egress guard fires first

Drop it and run **Client** with `Data Classification` left **empty**.

Expect `EgressRefused` — on the canvas, before any network call. Now set
`Data Classification = synthetic`, `Egress Route = direct`, and leave `Allow Direct Egress` off:
refused again. Two gates on a red line is deliberate. Enable it to proceed.

### 5b Endpoint pinning is required

Leave `Endpoint Tag` empty and run **Client**, then wire into Extract and run.

Expect a raise: default routing re-ranks every ~5 minutes, so an unpinned run is not reproducible.

To find valid tags for a model, in the container:

```bash
docker compose exec -T langflow python -c "
import asyncio,httpx
from extrct import openrouter
async def m():
    async with httpx.AsyncClient() as h:
        eps = await openrouter.fetch_endpoints('google/gemma-4-26b-a4b-it', http=h)
        for e in eps: print(e['tag'], '| structured_outputs:', 'structured_outputs' in (e.get('supported_parameters') or []))
asyncio.run(m())"
```

⚠ **Use the exact model id you will send.** A `:free` variant has its own, different endpoint
list — a tag from the paid slug 404s against it every time.

### 5c `require_parameters` converts a silent downgrade into a refusal

Pin an endpoint the command above shows as `structured_outputs: False` (e.g. `cloudflare`).

- `Require Parameters` **on** → **404**, "No endpoints found that can handle the requested parameters"
- `Require Parameters` **off** → **200**, silently served by a provider that never promised to honour your schema

The 404 is the desired outcome. Leave it on.

### 5d A working call

`Endpoint Tag = deepinfra/fp8`, `Data Classification = synthetic`, `Egress Route = direct`,
`Allow Direct Egress = on`. Wire into Extract.

Expect `final_status: ok` and a **Run Report** carrying `cost_usd` (~$0.00002) and the served
provider. Note the attribution is a **display name** — it cannot distinguish `google-vertex` from
`google-vertex/us-central1`, so treat divergence detection as best-effort.

---

## 6 · Confirm the log

Everything above wrote to Postgres.

```bash
docker compose exec -T extrct-postgres psql -U extrct -d extrct -c "SELECT schema_encoding, provider, final_status, count(*), round(avg(latency_ms)) AS ms FROM extraction_run GROUP BY 1,2,3 ORDER BY 4 DESC;"
```

```bash
docker compose exec -T extrct-postgres psql -U extrct -d extrct -c "SELECT count(*) AS rows_with_raw_text FROM extraction_run WHERE input_text IS NOT NULL;"
```

The second **must return 0**. That is the  property holding: the log knows the hash of every
input and the text of none.

And the one that pays for the whole design:

```bash
docker compose exec -T extrct-postgres psql -U extrct -d extrct -c "SELECT a.layer, a.valid, count(*) FROM extraction_attempt a GROUP BY 1,2 ORDER BY 3 DESC;"
```

That is your error taxonomy, accumulating across every run you have ever done — the thing no
library that owns its own retry loop would have let you keep.

---

## What to report back

For anything that behaved unexpectedly: the component, the field you changed, what you expected,
and the **Run Report** output. `run_uid` is a content hash, so I can reproduce your exact call
from it.

Most useful of all: **anything in §4 that did not fail.** A guard that does not fire is worse than
no guard, because it is trusted.
