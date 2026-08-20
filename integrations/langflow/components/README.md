# Langflow custom components

Mounted read-only into the Langflow container at `/components` (`LANGFLOW_COMPONENTS_PATH`).

**These are wrappers, not implementations.** The intended pattern is that a component here
calls `extrct-tools` — ideally over MCP, which is already the declared tool handshake —
so the canvas exercises the real tool contract instead of a parallel reimplementation.
Anything with logic of its own belongs in `packages/`, not here.

Langflow requires each category directory to contain an `__init__.py`; see
[Create custom Python components](https://docs.langflow.org/components-custom-components).

Empty for now — the directory exists so the bind mount resolves on a fresh clone.
