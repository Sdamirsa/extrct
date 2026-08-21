# apps — user-facing applications

Two lanes, one folder each. The folder boundary matches the rule boundary: each
lane has its own framework, its own posture, and its own README carrying the rules.

| Lane | Where | What |
|---|---|---|
| Mini apps | [`mini/`](mini/) | small, task-specific NiceGUI tools over the `extrct` library (first: `annotate`, the human extraction form). Lane rules and the copy-to-start template live in [`mini/README.md`](mini/README.md). |
| Workbench | `workbench/` *(planned, not started)* | the main UI: def-editor, run-monitor, and the canvas that *renders* def documents — data-heavy, multi-user, notifications. |

Design of record for the split and the mini-app lane:
[`docs/system-arch/workbench/mini-apps.md`](../docs/system-arch/workbench/mini-apps.md).
