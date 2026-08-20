"""flow-def/1.0 — one document that configures a whole extraction flow, with rules.

The Flow Controller's model. A flow document has one SECTION per component; each section
is validated by that component's own data model, then emitted as a SPARSE namespaced
override payload — only authored keys, because these ride into the components' existing
Overrides inputs, and a filled default would stomp a widget setting the author never
mentioned. One document, one `flow_uid`, and every emitted payload carries it as
`config_uid`, so every run touched by this flow joins on one content-addressed identity.

    {"client":    {"provider": "ollama", "model": "gemma4:31b-it-q8_0", "num_ctx": 16384},
     "extract":   {"run_tags": "pilot3"},
     "grounding": {"enabled": true, "mode": "inline"},
     "certainty": {"enabled": true, "enum_posterior": true},
     "wrapper":   {"enabled": true, "parallel_calls": 4}}

Section models: `client` is client-def/1.0 (client_model — dataclass-backed, loud).
`extract` and `schema` key sets are ASSERTED at import time against config.EXTRACT_KEYS /
SCHEMA_KEYS, so this module cannot drift from what the components actually consume.
`grounding` and `certainty` are defined here and consumed by the XAI components via
config.owned_subset; `enabled=false` short-circuits an XAI node without blocking the flow.
VALUES are validated by `validate_section` — the one validator, shared by the document
lane and by every component that applies an Overrides thread (owned_subset checks key
NAMES only; a `grounding.mode='Inline'` typo used to ride through and silently disable
injection, measured 2026-08-14).

RULES is the cross-component coherence registry — a plain list, concatenated over time.
Each rule states a dependency no single component can see. The three founding rules were
all RETIRED 2026-08-14 when the router refactor made their failure states unrepresentable
(evidence injection and logprob riders moved into pipeline.compose); their ids and
lessons remain as tombstones. Rules check the DOCUMENT, not the canvas: a section left
unauthored is out of a rule's jurisdiction.
"""

from __future__ import annotations

from typing import Any

import dataclasses
import json

from . import client_model
from .config import EXTRACT_KEYS, FORBIDDEN_CLIENT_KEYS, SCHEMA_KEYS, coerce
from .hashing import content_uid
from .merging import CONFLICT_POLICIES, MERGE_DEFAULTS, SCALAR_STRATEGIES
from .repair import DEFAULT_LADDER
from .schema import ENCODINGS, STRICT_NULLABLE
from .wrapping import WRAPPER_LOGICS

FLOW_MODEL_VERSION = "flow-def/1.0"

SECTIONS = ("client", "extract", "schema", "grounding", "certainty", "wrapper", "merger")

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
}


