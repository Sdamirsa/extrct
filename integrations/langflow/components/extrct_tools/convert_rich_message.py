"""Prep - Message — turn any mix of tables and JSON into one clean markdown note.

Layout, in this order: title, author text, one global/environment variable as a labelled
line, then every connected payload rendered for inspection. Multiple payloads concatenate
in connection order.

Rendering rules: a flat dict becomes a key/value table; a uniform list of flat dicts (and
any DataFrame) becomes a markdown table; anything nested becomes a fenced JSON block —
nesting flattened into table cells stops being inspectable, which is the whole point.

The variable line resolves the NAME via Langflow's variable service first, then the
container environment. Secret-looking values are refused and fingerprinted instead of
printed: this output is made to be pasted into issues and docs, exactly where a credential
must never land. An unresolved name renders as unresolved — absence is not evidence.
"""

import hashlib
import json
import os

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message

SECRET_NAME_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")


class ConvertRichMessage(Component):
    display_name: str = "Prep - Message"
    description: str = "Title + your text + one global variable + any mix of tables/JSON, as clean markdown."
    documentation: str = "docs/extraction-stack/"
    icon: str = "file-text"
    name: str = "convert_rich_message"

    inputs = [
        HandleInput(
            name="payload",
            display_name="Table / JSON",
            info=("Data, DataFrame or Message — any MIX of them, across multiple connections. "
                  "Rendered in connection order, each under its own heading when there is "
                  "more than one."),
            input_types=["Data", "DataFrame", "Message"],
            is_list=True,
            required=True,
        ),
        MessageTextInput(name="title", display_name="Title", value="",
                         info="Top-level heading. Empty skips the heading."),
        MessageTextInput(
            name="item_titles",
            display_name="Item Titles",
            info=("Comma-separated names for the payload sections, applied in CONNECTION "
                  "order — e.g. 'Certainty (Ollama), Grounding'. EMPTY = automatic: sections "
                  "are titled from the graph itself (source node's canvas name, plus the "
                  "output name when one node feeds several sections). Explicit names here "
                  "always win; 'Item N' remains the last resort."),
            value="",
        ),
        MultilineInput(name="body", display_name="Your Text", value="",
                       info="Written by you, verbatim under the title. Empty skips it."),
        MessageTextInput(
            name="global_var_name",
            display_name="Global Variable",
            info=("NAME of a Langflow Global Variable or container environment variable, "
                  "shown as a labelled line under your text. Secret-looking values "
                  "(name contains KEY/SECRET/TOKEN/... or value looks like a key) are "
                  "fingerprinted, never printed."),
            value="",
        ),
    ]

    outputs = [
        Output(name="markdown", display_name="Markdown", method="build_markdown"),
    ]

    # ------------------------------------------------------------------ engine workaround
    def _resolve_payloads_from_graph(self) -> list | None:
        """Re-resolve each payload from its edge's DECLARED output, in edge order.

        Works around a measured Langflow engine defect (vertex_types.py, ComponentVertex.
        _get_result): with several edges from ONE source node into the SAME list input,
        the engine's resolution loop takes the first matching edge and breaks — every
        slot receives that one output's value. Caught live 2026-08-10: three XAI outputs
        (clean_values, grounding_table, grounding_report) all rendered as clean_values.
        Pulling results[edge.source_handle.name] per edge is the resolution the engine
        intended; it also guarantees section titles and contents align. Returns None
        (keep engine-delivered payloads) outside a graph or on any miss.
        """
        try:
            vert = getattr(self, "_vertex", None)
            graph = getattr(vert, "graph", None)
            if vert is None or graph is None:
                return None
            edges = [e for e in vert.edges
                     if e.target_id == vert.id and getattr(e, "target_param", None) == "payload"]
            if not edges:
                return None
            out = []
            for e in edges:
                src = graph.get_vertex(e.source_id)
                results = getattr(src, "results", None)
                name = getattr(getattr(e, "source_handle", None), "name", None)
                if not isinstance(results, dict) or name not in results:
                    return None
                out.append(results[name])
            return out
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ provenance titles
    def _payload_edge_count(self) -> int | None:
        """How many edges are wired into the payload input; None outside a graph."""
        try:
            vert = getattr(self, "_vertex", None)
            if vert is None or getattr(vert, "graph", None) is None:
                return None
            return len([e for e in vert.edges
                        if e.target_id == vert.id and getattr(e, "target_param", None) == "payload"])
        except Exception:  # noqa: BLE001
            return None

    def _edge_titles(self, n_items: int) -> list[str] | None:
        """Best-effort section names from the graph's own wiring: the source node's canvas
        display name, plus the output name when one node feeds several sections. Returns
        None outside a graph or on ANY mismatch — provenance is a nicety, never a failure
        mode, and the caller falls back to Item N. Payload order follows edge order (the
        same iteration the param handler uses); if a rename ever lands on the wrong
        section after rewiring, the explicit Item Titles field wins positionally."""
        try:
            vert = getattr(self, "_vertex", None)
            graph = getattr(vert, "graph", None)
            if vert is None or graph is None:
                return None
            edges = [e for e in vert.edges
                     if e.target_id == vert.id and getattr(e, "target_param", None) == "payload"]
            if len(edges) != n_items:
                return None
            per_source: dict[str, int] = {}
            for e in edges:
                per_source[e.source_id] = per_source.get(e.source_id, 0) + 1
            titles = []
            for e in edges:
                src = graph.get_vertex(e.source_id)
                name = str(getattr(src, "display_name", None) or e.source_id)
                out = getattr(getattr(e, "source_handle", None), "name", None)
                if out and per_source[e.source_id] > 1:
                    name = f"{name} — {out}"
                titles.append(name)
            return titles
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def _cell(value) -> str:
        if value is None or value != value:  # second test catches pandas NaN
            return ""
        text = str(value).replace("|", "\\|").replace("\n", " ")
        return text if len(text) <= 200 else text[:200] + "…"

    @classmethod
    def _table(cls, rows: list[dict]) -> str:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        lines += ["| " + " | ".join(cls._cell(r.get(c)) for c in cols) + " |" for r in rows]
        return "\n".join(lines)

    @classmethod
    def _render(cls, value) -> str:
        # isinstance, not duck typing: Message ALSO exposes .data (its full internal dict),
        # so a hasattr check dumps sender/session/timestamp noise instead of the text.
        # Message must be tested before Data. Caught by the in-container test.
        if isinstance(value, DataFrame):
            rows = value.to_dict(orient="records")
            return cls._table(rows) if rows else "*(empty table)*"
        if isinstance(value, Message):
            return str(value.text)
        if isinstance(value, Data):
            value = value.data

        scalars = (str, int, float, bool, type(None))
        if isinstance(value, dict):
            if value and all(isinstance(v, scalars) for v in value.values()):
                return cls._table([{"key": k, "value": v} for k, v in value.items()])
            return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n```"
        if isinstance(value, list):
            if value and all(isinstance(r, dict) and all(isinstance(v, scalars) for v in r.values()) for r in value):
                return cls._table(value)
            return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n```"
        return str(value)

    # ------------------------------------------------------------------ variable line
    async def _variable_line(self) -> tuple[str, str]:
        """Returns (markdown_line, source). Empty line when no name is given."""
        name = str(self.global_var_name or "").strip()
        if not name:
            return "", "none"

        value, source = None, "unresolved"
        try:
            from langflow.services.deps import session_scope

            async with session_scope() as session:
                value = await self.get_variable(name=name, field="", session=session)
                source = "global_variable"
        except Exception:  # noqa: BLE001 - service absent or variable unknown; env is next
            value = None
        if value is None:
            env = os.environ.get(name)
            if env is not None:
                value, source = env, "environment"

        if value is None:
            return f"**{name}:** *(unresolved — no Global Variable or env var with this name)*", "unresolved"

        text = str(value)
        looks_secret = any(h in name.upper() for h in SECRET_NAME_HINTS) or text.startswith(("sk-", "Bearer "))
        if looks_secret:
            fp = hashlib.sha256(text.encode()).hexdigest()[:8]
            return f"**{name}:** *(secret withheld, sha256[:8]={fp})*", f"{source}:redacted"
        return f"**{name}:** {text}", source

    # ------------------------------------------------------------------ output
    async def build_markdown(self) -> Message:
        items = self.payload if isinstance(self.payload, list) else [self.payload]
        items = [i for i in items if i is not None]
        resolved = self._resolve_payloads_from_graph()
        graph_resolved = resolved is not None and len(resolved) >= len(items)
        if graph_resolved:
            items = resolved

        parts: list[str] = []
        if str(self.title or "").strip():
            parts.append(f"# {str(self.title).strip()}")
        if str(self.body or "").strip():
            parts.append(str(self.body).strip())
        var_line, var_source = await self._variable_line()
        if var_line:
            parts.append(var_line)
        if parts:
            parts.append("---")

        titles = [t.strip() for t in str(self.item_titles or "").split(",") if t.strip()]
        auto = self._edge_titles(len(items)) if len(titles) < len(items) else None
        for idx, item in enumerate(items, start=1):
            named = titles[idx - 1] if idx <= len(titles) else (auto[idx - 1] if auto else None)
            if named or len(items) > 1:
                parts.append(f"### {named or f'Item {idx}'}")
            parts.append(self._render(item))

        text = "\n\n".join(parts) if parts else "*(nothing to render)*"
        # Received-vs-wired accounting: a vertex builds only after ALL connected edges
        # resolve (AND-join, verified in the param handler), so these counts should always
        # match - if they ever diverge, this line turns a suspicion into a measurement.
        wired = self._payload_edge_count()
        mismatch = f" | WARNING: {wired} edges wired, {len(items)} payloads received" \
            if wired is not None and wired != len(items) else ""
        self.status = f"{len(items)} payload(s), {len(text)} chars, variable:{var_source}{mismatch}"
        return Message(text=text)
