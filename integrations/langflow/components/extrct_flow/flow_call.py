"""Flow - Call — one flow (or script) calls another, with a wait / post / check toggle.

Thin wrapper over extrct.call_model (call-def/1.0): the component gathers widget
values into a call document, the package validates it, stamps ``call_uid``, builds the
wire request, sends it, and parses the reply. Any Python caller gets identical
behaviour from ``call_model.execute_call`` — the canvas adds nothing but wiring.

Modes:
- ``wait``  -> POST /api/v2/workflows mode=sync: runs the target flow inline, Result
  carries its outputs. The blocking HTTP call is safe from inside a flow because sync
  component methods run under ``asyncio.to_thread`` (measured, component.py:1397) —
  the event loop stays free to serve the nested request. Depth-1 self-calls are the
  supported pattern; do not build deep synchronous chains (the worker pool is ~24
  threads on this box).
- ``post``  -> mode=background: returns ``{job_id, status, links}`` immediately. The
  job row is durable (DB) — poll it any time with ``check``. Errors of a posted flow
  reach the caller ONLY via check / Langfuse / server log; that is the price of not
  waiting, by design.
- ``check`` -> GET /api/v2/workflows?job_id=: status while running, status PLUS the
  reconstructed outputs once finished.

Payload delivery: the delivery node ("Flow - Trigger" by default — id, display name,
or type all work) is resolved to the target flow's REAL node ids with one GET before
sending. Resolution is load-bearing, not convenience (measured 2026-08-14): the sync
path matches tweak keys by id or display name, but the background path builds from the
DB and matches ``vertex.id`` ONLY (build.py:540) — an unresolved display-name key is
silently dropped there and the trigger fires empty. Resolution also proves the
receiver exists before anything runs. The payload rides as canonical JSON into the
trigger's ``data`` field on every matching node (v1-webhook-mirroring semantics).

Credentials: the API key is NEVER an input and never part of the call document — it
resolves from ``EXTRCT_LANGFLOW_API_KEY`` at send time (same red line as model keys).
Base URL is deployment, not content: ``EXTRCT_LANGFLOW_URL`` or the advanced override,
resolved AFTER ``call_uid`` is stamped, so the same call document hashes identically on
any machine.

No Overrides thread on this node ON PURPOSE: the call document IS authored
configuration; threading flow-config over it would be config-about-config. Revisit
only if a ``call`` section ever joins flow-def.
"""

import json

from lfx.custom.custom_component.component import Component
from lfx.io import (
    DropdownInput,
    HandleInput,
    IntInput,
    MessageTextInput,
    MultilineInput,
    Output,
)
from lfx.schema.data import Data
from lfx.schema.message import Message

from extrct import call_model

MANAGED_FIELDS = (
    "target", "payload", "payload_json", "delivery_node", "delivery_field",
    "input_value", "output_ids", "target_session_id", "job", "job_id",
)
MODE_FIELDS = {
    "wait": ("target", "payload", "payload_json", "delivery_node", "delivery_field",
             "input_value", "output_ids", "target_session_id"),
    # output_ids is hidden for post: the server ignores it in background mode (measured).
    "post": ("target", "payload", "payload_json", "delivery_node", "delivery_field",
             "input_value", "target_session_id"),
    "check": ("job", "job_id"),
}
REQUIRED_WHEN_SHOWN = ("target",)


