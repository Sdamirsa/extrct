"""Build a desktop executable of this app. Run ON the target OS (no cross-compile).

Thin wrapper over `nicegui-pack` (flags measured on nicegui 3.16.0, 2026-08-21:
--name, --windowed, --onefile/--onedir, --add-data, --icon,
--osx-bundle-identifier, --dry-run, --clean, --noconfirm). The app name comes from
data_model.json so the handshake stays the single source of naming.

    uv sync --group pack
    uv run python packaging/pack.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
HANDSHAKE = json.loads((APP_DIR / "data_model.json").read_text(encoding="utf-8"))

cmd = [
    "nicegui-pack",
    "--name", HANDSHAKE["app"],
    "--onefile",
    # --windowed requires ui.run(native=True); app.py switches to native when frozen.
    "--windowed",
    # Bundle the handshake next to the entry script inside the frozen app,
    # where app.py's Path(__file__).parent lookup finds it.
    "--add-data", f"{APP_DIR / 'data_model.json'}{os.pathsep}.",
    "app.py",
]

result = subprocess.run(cmd, cwd=APP_DIR)
if result.returncode != 0:
    sys.exit(result.returncode)
print(f"\nBuilt: {APP_DIR / 'dist' / HANDSHAKE['app']}")
