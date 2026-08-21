# packaging — desktop builds

Turns this app into a native desktop executable via `nicegui-pack` (PyInstaller
under the hood). CLI measured on nicegui 3.16.0, 2026-08-21.

**One build per OS — PyInstaller does not cross-compile.** Build the macOS app on a
Mac, the Windows exe on Windows, each from a clone of this repo on that machine.

```bash
uv sync --group pack          # nicegui[native] (pywebview) + pyinstaller
uv run python packaging/pack.py
# → dist/<app>            (single-file executable)
```

How the pieces fit:

- `app.py` detects frozen mode (PyInstaller sets `sys.frozen`) and switches itself
  to `native=True` with a free auto-picked port (`native.find_open_port()`) —
  exactly what `nicegui-pack --windowed` requires. In dev, `APP_NATIVE=1
  uv run python app.py` previews the native window without packaging.
- `data_model.json` is bundled via `--add-data` so the frozen app finds it next to
  the extracted entry script.
- `dist/`, `build/`, and `*.spec` are gitignored — built artifacts never enter git.

Platform notes: Windows needs the WebView2 runtime (preinstalled on current
Windows 10/11); Linux needs `webkit2gtk`. macOS code-signing/notarization is out of
scope until an app is actually distributed outside your own machines — an unsigned
app runs locally via right-click → Open.
