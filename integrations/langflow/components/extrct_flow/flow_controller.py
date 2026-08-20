"""Flow - Controller — one document configures the whole extraction flow (flow-def/1.0).

The cockpit node. A flow document — authored by hand, by an agent, or loaded from the
registry — carries one SECTION per component; this node validates every section against
that component's own data model, checks the CROSS-COMPONENT RULES no single node can see,
and emits one config thread per component:

    [Flow - Controller] ─Client Config────> [Client - Ollama / OpenRouter]  (Overrides)
                        ─Extract Config───> [Run - Structured Extract]      (Overrides)
                        ─Schema Config────> [Prep - Schema Builder]         (Overrides)
                        ─Grounding Config─> [XAI - Evidence Grounding]      (Overrides)
                        ─Certainty Config─> [XAI - Certainty Score]         (Overrides)

Payloads are SPARSE — only authored keys ride the wire, so a widget setting the document
does not mention stays exactly as set on the canvas. XAI sections carry `enabled`:
false short-circuits that XAI node (empty outputs, no error) without touching the graph.
Every output carries `config_uid = flow_uid`, so every run this flow touches joins on
one content-addressed identity.

Rules live in extrct.flow_model.RULES — a list concatenated over time; violations
raise HERE, all of them at once, each with its rule id. The founding rule is the Test-04
lesson: grounding.mode='inline' requires schema.request_evidence=true.

A bare client-def/1.0 document (or a Registry load_client row) is accepted and treated
as `{"client": ...}` — a stored client wires straight in.

THE GATE, ABSORBED (2026-08-11, retiring the Pipeline Gate component): this node also
directs the unified pipeline. The Client output materialises the READY client payload
from the client section (client-def/1.0 -> build_client_payload) — wire it straight
into an extract's Client input and the document alone decides Ollama vs OpenRouter
with its full config; no widget client needed. The Declared Plan output carries the
gate's three commitments: every pipeline member listed (disabled items KEPT — absence
is not evidence), every decision with a REASON and an AUTHORITY (which section or
default decided), and a per-item `binding` flag separating what the threads enforce
(XAI enabled) from what canvas wiring decides (wrapper/merger presence) — so declared
vs executed stays comparable against the run log.
"""

import json

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

from extrct import client_model, flow_model


