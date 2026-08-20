"""merge-def/1.0 — merge per-chunk extractions into one record, conflicts labeled.

The design borrows from three fields and adds what none of them has alone: map-reduce
long-document IE contributes vote-then-merge with ABSTENTION (a chunk that returned null
simply did not see the variable — absence is never a vote); record linkage / entity
resolution contributes blocking keys and similarity-threshold clustering for list items
extracted twice from overlapping chunks; data-fusion truth discovery contributes the rule
that a conflict is SURFACED, never silently resolved — every disputed variable carries
its full candidate set, provenance and severity in the report, whatever the resolution
strategy chose.

Deterministic and pure: no LLM inside. The LLM adjudication rung is deliberately
OUTSIDE this module — `adjudication_task()` composes a prompt for a Structured Extract
run, the same pattern as post-hoc grounding (the Posthoc Templater precedent: extrct
merge work is not generation).

Scalar machinery: votes are grouped by normalized equality (strings casefold + collapsed
whitespace; numbers within a RELATIVE tolerance of the group representative; dates/bools
exact). One group = agreement. Several = conflict: severity `major` when the top group
ties or holds no more than half the votes, else `minor`. Resolution strategies:
majority | first | last | longest | refuse; `label_and_null` policy nulls MAJOR
conflicts regardless of strategy. List machinery: greedy agglomerative clustering in
document order — an item joins the first cluster whose representative is similar enough
(difflib ratio on normalized text; per-field composite for objects; `list_key` narrows
matching to one field, a blocking key) — then each cluster's fields merge through the
scalar machinery, so item-level disputes land in the report exactly like top-level ones.

Merged output stays SCHEMA-SHAPED and clean: provenance, candidates and severities live
in the report, never inside the values. `_evidence` is stripped (chunk-relative offsets
do not survive merging; re-ground post-merge on the full text via the post-hoc lane).
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from .hashing import content_uid

MERGE_MODEL_VERSION = "merge-def/1.0"
SCALAR_STRATEGIES = ("majority", "first", "last", "longest", "refuse")
CONFLICT_POLICIES = ("label_and_resolve", "label_and_null")

MERGE_DEFAULTS: dict[str, Any] = {
    "scalar_strategy": "majority",
    "conflict_policy": "label_and_resolve",
    "numeric_tolerance": 0.01,
    "list_similarity": 0.85,
    "list_key": "",
}


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _sim(a: Any, b: Any, tol: float) -> float:
    na, nb = _norm(a), _norm(b)
    if isinstance(na, float) and isinstance(nb, float):
        return 1.0 if abs(na - nb) <= tol * max(abs(na), abs(nb), 1e-9) else 0.0
    if isinstance(na, str) and isinstance(nb, str):
        return difflib.SequenceMatcher(None, na, nb).ratio()
    return 1.0 if na == nb else 0.0


def _obj_sim(a: dict, b: dict, tol: float, list_key: str) -> float:
    if list_key and a.get(list_key) is not None and b.get(list_key) is not None:
        return _sim(a[list_key], b[list_key], tol)
    common = [k for k in a if k in b and a[k] is not None and b[k] is not None
              and not isinstance(a[k], (dict, list)) and not isinstance(b[k], (dict, list))]
    if not common:
        return 0.0
    return sum(_sim(a[k], b[k], tol) for k in common) / len(common)


def _group_scalar(votes: list[tuple[int, Any]], tol: float) -> list[dict]:
    """Votes -> groups by normalized equality, ordered by count desc then first seen."""
    groups: list[dict] = []
    for idx, value in votes:
        nv = _norm(value)
        placed = False
        for g in groups:
            ref = g["norm"]
            same = (abs(nv - ref) <= tol * max(abs(ref), abs(nv), 1e-9)
                    if isinstance(nv, float) and isinstance(ref, float) else nv == ref)
            if same:
                g["count"] += 1
                g["chunks"].append(idx)
                placed = True
                break
        if not placed:
            groups.append({"value": value, "norm": nv, "count": 1, "chunks": [idx]})
    return sorted(groups, key=lambda g: (-g["count"], g["chunks"][0]))


def _resolve(groups: list[dict], votes: list[tuple[int, Any]], strategy: str):
    if len(groups) == 1:
        return groups[0]["value"]
    if strategy == "refuse":
        return None
    if strategy == "first":
        return min(votes, key=lambda v: v[0])[1]
    if strategy == "last":
        return max(votes, key=lambda v: v[0])[1]
    if strategy == "longest":
        return max(votes, key=lambda v: len(str(v[1])))[1]
    return groups[0]["value"]  # majority; sort already broke ties by document order


def _severity(groups: list[dict]) -> str | None:
    if len(groups) <= 1:
        return None
    total = sum(g["count"] for g in groups)
    if groups[0]["count"] == groups[1]["count"] or groups[0]["count"] * 2 <= total:
        return "major"
    return "minor"


class _Merger:
    def __init__(self, config: dict[str, Any]):
        self.cfg = {**MERGE_DEFAULTS, **(config or {})}
        if self.cfg["scalar_strategy"] not in SCALAR_STRATEGIES:
            msg = f"scalar_strategy {self.cfg['scalar_strategy']!r} not in {SCALAR_STRATEGIES}"
            raise ValueError(msg)
        if self.cfg["conflict_policy"] not in CONFLICT_POLICIES:
            msg = f"conflict_policy {self.cfg['conflict_policy']!r} not in {CONFLICT_POLICIES}"
            raise ValueError(msg)
        self.variables: dict[str, dict] = {}
        self.conflicts: list[dict] = []

    # -- scalars ----------------------------------------------------------------------
    def scalar(self, path: str, votes: list[tuple[int, Any]]):
        votes = [(i, v) for i, v in votes if v is not None]
        if not votes:
            self.variables[path] = {"kind": "scalar", "votes": 0, "value": None}
            return None
        groups = _group_scalar(votes, float(self.cfg["numeric_tolerance"]))
        sev = _severity(groups)
        value = _resolve(groups, votes, self.cfg["scalar_strategy"])
        if sev == "major" and self.cfg["conflict_policy"] == "label_and_null":
            value = None
        entry = {"kind": "scalar", "votes": len(votes), "value": value,
                 "agreement": round(groups[0]["count"] / len(votes), 3),
                 "conflict": sev is not None}
        if sev:
            entry["severity"] = sev
            entry["strategy"] = self.cfg["scalar_strategy"]
            entry["candidates"] = [{"value": g["value"], "count": g["count"], "chunks": g["chunks"]}
                                   for g in groups]
            self.conflicts.append({"path": path, "severity": sev, "resolved": value,
                                   "candidates": entry["candidates"]})
        self.variables[path] = entry
        return value

    # -- lists ------------------------------------------------------------------------
    def list_(self, path: str, items: list[tuple[int, Any]]):
        tol = float(self.cfg["numeric_tolerance"])
        thr = float(self.cfg["list_similarity"])
        key = str(self.cfg["list_key"] or "")
        clusters: list[dict] = []  # {"rep": item, "members": [(idx, item)]}
        for idx, item in items:
            if item is None:
                continue
            best, best_sim = None, 0.0
            for c in clusters:
                s = (_obj_sim(item, c["rep"], tol, key)
                     if isinstance(item, dict) and isinstance(c["rep"], dict)
                     else _sim(item, c["rep"], tol))
                if s > best_sim:
                    best, best_sim = c, s
            if best is not None and best_sim >= thr:
                best["members"].append((idx, item))
            else:
                clusters.append({"rep": item, "members": [(idx, item)]})

        merged, detail = [], []
        for ci, c in enumerate(clusters):
            members = c["members"]
            if isinstance(c["rep"], dict):
                keys = list(dict.fromkeys(k for _, it in members for k in it))
                obj = {}
                for k in keys:
                    obj[k] = self.node(f"{path}[{ci}].{k}",
                                       [(i, it.get(k)) for i, it in members])
                merged.append(obj)
            else:
                merged.append(members[0][1])  # first appearance keeps its original form
            detail.append({"sources": sorted({i for i, _ in members}), "size": len(members)})
        self.variables[path] = {"kind": "list", "items": len(merged),
                                "raw_items": len([1 for _, it in items if it is not None]),
                                "clusters": detail}
        return merged

    # -- structure walk ---------------------------------------------------------------
    def node(self, path: str, votes: list[tuple[int, Any]]):
        present = [(i, v) for i, v in votes if v is not None]
        if any(isinstance(v, list) for _, v in present):
            flat: list[tuple[int, Any]] = []
            for i, v in present:
                if isinstance(v, list):
                    flat.extend((i, x) for x in v)
                else:  # a scalar where others sent lists joins as a one-item list
                    flat.append((i, v))
            return self.list_(path, flat)
        if any(isinstance(v, dict) for _, v in present):
            keys = list(dict.fromkeys(k for _, v in present if isinstance(v, dict) for k in v))
            return {k: self.node(f"{path}.{k}" if path else k,
                                 [(i, v.get(k)) for i, v in present if isinstance(v, dict)])
                    for k in keys}
        return self.scalar(path, present)


def merge_extractions(inputs: list[dict[str, Any]], *, schema: dict | None = None,
                      config: dict[str, Any] | None = None) -> dict[str, Any]:
    """inputs: [{chunk_idx, chunk_uid?, run_uid?, extracted: {...}}, ...] in document
    order. Returns {version, merge_uid, merged, variables, conflicts, stats, config}."""
    usable = [d for d in inputs or [] if isinstance(d.get("extracted"), dict)]
    if not usable:
        msg = "no usable extractions to merge (every input lacks an extracted object)"
        raise ValueError(msg)
    usable = sorted(usable, key=lambda d: int(d.get("chunk_idx", 0)))
    stripped = []
    for d in usable:
        ex = {k: v for k, v in d["extracted"].items() if k != "_evidence"}
        stripped.append((int(d.get("chunk_idx", 0)), ex))

    m = _Merger(config or {})
    keys = list(dict.fromkeys(k for _, ex in stripped for k in ex))
    merged = {k: m.node(k, [(i, ex.get(k)) for i, ex in stripped]) for k in keys}

    body = {"version": MERGE_MODEL_VERSION, "config": m.cfg,
            "inputs": [{"chunk_idx": i, "extracted": ex} for i, ex in stripped]}
    majors = [c for c in m.conflicts if c["severity"] == "major"]
    return {"version": MERGE_MODEL_VERSION, "merge_uid": content_uid(body),
            "merged": merged, "variables": m.variables, "conflicts": m.conflicts,
            "config": m.cfg, "schema_uid": (schema or {}).get("schema_uid"),
            "run_uids": [d.get("run_uid") for d in usable if d.get("run_uid")],
            "stats": {"inputs": len(usable), "variables": len(m.variables),
                      "conflicts": len(m.conflicts), "major_conflicts": len(majors)}}


def adjudication_task(result: dict[str, Any], *, min_severity: str = "major") -> str:
    """A composed prompt for the LLM adjudication rung — run it through a Structured
    Extract (the Posthoc Templater pattern); this module never calls a model. Empty
    string when nothing qualifies."""
    wanted = ("major",) if min_severity == "major" else ("major", "minor")
    picked = [c for c in result.get("conflicts", []) if c["severity"] in wanted]
    if not picked:
        return ""
    lines = ["The following variables were extracted from different parts of ONE document "
             "with conflicting values. For each variable, decide the single best value, "
             "using the source document as the only authority. Answer as JSON: "
             '{"<variable>": <value>, ...}.', ""]
    for c in picked:
        cands = "; ".join(f"{json.dumps(g['value'], ensure_ascii=False)} "
                          f"(seen in chunk(s) {g['chunks']}, {g['count']}x)"
                          for g in c["candidates"])
        lines.append(f"- {c['path']}: {cands}")
    return "\n".join(lines)