class ExtrctFlowCall(Component):
    display_name = "Flow - Call"
    description = "Call another flow: wait for its outputs, post it as a background job, or check a job."
    documentation = "docs/system-arch/extraction-stack/flow-calls.md"
    icon = "send"
    name = "extrct_flow_call"

    inputs = [
        DropdownInput(
            name="mode",
            display_name="Mode",
            options=list(call_model.MODES),
            value="wait",
            real_time_refresh=True,
            info="wait = sync, Result carries the target flow's outputs. post = background job, "
                 "Result carries the job handle. check = poll a job_id for status/outputs.",
        ),
        MessageTextInput(
            name="target",
            display_name="Target Flow",
            info="Flow id (UUID) of the flow to call — copy it from the flow's URL or API access pane.",
            value="",
            required=True,
            show=True,
        ),
        HandleInput(
            name="payload",
            display_name="Payload",
            input_types=["Data", "Message"],
            required=False,
            show=True,
            info="The JSON delivered to the target's Flow - Trigger. Data wins over pasted JSON below. "
                 "A flow-def document is the natural payload — chained controllers come free.",
        ),
        MultilineInput(
            name="payload_json",
            display_name="Payload JSON (paste)",
            value="",
            show=True,
            advanced=True,
            info="Fallback when nothing is wired to Payload. Must be a JSON object.",
        ),
        MessageTextInput(
            name="delivery_node",
            display_name="Delivery Node",
            value="Flow - Trigger",
            show=True,
            advanced=True,
            info="Node id, display name, or component type in the target flow whose field receives "
                 "the payload. Resolved to real node ids via one GET before sending (measured: the "
                 "background path matches node ids ONLY; resolution makes wait and post uniform and "
                 "verifies the receiver exists). Default reaches any Flow - Trigger.",
        ),
        MessageTextInput(
            name="delivery_field",
            display_name="Delivery Field",
            value="data",
            show=True,
            advanced=True,
            info="Input field on the delivery node. 'data' is also what the v1 webhook route injects into.",
        ),
        MessageTextInput(
            name="input_value",
            display_name="Input Value",
            value="",
            show=True,
            advanced=True,
            info="Optional chat-style input for targets with a Chat Input node. Usually empty here.",
        ),
        MessageTextInput(
            name="output_ids",
            display_name="Output IDs",
            value="",
            show=True,
            advanced=True,
            info="Comma-separated component ids that count as 'the answer' (wait mode only; the server "
                 "validates them against the flow before running).",
        ),
        MessageTextInput(
            name="target_session_id",
            display_name="Target Session ID",
            value="",
            show=True,
            advanced=True,
            info="Optional: scope the target run's memory/history; also your correlation handle in "
                 "Langfuse. NOT named 'session_id': that shadows a Component base attribute which "
                 "resolves to the RUNNING graph's session in-flow (measured 2026-08-14 — a posted job "
                 "silently inherited the caller's session; same trap family as '_inputs').",
        ),
        HandleInput(
            name="job",
            display_name="Job",
            input_types=["Data"],
            required=False,
            show=False,
            info="check mode: wire the Result of a previous post call; its job_id is used.",
        ),
        MessageTextInput(
            name="job_id",
            display_name="Job ID",
            value="",
            show=False,
            info="check mode: the job to poll, if nothing is wired to Job.",
        ),
        IntInput(
            name="timeout_s",
            display_name="Timeout (s)",
            value=600,
            advanced=True,
            info="HTTP timeout. wait mode holds the connection for the whole target run — size it to the "
                 "target flow, and prefer post+check for anything sweep-sized.",
        ),
        MessageTextInput(
            name="base_url",
            display_name="Base URL",
            value="",
            advanced=True,
            info="Deployment override. Empty = EXTRCT_LANGFLOW_URL, else http://localhost:7860 "
                 "(correct from inside the langflow container). Never part of call_uid.",
        ),
    ]

    # Both outputs share one cached _execute(): wiring both costs one call, not two.
    outputs = [
        Output(name="result", display_name="Result", method="build_result", group_outputs=True),
        Output(name="call_record", display_name="Call Record", method="build_record", group_outputs=True),
    ]

    _executed: dict | None = None

    def update_build_config(self, build_config, field_value, field_name=None):
        """Same contract as the Registry/clients: in-place mutation, membership-guarded
        writes (dotdict.__missing__ turns a typo'd key into a silent no-op), is_refresh
        re-applies state on template rebuilds. No network here (S4)."""
        def put(name, key, value):
            if name in build_config and isinstance(build_config[name], dict):
                build_config[name][key] = value

        if field_name == "mode" or build_config.get("is_refresh"):
            mode = field_value if field_name == "mode" else (
                (build_config.get("mode", {}) or {}).get("value") or "wait"
            )
            # Unknown mode (stale frozen node meeting newer options): show everything
            # rather than strand the user with fields they cannot reach.
            used = MODE_FIELDS.get(mode, MANAGED_FIELDS)
            for f in MANAGED_FIELDS:
                put(f, "show", f in used)
                if f in REQUIRED_WHEN_SHOWN:
                    put(f, "required", f in used)
        return build_config

    @staticmethod
    def _first(value):
        return (value[0] if value else None) if isinstance(value, list) else value

    def _payload_value(self):
        """Wired Data/Message wins; pasted JSON is the fallback. Returns dict or raw
        string — call_definition parses strings and refuses non-objects loudly.

        Message is type-checked BEFORE the dict branch (measured 2026-08-14): a real
        lfx Message ALWAYS carries a populated .data envelope (sender, files, a
        Properties object, a per-run timestamp), so duck-typing on .data would deliver
        envelope junk, crash canonical_json on Properties, and make call_uid
        non-deterministic via the timestamp. The message's JSON lives in .text."""
        v = self._first(self.payload)
        if v is not None:
            if isinstance(v, Message):
                t = getattr(v, "text", None)
                if isinstance(t, str) and t.strip():
                    return t
            else:
                d = getattr(v, "data", None)
                if isinstance(d, dict) and d:
                    return d
                if isinstance(v, dict):
                    return v
        return self.payload_json or ""

    def _job_id_value(self) -> str:
        v = self._first(self.job)
        d = getattr(v, "data", None)
        if isinstance(d, dict) and d.get("job_id"):
            return str(d["job_id"])
        return str(self.job_id or "")

    def _execute(self) -> dict:
        if self._executed is not None:
            return self._executed

        doc = call_model.call_definition({
            "mode": self.mode,
            "target": str(self.target or ""),
            "payload": self._payload_value(),
            "delivery_node": str(self.delivery_node or ""),
            "delivery_field": str(self.delivery_field or ""),
            "input_value": str(self.input_value or ""),
            "output_ids": str(self.output_ids or ""),
            "session_id": str(self.target_session_id or ""),
            "timeout_s": int(self.timeout_s or 600),
            "job_id": self._job_id_value(),
        })
        result = call_model.execute_call(doc, base_url=str(self.base_url or "") or None)
        self._executed = {"doc": doc, "result": result}
        return self._executed

    def build_result(self) -> Data:
        ex = self._execute()
        r = ex["result"]
        if r["kind"] == "job":
            self.status = (f"posted | call {ex['doc']['call_uid']} | job {r['job_id']} ({r['status']}) | "
                           f"{r['latency_ms']} ms — poll with mode=check")
        else:
            n_out = len(r.get("outputs") or [])
            reason = f" ({r['output_reason']})" if r.get("output_reason") and not r.get("output_text") else ""
            self.status = (f"{r.get('status')} | call {ex['doc']['call_uid']} | {n_out} output group(s)"
                           f"{reason} | {r['latency_ms']} ms")
            if r.get("errors"):
                self.status += f" | errors: {json.dumps(r['errors'])[:200]}"
        return Data(data=r)

    def build_record(self) -> Data:
        """The audit half: what was authored (the full call document, call_uid) next
        to what happened (status, job_id, latency, resolved base_url)."""
        ex = self._execute()
        r = ex["result"]
        return Data(data={
            "call": ex["doc"],
            "executed": {
                "kind": r["kind"],
                "status": r.get("status"),
                "job_id": r.get("job_id"),
                "http_status": r["http_status"],
                "latency_ms": r["latency_ms"],
                "base_url": r["base_url"],
                "flow_id": r.get("flow_id"),
                "delivery_ids": r.get("delivery_ids"),
            },
        })
