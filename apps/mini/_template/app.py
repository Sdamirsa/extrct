"""Entry point template. The app renders; the library decides.

Keep this file wiring-only — page layout plus calls into `ui/`, where real code
grows. Every document is built and validated by `extrct`, and the app's I/O must
match data_model.json (which this stub reads, so drift shows up on first page load).

Two measured NiceGUI facts (3.16.0, 2026-08-21) this file encodes:
- `ui.run(host=...)` defaults to 0.0.0.0 in non-native mode — the explicit
  APP_HOST default below IS the repo's localhost-only rule; do not remove it.
  The Dockerfile sets APP_HOST=0.0.0.0 for container-internal listening while
  compose publishes on 127.0.0.1.
- UI is built inside @ui.page('/'): the shared auto-index page was removed in
  NiceGUI 3.0 (module-level widgets shared state across sessions), and packaging
  (`nicegui-pack`) requires a page function anyway.

For a desktop window instead of a browser tab, install "nicegui[native]" and pass
native=True to ui.run().
"""

import json
import os
from pathlib import Path

from nicegui import ui

HANDSHAKE = json.loads(
    (Path(__file__).parent / "data_model.json").read_text(encoding="utf-8")
)


@ui.page("/")
def index() -> None:
    ui.label(HANDSHAKE["app"]).classes("text-2xl font-bold")
    ui.label(HANDSHAKE["description"]).classes("text-gray-500")
    ui.label(
        "Template stub — replace this body, keep the shape. "
        "README.md holds the handshake this app must honour."
    )
    with ui.expansion("Declared inputs and outputs (data_model.json)"):
        ui.code(json.dumps(HANDSHAKE, indent=2), language="json")


# The __mp_main__ guard is required: native mode re-imports this module in a
# child process.
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8080")),
        title=HANDSHAKE["app"],
        reload=False,
    )
