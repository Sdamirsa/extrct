"""Flow - Response — the outbound half of the trigger handshake (flow-response/1.0).

Flow - Trigger is the mouth IN; this is the mouth OUT: wire the pipeline's typed
outputs in, get ONE canonical envelope a calling flow (or script) can parse without
knowing this flow's internals. Put it directly before the Chat Output that Flow - Call
reads (wire Response Text -> Chat Output input; select that Chat Output in the
caller's Output IDs for a single-answer response).

Why not the stock Data Operations "Combine" (measured on a real canvas, 2026-08-14):
Combine does a FLAT key union — extracted + certainty + grounding all carry run_uid,
so the union turned it into a list of duplicate values, and colliding keys would
silently overwrite. This envelope keeps every section namespaced, reconciles run_uid
to one scalar (differing uids raise — two runs in one response is a wiring bug),
declares what is present (absence is not evidence, independent measurement), and flags sections that
report ok=false/error so the caller can branch without digging.

Response Text is canonical JSON with NO markdown fence — `json.loads(output_text)` on
the calling side, no stripping. The package function `call_model.build_flow_response`
owns all logic (S3); this node only gathers wires.
"""

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

from extrct import call_model
from extrct.hashing import canonical_json


class ExtrctFlowResponse(Component):
    display_name = "Flow - Response"
    description = "Bundle pipeline outputs into one canonical, caller-parseable envelope (flow-response/1.0)."
    documentation = "docs/system-arch/extraction-stack/flow-calls.md"
    icon = "reply"
    name = "extrct_flow_response"

    inputs = [
        HandleInput(name="extracted", display_name="Extracted", input_types=["Data"], required=False,
                    info="Run - Structured Extract's Extracted output (or Merged Extraction)."),
        HandleInput(name="certainty", display_name="Certainty", input_types=["Data"], required=False,
                    info="XAI - Certainty Score's Field Certainty output."),
        HandleInput(name="grounding", display_name="Grounding", input_types=["Data"], required=False,
                    info="XAI - Evidence Grounding's Grounding Report output."),
        HandleInput(name="report", display_name="Run Report", input_types=["Data"], required=False,
                    info="Run - Structured Extract's Run Report output — carries run_uid, status, usage."),
        HandleInput(name="merged", display_name="Merged", input_types=["Data"], required=False,
                    info="PostPrep - Extraction Merger output, for the long-text lane."),
        HandleInput(name="extra", display_name="Extra", input_types=["Data"], is_list=True, required=False,
                    info="Anything else worth returning; kept as a list under sections.extra."),
    ]

    outputs = [
        Output(name="response", display_name="Response", method="build_response", group_outputs=True),
        Output(name="response_text", display_name="Response Text", method="build_text", group_outputs=True),
    ]

    _envelope: dict | None = None

    @staticmethod
    def _one(value):
        if isinstance(value, list):
            value = value[0] if value else None
        data = getattr(value, "data", value)
        return data if isinstance(data, dict) else None

    @staticmethod
    def _many(value):
        items = value if isinstance(value, list) else ([value] if value is not None else [])
        out = []
        for item in items:
            data = getattr(item, "data", item)
            if isinstance(data, dict) and data:
                out.append(data)
        return out

    def _build(self) -> dict:
        if self._envelope is None:
            self._envelope = call_model.build_flow_response(
                {
                    "extracted": self._one(self.extracted),
                    "certainty": self._one(self.certainty),
                    "grounding": self._one(self.grounding),
                    "report": self._one(self.report),
                    "merged": self._one(self.merged),
                },
                extra=self._many(self.extra),
            )
            env = self._envelope
            flags = f" | flagged: {', '.join(env['flagged'])}" if env["flagged"] else ""
            self.status = (f"{env['response_uid']} | run {env['run_uid']} | "
                           f"sections: {', '.join(env['present'])}{flags}")
        return self._envelope

    def build_response(self) -> Data:
        return Data(data=self._build())

    def build_text(self) -> Message:
        """Canonical JSON, deliberately unfenced — the calling side does
        json.loads(output_text) with zero stripping."""
        return Message(text=canonical_json(self._build()))