def validate_section(name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Coerce and vocabulary-check ONE section's values. Returns a new dict; loud on garbage.

    The single validator for section VALUES, called both by parse_flow_document (the
    document lane) and by every component that applies an Overrides thread (the canvas
    lane). Added 2026-08-14 after a measured gap: `config.owned_subset` validates KEY
    NAMES only, so `grounding.mode='Inline'` or a string-typed `grounding.enabled='false'`
    rode straight into a component's emitted config — grounding then reported ON while the
    router silently skipped injection, and the failure blamed the model. S9: a typo dies
    where it is entered.
    """
    if name not in _SECTION_FIELDS:
        msg = f"unknown section {name!r}; sections with a field table: {sorted(_SECTION_FIELDS)}"
        raise ValueError(msg)
    fields = _SECTION_FIELDS[name]
    out: dict[str, Any] = {}
    for k, v in (values or {}).items():
        if name == "schema" and k == "request_evidence":
            msg = ("schema.request_evidence MOVED (2026-08-14): evidence injection now "
                   "happens at request time inside Run - Structured Extract, driven by the "
                   "grounding config. Author grounding.enabled=true (mode inline/auto) "
                   "instead and delete this key.")
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


def parse_flow_document(raw: dict[str, Any]) -> dict[str, Any]:
    """Any authored shape -> normalized `{version, sections, flow_uid}`.

    Tolerated shorthands: a bare client document (has `provider`, no section keys) and a
    Registry load_client row (has `definition`) both become `{"client": ...}` — so a
    stored client wires straight into the controller. Loud on: unknown section, unknown
    key (with the allowed list), type garbage, vocabulary violations."""
    if not isinstance(raw, dict) or not raw:
        msg = 'empty flow document; expected {"provider": {...}, "extract": {...}, ...}'
        raise ValueError(msg)
    if isinstance(raw.get("provider"), dict):
        # "provider" is the PREFERRED authored spelling since 2026-08-14 (user call:
        # "client" was not understandable on the canvas). Internally the section stays
        # "client" so every stored document and its flow_uid remain valid.
        if "client" in raw:
            msg = 'both "provider" and "client" sections authored — they are the same section; use one'
            raise ValueError(msg)
        raw = {("client" if k == "provider" else k): v for k, v in raw.items()}
    if "definition" in raw or ("provider" in raw and not (set(raw) & set(SECTIONS))):
        provider, settings = client_model.parse_document(raw)
        raw = {"client": {"provider": provider, **settings}}

    sections: dict[str, dict[str, Any]] = {}
    for key, val in raw.items():
        if key in ("version", "flow_uid"):
            continue
        if key not in SECTIONS:
            msg = f"unknown section {key!r}; sections: {SECTIONS}"
            raise ValueError(msg)
        if not isinstance(val, dict):
            msg = f"section {key!r} must be an object, got {val!r}"
            raise ValueError(msg)
        if key == "client":
            provider = str(val.get("provider") or "")
            checked = client_model.validate_client_settings(
                provider, {k: v for k, v in val.items() if k != "provider"})
            sections["client"] = {"provider": provider, **checked}
            continue
        sections[key] = validate_section(key, val)
    if not sections:
        msg = "flow document has no sections"
        raise ValueError(msg)

    doc = {"version": FLOW_MODEL_VERSION, "sections": sections}
    doc["flow_uid"] = content_uid(doc)
    return doc


# --- the rules registry ---------------------------------------------------------------
# Concatenate over time; never delete a rule that caught a real failure. Each rule:
# {"id", "description", "check": sections -> violation message | None}. A rule only
# speaks when the sections it needs are authored — the document is its jurisdiction.
#
# RETIRED 2026-08-14 (all three founding rules), NOT deleted: the router refactor made
# every one of them unrepresentable. R1: schema.request_evidence no longer exists —
# pipeline.compose injects the evidence mirror at request time whenever grounding is on
# (an authored request_evidence key raises with a migration message at parse). R2/R3:
# certainty carries its own request-time riders (logprobs / top_logprobs, upgrade-only
# merge into the client spec), so an under-provisioned client can no longer occur; an
# active check here would FALSE-POSITIVE on router flows the riders already fix. A
# retired rule's check returns None unconditionally; the id and the lesson stay.

def _retired(_sections: dict) -> None:
    return None


RULES: list[dict[str, Any]] = [
    {"id": "R1-inline-grounding-needs-inline-evidence", "check": _retired, "retired": "2026-08-14",
     "description": ("RETIRED: inline grounding needed schema.request_evidence (the Test-04 "
                     "evidence-less-payload failure). Now unrepresentable — the router injects "
                     "evidence at request time whenever the grounding config is on.")},
    {"id": "R2-enum-posterior-needs-top-logprobs", "check": _retired, "retired": "2026-08-14",
     "description": ("RETIRED: the enum posterior needed client top_logprobs >= 1. Now "
                     "unrepresentable — certainty.top_logprobs rides as an upgrade-only "
                     "request rider (pipeline.compose).")},
    {"id": "R3-certainty-needs-logprobs", "check": _retired, "retired": "2026-08-14",
     "description": ("RETIRED: certainty needed client logprobs=true. Now unrepresentable — "
                     "certainty-enabled sets the logprobs rider (pipeline.compose).")},
]


def validate_rules(sections: dict[str, Any]) -> list[str]:
    """Every violated rule as '[id] message'. Empty list = coherent document."""
    out = []
    for rule in RULES:
        msg = rule["check"](sections)
        if msg:
            out.append(f"[{rule['id']}] {msg}")
    return out


def section_config(doc: dict[str, Any], name: str) -> dict[str, Any]:
    """One component's override payload: sparse namespaced keys + config_uid=flow_uid.

    The client section namespaces under its PROVIDER (`ollama.model`, ...), matching
    apply_client_overrides; other sections namespace under their own name. An unauthored
    section emits only the config_uid — downstream Overrides apply nothing and the
    component keeps its widget settings."""
    if name not in SECTIONS:
        msg = f"unknown section {name!r}; sections: {SECTIONS}"
        raise ValueError(msg)
    sec = (doc.get("sections") or {}).get(name)
    out: dict[str, Any] = {}
    if sec:
        if name == "client":
            provider = sec["provider"]
            out = {f"{provider}.{k}": v for k, v in sec.items() if k != "provider"}
        else:
            out = {f"{name}.{k}": v for k, v in sec.items()}
    out["config_uid"] = doc["flow_uid"]
    return out


# --- the variable catalog -------------------------------------------------------------
# Every controllable variable across every data model, generated FROM the models at
# import time — never a parallel list. Feeds the default tables of Flow - Config and
# Flow - Sweep ("maximum variables", user decision 2026-08-11): a fully populated table
# is AUTHORITATIVE for every row it keeps; deleting a row releases that key back to the
# widget. In the sweep table every value ships as a one-candidate JSON array — turn any
# into a real list to sweep it; single candidates never multiply the grid.

_SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "extract": {"run_tags": "", "ladder": list(DEFAULT_LADDER), "max_reprompts": 1,
                "max_retries": 2, "log_to_db": True, "store_input_text": False,
                "repair_strategy": "patch_only"},
    "schema": {"encoding": STRICT_NULLABLE,
               "root_name": "extract", "additional_properties": False},
    "grounding": {"enabled": True, "mode": "auto", "fuzzy_threshold": 0.75, "log_to_db": True},
    "certainty": {"enabled": True, "enum_posterior": True, "top_logprobs": 3, "log_to_db": True},
    "wrapper": {"enabled": False, "logic": "paragraph_pack", "max_chars": 4000,
                "overlap_chars": 400, "parallel_calls": 4,
                "store_text": False, "log_to_db": True},
    "merger": {**MERGE_DEFAULTS, "log_to_db": True},
}
for _s, _d in _SECTION_DEFAULTS.items():  # import-time drift guard, same spirit as above
    assert set(_d) == set(_SECTION_FIELDS[_s]), f"{_s} defaults drifted from its field table"

# Measured lessons and examples, surfaced as the help column. Vocab fields get their
# closed list automatically; a hint here is appended (or stands alone for open fields).
_HINTS: dict[str, str] = {
    "ollama.model": "the tag as pulled - e.g. gemma4:31b-it-q8_0, qwen3.5:7b, llama3.3:70b",
    "ollama.base_url": "e.g. http://your-gpu-host:11434 (GPU host) | http://host.docker.internal:11434 (laptop)",
    "ollama.mode": "json_schema enforcement is per MODEL FAMILY (gemma4 honors; qwen3.5/3.6 silently ignore) - probe first",
    "ollama.num_ctx": "context window; too small SILENTLY truncates input - e.g. 4096, 8192, 16384, 32768",
    "ollama.num_predict": "max generated tokens; truncation = failed extraction - e.g. 512, 1024, 4096",
    "ollama.num_gpu": "-1 auto (recommended); 0 forces CPU (never send 0 to the GPU host); N = layer count",
    "ollama.think": "false | true | low | medium | high (model-dependent; false for extraction)",
    "ollama.keep_alive": "how long the model stays loaded - e.g. 5m, 30m, 24h",
    "ollama.top_logprobs": "0 = off; 3+ required for enum-posterior certainty",
    "openrouter.model": "EXACT slug incl. any :free suffix - e.g. google/gemma-4-26b-a4b-it:free",
    "openrouter.endpoint_tag": "full tag from the client's Endpoints output; pin it for reproducibility",
    "openrouter.top_logprobs": "0 = off; 3+ required for enum-posterior certainty",
    "openrouter.max_output_tokens": "e.g. 1024, 2048, 8192",
    "extract.run_tags": "comma-separated labels - e.g. pilot3,longtext,test-06",
    "extract.ladder": 'deterministic repair rungs, in order - e.g. ["json_repair", "coerce"]',
    "schema.root_name": "the wrapper object's name in the schema - e.g. extract, note, report",
    "grounding.fuzzy_threshold": ("0.5-1.0; 0.75 is the benchmarked default; 1.0 demands that EVERY "
                                  "quoted token be found in order (gaps still allowed), not an exact match"),
    "certainty.top_logprobs": "request-time RIDER: the router upgrades the client to at least this (never downgrades); 3 is the enum-posterior floor",
    "wrapper.enabled": "true switches the router to the chunked lane (wrap -> N requests -> merge)",
    "wrapper.parallel_calls": "in-flight bound across chunks - measured sane defaults: 4 ollama, 8 openrouter",
    "wrapper.max_chars": "chunk size - e.g. 2000, 4000, 8000 (model context minus schema)",
    "wrapper.overlap_chars": "context carried across cuts - roughly 10% of max_chars",
    "merger.list_key": "blocking key for object lists - e.g. name, code; empty = composite similarity",
    "merger.list_similarity": "0.5-1.0 clustering threshold - e.g. 0.8, 0.85, 0.9 (tune on labeled pairs)",
    "merger.numeric_tolerance": "relative: 0.01 groups 55 with 55.2 and splits 55 from 45",
}


def _catalog_type(ann: str) -> str:
    base = ann.split("|")[0].strip()
    if base.startswith(("tuple", "dict", "list")):
        return "json"
    return base if base in ("str", "int", "float", "bool") else "str"


def _catalog_entries() -> list[tuple[str, str, Any]]:
    """(namespaced name, table type, python default) for every controllable variable."""
    out: list[tuple[str, str, Any]] = []
    for provider in ("ollama", "openrouter"):
        for f in dataclasses.fields(client_model._SPECS[provider]):  # noqa: SLF001 - same package
            if f.name in FORBIDDEN_CLIENT_KEYS:
                continue
            default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
            default = list(default) if isinstance(default, tuple) else default
            out.append((f"{provider}.{f.name}", _catalog_type(str(f.type)), default))
    for section in ("extract", "schema", "grounding", "certainty", "wrapper", "merger"):
        for key, type_ in _SECTION_FIELDS[section].items():
            out.append((f"{section}.{key}", type_, _SECTION_DEFAULTS[section][key]))
    return out


def _catalog_help(full: str, type_: str, default: Any) -> str:
    provider, _, key = full.partition(".")
    vocab = (client_model.ALLOWED_VALUES.get(provider, {}).get(key)
             or _SECTION_VOCAB.get((provider, key)))
    parts = []
    if vocab:
        parts.append("one of: " + " | ".join(str(v) for v in vocab))
    if full in _HINTS:
        parts.append(_HINTS[full])
    if not parts:
        shown = json.dumps(default, ensure_ascii=False)
        parts.append(f"{type_}; default {shown}")
    return " - ".join(parts)


def _stringify(type_: str, default: Any) -> str:
    if type_ == "json":
        return json.dumps(default, ensure_ascii=False)
    if type_ == "bool":
        return "true" if default else "false"
    return "" if default is None else str(default)


def variable_catalog() -> list[dict[str, str]]:
    """The Flow - Config default table: name, type, value (default), help."""
    return [{"name": n, "type": t, "value": _stringify(t, d),
             "help": _catalog_help(n, t, d)}
            for n, t, d in _catalog_entries()]


def sweep_catalog() -> list[dict[str, str]]:
    """The Flow - Sweep default table: every value a ONE-candidate JSON array — extend
    any array to sweep that variable; single candidates keep the grid at one cell."""
    return [{"name": n, "type": t, "values": json.dumps([d], ensure_ascii=False),
             "help": _catalog_help(n, t, d)}
            for n, t, d in _catalog_entries()]
