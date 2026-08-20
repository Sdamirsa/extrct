"""Switchboard config generator - the factorial sweep confirmatory factorial as data.

Enumerates pipeline configs across the four axes from the architecture
(diagrams.md section 2): representation x query tooling x control flow x
policy check = 4 x 2 x 2 x 2 = 32 cells. Model locality is held apart and
reported separately, per the architecture, unless cross_model_locality is on.

Every config's `uid` is a CONTENT HASH of the config body, not a random uuid.
Two identical configs anywhere produce the same uid; any change to any field
produces a different one. That is replayability (re-derivable from content-addressed
registries) expressed in the config layer rather than bolted on later.

Prototype scaffolding. Flows are not artifacts of record; when this
shape settles it moves into packages/extrct-contracts.
"""

import hashlib
import json

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, FloatInput, MultiselectInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

SCHEMA_VERSION = "extrct-pipeline/v1"

# The four EHR DB serving levels (a work package five-layer store, 4 of which are served).
# Names are provisional - confirm against the five-layer store spec at .
SERVING_LEVELS = {
    "L1_raw_text": {"backend": "documentreference", "structured": False},
    "L2_sectioned": {"backend": "documentreference+sections", "structured": False},
    "L3_omop": {"backend": "postgres_omop", "structured": True},
    "L4_meds_events": {"backend": "meds_parquet+duckdb", "structured": True},
}

TOOLINGS = ["controlled_dataReq", "free_dataReq"]
CONTROL_FLOWS = ["gated", "loop"]
POLICY_MODES = ["rules_only", "rules_plus_judge"]
LOCALITIES = ["local", "proprietary"]

# Execution order, from the runtime sequence in diagrams.md section 4:
# plan -> policy gate -> data request -> synthesis.
ITEM_ORDER = ["planning_schema", "policy_check", "controlled_dataReq", "free_dataReq", "advisor"]


def _as_list(value, fallback: list[str]) -> list[str]:
    """Accept a list (MultiselectInput) or a comma-separated string."""
    if not value:
        return list(fallback)
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    else:
        items = [str(v).strip() for v in value if str(v).strip()]
    return items or list(fallback)


