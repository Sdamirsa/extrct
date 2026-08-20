"""The repair ladder — ordered, independently switchable, every rung logged.

Repair is an ABLATION AXIS, not a fallback. Each rung is declared in the run config and
recorded per attempt, and `final_status` distinguishes `ok` from `repaired`. Without that
distinction repair quietly inflates the success rate of whichever cell needed it most, and
a the factorial sweep comparison silently measures the ladder instead of the model.

Rungs, in order:
  request      the call itself (owned by the client, not this module)
  json_repair  deterministic text fixes - fences, trailing commas, unbalanced braces
  coerce       schema-guided casts: "47%" -> 47.0, case-insensitive enum match
  reprompt     re-send to the SAME model with the verbatim validation errors
  llm_repair   a DIFFERENT (often cheaper) model sees only schema + bad output + errors

Deliberately absent: range clamping. Coercing `lvef: 250` into `100` would launder a wrong
answer into a well-formed one. Range violations stay errors - which matters because Ollama's
grammar does not enforce `minimum`/`maximum` at all, so this validator is the only thing
catching them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema import Draft202012Validator

LAYERS = ("json_repair", "coerce", "reprompt", "llm_repair")
DEFAULT_LADDER = ("json_repair", "coerce")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def validate(obj: Any, schema: dict) -> list[dict]:
    """Return verbatim validation errors. Empty list means valid."""
    validator = Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(obj), key=lambda e: list(e.path)):
        out.append(
            {
                "loc": [str(p) for p in err.path],
                "validator": err.validator,
                "msg": err.message,
                "expected": err.validator_value if not callable(err.validator_value) else None,
            }
        )
    return out


def parse_text(raw: str) -> tuple[Any | None, str | None]:
    """Strict parse first. Returns (obj, error)."""
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def repair_json_text(raw: str) -> tuple[Any | None, bool]:
    """Deterministic, no model. Returns (obj, changed)."""
    obj, err = parse_text(raw)
    if err is None:
        return obj, False

    stripped = _FENCE.sub("", raw).strip()
    obj, err = parse_text(stripped)
    if err is None:
        return obj, True

    try:
        from json_repair import repair_json

        fixed = repair_json(stripped)
        obj, err = parse_text(fixed if isinstance(fixed, str) else json.dumps(fixed))
        if err is None:
            return obj, True
    except ImportError:
        pass
    return None, False


def _coerce_scalar(value: Any, node: dict, path: str, notes: list[str]) -> Any:
    types = node.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    enum = node.get("enum")

    if enum and isinstance(value, str) and value not in enum:
        for option in enum:
            if isinstance(option, str) and option.lower() == value.lower():
                notes.append(f"{path}: enum case-corrected {value!r} -> {option!r}")
                return option

    if "number" in types or "integer" in types:
        if isinstance(value, str):
            m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
            if m:
                num = float(m.group()) if "number" in types else int(float(m.group()))
                notes.append(f"{path}: cast {value!r} -> {num!r}")
                return num
        if isinstance(value, bool):
            return value

    if "boolean" in types and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            notes.append(f"{path}: cast {value!r} -> True")
            return True
        if low in ("false", "no", "0"):
            notes.append(f"{path}: cast {value!r} -> False")
            return False

    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        notes.append(f"{path}: cast {value!r} -> str")
        return str(value)

    return value


def coerce_to_schema(obj: Any, schema: dict, *, path: str = "$") -> tuple[Any, list[str]]:
    """Schema-guided type coercion. Never clamps ranges - see module docstring."""
    notes: list[str] = []

    def walk(value: Any, node: dict, where: str) -> Any:
        if not isinstance(node, dict):
            return value
        types = node.get("type")
        types = [types] if isinstance(types, str) else list(types or [])

        if "object" in types and isinstance(value, dict):
            props = node.get("properties") or {}
            return {k: (walk(v, props[k], f"{where}.{k}") if k in props else v) for k, v in value.items()}
        if "array" in types and isinstance(value, list):
            items = node.get("items") or {}
            return [walk(v, items, f"{where}[{i}]") for i, v in enumerate(value)]
        if value is None:
            return None
        return _coerce_scalar(value, node, where, notes)

    return walk(obj, schema, path), notes


async def run_ladder(
    raw_text: str,
    schema: dict,
    *,
    ladder: tuple[str, ...] = DEFAULT_LADDER,
    reprompt: Callable[[str, list[dict]], Awaitable[str]] | None = None,
    llm_repair: Callable[[str, list[dict]], Awaitable[str]] | None = None,
    max_reprompts: int = 1,
) -> dict[str, Any]:
    """Walk the rungs until valid or exhausted. Returns a full attempt log.

    `final_status` is `ok` only when the FIRST parse validated with no rung applied.
    Anything else is `repaired`, so analysis can exclude repaired runs.
    """
    attempts: list[dict] = []
    layers_used: list[str] = []

    def record(layer: str, obj: Any, errors: list[dict], note: str | None = None) -> None:
        attempts.append(
            {
                "attempt_no": len(attempts) + 1,
                "layer": layer,
                "valid": not errors,
                "validation_errors": errors,
                "note": note,
            }
        )

    obj, parse_err = parse_text(raw_text)
    errors = validate(obj, schema) if parse_err is None else [{"loc": [], "validator": "json", "msg": parse_err}]
    record("request", obj, errors)
    if not errors:
        return {"obj": obj, "final_status": "ok", "layers_used": [], "attempts": attempts}

    if "json_repair" in ladder and parse_err is not None:
        repaired, changed = repair_json_text(raw_text)
        if changed and repaired is not None:
            layers_used.append("json_repair")
            obj = repaired
            errors = validate(obj, schema)
            record("json_repair", obj, errors)
            if not errors:
                return {"obj": obj, "final_status": "repaired", "layers_used": layers_used, "attempts": attempts}

    if "coerce" in ladder and obj is not None:
        coerced, notes = coerce_to_schema(obj, schema)
        if notes:
            layers_used.append("coerce")
            obj = coerced
            errors = validate(obj, schema)
            record("coerce", obj, errors, note="; ".join(notes[:6]))
            if not errors:
                return {"obj": obj, "final_status": "repaired", "layers_used": layers_used, "attempts": attempts}

    if "reprompt" in ladder and reprompt is not None:
        for _ in range(max_reprompts):
            raw_text = await reprompt(raw_text, errors)
            layers_used.append("reprompt")
            obj, parse_err = parse_text(raw_text)
            errors = (
                validate(obj, schema) if parse_err is None else [{"loc": [], "validator": "json", "msg": parse_err}]
            )
            record("reprompt", obj, errors)
            if not errors:
                return {"obj": obj, "final_status": "repaired", "layers_used": layers_used, "attempts": attempts}

    if "llm_repair" in ladder and llm_repair is not None:
        raw_text = await llm_repair(raw_text, errors)
        layers_used.append("llm_repair")
        obj, parse_err = parse_text(raw_text)
        errors = validate(obj, schema) if parse_err is None else [{"loc": [], "validator": "json", "msg": parse_err}]
        record("llm_repair", obj, errors)
        if not errors:
            return {"obj": obj, "final_status": "repaired", "layers_used": layers_used, "attempts": attempts}

    return {"obj": obj, "final_status": "invalid", "layers_used": layers_used, "attempts": attempts}
