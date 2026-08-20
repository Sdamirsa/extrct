"""Variable rows -> JSON Schema -> Pydantic source.

The whole pathway rests on one observation: Langflow's ceiling was never a LangChain
limitation, it was a four-column table. `lfx.helpers.base_model.build_model_from_schema`
accepts only {str,int,float,bool,list,dict}, makes every field required, and rejects
enum/literal/date/object/Optional. Adding columns removes the ceiling.

Our columns: name, type, is_list, description, parent, options, constraints, required.
The last two are additions - they are cheap now and painful to retrofit once schemas are
hashed and cited by runs.

`to_pydantic_source` is the swap surface: the same variable rows produce a JSON Schema
for our raw HTTP path and a real Pydantic class for extrct-agent. The SCHEMA is the
handshake between the two implementations, not the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hashing import content_uid

# type -> (json type, extra keywords)
TYPE_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    "str": ("string", {}),
    "string": ("string", {}),
    "text": ("string", {}),
    "int": ("integer", {}),
    "integer": ("integer", {}),
    "float": ("number", {}),
    "number": ("number", {}),
    "bool": ("boolean", {}),
    "boolean": ("boolean", {}),
    "date": ("string", {"format": "date"}),
    "datetime": ("string", {"format": "date-time"}),
    "object": ("object", {}),
}

_PY_TYPE = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
}

# Constraint keys we pass through, mapped to JSON Schema keywords.
CONSTRAINT_MAP = {
    "ge": "minimum",
    "minimum": "minimum",
    "le": "maximum",
    "maximum": "maximum",
    "gt": "exclusiveMinimum",
    "lt": "exclusiveMaximum",
    "pattern": "pattern",
    "min_length": "minLength",
    "minLength": "minLength",
    "max_length": "maxLength",
    "maxLength": "maxLength",
    "min_items": "minItems",
    "max_items": "maxItems",
}


class SchemaError(ValueError):
    """Raised loudly. A malformed schema must never reach a model half-built."""


@dataclass
class Variable:
    name: str
    type: str = "str"
    is_list: bool = False
    description: str = ""
    parent: str | None = None
    options: list[Any] | None = None
    constraints: dict[str, Any] | None = None
    required: bool = True
    ordinal: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Variable:
        """Tolerant of the shapes a Langflow TableInput or a DB row actually produce."""
        # `options` arrives two ways and both must work: "TTE,CMR" from the table UI, and
        # '["TTE","CMR"]' from Postgres via ::text. Comma-splitting the JSON form silently
        # produces ['["TTE"', '"CMR"'] - a valid-looking enum of garbage. Caught by the
        # registry round-trip test.
        opts = row.get("options")
        if isinstance(opts, str):
            text = opts.strip()
            if text.startswith("["):
                import json

                try:
                    parsed = json.loads(text)
                    opts = list(parsed) if isinstance(parsed, list) else None
                except ValueError as exc:
                    msg = f"{row.get('name')!r}: options looks like JSON but does not parse: {text!r}"
                    raise SchemaError(msg) from exc
            else:
                opts = [o.strip() for o in text.split(",") if o.strip()] or None
        cons = row.get("constraints")
        if isinstance(cons, str) and cons.strip():
            import json

            try:
                cons = json.loads(cons)
            except ValueError as exc:
                msg = f"{row.get('name')!r}: constraints is not valid JSON: {cons!r}"
                raise SchemaError(msg) from exc
        parent = row.get("parent") or row.get("parent_uid") or None
        return cls(
            name=str(row.get("name", "")).strip(),
            type=str(row.get("type", "str")).strip().lower(),
            is_list=_as_bool(row.get("is_list", row.get("as_list", False))),
            description=str(row.get("description") or ""),
            parent=str(parent).strip() if parent else None,
            options=opts or None,
            constraints=cons or None,
            required=_as_bool(row.get("required", True)),
            ordinal=int(row.get("ordinal") or 0),
        )


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


def _leaf(var: Variable) -> dict[str, Any]:
    if var.type not in TYPE_MAP:
        msg = f"{var.name!r}: unknown type {var.type!r}. Known: {sorted(set(TYPE_MAP))}"
        raise SchemaError(msg)
    json_type, extra = TYPE_MAP[var.type]
    node: dict[str, Any] = {"type": json_type, **extra}
    if var.description:
        node["description"] = var.description
    if var.options:
        node["enum"] = list(var.options)
    for key, value in (var.constraints or {}).items():
        mapped = CONSTRAINT_MAP.get(key)
        if mapped is None:
            msg = f"{var.name!r}: unknown constraint {key!r}. Known: {sorted(set(CONSTRAINT_MAP))}"
            raise SchemaError(msg)
        node[mapped] = value
    return node


STRICT_NULLABLE = "strict_nullable"
NATIVE_REQUIRED = "native_required"
ENCODINGS = (STRICT_NULLABLE, NATIVE_REQUIRED)


def build_schema(
    variables: list[Variable] | list[dict],
    *,
    root_name: str = "extract",
    encoding: str = STRICT_NULLABLE,
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Assemble rows into a JSON Schema.

    `encoding` is a DECLARED EXPERIMENTAL AXIS, not a backend detail. Measured on identical
    variable rows: under `native_required` the model omitted an optional field entirely,
    while under `strict_nullable` it filled it. Same variables, different completeness - so
    if one cell uses one encoding and another cell uses the other, a completeness difference
    could be the encoding rather than the model. Pin it per run.

      strict_nullable  EVERY property appears in `required`; optionality is a nullable union
                       (`["string","null"]`). Not stylistic - OpenAI strict mode REJECTS a
                       schema whose `required` omits a property, so "optional" must live in
                       the type.
      native_required  Real `required` list; optional properties may simply be absent from
                       the response. Ollama honours this natively.

    The two encodings hash differently, which is correct: they are different requests.

    NOTE: this is distinct from OpenRouter's `response_format.json_schema.strict`, which
    asks the provider to hard-enforce and does not change the schema at all. See
    `OpenRouterSpec.response_format_strict`.
    """
    if encoding not in ENCODINGS:
        msg = f"unknown encoding {encoding!r}. Known: {ENCODINGS}"
        raise SchemaError(msg)
    strict = encoding == STRICT_NULLABLE
    vars_: list[Variable] = [v if isinstance(v, Variable) else Variable.from_row(v) for v in variables]
    vars_ = [v for v in vars_ if v.name]
    if not vars_:
        msg = "no variables: nothing to build"
        raise SchemaError(msg)

    names = [v.name for v in vars_]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        msg = f"duplicate variable names: {sorted(dupes)}. Names must be unique within a schema set."
        raise SchemaError(msg)

    by_name = {v.name: v for v in vars_}
    children: dict[str | None, list[Variable]] = {}
    for v in vars_:
        if v.parent and v.parent not in by_name:
            msg = f"{v.name!r}: parent {v.parent!r} is not a variable in this set"
            raise SchemaError(msg)
        if v.parent and by_name[v.parent].type != "object":
            msg = f"{v.name!r}: parent {v.parent!r} has type {by_name[v.parent].type!r}, must be 'object'"
            raise SchemaError(msg)
        children.setdefault(v.parent, []).append(v)

    _check_cycles(by_name)

    def node_for(var: Variable) -> dict[str, Any]:
        if var.type == "object":
            kids = sorted(children.get(var.name, []), key=lambda c: (c.ordinal, c.name))
            if not kids:
                msg = f"{var.name!r} is type 'object' but has no children"
                raise SchemaError(msg)
            inner = _object_node(kids, node_for, strict=strict, additional_properties=additional_properties)
            if var.description:
                inner["description"] = var.description
        else:
            inner = _leaf(var)

        if var.is_list:
            wrapper: dict[str, Any] = {"type": "array", "items": inner}
            for key in ("minItems", "maxItems"):
                if key in inner:
                    wrapper[key] = inner.pop(key)
            if var.description:
                wrapper["description"] = inner.pop("description", var.description)
            return wrapper
        return inner

    roots = sorted(children.get(None, []), key=lambda c: (c.ordinal, c.name))
    if not roots:
        msg = "every variable has a parent: no root to build from"
        raise SchemaError(msg)

    schema = _object_node(roots, node_for, strict=strict, additional_properties=additional_properties)
    schema["title"] = root_name
    return schema


