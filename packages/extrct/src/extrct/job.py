"""job-def/1.0 - one document that configures a whole extraction job, with rules.

The YAML face of the library. A job document has one SECTION per concern; each section
is validated by the same machinery the Python API uses, then kept SPARSE - only
authored keys, so defaults stay visible as defaults and the document reads as intent:

    provider:
      provider: ollama
      model: qwen3:4b-instruct
      num_ctx: 8192
    schema:
      root_name: echo_report
      variables:
        - {name: lvef, type: float, description: "LVEF in percent", constraints: {ge: 0, le: 100}}
        - {name: mitral_regurgitation, type: str, options: ["none", "mild", "moderate", "severe"]}
    extract:   {ladder: ["json_repair", "coerce"], run_tags: "pilot"}
    grounding: {enabled: true}
    certainty: {enabled: true}
    storage:   {backend: sqlite, path: runs.sqlite}

Identity: `job_uid` hashes the CONTENT sections (client definition, schema envelope,
extract/grounding/certainty/wrapper/merger). The `storage` section is deployment -
where the log lands does not change what the job is - so it is deliberately excluded,
the same rule that keeps gateway URLs out of client_uid.

Validation is loud everywhere: unknown sections, unknown keys (with the allowed list),
type garbage, and closed-vocabulary violations all raise at load time. A typo dies
where it is entered, never as a silently skipped pipeline step. Values are checked, not
just key names - a measured lesson: `grounding.mode: Inline` (capital I) once rode
through name-only checking, grounding reported ON, the pipeline silently skipped
injection, and the failure blamed the model.
"""

from __future__ import annotations

from typing import Any

from . import client_model
from .config import EXTRACT_KEYS, SCHEMA_KEYS, coerce
from .hashing import content_uid
from .merging import CONFLICT_POLICIES, SCALAR_STRATEGIES
from .schema import ENCODINGS, Variable, schema_envelope
from .wrapping import WRAPPER_LOGICS

JOB_MODEL_VERSION = "job-def/1.0"

SECTIONS = ("client", "schema", "extract", "grounding", "certainty", "wrapper", "merger", "storage")

_SECTION_FIELDS: dict[str, dict[str, str]] = {
    "extract": {"run_tags": "str", "ladder": "json", "max_reprompts": "int",
                "max_retries": "int", "log_to_db": "bool", "store_input_text": "bool",
                "repair_strategy": "str"},
    "schema": {"encoding": "str", "root_name": "str",
               "additional_properties": "bool"},
    "grounding": {"enabled": "bool", "mode": "str", "fuzzy_threshold": "float",
                  "log_to_db": "bool"},
    "certainty": {"enabled": "bool", "enum_posterior": "bool", "top_logprobs": "int",
                  "log_to_db": "bool"},
    "wrapper": {"enabled": "bool", "logic": "str", "max_chars": "int",
                "overlap_chars": "int", "parallel_calls": "int",
                "store_text": "bool", "log_to_db": "bool"},
    "merger": {"scalar_strategy": "str", "conflict_policy": "str",
               "numeric_tolerance": "float", "list_similarity": "float",
               "list_key": "str", "log_to_db": "bool"},
    "storage": {"backend": "str", "path": "str", "dsn": "str", "dsn_env": "str"},
}

# Import-time drift guards: if config.py gains or loses an override key, this module
# refuses to import until the field table above is updated to match.
assert set(_SECTION_FIELDS["extract"]) == EXTRACT_KEYS, "extract section drifted from config.EXTRACT_KEYS"
assert set(_SECTION_FIELDS["schema"]) == SCHEMA_KEYS, "schema section drifted from config.SCHEMA_KEYS"

GROUNDING_KEYS = set(_SECTION_FIELDS["grounding"])
CERTAINTY_KEYS = set(_SECTION_FIELDS["certainty"])
WRAPPER_KEYS = set(_SECTION_FIELDS["wrapper"])
MERGER_KEYS = set(_SECTION_FIELDS["merger"])

_SECTION_VOCAB: dict[tuple[str, str], tuple] = {
    ("schema", "encoding"): tuple(ENCODINGS),
    ("grounding", "mode"): ("inline", "posthoc", "auto"),
    ("wrapper", "logic"): tuple(WRAPPER_LOGICS),
    ("merger", "scalar_strategy"): tuple(SCALAR_STRATEGIES),
    ("merger", "conflict_policy"): tuple(CONFLICT_POLICIES),
    ("storage", "backend"): ("none", "sqlite", "postgres"),
}


