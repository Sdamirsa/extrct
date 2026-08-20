# Langflow working reference

*Compiled 2026-08-04 from [docs.langflow.org](https://docs.langflow.org/) against the
running instance (Langflow 1.11.0). This is a build-time reference, not architecture —
the position on Langflow's role is in
[`../../integrations/langflow/README.md`](../../../integrations/langflow/README.md).
Anything below marked **unverified** was not confirmed against a running instance.*

**Local endpoints:** Langflow http://localhost:7860 · Langfuse http://localhost:3000 ·
Label Studio http://localhost:8080. Credentials in `deploy/.env`.

---

## 1. Mental model

A **flow** is a directed graph of **components**. Components have **inputs**, **outputs**,
and **ports**; ports connect output→input and are type-matched. The **Playground** is the
chat runner, and it only appears when the flow contains a **Chat Input** connected through
to a **Chat Output**.

Three things that are easy to get wrong early:

- **Port colour is the type contract.** Indigo = Message, fuchsia = LanguageModel,
  emerald = Embeddings, cyan = Tool, orange = Memory, red = JSON, grey = unknown/multiple.
  If a connection refuses to form, it is a type mismatch, not a UI bug.
- **A component emitting more than one type makes you pick.** Click the output label to
  select which type it emits.
- **Freezing a component freezes everything upstream of it too.** Useful for pinning an
  expensive retrieval step while iterating downstream; surprising if you don't know it.

Components carry a **version stamped at the moment you added them to that flow**, not the
version of the running server. Langflow distinguishes *update ready* (no breaking changes)
from *update available* (may disconnect ports). Back up the flow before accepting the
latter.

## 2. Data types

| Type | Import | Use |
|---|---|---|
| `Message` | `from lfx.schema import Message` | chat-shaped text, has `sender`, `text` |
| `Data` | `from lfx.schema import Data` | arbitrary structured record, `.data` dict |
| `DataFrame` | `from lfx.schema import DataFrame` | tabular, wraps pandas |

Return type annotations on your methods are what drive port colour and validation, so
annotate them.

## 3. Custom components — the important one

This is how Langflow stays useful to us: components are thin wrappers over `extrct-tools`,
never reimplementations. See §9.

### Skeleton

```python
from lfx.custom.custom_component.component import Component
from lfx.io import StrInput, BoolInput, DropdownInput, Output
from lfx.schema import Data, DataFrame, Message


class CohortQuery(Component):
    display_name: str = "Cohort Query"
    description: str = "Run a parameterised query against the EHR DB."
    documentation: str = "https://internal/docs"
    icon: str = "database"          # Lucide icon name
    name: str = "cohort_query"      # internal id, defaults to class name
    priority: int = 100             # lower sorts first

    inputs = [
        StrInput(name="patient_id", display_name="Patient ID"),
        DropdownInput(
            name="mode",
            display_name="Mode",
            options=["fixed_library", "model_written_sql"],
            value="fixed_library",
        ),
        BoolInput(name="verbose", display_name="Verbose", value=False),
    ]

    outputs = [
        Output(name="rows", display_name="Rows", method="run_query"),
    ]

    def run_query(self) -> DataFrame:
        self.log(f"querying {self.patient_id} via {self.mode}")
        ...
        self.status = f"{len(df)} rows"     # shown on the node in the UI
        return DataFrame(df)
```

Rules that actually bite:

- **`Output.method` must name a real method.** That method is what Langflow calls when the
  port is consumed.
- **Input values are read as `self.<input_name>`.** No accessor, no `get()`.
- `self.status` writes the little status line on the node. `self.log(...)` writes to the
  component log. `self.stop("output_name")` halts one output path without killing the rest.
- Import path: `from lfx.custom.custom_component.component import Component`. The old
  `from langflow.custom import Component` still works for backward compatibility, and most
  blog posts still show it — prefer the `lfx` path in anything we write.

### Where files go

Our container mounts `prototypes/components/` at `/components` (`LANGFLOW_COMPONENTS_PATH`).
Layout:

```
prototypes/components/
  ehr/                    <- category folder, becomes the sidebar section
    __init__.py           <- REQUIRED, or nothing loads
    cohort_query.py
```

```python
# prototypes/components/ehr/__init__.py
from .cohort_query import CohortQuery

__all__ = ["CohortQuery"]
```

- **Maximum depth is two levels.** `category/component.py` works;
  `category/sub/component.py` does not.
- A missing `__init__.py` is the single most common reason custom components don't appear.
- **Our mount is read-only** (`:ro`), deliberately — edits happen in the repo and are picked
  up on restart, not saved from the browser into a container layer that vanishes.
- `LANGFLOW_CUSTOM_COMPONENT_ADMIN_ONLY=true` in our compose, so only the superuser can
  create them in-UI. That's us; it matters if we ever demo on a shared login.

## 4. Agents and tool mode

The Agent component is an LLM loop: input → choose tool → execute → synthesise.

- **Tool Mode** is a toggle in a component's header menu. Enabling it rewrites the
  component's inputs and exposes a **Toolset** port. Connect Toolset → the Agent's **Tools**
  port. Components that already emit `Tool` skip this step.
- **Leave tool input fields blank** where you want the agent to supply the value. A filled
  field is a fixed value, not a default.
- Tool **Name** is fixed; **Description** and **Slug** are editable by double-clicking the
  action row. The docs are explicit that this is the first lever to pull when an agent
  misuses a tool — descriptions are what the model routes on.
- One agent can hold many tools; each tool can expose several actions. Disable actions you
  don't want offered.
- Agent has two outputs: **Response** (Message) and **Structured Response** (JSON to a
  schema). **Structured Response costs an extra LLM call.**
- Only **one API key per provider** can be configured globally.
- The Playground renders tool calls and their outputs inline, which is the fastest way to
  see why a tool was or wasn't chosen.

## 5. MCP — both directions

This is the load-bearing integration for us; MCP is already our declared tool handshake.

### Langflow as MCP *client* (consuming our tools)

Add servers in Settings first — the MCP Tools component is not draggable from the sidebar
until a server is registered. Three modes:

| Mode | Fields | Use |
|---|---|---|
| **STDIO** | command, args, env | local process, e.g. command `uvx`, arg `mcp-server-fetch` |
| **Streamable HTTP / SSE** | URL, headers | **what we'll use** for `extrct-tools` |
| **JSON** | paste the server config object | fastest for copying an existing config |

Component parameters: `mcp_server`, `tool` (blank = expose all), `use_cache`
(default false), `verify_ssl` (default true, and it applies globally to HTTPS).

Then enable Tool Mode on MCP Tools and wire Toolset → Agent Tools.

Credentials belong in **global variables** referenced from headers, not typed into the flow.

### Langflow as MCP *server* (exposing flows)

```
http://localhost:7860/api/v1/mcp/project/PROJECT_ID/streamable
```

- **A flow is only exposed if it has a Chat Output component.**
- Choose which flows are exposed under Projects → **MCP Server** tab → **Edit Tools**.
- Auth per project: API key (`x-api-key`), OAuth, or None. None is for trusted environments
  only — ours is loopback-bound, but prefer the key anyway.

We probably won't need this direction soon; it exists if we want a demo flow callable from
elsewhere.

## 6. Models — pointing at our gateway

The core **Language Model** component has an **OpenAI Compatible** provider option that
accepts any OpenAI-compatible base URL. That is exactly the Model Gateway contract,
so vLLM and Ollama are reachable without a bespoke component.

Parameters: `provider`, `model_name`, `input_value`, `system_message`, `temperature`,
`stream`.

Providers are configured globally: profile icon → **Settings** → **Model Providers**.

### Right now: Ollama on the host, and the localhost trap

Verified 2026-08-04. Ollama **is** already listening on the host at `:11434` and answers
`/v1/models` — but it has **zero models pulled**, so there is nothing to call yet:

```bash
ollama pull qwen2.5:7b
```

Then the trap. Langflow runs **inside a container**, so `localhost` means the container, not
your machine. Measured from inside `extrct-langflow-1`:

| Base URL | Result |
|---|---|
| `http://localhost:11434/v1` | **ConnectionError** |
| `http://host.docker.internal:11434/v1` | **HTTP 200** |

So configure the OpenAI Compatible provider with base URL
`http://host.docker.internal:11434/v1`. This is a Docker Desktop convenience name; it does
**not** exist on plain Linux, so anything we write for the hospital boxes must take the
gateway URL from config rather than hardcoding it.

This host-Ollama path is a **stopgap for learning Langflow**, not the architecture. The real
arrangement is vLLM and Ollama behind the Model Gateway at a single `/v1` router that is
also the egress filter (, M-04). Don't let a `host.docker.internal` URL leak into
anything that outlives the prototype.

## 7. Structured Output

Closest thing Langflow has to our schema-as-acceptance-gate philosophy.

Inputs: `llm` (a LanguageModel port), `input_value`, `system_prompt` (format instructions),
`schema_name`, `output_schema`.

`output_schema` is a table, one row per field:

| Column | Meaning |
|---|---|
| **Name** | field id; referenced downstream as `{FIELD_NAME}` |
| **Description** | what to extract — this is prompt text, treat it as such |
| **Type** | `str` (default), `int`, `float`, `bool`, `dict` |
| **As List** | emit a list instead of a scalar |

Outputs as **Structured Output Data** or **Structured Output DataFrame**.

**Corrected 2026-08-05 — an earlier version of this note called it prompt-and-parse. It is
not.** Reading the source: `structured_output.py` runs **trustcall** (`create_extractor`,
bind_tools + JSONPatch repair, `DEFAULT_MAX_ATTEMPTS=3`) with `llm.with_structured_output` as
fallback. So it *does* retry, and "Pydantic AI adds validation retry" is a false claim.

The real limits are one level down, and they are sharper:

- **The schema ceiling.** Both this component and `Agent.json_response` build their model via
  `lfx.helpers.base_model.build_model_from_schema`, which accepts only
  `{str, int, float, bool, list, dict}` and makes **every field required**. Verified
  in-container: `literal`, `enum`, `date`, `object`, `Optional[str]` all raise
  `ValueError: Invalid type`. So `Field(pattern=...)`, `ge/le`, `Literal[...]`, nesting and
  optionality are **unrepresentable** — not merely unenforced.
- **Mode selection is exception-driven.** `orchestrate_structured_output()` picks native if
  `hasattr(llm, "with_structured_output")` and silently falls back on exception, so the same
  flow can change extraction mechanism run-to-run with no signal. For a factorial study that
  is a confound.
- **The output is force-wrapped** as `objects: list[Model]` with `min_length=1`, so "exactly
  one object" and "possibly zero" are both inexpressible.

There is also a long-standing open issue about structured output on Ollama
(langflow#7169). State the comparison accurately: a schema-first library's advantage
over the stock components is schema expressiveness, explicit mode control, and loud
failure — not the existence of retry.

## 7b. Getting values out of nested JSON

Three nodes handle structured data, and they are not interchangeable.

**JSON Operations** (sidebar: *Data Operations*) — **the right tool.** Eight operations,
**one per node** (`limit=1`), so fan several out from the same port, one per gate. *Select
Keys* is **top-level only**; *Path Selection* and *JQ Expression* both run real **jq**, which
is installed in our image, so nesting and filtering are fully available.

```
{enabled_steps: [.pipeline_config.steps[] | select(.enabled) | .name]}
```

**Output-shape rule:** jq returning a **dict** → `Data(data=result)` with keys intact; jq
returning a **list or scalar** → `Data({"result": ...})`. Wrap list results in an object (as
above) to name the key yourself instead of inheriting a generic `result` that collides as
soon as two of these feed the same node.

*Quirk:* JQ Expression unwraps a top-level `data` key if present
(`jq_input = data_json["data"] if "data" in data_json else data_json`). Never introduce a
top-level `data` key or every expression silently shifts one level down.

**Parser** — structured data → **text**, for prompts and eyeballing. Not a JSON extractor;
it returns a `Message`, and `{some_dict}` renders Python `repr` with single quotes, which is
not valid JSON. `Data` is formatted with `format_map`, so nested subscripts work
(`{model_config[model_name]}`, `{pipeline_config[steps][0][name]}`) — but a missing
**top-level** key silently renders empty while a missing **nested** key raises `KeyError` and
kills the node. `DataFrame` input iterates one line per row; a `Data` object yields one block
total.

**Python Interpreter** — **cannot receive data at all.** Its only inputs are
`global_imports` (not wireable) and `python_code` (accepts a `Message`) — the visible port is
the *code* field. Data can only arrive by being interpolated into the source string. It also
rejects inline imports (`validate_code_safety`), restricts builtins (`safe_builtins`), and
returns captured stdout as `Data({"result": <str>})`, discarding structure. `Python Function`
is `legacy = True` and likewise has no data port. Use a custom component instead: a real
`HandleInput` port, several typed outputs, and a file in the repo rather than logic trapped
in flow JSON.

## 8. Sessions, memory, variables, API

**Sessions.** Chat history groups by `session_id`, which **defaults to the flow ID** — so
every conversation in a flow lands in one pile unless you set it. Set a custom session ID
per user/run to isolate. Messages persist in Langflow's `messages` table. The **Message
History** component reads them back; storage alone is not memory.

**Global variables.** Settings → Global Variables. Two types: **Generic** (visible in the
editor, still encrypted at rest) and **Credential** (masked; *cannot* be used in Session ID
fields). Both are encrypted in the DB. Referenced anywhere a globe icon appears. To seed
from the environment:

```
LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT=EXTRCT_DB_PASSWORD,GATEWAY_TOKEN
```

Only Name and Value come from the env, and they arrive typed as **Credential**.

**Component Parameters panel / the `API` toggle.** Selecting a component → **Parameters**
opens a pane that does two things: controls field visibility (mirroring the `advanced` flag
in the component's code) and marks fields as **API inputs**. Toggling `API` on a field saves
it as `api_editable`, which makes it appear in the `tweaks` object of the snippets generated
under **Share → API access**.

**It gates nothing.** Tweaks work against any component field regardless of this toggle — it
curates the generated snippet, it is not access control. Do not read an un-exposed field as
protected. (The older *Input Schema* pane under Share → API access was removed in 1.11;
this panel replaced it.)

**Running a flow over HTTP:**

```bash
curl --request POST \
  --url "http://localhost:7860/api/v1/run/$FLOW_ID?stream=false" \
  --header "Content-Type: application/json" \
  --header "x-api-key: $LANGFLOW_API_KEY" \
  --data '{"input_value": "hello world!", "output_type": "chat", "input_type": "chat"}'
```

`POST /api/v1/run/advanced/{flow_id}` takes explicit inputs, outputs, tweaks and
`session_id`. **Tweaks** override component settings per-call without editing the flow:

```json
"tweaks": { "ChatOutput-6zcZt": { "should_store_message": true } }
```

Keys: `GET /api/v1/api_key/`, `POST /api/v1/api_key/`.

**LFX** (bundled since Langflow 1.6) runs a flow JSON with no server, no DB:
`lfx run` streams to stdout, `lfx serve` exposes `/flows/{flow_id}/run`. It uses a
`NoopSession`, so nothing persists — no saved flows, no message history. Useful for turning
a prototype into a callable step without dragging the whole server along. `lfx pull` /
`lfx push` sync `prototypes/flows/` against the running server.

## 9. How we should actually use this

The architecture note has the full argument; the operational short version:

1. **Never rebuild the switchboard here.** It exists, in LangGraph, and it runs all 8 cells
   deterministically.
2. **Write custom components as thin wrappers over `extrct-tools`, ideally over MCP.** Then
   every hour on the canvas exercises the real tool contract instead of a parallel
   implementation, and there is nothing to keep in sync.
3. **Flows are disposable.** `prototypes/flows/` carries no reproducibility guarantee — flow
   JSON is DB-bound, carries UI coordinates beside logic, and does not export
   deterministically. If a flow proves an idea, reimplement it in `extrct-agent`.
4. **Synthetic and de-identified data only**. Langflow is a code-execution
   surface with a 2026 CVE record; it stays out of the PHI blast radius.

## 10. Gotchas

- **`LANGFUSE_HOST` is deprecated — use `LANGFUSE_BASE_URL`.** Both are set in our compose;
  `BASE_URL` wins when both are present. These are read **at container start**, so editing
  `deploy/.env` needs `docker compose --profile prototype up -d`, never `restart` —
  `restart` reuses the container's existing config.
- **MCP tool settings can be lost on upgrade.** Flows upgraded from ≤ 1.7.1 need
  `tool_mode = True` set manually in the component's code editor. Keep MCP server config in
  compose env, not clicked into the UI.
- **Custom components invisible** → almost always a missing `__init__.py`, or nesting deeper
  than `category/component.py`.
- **No Playground input box** → the flow lacks Chat Input → model/agent → Chat Output.
- **Postgres 15+ required** (`UNIQUE NULLS DISTINCT`). Ours is 17.
- **Database mismatch after upgrade** → `langflow migration --fix`.
- **Two Langflow instances in one browser** → auth errors; use separate browser profiles.
- **Component "update available"** (as opposed to "update ready") can disconnect ports.
  Duplicate the flow first.
- Flow JSON export is **not** deterministic — never diff it expecting signal.

## Sources

[Components](https://docs.langflow.org/concepts-components) ·
[Custom components](https://docs.langflow.org/components-custom-components) ·
[Flows](https://docs.langflow.org/concepts-overview) ·
[Agents](https://docs.langflow.org/agents) ·
[Agent tools](https://docs.langflow.org/agents-tools) ·
[MCP client](https://docs.langflow.org/mcp-client) ·
[MCP server](https://docs.langflow.org/mcp-server) ·
[Models](https://docs.langflow.org/components-models) ·
[Structured Output](https://docs.langflow.org/structured-output) ·
[Memory](https://docs.langflow.org/memory) ·
[Global variables](https://docs.langflow.org/configuration-global-variables) ·
[API](https://docs.langflow.org/api-reference-api-examples) ·
[LFX](https://docs.langflow.org/lfx-overview) ·
[Environment variables](https://docs.langflow.org/environment-variables) ·
[Langfuse integration](https://docs.langflow.org/integrations-langfuse) ·
[Troubleshoot](https://docs.langflow.org/troubleshoot)
