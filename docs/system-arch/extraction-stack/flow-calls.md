# Flow-to-flow calls — call-def/1.0, Flow - Trigger, Flow - Call

One flow (or any script) calls another over Langflow's own HTTP API. Package
authority: `extrct/call_model.py`; canvas surfaces: **Flow - Trigger** (the receiving
mouth) and **Flow - Call** (the sender) in `extrct_flow`. Everything below marked
*measured* was verified live against Langflow 1.11.0 in the running container on
2026-08-14 (18/18 E2E steps, 20/20 unit checks, both suites in-container).

```
FLOW A                                                  FLOW B
[...] ─payload (Data)─> [Flow - Call] ────HTTP────> [Flow - Trigger] ─Payload─> [Flow - Controller] ─> ...
                         mode: wait │ post │ check
any script:  curl -X POST .../api/v1/webhook/<flow>  ──┘   (fire-and-forget lane)
```

## call-def/1.0 — the call as data

```json
{"mode": "wait", "target": "<flow-id UUID>", "payload": {"client": {"provider": "ollama"}},
 "delivery_node": "Flow - Trigger", "delivery_field": "data",
 "output_ids": [], "target_session_id": "", "timeout_s": 600, "job_id": ""}
```

`call_definition()` validates loudly (unknown keys raise with the allowed list; every
mode's requirements checked at authoring time), fills every default so the stored
document is complete, and stamps `call_uid = sha256(canonical_json)[:16]`.
**Deployment is not content**: `base_url` and the API key are forbidden inside the
document and raise on sight — they resolve from `EXTRCT_LANGFLOW_URL` /
`EXTRCT_LANGFLOW_API_KEY` inside `execute_call`, after the uid exists. Same JSON, same
`call_uid`, any machine.

Three modes, one endpoint (`/api/v2/workflows`, *measured*):

| mode | wire | returns |
|---|---|---|
| `wait` | POST `mode="sync"` | the target's outputs, inline; `output_ids` selects "the answer" |
| `post` | POST `mode="background"` | `{job_id, status, links}` immediately; the job row is durable (DB) |
| `check` | GET `?job_id=` | status while running; status + reconstructed outputs once `completed` |

## The measured traps this module encodes

- **Background tweaks match node ids ONLY** (`langflow/api/build.py:540`); the sync
  path matches node id *or* display name. An unresolved display-name key is silently
  dropped in background mode and the trigger fires empty. Therefore `execute_call`
  **resolves** `delivery_node` (id, display name, or component type) to the target
  flow's real node ids with one GET before sending — uniform across modes, and it
  proves the receiver exists before anything runs. Every matching node receives the
  payload (mirroring the v1 webhook route).
- **Job statuses are lowercase**: `queued`, `in_progress`, `completed`, `failed`.
- **A FAILED job answers the status GET with HTTP 500 + code `JOB_FAILED`** (timeout:
  408 + `EXECUTION_TIMEOUT`). `parse_wire` returns these as a `status: "failed" /
  "timed_out"` result with the target's own error detail in `errors` — an answer the
  flow can branch on, not an exception. Every other 4xx/5xx raises with a what-to-DO
  hint (403 → key setup; 404 → flow id; 422 → request shape).
- **v2 rejects endpoint names** (422; flow ids only — enforced at authoring time with
  the measured message). Endpoint names work only on the v1 webhook route.
- **A v2 sync response carries the run's own top-level `job_id`** — when flow A posts
  flow B, two job ids exist; the posted one lives in Flow - Call's Result/Call Record.
- **Self-calls cannot deadlock**: sync component methods run under `asyncio.to_thread`
  (component.py:1397), so the event loop serves the nested request while the caller
  blocks in a worker thread. Depth-1 is the supported pattern; the pool is ~24 threads.
- **Component inputs must not shadow base attributes**: an input named `session_id`
  resolves to the RUNNING graph's session at flow runtime (a posted job silently
  inherited the caller's session before the input was renamed `target_session_id`).
  Same family as the `_inputs` collision.

## Flow - Trigger (`extrct_Webhook_trigger`)

A 1-node ingress: `data` in, **Payload** (Data) out, typically wired straight into the
Flow - Controller's document input — that is what makes a whole flow callable-as-API.
The capital-W `Webhook` in BOTH the class name (`ExtrctWebhookTrigger`) and the
internal name is load-bearing (*measured*): the v1 route
`POST /api/v1/webhook/<flow-id-or-endpoint-name>` finds receivers by the case-sensitive
substring `"Webhook" in node.id`, and a canvas drag derives the node id from the
CLASS (`ext:extrct_flow:<ClassName>@extra-<sfx>` — measured from a real drag; caught
live 2026-08-14 when a dragged trigger's id carried the old class name and would have
been invisible to the webhook route). The body is injected into the field named `data`.
One component, two delivery lanes: v1 webhook (any external script, one line, 202
fire-and-forget) and v2 tweaks (Flow - Call).

Stricter than the stock Webhook on purpose: invalid JSON **raises with the parse
error** (stock silently wraps it as `{"payload": text}` and the sender's bug surfaces
three nodes later as a schema mismatch); a missing payload raises with both delivery
recipes unless `Allow Empty` is on. In v1-webhook (background) runs a raise reaches
only the server log / Langfuse — callers who want errors use the v2 lanes.

## Flow - Response (`extrct_flow_response`)

The outbound half of the handshake — put it directly before the Chat Output the
caller reads. Typed slots (Extracted / Certainty / Grounding / Run Report / Merged +
an Extra list) become ONE `flow-response/1.0` envelope: sections stay **namespaced**
(the stock Data Operations "Combine" does a flat key union — measured on a real
canvas 2026-08-14, it turned `run_uid` into a list of duplicates and would silently
overwrite colliding keys), `run_uid` reconciles to a single scalar (differing uids
across sections raise — one response describes one run), `present` declares coverage
(absence is not evidence), `flagged` lists sections reporting `ok=false`/`error`, and
`response_uid` is the content hash. **Response Text** is canonical JSON with no
markdown fence — verified end-to-end: the caller does `json.loads(output_text)` with
zero stripping. The recommended callable-flow chain:

```
[Flow - Trigger] ─> [Flow - Controller] ─> ...pipeline... ─> [Flow - Response] ─Response Text─> [Chat Output]
```

Point the caller's Output IDs at that Chat Output for a single-answer response.

## Flow - Call (`extrct_flow_call`)

Mode dropdown (S5 dynamic UI): **wait** / **post** / **check** (job handle wired from a
previous post, or a pasted job id). Payload from a Data/Message handle (a flow-def
document is the natural payload — chained controllers come free) or pasted JSON.
Outputs (one cached execution): **Result** — the parsed answer (outputs / job handle /
status), and **Call Record** — the full call document + `call_uid` next to what
happened (`status`, `job_id`, `http_status`, `latency_ms`, resolved `base_url`,
`delivery_ids`): declared next to executed, independent measurement's join point.

Error surfacing by mode: `wait` returns errors inline; `post` is deliberately blind —
a posted flow's failure reaches the caller only via `check`, Langfuse, or the server
log. Size `timeout_s` to the target in wait mode (it holds the connection for the
whole run; the same timeout also covers the resolve GET) and prefer post+check for
anything sweep-sized.

## Setup (one-time)

1. Mint an API key: Langflow UI → Settings → API Keys.
2. `deploy/.env`: `EXTRCT_LANGFLOW_API_KEY=<key>` (the compose file only passes it
   through; the key never appears in a flow, a document, or git).
3. `docker compose --profile prototype up -d` — env is read at boot, `restart` is not
   enough. `EXTRCT_LANGFLOW_URL` defaults to `http://localhost:7860`, correct from
   inside the container.

The v1 webhook lane needs the same `x-api-key` (WEBHOOK_AUTH_ENABLE is true here,
*measured*); with the 127.0.0.1 port binding it stays a loopback-only surface. 
unchanged: whatever rides a payload is synthetic / de-identified only.

## Ops notes

- v1 webhook runs are `asyncio.create_task` fire-and-forget with **no queue cap**
  (*measured*) — pace external senders; the v2 background lane is bounded and durable.
- A server restart kills in-flight background runs; the job row survives (its status
  stays truthful) and completed results remain queryable.
- Package edits (`extrct/`) need a container restart to reach IN-FLOW components — the
  server process caches imports; exec-based tests see live files and will happily pass
  against code the server is not yet running.
