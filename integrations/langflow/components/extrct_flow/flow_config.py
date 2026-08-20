"""Flow Config — one typed, namespaced config payload for the whole flow.

The widget layer cannot be wired (toggles/dropdowns take no edges), but every ExtrCT
component builds its behavior from dicts — so configuration rides the graph as Data.
Wire this node's Config output into the `Overrides` input of the clients, the extract,
and the schema builder; each consumes only its own namespaces and RAISES on typos.

    [Prep - Flow Config] ─Config──┬─> [Client - Ollama].Overrides
                                  ├─> [Client - OpenRouter].Overrides
                                  ├─> [Run - Structured Extract].Overrides
                                  └─> [Prep - Schema Builder].Overrides

Namespaces: `ollama.` / `openrouter.` (provider-specific), `client.` (both clients),
`extract.`, `schema.`. config_uid content-addresses the cell.
"""

from lfx.custom.custom_component.component import Component
from lfx.io import Output, TableInput
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.schema.table import EditMode

from extrct import config as C
from extrct import flow_model
from extrct.hashing import content_uid

COLUMNS = [
    {"name": "name", "display_name": "Name", "type": "str", "default": "client.logprobs",
     "description": "NAMESPACED key: ollama. / openrouter. / client. (both) / extract. / "
                    "schema. / grounding. / certainty. / wrapper. / merger. "
                    "Unknown keys fail loudly at the consuming node - typos cannot no-op.",
     "edit_mode": EditMode.INLINE},
    {"name": "type", "display_name": "Type", "type": "str", "default": "bool",
     "description": "str | int | float | bool | json", "edit_mode": EditMode.INLINE},
    {"name": "value", "display_name": "Value", "type": "str", "default": "true",
     "description": "Coerced to Type. bool: true/false. json: any JSON literal.",
     "edit_mode": EditMode.INLINE},
    {"name": "help", "display_name": "Help", "type": "str", "default": "",
     "description": "Allowed values / examples / the measured lesson. Informational only - "
                    "parsers ignore this column.",
     "edit_mode": EditMode.POPOVER},
]

# THE MAXIMUM TABLE (user decision 2026-08-11): every controllable variable from every
# data model, defaults filled, generated from the models at import time. A kept row is
# AUTHORITATIVE for its key at the consuming node - DELETE the rows you want the widget
# settings to keep deciding. Frozen-code note: this default is baked into a node at drag
# time; a re-drag refreshes it after model changes.
DEFAULT_ROWS = flow_model.variable_catalog()


class ExtrctFlowConfig(Component):
    display_name: str = "Flow - Config"
    description: str = "Typed, namespaced flow variables as one Data payload with a content-addressed uid."
    documentation: str = "docs/system-arch/extraction-stack/grounding-certainty-design.md"
    icon: str = "settings-2"
    name: str = "extrct_flow_config"

    inputs = [
        TableInput(name="config_rows", display_name="Variables", table_schema=COLUMNS,
                   value=DEFAULT_ROWS, required=True,
                   info="One row per flow variable. Namespaced names, typed values."),
    ]

    outputs = [
        Output(name="config", display_name="Config", method="build_config", group_outputs=True),
        Output(name="config_uid_out", display_name="Config UID", method="build_uid", group_outputs=True),
    ]

    def _config(self) -> dict:
        cfg = C.parse_config_rows(list(self.config_rows or []))
        if not cfg:
            msg = "no variables defined"
            raise ValueError(msg)
        cfg["config_uid"] = content_uid({k: v for k, v in cfg.items() if k != "config_uid"})
        return cfg

    def build_config(self) -> Data:
        cfg = self._config()
        self.status = f"{len(cfg) - 1} variable(s) | {cfg['config_uid']}"
        return Data(data=cfg)

    def build_uid(self) -> Message:
        return Message(text=self._config()["config_uid"])