def validate_section(name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Coerce and vocabulary-check ONE section's values. Returns a new dict; loud on
    garbage. The single validator for section VALUES - the YAML lane and any override
    lane both come through here, so a typo cannot survive in either."""
    if name not in _SECTION_FIELDS:
        msg = f"unknown section {name!r}; sections with a field table: {sorted(_SECTION_FIELDS)}"
        raise ValueError(msg)
    fields = _SECTION_FIELDS[name]
    out: dict[str, Any] = {}
    for k, v in (values or {}).items():
        if name == "schema" and k == "request_evidence":
            msg = ("schema.request_evidence is not a schema setting: evidence injection "
                   "is driven by the grounding section (grounding.enabled with mode "
                   "inline/auto) and happens at request time inside the pipeline. "
                   "Author the grounding section instead and delete this key.")
            raise ValueError(msg)
        if k not in fields:
            msg = f"{name}.{k}: not a {name} setting. Allowed: {sorted(fields)}"
            raise ValueError(msg)
        try:
            cv = coerce(v, fields[k])
        except ValueError as exc:
            msg = f"{name}.{k}: {exc} (declared type {fields[k]})"
            raise ValueError(msg) from exc
        vocab = _SECTION_VOCAB.get((name, k))
        if vocab and cv not in vocab:
            msg = f"{name}.{k}: {cv!r} is not one of {vocab}"
            raise ValueError(msg)
        out[k] = cv
    if name == "grounding" and "fuzzy_threshold" in out and not 0.5 <= out["fuzzy_threshold"] <= 1.0:
        msg = f"grounding.fuzzy_threshold must be within [0.5, 1.0], got {out['fuzzy_threshold']}"
        raise ValueError(msg)
    if name == "merger" and "list_similarity" in out and not 0.5 <= out["list_similarity"] <= 1.0:
        msg = f"merger.list_similarity must be within [0.5, 1.0], got {out['list_similarity']}"
        raise ValueError(msg)
    return out


def parse_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Any authored job shape -> normalized job document.

    Returns {version, client (client-def document), schema (schema envelope), the other
    authored sections (sparse), storage, job_uid}. The client section may be authored
    under the friendlier top-level key `provider` - internally it stays `client` so
    documents and uids are stable.
    """
    if not isinstance(raw, dict) or not raw:
        msg = 'empty job document; expected {"provider": {...}, "schema": {...}, ...}'
        raise ValueError(msg)
    raw = dict(raw)
    if isinstance(raw.get("provider"), dict):
        if "client" in raw:
            msg = 'both "provider" and "client" sections authored - they are the same section; use one'
            raise ValueError(msg)
        raw["client"] = raw.pop("provider")

    unknown = [k for k in raw if k not in SECTIONS and k not in ("version", "job_uid")]
    if unknown:
        msg = f"unknown section(s) {unknown}; sections: {SECTIONS} (the client section may be spelled 'provider')"
        raise ValueError(msg)

    # --- client: validated against the real spec via the provider registry ---
    if not isinstance(raw.get("client"), dict):
        msg = 'a job needs a client section: provider: {provider: "ollama" | "openrouter" | ..., <settings>}'
        raise ValueError(msg)
    provider, settings = client_model.parse_document(raw["client"])
    client_doc = client_model.client_definition(provider, settings)

    # --- schema: variables + settings -> one envelope, one source of truth ---
    schema_section = raw.get("schema")
    if not isinstance(schema_section, dict) or not schema_section.get("variables"):
        msg = "a job needs schema.variables: the list of variables to extract"
        raise ValueError(msg)
    variables = schema_section["variables"]
    if not isinstance(variables, list):
        msg = f"schema.variables must be a list of variable rows, got {type(variables).__name__}"
        raise ValueError(msg)
    schema_settings = validate_section(
        "schema", {k: v for k, v in schema_section.items() if k != "variables"})
    rows = [v if isinstance(v, Variable) else Variable.from_row(v) for v in variables]
    envelope = schema_envelope(
        rows,
        root_name=schema_settings.get("root_name", "extract"),
        encoding=schema_settings.get("encoding", "strict_nullable"),
        additional_properties=bool(schema_settings.get("additional_properties", False)),
    )

    doc: dict[str, Any] = {"version": JOB_MODEL_VERSION, "client": client_doc, "schema": envelope}
    for name in ("extract", "grounding", "certainty", "wrapper", "merger", "storage"):
        if name in raw:
            if not isinstance(raw[name], dict):
                msg = f"section {name!r} must be an object, got {raw[name]!r}"
                raise ValueError(msg)
            doc[name] = validate_section(name, raw[name])

    # storage is deployment, not content: same rule that keeps gateway URLs out of client_uid
    doc["job_uid"] = content_uid({
        "version": JOB_MODEL_VERSION,
        "client_uid": client_doc["client_uid"],
        "schema_uid": envelope["schema_uid"],
        "sections": {k: doc[k] for k in ("extract", "grounding", "certainty", "wrapper", "merger")
                     if k in doc},
    })
    return doc


def load_job(path) -> dict[str, Any]:
    """Read a YAML (or JSON - valid YAML) job file and normalize it. The recommended
    pattern: configs live in versioned YAML files, code points at a file. Accepts a
    str or any PathLike; always read as UTF-8 regardless of platform locale."""
    import yaml

    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        msg = f"{path}: expected a mapping at the top level, got {type(raw).__name__}"
        raise ValueError(msg)
    return parse_job(raw)
