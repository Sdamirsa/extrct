"""Schema Diff — assert two schema envelopes are identical, and show where they differ.

Exists because the Python Interpreter cannot do this: it has no data port, its only wireable
input is the code field itself, so two schemas can never reach it.

The fast path is the uid. Two schemas with the same content hash ARE the same schema — that
is what content addressing means — so a matching uid is proof, not a heuristic. The deep diff
only runs when they differ, and its job is to say WHERE.
"""

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, DropdownInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message

_MISSING = object()


def _walk(a: Any, b: Any, path: str, out: list[dict], ignore: set[str]) -> None:
    if path.split(".")[-1].split("[")[0] in ignore:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            _walk(a.get(key, _MISSING), b.get(key, _MISSING), f"{path}.{key}" if path else key, out, ignore)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append({"path": path, "kind": "length", "a": f"{len(a)} items", "b": f"{len(b)} items"})
        for i in range(max(len(a), len(b))):
            _walk(
                a[i] if i < len(a) else _MISSING,
                b[i] if i < len(b) else _MISSING,
                f"{path}[{i}]", out, ignore,
            )
        return
    if a is _MISSING:
        out.append({"path": path, "kind": "only_in_b", "a": "", "b": _fmt(b)})
    elif b is _MISSING:
        out.append({"path": path, "kind": "only_in_a", "a": _fmt(a), "b": ""})
    elif a != b:
        out.append({"path": path, "kind": "changed", "a": _fmt(a), "b": _fmt(b)})


def _fmt(v: Any) -> str:
    s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


class ExtrctSchemaDiff(Component):
    display_name: str = "QC - Schema Diff"
    description: str = "Assert two schemas are identical; show exactly where they differ."
    documentation: str = "docs/extraction-stack/testing-guide.md"
    icon: str = "git-compare"
    name: str = "extrct_schema_diff"

    inputs = [
        DataInput(name="schema_a", display_name="Schema A", required=True,
                  info="A schema envelope from a Schema Builder."),
        DataInput(name="schema_b", display_name="Schema B", required=True,
                  info="The schema to compare against — e.g. the same set rebuilt from the Registry."),
        StrInput(name="label_a", display_name="Label A", value="A", advanced=True),
        StrInput(name="label_b", display_name="Label B", value="B", advanced=True),
        DropdownInput(
            name="mode",
            display_name="Mode",
            info=(
                "uid_only trusts the content hash: identical uid IS identical schema. "
                "full also deep-diffs the JSON, which only matters when you want to see where "
                "two different schemas diverge."
            ),
            options=["full", "uid_only"],
            value="full",
        ),
        BoolInput(
            name="ignore_title",
            display_name="Ignore Root Name",
            info="Treat schemas differing only by `title` as equal. Off by default: Root Name is hashed into the uid, so a difference there is a real identity difference.",
            value=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(name="verdict", display_name="Verdict", method="build_verdict", group_outputs=True),
        Output(name="differences", display_name="Differences", method="build_differences", group_outputs=True),
        Output(name="report", display_name="Report", method="build_report", group_outputs=True),
    ]

    _result: dict | None = None

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        data = getattr(value, "data", value)
        return data if isinstance(data, dict) else {}

    def _compare(self) -> dict:
        if self._result is not None:
            return self._result

        a_env, b_env = self._unwrap(self.schema_a), self._unwrap(self.schema_b)
        if not a_env or not b_env:
            msg = "Both Schema A and Schema B must be connected to a Schema Builder's Schema output."
            raise ValueError(msg)

        a_schema = a_env.get("schema", a_env)
        b_schema = b_env.get("schema", b_env)
        a_uid, b_uid = a_env.get("schema_uid"), b_env.get("schema_uid")
        a_enc, b_enc = a_env.get("encoding"), b_env.get("encoding")

        diffs: list[dict] = []
        if self.mode == "full":
            _walk(a_schema, b_schema, "", diffs, {"title"} if self.ignore_title else set())

        uid_match = bool(a_uid) and a_uid == b_uid
        equal = uid_match if self.mode == "uid_only" else (uid_match and not diffs)

        notes: list[str] = []
        if a_enc != b_enc:
            notes.append(
                f"encoding differs: {self.label_a}={a_enc!r} vs {self.label_b}={b_enc!r}. "
                "Encoding is hashed, so this alone guarantees different uids — and it changes "
                "what gets extracted, not just the schema text."
            )
        if uid_match and diffs:
            notes.append(
                "uids match but the deep diff found differences. That should be impossible and "
                "means canonicalisation is broken — report this."
            )
        if not uid_match and not diffs and self.mode == "full":
            notes.append(
                "uids differ but the schemas are structurally identical. Check envelope fields "
                "outside `schema` (root_name, additional_properties) — they are hashed too."
            )

        self._result = {
            "equal": equal, "uid_match": uid_match,
            "uid_a": a_uid, "uid_b": b_uid,
            "encoding_a": a_enc, "encoding_b": b_enc,
            "differences": diffs, "notes": notes,
        }
        return self._result

    def build_verdict(self) -> Message:
        r = self._compare()
        la, lb = self.label_a or "A", self.label_b or "B"
        if r["equal"]:
            text = f"PASS — {la} and {lb} are the same schema (uid {r['uid_a']})."
            self.status = f"PASS {r['uid_a']}"
        else:
            n = len(r["differences"])
            text = (
                f"FAIL — {la} and {lb} differ.\n"
                f"  {la}: {r['uid_a']} (encoding {r['encoding_a']})\n"
                f"  {lb}: {r['uid_b']} (encoding {r['encoding_b']})\n"
                f"  {n} structural difference(s)."
            )
            self.status = f"FAIL: {n} difference(s)"
        for note in r["notes"]:
            text += f"\n  NOTE: {note}"
        return Message(text=text)

    def build_differences(self) -> DataFrame:
        r = self._compare()
        la, lb = self.label_a or "A", self.label_b or "B"
        rows = [{"path": d["path"] or "<root>", "kind": d["kind"], la: d["a"], lb: d["b"]} for d in r["differences"]]
        self.status = f"{len(rows)} difference(s)"
        return DataFrame(rows)

    def build_report(self) -> Data:
        return Data(data=self._compare())
