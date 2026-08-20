"""Flow configuration as data — typed variables, namespaced overrides, sweep grids.

The widget layer cannot be parameterized (toggles/dropdowns take no wires), but every
ExtrCT component builds its behavior from dicts — so configuration rides the graph as
Data and is applied at the dict level. Rules, each carrying a lesson already paid for:

- KEYS ARE NAMESPACED (`ollama.model`, `openrouter.zdr`, `client.logprobs`,
  `extract.run_tags`, `schema.encoding`): one payload can feed every node in a flow
  without ambiguity — a flow with BOTH clients needs per-provider model keys.
- UNKNOWN KEYS RAISE within an owned namespace (the dotdict.__missing__ lesson: a typo
  must fail on the canvas, never no-op). `client.X` must exist on the RECEIVING provider's
  spec — if you meant one provider only, use its prefix.
- `api_key` and `capability` are NOT overridable (a key in flow config would ride exports;
  capability is fetched, never authored).
- Configs are content-addressed: config_uid = content_uid(config) names the cell.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from typing import Any

from .hashing import content_uid
from .ollama import OllamaSpec
from .openrouter import OpenRouterSpec

TYPES = ("str", "int", "float", "bool", "json")

FORBIDDEN_CLIENT_KEYS = {"api_key", "capability"}
CLIENT_FIELDS = {
    "ollama": {f.name for f in dataclasses.fields(OllamaSpec)} - FORBIDDEN_CLIENT_KEYS,
    "openrouter": {f.name for f in dataclasses.fields(OpenRouterSpec)} - FORBIDDEN_CLIENT_KEYS,
}
EXTRACT_KEYS = {"run_tags", "ladder", "max_reprompts", "max_retries", "log_to_db",
                "store_input_text", "repair_strategy"}
# request_evidence left 2026-08-14: evidence injection moved into the router's request
# composition (grounding.mode inline/auto) — pipeline.compose owns it now.
SCHEMA_KEYS = {"encoding", "root_name", "additional_properties"}

# Non-namespaced metadata keys allowed to ride a config payload untouched.
META_KEYS = {"config_uid"}

GRID_CAP = 500  # a sweep beyond this is a conductor's job, not a canvas node's


def coerce(value: Any, type_: str) -> Any:
    """Typed coercion for table-entered values. Loud on garbage."""
    t = (type_ or "str").strip().lower()
    if t not in TYPES:
        msg = f"unknown type {type_!r}; use one of {TYPES}"
        raise ValueError(msg)
    if t == "str":
        return str(value)
    if t == "bool":
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        msg = f"cannot read {value!r} as bool (use true/false)"
        raise ValueError(msg)
    if t == "int":
        return int(str(value).strip())
    if t == "float":
        return float(str(value).strip())
    if isinstance(value, str):
        return json.loads(value)
    return value  # already-structured json (a parsed sweep candidate rides through)


def parse_config_rows(rows: list[dict]) -> dict[str, Any]:
    """Table rows (name, type, value) -> one typed, namespaced config dict."""
    out: dict[str, Any] = {}
    for r in rows or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        if name in out:
            msg = f"duplicate config key {name!r}"
            raise ValueError(msg)
        if name not in META_KEYS and "." not in name:
            msg = (f"config key {name!r} is not namespaced. Use a prefix: ollama. / "
                   f"openrouter. / client. / extract. / schema. / grounding. / certainty. / wrapper. / merger.")
            raise ValueError(msg)
        out[name] = coerce(r.get("value"), r.get("type"))
    return out


def parse_sweep_rows(rows: list[dict]) -> dict[str, list[Any]]:
    """Table rows (name, type, values) -> {key: [candidate values]}.

    `values` is a JSON array ("[0.0, 0.7]") or a comma-separated list ("a,b,c"); each
    element is coerced to the declared type.
    """
    out: dict[str, list[Any]] = {}
    for r in rows or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        if "." not in name:
            msg = f"sweep key {name!r} is not namespaced (ollama./openrouter./client./extract./schema./grounding./certainty./wrapper./merger.)"
            raise ValueError(msg)
        if name in out:
            msg = f"duplicate sweep key {name!r}"
            raise ValueError(msg)
        raw = str(r.get("values") if r.get("values") is not None else "").strip()
        if not raw:
            msg = f"sweep key {name!r} has no values"
            raise ValueError(msg)
        if raw.startswith("["):
            items = json.loads(raw)
        else:
            items = [x.strip() for x in raw.split(",") if x.strip()]
        vals = [coerce(x, r.get("type")) for x in items]
        if not vals:
            msg = f"sweep key {name!r} produced an empty value list"
            raise ValueError(msg)
        out[name] = vals
    return out


def expand_grid(sweep: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Full cross product, each cell stamped with its config_uid. Loud on explosion."""
    if not sweep:
        return []
    keys = sorted(sweep)
    n = 1
    for k in keys:
        n *= len(sweep[k])
    if n > GRID_CAP:
        msg = (f"grid has {n} cells (> {GRID_CAP}). A sweep this size belongs to the "
               f"conductor (Dagster/batch), not a canvas node - or prune the value lists.")
        raise ValueError(msg)
    cells = []
    for combo in itertools.product(*(sweep[k] for k in keys)):
        cfg = dict(zip(keys, combo, strict=True))
        cfg["config_uid"] = content_uid(cfg)
        cells.append(cfg)
    return cells


def apply_client_overrides(provider: str, spec_kwargs: dict[str, Any],
                           config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply `<provider>.X` and generic `client.X` keys over a spec kwargs dict.

    Specific beats generic. Unknown X within an owned namespace RAISES. Returns
    (new_kwargs, applied_keys) — new dict, input never mutated.
    """
    if provider not in CLIENT_FIELDS:
        msg = f"unknown provider {provider!r}"
        raise ValueError(msg)
    fields = CLIENT_FIELDS[provider]
    generic: dict[str, Any] = {}
    specific: dict[str, Any] = {}
    for full, v in (config or {}).items():
        if full in META_KEYS:
            continue
        ns, _, key = full.partition(".")
        if ns == provider:
            bucket = specific
        elif ns == "client":
            bucket = generic
        else:
            continue  # other namespaces belong to other nodes
        if key in FORBIDDEN_CLIENT_KEYS:
            msg = f"{full!r} is not overridable (credentials/capability never ride flow config)"
            raise ValueError(msg)
        if key not in fields:
            msg = (f"{full!r}: {key!r} is not a field of the {provider} spec. "
                   f"Check the spelling, or use the other provider's prefix.")
            raise ValueError(msg)
        bucket[key] = v
    merged = {**generic, **specific}
    if not merged:
        return dict(spec_kwargs), []
    out = dict(spec_kwargs)
    out.update(merged)
    return out, sorted(merged)


def owned_subset(config: dict[str, Any], namespace: str, allowed: set[str]) -> dict[str, Any]:
    """Extract `<namespace>.X` keys, validating X against `allowed`. Loud on typos."""
    out = {}
    for full, v in (config or {}).items():
        if full in META_KEYS:
            continue
        ns, _, key = full.partition(".")
        if ns != namespace:
            continue
        if key not in allowed:
            msg = f"{full!r}: {key!r} is not an overridable {namespace} setting. Allowed: {sorted(allowed)}"
            raise ValueError(msg)
        out[key] = v
    return out
