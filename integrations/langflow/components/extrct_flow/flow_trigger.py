"""Flow - Trigger — the JSON mouth of a triggerable flow.

A 1-node ingress: JSON arrives (webhook body or tweak), parses LOUDLY, and rides out as
Data — typically straight into the Flow - Controller's document input, which is what
makes a whole flow callable-as-API with one wire.

Measured lessons (Langflow 1.11.0 source, 2026-08-14):
- BOTH the class name ``ExtrctWebhookTrigger`` and the internal ``name`` contain
  capital-W ``Webhook`` ON PURPOSE and it is load-bearing: the v1 route
  ``POST /api/v1/webhook/<flow>`` finds receiving nodes by the case-sensitive substring
  ``"Webhook" in node.id`` and injects the raw request body into their input field
  literally named ``data``. Canvas drags derive the node id from the CLASS
  (measured from a real drag, 2026-08-14: ``ext:extrct_flow:<ClassName>@extra-<sfx>``),
  API-assembled nodes traditionally from the name attr — so BOTH must carry the
  substring. Rename either and v1 webhook delivery silently stops for one lane.
- The same field is equally reachable without the substring trick via v2:
  ``POST /api/v2/workflows`` with ``tweaks {"Flow - Trigger": {"data": "<json>"}}``
  (tweaks match display name as well as node id).
- The stock Webhook component silently converts invalid JSON into
  ``{"payload": "<raw text>"}`` — a malformed sender fails invisibly downstream. This
  trigger refuses instead (extrct.call_model.parse_trigger_payload), so the sender's bug
  dies HERE with the parse error, not three nodes later as a schema mismatch.
- v1 webhook runs are fire-and-forget: a raise in background mode surfaces only in the
  server log and Langfuse, never to the caller. The caller who wants errors uses the
  v2 sync or background lane (Flow - Call's wait / post+check modes).
"""

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, MultilineInput, Output
from lfx.schema.data import Data

from extrct import call_model


class ExtrctWebhookTrigger(Component):
    display_name = "Flow - Trigger"
    description = "Receives a JSON payload (v1 webhook or v2 tweak) and emits it as Data, parsed loudly."
    documentation = "docs/system-arch/extraction-stack/flow-calls.md"
    icon = "webhook"
    name = "extrct_Webhook_trigger"  # capital W is load-bearing — see module docstring

    inputs = [
        MultilineInput(
            name="data",
            display_name="Payload",
            info=(
                "Filled by the caller: v1 webhook injects the raw POST body here; v2 callers "
                "tweak this field by node id or by display name 'Flow - Trigger'. Paste JSON "
                "here for a canvas dry-run. Must be a JSON object."
            ),
            value="",
        ),
        BoolInput(
            name="allow_empty",
            display_name="Allow Empty",
            info=(
                "OFF (default): no payload raises with the two delivery recipes — a triggered "
                "flow that ran without its payload is a caller bug, not a quiet no-op. "
                "ON: an empty payload becomes {} so the flow can run on canvas defaults."
            ),
            value=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(name="payload", display_name="Payload", method="build_payload"),
    ]

    def build_payload(self) -> Data:
        obj = call_model.parse_trigger_payload(self.data, allow_empty=bool(self.allow_empty))
        if not obj:
            self.status = "empty payload (Allow Empty is on) — downstream runs on its own defaults"
            return Data(data={})
        keys = ", ".join(sorted(obj)[:8])
        self.status = f"payload ok: {len(obj)} top-level key(s) [{keys}], {len(str(self.data))} chars"
        return Data(data=obj)