class ExtrctFlowController(Component):
    display_name: str = "Flow - Controller"
    description: str = "One flow-def document -> validated, rule-checked config threads, one per component."
    documentation: str = "docs/system-arch/extraction-stack/flow-control.md"
    icon: str = "sliders-horizontal"
    name: str = "extrct_flow_controller"

    inputs = [
        HandleInput(
            name="definition_in",
            display_name="Flow Definition",
            input_types=["Data", "Message", "DataFrame"],
            required=False,
            info=('Flow document: {"client": {...}, "extract": {...}, "schema": {...}, '
                  '"grounding": {...}, "certainty": {...}} — any subset of sections. '
                  "A bare client definition or a Registry load_client row also works "
                  "(becomes the client section). Takes precedence over the JSON field."),
        ),
        MultilineInput(
            name="definition_json",
            display_name="Flow JSON",
            value="",
            info=("Used when nothing is wired into Flow Definition. Same shapes; unknown "
                  "sections/keys and rule violations raise here with the full list."),
        ),
    ]

    outputs = [
        Output(name="client_config", display_name="Provider Overrides", method="build_client_config",
               group_outputs=True),
        Output(name="extract_config", display_name="Extract Config", method="build_extract_config",
               group_outputs=True),
        Output(name="schema_config", display_name="Schema Config", method="build_schema_config",
               group_outputs=True),
        Output(name="grounding_config", display_name="Grounding Config", method="build_grounding_config",
               group_outputs=True),
        Output(name="certainty_config", display_name="Certainty Config", method="build_certainty_config",
               group_outputs=True),
        Output(name="wrapper_config", display_name="Wrapper Config", method="build_wrapper_config",
               group_outputs=True),
        Output(name="merger_config", display_name="Merger Config", method="build_merger_config",
               group_outputs=True),
        Output(name="client_payload", display_name="Ready Provider", method="build_client",
               group_outputs=True),
        Output(name="declared_plan", display_name="Declared Plan", method="build_plan",
               group_outputs=True),
        Output(name="flow_doc", display_name="Flow Doc", method="build_flow_doc",
               group_outputs=True),
    ]

    def _doc(self) -> dict:
        raw = None
        wired = getattr(self, "definition_in", None)
        if isinstance(wired, list):
            wired = wired[0] if wired else None
        if wired is not None:
            if hasattr(wired, "to_dict") and not isinstance(wired, (Data, Message)):
                rows = wired.to_dict(orient="records")  # Registry load_client lane
                raw = rows[0] if rows else None
            elif isinstance(wired, Message):
                text = str(wired.text or "").strip()
                if text:
                    raw = json.loads(text)
            elif isinstance(wired, Data):
                raw = wired.data
            elif isinstance(wired, dict):
                raw = wired
        if raw is None:
            text = str(self.definition_json or "").strip()
            if text:
                raw = json.loads(text)
        if not isinstance(raw, dict) or not raw:
            msg = ('No flow definition. Wire one in or paste JSON, e.g. '
                   '{"client": {"provider": "ollama", "model": "..."}, '
                   '"grounding": {"enabled": true, "mode": "inline"}, '
                   '"schema": {"request_evidence": true}}')
            raise ValueError(msg)
        doc = flow_model.parse_flow_document(raw)
        violations = flow_model.validate_rules(doc["sections"])
        if violations:
            msg = "flow rules violated:\n" + "\n".join(violations)
            raise ValueError(msg)
        secs = sorted(doc["sections"])
        self.status = (f"sections: {', '.join(secs)} | {len(flow_model.RULES)} rules passed "
                       f"| flow_uid={doc['flow_uid']}")
        return doc

    def _config(self, section: str) -> Data:
        return Data(data=flow_model.section_config(self._doc(), section))

    def build_client_config(self) -> Data:
        return self._config("client")

    def build_extract_config(self) -> Data:
        return self._config("extract")

    def build_schema_config(self) -> Data:
        return self._config("schema")

    def build_grounding_config(self) -> Data:
        return self._config("grounding")

    def build_certainty_config(self) -> Data:
        return self._config("certainty")

    def build_wrapper_config(self) -> Data:
        return self._config("wrapper")

    def build_merger_config(self) -> Data:
        return self._config("merger")

    def build_client(self) -> Data:
        """The READY client payload from the client section — the routing half of the
        absorbed gate. One wire into an extract's Client input; the document decides
        the provider and every setting."""
        doc = self._doc()
        sec = doc["sections"].get("client")
        if not sec:
            msg = ('The Client output needs a "client" section, e.g. '
                   '{"client": {"provider": "ollama", "model": "gemma4:31b-it-q8_0"}} — '
                   "or keep using widget clients with the Client Config thread instead.")
            raise ValueError(msg)
        defn = client_model.client_definition(
            sec["provider"], {k: v for k, v in sec.items() if k != "provider"})
        payload = client_model.build_client_payload(defn)
        payload["config_uid"] = doc["flow_uid"]
        return Data(data=payload)

    def build_plan(self) -> Data:
        """The declared plan — the gate's evidence half. Disabled items are KEPT, every
        decision carries its authority, `binding` says whether a thread enforces it."""
        doc = self._doc()
        secs = doc["sections"]

        def sect(name):
            return f"section:{name}" if name in secs else "default"

        client = secs.get("client")
        items = [
            {"item": "client", "enabled": True,
             "provider": (client or {}).get("provider") or "wiring decides",
             "reason": ("document routes the provider" if client
                        else "no client section; the wired widget client serves"),
             "gated_by": sect("client"), "binding": bool(client)},
            {"item": "structured_extract", "enabled": True,
             "reason": "the pipeline core; always runs", "gated_by": "default",
             "binding": False},
            {"item": "wrapper", "enabled": "wrapper" in secs,
             "reason": ("long-text lane configured" if "wrapper" in secs
                        else "not configured; direct extraction"),
             "gated_by": sect("wrapper"), "binding": False},
            {"item": "merger", "enabled": "merger" in secs,
             "reason": ("merge configured for chunked extraction" if "merger" in secs
                        else "not configured"),
             "gated_by": sect("merger"), "binding": False},
        ]
        for xai in ("grounding", "certainty"):
            enabled = (secs.get(xai) or {}).get("enabled", True) is not False
            items.append({"item": xai, "enabled": enabled,
                          "reason": (f"{xai} section switches it "
                                     + ("on" if enabled else "off") if xai in secs
                                     else "default on when the node is wired"),
                          "gated_by": sect(xai),
                          "binding": xai in secs})  # the thread enforces authored state
        return Data(data={"version": doc["version"], "flow_uid": doc["flow_uid"],
                          "items": items,
                          "note": ("binding=true is enforced by this node's threads; "
                                   "binding=false is declared intent — canvas wiring "
                                   "decides, and the run log is the executed record")})

    def build_flow_doc(self) -> Data:
        return Data(data=self._doc())