def _object_node(kids, node_for, *, strict: bool, additional_properties: bool) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for kid in kids:
        node = node_for(kid)
        if strict and not kid.required:
            node = _make_nullable(node)
        props[kid.name] = node
        # strict mode: every property must be listed as required
        if strict or kid.required:
            required.append(kid.name)
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if not additional_properties:
        out["additionalProperties"] = False
    return out


def _make_nullable(node: dict[str, Any]) -> dict[str, Any]:
    node = dict(node)
    t = node.get("type")
    if isinstance(t, str):
        node["type"] = [t, "null"]
    elif isinstance(t, list) and "null" not in t:
        node["type"] = [*t, "null"]
    return node


def _check_cycles(by_name: dict[str, Variable]) -> None:
    for start in by_name:
        seen, cur = set(), start
        while cur is not None:
            if cur in seen:
                msg = f"cycle in parent chain involving {cur!r}"
                raise SchemaError(msg)
            seen.add(cur)
            cur = by_name[cur].parent if cur in by_name else None


def schema_uid(schema: dict[str, Any]) -> str:
    return content_uid(schema)


def leaf_paths(schema: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of every scalar leaf (arrays of scalars count as one leaf).

    Arrays of OBJECTS are skipped: their element count is unknown at schema time, so a
    per-element evidence request cannot be declared. Recorded limitation (v1).
    """
    out: list[str] = []
    for name, node in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        # unwrap strict_nullable unions to find the effective node
        eff = node
        if "anyOf" in eff and isinstance(eff["anyOf"], list):
            eff = next((s for s in eff["anyOf"] if s.get("type") != "null"), eff)
        t = eff.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        if t == "object" or (eff.get("properties") is not None):
            out.extend(leaf_paths(eff, path))
        elif t == "array":
            items = eff.get("items") or {}
            it = items.get("type")
            if isinstance(it, list):
                it = next((x for x in it if x != "null"), None)
            if it == "object" or items.get("properties") is not None:
                continue  # array of objects: skipped (v1 limitation)
            out.append(path)
        else:
            out.append(path)
    return out


def _quote_field(strict: bool, hint: str) -> dict[str, Any]:
    return {
        "type": ["string", "null"] if strict else "string",
        # "never the value itself" matters: measured live, models echo inferred values
        # (e.g. temporality "current") instead of quoting the sentence that justifies them.
        "description": f"COPY-PASTE the exact source passage that justifies {hint} - the "
                       "characters must appear verbatim in the source document. NEVER "
                       "repeat the extracted value itself; if the value is inferred (a "
                       "category, a status), quote the sentence you inferred it FROM. "
                       "Do not paraphrase or reorder words. null if nothing supports it.",
    }


def evidence_mirror(schema: dict[str, Any], encoding: str, hint: str = "this value") -> dict[str, Any]:
    """MIRROR the value structure, with every scalar leaf replaced by a quote string.

    v2 of the evidence shape (v1 flattened to top-level leaves, which gave one blanket
    quote for a whole array and nothing for arrays of objects — caught by the user on the
    first real nested schema). Arrays of scalars become arrays of quotes (one per item,
    same order); arrays of objects become arrays of per-field quote objects.
    """
    strict = encoding == STRICT_NULLABLE
    eff = schema
    if "anyOf" in eff and isinstance(eff["anyOf"], list):
        eff = next((s for s in eff["anyOf"] if s.get("type") != "null"), eff)
    t = eff.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t == "object" or eff.get("properties") is not None:
        props = {k: evidence_mirror(v, encoding, hint=f"'{k}'")
                 for k, v in (eff.get("properties") or {}).items() if k != "_evidence"}
        return {"type": "object", "properties": props,
                "required": list(props) if strict else [],
                "additionalProperties": False,
                "description": "One evidence entry per extracted value, same structure and order."}
    if t == "array":
        items = eff.get("items") or {}
        return {"type": "array",
                "description": "ONE quote per array item, in the SAME order as the extracted array.",
                "items": evidence_mirror(items, encoding, hint="this array item")}
    return _quote_field(strict, hint)


def augment_with_evidence(schema: dict[str, Any], encoding: str) -> dict[str, Any]:
    """Add the parallel `_evidence` object mirroring the value structure (v2).

    The values object stays untouched; grounding-research.md records why quotes (never
    offsets) and why a parallel object (values purity). The augmented schema hashes to a
    new schema_uid automatically.
    """
    mirror = evidence_mirror(schema, encoding)
    if not (mirror.get("properties") or {}):
        return schema
    strict = encoding == STRICT_NULLABLE
    out = dict(schema)
    out_props = dict(out.get("properties") or {})
    mirror["description"] = ("Supporting evidence: for each extracted value, the exact "
                             "source text it came from, in the same structure and order.")
    out_props["_evidence"] = mirror
    out["properties"] = out_props
    if strict and "required" in out and "_evidence" not in out["required"]:
        out = {**out, "required": [*out["required"], "_evidence"]}
    return out


def evidence_schema(schema: dict[str, Any], encoding: str, root_name: str = "evidence") -> dict[str, Any]:
    """STANDALONE schema for post-hoc grounding: a second LLM call answers only with the
    `_evidence` mirror. Values purity is total — the extraction run is never perturbed."""
    mirror = evidence_mirror(schema, encoding)
    mirror["description"] = ("For every extracted value you were shown, the VERBATIM "
                             "supporting quote from the source document.")
    return {
        "type": "object",
        "properties": {"_evidence": mirror},
        "required": ["_evidence"],
        "additionalProperties": False,
        "title": root_name,
    }


def schema_envelope(
    variables: list[Variable] | list[dict],
    *,
    root_name: str = "extract",
    encoding: str = STRICT_NULLABLE,
    additional_properties: bool = False,
    request_evidence: bool = False,
) -> dict[str, Any]:
    """Schema plus the metadata a run record needs. One source of truth for the encoding.

    The clients unwrap this rather than taking a bare schema, so the encoding cannot drift
    out of sync with the schema it produced - which it would if the axis were recorded by
    hand in a second field. `request_evidence` augments the schema with the `_evidence`
    quote object (grounding); off produces a byte-identical schema to before the feature.
    """
    schema = build_schema(
        variables, root_name=root_name, encoding=encoding, additional_properties=additional_properties
    )
    if request_evidence:
        schema = augment_with_evidence(schema, encoding)
    return {
        "schema": schema,
        "schema_uid": schema_uid(schema),
        "encoding": encoding,
        "root_name": root_name,
        "additional_properties": additional_properties,
        "request_evidence": bool(request_evidence),
        "variable_count": len([v for v in variables if (v.name if isinstance(v, Variable) else v.get("name"))]),
    }


def unwrap_schema(schema_or_envelope: dict[str, Any] | None) -> tuple[dict | None, str | None]:
    """Accept either a bare JSON Schema or a schema_envelope. Returns (schema, encoding)."""
    if not schema_or_envelope:
        return None, None
    if "schema" in schema_or_envelope and isinstance(schema_or_envelope.get("schema"), dict):
        return schema_or_envelope["schema"], schema_or_envelope.get("encoding")
    return schema_or_envelope, None


def to_pydantic_source(
    variables: list[Variable] | list[dict],
    *,
    root_name: str = "Extract",
) -> str:
    """Emit importable Pydantic source. The swap test swap surface.

    extrct-agent imports this class and hands it to Pydantic AI; the raw HTTP path uses the
    JSON Schema built from the same rows. Same handshake, two implementations.
    """
    vars_ = [v if isinstance(v, Variable) else Variable.from_row(v) for v in variables]
    vars_ = [v for v in vars_ if v.name]
    by_name = {v.name: v for v in vars_}
    children: dict[str | None, list[Variable]] = {}
    for v in vars_:
        children.setdefault(v.parent, []).append(v)

    blocks: list[str] = []

    def class_name(n: str) -> str:
        return "".join(p.capitalize() or "_" for p in n.replace("-", "_").split("_"))

    def emit(var_name: str | None, cls: str) -> None:
        kids = sorted(children.get(var_name, []), key=lambda c: (c.ordinal, c.name))
        for kid in kids:
            if kid.type == "object":
                emit(kid.name, class_name(kid.name))
        lines = [f"class {cls}(BaseModel):"]
        if not kids:
            lines.append("    pass")
        for kid in kids:
            lines.append(f"    {kid.name}: {_py_annotation(kid, class_name)} = {_py_field(kid)}")
        blocks.append("\n".join(lines))

    emit(None, root_name)
    # NOTE: deliberately no `from __future__ import annotations`. It makes every annotation
    # a lazy string, and pydantic then cannot resolve the nested class when the source is
    # exec'd into a fresh namespace ("Extract is not fully defined"). Classes are emitted
    # children-first, so eager annotations resolve naturally. `X | None` needs no import on
    # the >=3.10 floor this package targets.
    header = (
        "# Generated by extrct.schema.to_pydantic_source - do not edit by hand.\n"
        "from typing import Literal\n\n"
        "from pydantic import BaseModel, Field\n"
    )
    return header + "\n\n" + "\n\n\n".join(blocks) + "\n"


def _py_annotation(var: Variable, class_name) -> str:
    if var.type == "object":
        base = class_name(var.name)
    elif var.options:
        base = "Literal[" + ", ".join(repr(o) for o in var.options) + "]"
    else:
        json_type, _ = TYPE_MAP.get(var.type, ("string", {}))
        base = _PY_TYPE.get(json_type, "str")
    if var.is_list:
        base = f"list[{base}]"
    if not var.required:
        base = f"{base} | None"
    return base


def _py_field(var: Variable) -> str:
    args: list[str] = ["None"] if not var.required else ["..."]
    if var.description:
        args.append(f"description={var.description!r}")
    for key, value in (var.constraints or {}).items():
        if key in ("ge", "le", "gt", "lt", "min_length", "max_length", "pattern"):
            args.append(f"{key}={value!r}")
    return "Field(" + ", ".join(args) + ")"