def content_uid(body: dict) -> str:
    """Deterministic content hash. Canonical JSON in, 16 hex chars out."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class SwitchboardConfigs(Component):
    display_name: str = "Flow - Switchboard"
    description: str = "Enumerate the factorial sweep as content-addressed pipeline configs."
    documentation: str = "docs/reference/langflow-reference.md"
    icon: str = "grid-3x3"
    name: str = "switchboard_configs"

    inputs = [
        MultiselectInput(
            name="serving_levels",
            display_name="EHR Serving Levels",
            info="Representation axis. All four = the confirmatory factorial.",
            options=list(SERVING_LEVELS),
            value=list(SERVING_LEVELS),
        ),
        MultiselectInput(
            name="toolings",
            display_name="Query Tooling",
            info="controlled_dataReq = fixed query library. free_dataReq = model-written SQL.",
            options=TOOLINGS,
            value=TOOLINGS,
        ),
        MultiselectInput(
            name="control_flows",
            display_name="Control Flow (Planning Schema)",
            options=CONTROL_FLOWS,
            value=CONTROL_FLOWS,
        ),
        MultiselectInput(
            name="policy_modes",
            display_name="Policy Check",
            options=POLICY_MODES,
            value=POLICY_MODES,
        ),
        DropdownInput(
            name="model_locality",
            display_name="Model Locality",
            info="Reported apart from the factorial unless crossed in below.",
            options=LOCALITIES,
            value="local",
        ),
        BoolInput(
            name="cross_model_locality",
            display_name="Cross Model Locality Into The Grid",
            info="Off (default): locality is a fixed attribute, 32 cells. On: 64 cells.",
            value=False,
        ),
        StrInput(
            name="default_model_name",
            display_name="Default Model",
            value="qwen2.5:7b",
        ),
        FloatInput(
            name="default_temperature",
            display_name="Default Temperature",
            value=0.0,
        ),
        StrInput(
            name="select_cell",
            display_name="Select Cell",
            info="cell_id or 0-based index for the Selected Config output. Blank = first cell.",
            value="",
        ),
    ]

    outputs = [
        Output(name="configs", display_name="Configs", method="build_configs_table", group_outputs=True),
        Output(name="selected_config", display_name="Selected Config", method="build_selected", group_outputs=True),
        Output(name="manifest", display_name="Manifest", method="build_manifest", group_outputs=True),
    ]

    # --- construction ------------------------------------------------------

    def _model_config(self, role: str, model_name: str | None = None, temperature: float | None = None) -> dict:
        return {
            "role": role,
            "model_name": model_name or self.default_model_name,
            "temperature": float(self.default_temperature if temperature is None else temperature),
        }

    def _agent_config(self, tooling: str, control_flow: str, policy_mode: str) -> list[dict]:
        """The ordered agent items for one cell. Disabled items are KEPT, with a reason."""
        items = [
            {
                "kind": "planning_schema",
                "name": "planning_schema",
                "enabled": True,
                "mode": control_flow,
                "declares_plan_upfront": control_flow == "gated",
            },
            {
                "kind": "policy_check",
                "name": "policy_check",
                "enabled": True,
                "mode": policy_mode,
                "can_escalate": policy_mode == "rules_plus_judge",
                "model_config": self._model_config("policy_judge") if policy_mode == "rules_plus_judge" else None,
            },
        ]
        for tool in TOOLINGS:
            active = tool == tooling
            items.append(
                {
                    "kind": "tool",
                    "name": tool,
                    "enabled": active,
                    "reason": None if active else f"tooling axis fixed to {tooling}",
                    # Only free_dataReq needs a model - it writes SQL.
                    "model_config": self._model_config("sql_author") if (active and tool == "free_dataReq") else None,
                }
            )
        items.append(
            {
                "kind": "advisor",
                "name": "advisor",
                "enabled": True,
                "model_config": self._model_config("advisor"),
            }
        )
        return sorted(items, key=lambda i: ITEM_ORDER.index(i["name"]))

    def _config(self, level: str, tooling: str, control_flow: str, policy_mode: str, locality: str) -> dict:
        body = {
            "schema_version": SCHEMA_VERSION,
            "cell_id": f"{level}|{tooling}|{control_flow}|{policy_mode}",
            "ehr_config": {
                "serving_level": level,
                "backend": SERVING_LEVELS[level]["backend"],
                "structured": SERVING_LEVELS[level]["structured"],
                "provenance_required": True,
            },
            "model_locality": locality,
            "defaults": {
                "model_name": self.default_model_name,
                "temperature": float(self.default_temperature),
            },
            "agent_config": self._agent_config(tooling, control_flow, policy_mode),
        }
        # uid is computed over the body and then attached - never part of its own hash.
        return {"uid": content_uid(body), **body}

    def _all_configs(self) -> list[dict]:
        levels = [x for x in _as_list(self.serving_levels, list(SERVING_LEVELS)) if x in SERVING_LEVELS]
        toolings = [x for x in _as_list(self.toolings, TOOLINGS) if x in TOOLINGS]
        flows = [x for x in _as_list(self.control_flows, CONTROL_FLOWS) if x in CONTROL_FLOWS]
        policies = [x for x in _as_list(self.policy_modes, POLICY_MODES) if x in POLICY_MODES]
        localities = LOCALITIES if self.cross_model_locality else [self.model_locality]

        return [
            self._config(level, tooling, flow, policy, locality)
            for level in levels
            for tooling in toolings
            for flow in flows
            for policy in policies
            for locality in localities
        ]

    def _select(self, configs: list[dict]) -> dict:
        key = str(self.select_cell or "").strip()
        if not key:
            return configs[0] if configs else {}
        if key.isdigit() and int(key) < len(configs):
            return configs[int(key)]
        for cfg in configs:
            if key in (cfg["cell_id"], cfg["uid"]):
                return cfg
        return configs[0] if configs else {}

    # --- outputs -----------------------------------------------------------

    def build_configs_table(self) -> DataFrame:
        """One row per cell - this grid is what maps onto Dagster partitions."""
        rows = [
            {
                "uid": c["uid"],
                "cell_id": c["cell_id"],
                "serving_level": c["ehr_config"]["serving_level"],
                "tooling": next(i["name"] for i in c["agent_config"] if i["kind"] == "tool" and i["enabled"]),
                "control_flow": next(i["mode"] for i in c["agent_config"] if i["kind"] == "planning_schema"),
                "policy_mode": next(i["mode"] for i in c["agent_config"] if i["kind"] == "policy_check"),
                "model_locality": c["model_locality"],
            }
            for c in self._all_configs()
        ]
        self.status = f"{len(rows)} cells"
        return DataFrame(rows)

    def build_selected(self) -> Data:
        cfg = self._select(self._all_configs())
        self.status = cfg.get("cell_id", "none")
        return Data(data=cfg)

    def build_manifest(self) -> Data:
        configs = self._all_configs()
        uids = [c["uid"] for c in configs]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "count": len(configs),
            "unique_uids": len(set(uids)),
            "configs": configs,
        }
        self.status = f"{len(configs)} configs, {len(set(uids))} unique uids"
        return Data(data=manifest)
