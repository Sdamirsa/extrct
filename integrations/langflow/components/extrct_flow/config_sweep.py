"""Config Sweep — value LISTS per variable, expanded to the full grid of config cells.

The generator half of sweeping: each row declares candidate values for one namespaced
key; the output is the cross product, each cell content-addressed. Execution belongs to
`Run - Sweep Extract`, which takes this grid — generation stays pure, the cap
stays loud (> 500 cells is the conductor's job, not a canvas node's).

    [Prep - Config Sweep] ─Configs (DataFrame)──> [Run - Sweep Extract]
"""

import json

from lfx.custom.custom_component.component import Component
from lfx.io import Output, TableInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.table import EditMode

from extrct import config as C
from extrct import flow_model

COLUMNS = [
    {"name": "name", "display_name": "Name", "type": "str", "default": "client.temperature",
     "description": "NAMESPACED key (ollama./openrouter./client./extract./schema./"
                    "grounding./certainty./wrapper./merger.).",
     "edit_mode": EditMode.INLINE},
    {"name": "type", "display_name": "Type", "type": "str", "default": "float",
     "description": "str | int | float | bool | json - applied to EACH value.",
     "edit_mode": EditMode.INLINE},
    {"name": "values", "display_name": "Values", "type": "str", "default": "[0.0, 0.7]",
     "description": "JSON array of CANDIDATES ([0.0, 0.7]) or comma list (a,b,c). The grid "
                    "is the cross product over all rows; a one-candidate array pins a "
                    "constant without multiplying the grid.",
     "edit_mode": EditMode.POPOVER},
    {"name": "help", "display_name": "Help", "type": "str", "default": "",
     "description": "Allowed values / examples / the measured lesson. Informational only - "
                    "parsers ignore this column.",
     "edit_mode": EditMode.POPOVER},
]

# THE MAXIMUM TABLE (user decision 2026-08-11): every controllable variable, each value
# a ONE-candidate JSON array - extend any array to sweep that variable; until then the
# grid stays a single cell. Delete rows to release keys to the widgets. Baked into the
# node at drag time; re-drag to refresh after model changes.
DEFAULT_ROWS = flow_model.sweep_catalog()


class ExtrctConfigSweep(Component):
    display_name: str = "Flow - Sweep"
    description: str = "Expand value lists into the full grid of content-addressed config cells."
    documentation: str = "docs/extraction-stack/grounding-certainty-design.md"
    icon: str = "grid-3x3"
    name: str = "extrct_config_sweep"

    inputs = [
        TableInput(name="sweep_rows", display_name="Variables", table_schema=COLUMNS,
                   value=DEFAULT_ROWS, required=True,
                   info="One row per swept variable; single-value rows pin a constant across the grid."),
    ]

    outputs = [
        Output(name="configs", display_name="Configs", method="build_configs", group_outputs=True),
        Output(name="grid_summary", display_name="Grid Summary", method="build_summary", group_outputs=True),
    ]

    def _cells(self) -> list[dict]:
        sweep = C.parse_sweep_rows(list(self.sweep_rows or []))
        cells = C.expand_grid(sweep)
        if not cells:
            msg = "no sweep variables defined"
            raise ValueError(msg)
        return cells

    def build_configs(self) -> DataFrame:
        cells = self._cells()
        self.status = f"{len(cells)} config cell(s)"
        return DataFrame([
            {"config_uid": c["config_uid"],
             "config": json.dumps({k: v for k, v in c.items() if k != "config_uid"},
                                  ensure_ascii=False)}
            for c in cells
        ])

    def build_summary(self) -> Data:
        cells = self._cells()
        keys = sorted(k for k in cells[0] if k != "config_uid")
        return Data(data={"cells": len(cells), "keys": keys,
                          "cap": C.GRID_CAP,
                          "config_uids": [c["config_uid"] for c in cells]})
