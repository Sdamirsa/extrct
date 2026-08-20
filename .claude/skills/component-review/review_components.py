"""Mechanical review of every ExtrCT component against the component standard."""
import ast
import re
from pathlib import Path

from lfx.custom.custom_component.custom_component import CustomComponent
from lfx.custom.utils import build_custom_component_template

BUNDLES = ("extrct_main", "extrct_xai", "extrct_tools", "extrct_flow", "extrct_batch")
# Client -> Provider renamed 2026-08-14 (user call); Adapter added for Wrapper & Merger.
CATEGORIES = ("Prep", "Provider", "Run", "PostPrep", "XAI", "QC", "DB", "Flow", "Adapter")
HANDLE_TYPES = {"HandleInput", "DataFrameInput"}


def kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def review(path: Path) -> dict:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    r = {"file": f"{path.parent.name}/{path.name}", "issues": []}

    try:
        build_custom_component_template(CustomComponent(_code=src))
    except Exception as exc:  # noqa: BLE001
        r["issues"].append(f"S15 loader FAILS: {type(exc).__name__}: {str(exc)[:80]}")

    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    attrs = {}
    inputs_calls, outputs_calls = [], []
    has_ubc, has_status = False, False
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "update_build_config":
            has_ubc = True
    for node in cls.body:
        if isinstance(node, (ast.AnnAssign, ast.Assign)):
            tgt = node.target.id if isinstance(node, ast.AnnAssign) else (
                node.targets[0].id if isinstance(node.targets[0], ast.Name) else None)
            if tgt in ("display_name", "name", "documentation", "icon"):
                v = node.value
                attrs[tgt] = v.value if isinstance(v, ast.Constant) else None
            if tgt == "inputs":
                inputs_calls = [e for e in node.value.elts if isinstance(e, ast.Call)]
            if tgt == "outputs":
                outputs_calls = [e for e in node.value.elts if isinstance(e, ast.Call)]
    has_status = "self.status" in src

    # S1 display name pattern + category
    disp = attrs.get("display_name") or ""
    r["display"] = disp
    cat = disp.split(" - ")[0] if " - " in disp else ""
    if cat not in CATEGORIES:
        r["issues"].append(f"S1 category {cat!r} not in vocabulary")
    if disp.endswith("ExtrCT"):
        r["issues"].append("S1 stale ExtrCT suffix")
    # S2 internal name present
    if not attrs.get("name"):
        r["issues"].append("S2 no internal name attribute")
    # S4 options_metadata ban - actual kwarg usage, not the ban's own documentation
    if re.search(r"options_metadata\s*=", src):
        r["issues"].append("S4 options_metadata USED (banned)")
    # S6 override_skip never on handle-fed inputs
    for c in inputs_calls:
        fn = c.func.id if isinstance(c.func, ast.Name) else ""
        if fn in HANDLE_TYPES and kw(c, "override_skip") is not None:
            nm = kw(c, "name")
            r["issues"].append(f"S6 override_skip on handle input {getattr(nm, 'value', '?')!r}")
        # S7 HandleInput must declare input_types
        if fn == "HandleInput" and kw(c, "input_types") is None:
            nm = kw(c, "name")
            r["issues"].append(f"S7 HandleInput {getattr(nm, 'value', '?')!r} without input_types")
    # S8 multi-output -> group_outputs on every output
    if len(outputs_calls) > 1:
        for c in outputs_calls:
            g = kw(c, "group_outputs")
            if g is None or getattr(g, "value", None) is not True:
                nm = kw(c, "name")
                r["issues"].append(f"S8 output {getattr(nm, 'value', '?')!r} lacks group_outputs=True")
    # S8 output methods must exist
    methods = {n.name for n in ast.walk(cls) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for c in outputs_calls:
        m = kw(c, "method")
        if m is not None and getattr(m, "value", None) not in methods:
            r["issues"].append(f"S8 output method {getattr(m, 'value', '?')!r} missing")
    # S9 status line
    if not has_status:
        r["issues"].append("S9 never sets self.status")
    # S13: DERIVED/observability logging must be guarded; a write that IS the operation
    # (registry ops, an explicit persist toggle) must be LOUD. Unguarded writes are a
    # manual-judgment note, not an automatic violation.
    writes = re.findall(r"storage\.(save_\w+|annotate_run|seed_\w+)", src)
    if writes and "except Exception" not in src:
        r["notes"] = f"S13 unguarded DB writes {sorted(set(writes))} - loud-by-design or a miss?"
    # S14 documentation attribute
    if not attrs.get("documentation"):
        r["issues"].append("S14 no documentation attribute")
    # S4/S14 note update_build_config for manual eyes
    r["ubc"] = has_ubc
    return r


rows = []
for bundle in BUNDLES:
    for py in sorted(Path("/components", bundle).glob("*.py")):
        if not py.name.startswith("__"):
            rows.append(review(py))

clean = [r for r in rows if not r["issues"]]
print(f"{len(rows)} components reviewed; {len(clean)} fully clean\n")
for r in rows:
    flag = " [update_build_config -> manual check]" if r["ubc"] else ""
    note = f"  ({r['notes']})" if r.get("notes") else ""
    if r["issues"]:
        print(f"ISSUES  {r['file']} ({r['display']}){flag}{note}")
        for i in r["issues"]:
            print(f"        - {i}")
    else:
        print(f"clean   {r['file']} ({r['display']}){flag}{note}")
