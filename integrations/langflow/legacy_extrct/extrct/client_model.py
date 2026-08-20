"""Client definitions as data — authored, validated, content-addressed (client-def/1.0).

The extraction-variable model lets a table (or an agent) define WHAT to extract; this
module is its sibling for HOW to call a model. A client definition is a plain JSON
document — something an agent can author without ever touching a canvas widget:

    {"provider": "ollama", "settings": {"model": "gemma4:31b-it-q8_0", "num_ctx": 16384}}

`client_definition()` validates it against the REAL spec dataclasses (single source of
truth — no parallel field list to drift), fills defaults, and stamps a content uid.
`build_client_payload()` then materialises the exact payload the Client components emit
and Run - Structured Extract already consumes — nothing downstream changes.

Identity, three layers, deliberately distinct:
- client_uid  names the DEFINITION   (this module; machine-independent by construction)
- config_uid  names a config/sweep cell applied on top   (config.py)
- run_uid     names one call                              (storage / request record)

Deployment never leaks into identity: OpenRouter egress fields left at their
defaults are resolved from EXTRCT_GATEWAY_URL at payload-build time — mirroring the
Client component (`gateway set -> gateway route; empty -> explicit direct`) — AFTER the
uid is stamped. The same definition JSON yields the same client_uid on any machine; the
resolved route rides the run row. Explicitly authored egress settings are respected and
DO shape the uid (authoring intent is content). Enforcement stays where it always was:
`check_egress` runs inside `build_client_payload`, so a definition missing
`data_classification` fails loudly there, with the  message, before any use.

Credentials cannot ride a definition: `api_key` and `capability` raise on sight
(FORBIDDEN_CLIENT_KEYS), and neither appears in the normalized document. A client built
from a definition therefore carries `capability={}` — pin `endpoint_tag` explicitly and
keep `require_parameters=True` (the measured loud-refusal gate) when authoring for
OpenRouter; capability enrichment remains a Client-component runtime feature.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

from .config import FORBIDDEN_CLIENT_KEYS
from .hashing import content_uid
from .ollama import OllamaSpec
from .openrouter import ALWAYS, AUTO, DATA_CLASSES, NEVER, OpenRouterSpec, check_egress

CLIENT_MODEL_VERSION = "client-def/1.0"

_SPECS = {"ollama": OllamaSpec, "openrouter": OpenRouterSpec}
PROVIDERS = tuple(_SPECS)

# Payload settings that are real but live OUTSIDE the spec dataclass (envelope keys).
ENVELOPE_FIELDS: dict[str, dict[str, str]] = {
    "ollama": {"api_path": "/api/chat"},  # /v1 has three verified silent-disable shapes
    "openrouter": {},
}

# Egress trio: resolved from the environment at payload-build time IF AND ONLY IF all
# three still sit at their dataclass defaults (exactly the state check_egress would
# refuse). Any explicit authoring wins and is never touched.
_DEPLOYMENT_DEFAULTS = {"egress_route": "gateway", "gateway_url": "", "allow_direct_egress": False}

# Closed vocabularies, from the measured spec contracts. A wrong member raises at
# definition time instead of surfacing as a provider 400 (or a silent reroute).
# data_classification: membership checked here when SET; emptiness stays check_egress's
# call so policy lives in exactly one place.
ALLOWED_VALUES: dict[str, dict[str, tuple]] = {
    "ollama": {
        "mode": ("json_schema", "json_object", "prompted"),
    },
    "openrouter": {
        "mode": ("json_schema", "json_object", "prompted"),
        "egress_route": ("gateway", "direct"),
        "data_classification": tuple(DATA_CLASSES),
        "send_seed": (AUTO, ALWAYS, NEVER),
        "send_temperature": (AUTO, ALWAYS, NEVER),
        "send_top_p": (AUTO, ALWAYS, NEVER),
        "max_output_field": ("auto", "max_tokens", "max_completion_tokens"),
        "reasoning_control": ("off", "effort", "budget"),
    },
}

_BASE_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "dict": dict}


def _annotation(provider: str, key: str) -> str:
    for f in dataclasses.fields(_SPECS[provider]):
        if f.name == key:
            return str(f.type)
    return "str"  # envelope keys


def _check_value(provider: str, key: str, value: Any) -> Any:
    """Type-check one setting against the spec dataclass annotation. JSON-native in,
    normalized out (lists stay lists — the document is pure JSON; spec construction
    converts). Strings are coerced for numeric/bool fields (table-entered values);
    a number where a string belongs raises — that is an authoring mistake, not a format.
    """
    ann = _annotation(provider, key)
    optional = "None" in ann
    if value is None:
        if optional:
            return None
        raise ValueError(f"{provider}.{key}: null is not allowed (field is {ann})")
    base = ann.split("|")[0].strip()

    if base.startswith("tuple"):
        if isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value):
            return list(value)
        raise ValueError(f"{provider}.{key}: expected a list of strings, got {value!r}")
    py = _BASE_TYPES.get(base)
    if py is None:
        return value  # unknown annotation: pass through rather than invent policy
    if py is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return value.strip().lower() in ("true", "1", "yes", "on")
        raise ValueError(f"{provider}.{key}: expected true/false, got {value!r}")
    if py is int:
        if isinstance(value, bool):
            raise ValueError(f"{provider}.{key}: expected an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value.strip())
        raise ValueError(f"{provider}.{key}: expected an integer, got {value!r}")
    if py is float:
        if isinstance(value, bool):
            raise ValueError(f"{provider}.{key}: expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        raise ValueError(f"{provider}.{key}: expected a number, got {value!r}")
    if py is dict:
        if isinstance(value, dict):
            return value
        raise ValueError(f"{provider}.{key}: expected an object, got {value!r}")
    # str
    if isinstance(value, str):
        return value
    raise ValueError(f"{provider}.{key}: expected a string, got {value!r} "
                     f"(numbers are not silently stringified)")


def parse_document(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Any accepted authoring shape -> (provider, settings). Three shapes:

    canonical      {"provider": ..., "settings": {...}}
    flat shorthand {"provider": ..., <setting keys>}          # agent-friendly
    registry row   {"name": ..., "definition": <doc|JSON text>}  # the load_client lane

    The row shape recurses into its stored document, so a Registry load_client result
    wires straight into anything that calls this. Loud on nothing usable."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError('empty document; expected {"provider": ..., "settings": {...}}')
    inner = raw.get("definition")
    if inner is not None:
        if isinstance(inner, str):
            inner = json.loads(inner)
        return parse_document(inner)
    provider = str(raw.get("provider") or "")
    if isinstance(raw.get("settings"), dict):
        return provider, dict(raw["settings"])
    settings = {k: v for k, v in raw.items()
                if k not in ("provider", "version", "client_uid", "settings")}
    return provider, settings


def validate_client_settings(provider: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate authored settings. Loud on: unknown provider, unknown key (with the
    allowed list), forbidden key, type mismatch, closed-vocabulary violation."""
    if provider not in _SPECS:
        raise ValueError(f"unknown provider {provider!r}; known: {PROVIDERS}")
    fields = {f.name for f in dataclasses.fields(_SPECS[provider])} - FORBIDDEN_CLIENT_KEYS
    envelope = ENVELOPE_FIELDS[provider]
    vocab = ALLOWED_VALUES[provider]
    out: dict[str, Any] = {}
    for key, value in (settings or {}).items():
        if key in FORBIDDEN_CLIENT_KEYS:
            raise ValueError(f"{provider}.{key}: never part of a definition "
                             f"(credentials are resolved at send time; capability is fetched, not authored)")
        if key not in fields and key not in envelope:
            raise ValueError(f"{provider}.{key}: not a field of the {provider} client. "
                             f"Allowed: {sorted(fields | set(envelope))}")
        v = _check_value(provider, key, value)
        if key in vocab and v != "" and v not in vocab[key]:
            raise ValueError(f"{provider}.{key}: {v!r} is not one of {vocab[key]}")
        out[key] = v
    return out


def client_definition(provider: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Authored settings -> normalized, defaults-filled, content-addressed document.

    Idempotent: feeding a document's own provider+settings back yields the same
    client_uid. The document is pure JSON (tuples stored as lists)."""
    authored = validate_client_settings(provider, settings or {})
    full: dict[str, Any] = {}
    for f in dataclasses.fields(_SPECS[provider]):
        if f.name in FORBIDDEN_CLIENT_KEYS:
            continue  # never in the document, not even as defaults
        default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
        full[f.name] = list(default) if isinstance(default, tuple) else default
    full.update(ENVELOPE_FIELDS[provider])
    full.update(authored)
    doc = {"version": CLIENT_MODEL_VERSION, "provider": provider, "settings": full}
    doc["client_uid"] = content_uid(doc)
    return doc


def spec_from_definition(defn: dict[str, Any], *, resolve_env: bool = True):
    """Definition document -> (frozen spec instance, provider). Env resolution per the
    module docstring; `resolve_env=False` reproduces the document verbatim."""
    provider = defn.get("provider")
    if provider not in _SPECS:
        raise ValueError(f"definition has unknown provider {provider!r}; known: {PROVIDERS}")
    settings = dict(defn.get("settings") or {})
    for key in ENVELOPE_FIELDS[provider]:
        settings.pop(key, None)
    if provider == "openrouter":
        if isinstance(settings.get("quantizations"), list):
            settings["quantizations"] = tuple(settings["quantizations"])
        untouched = all(settings.get(k, d) == d for k, d in _DEPLOYMENT_DEFAULTS.items())
        if resolve_env and untouched:
            gateway = (os.environ.get("EXTRCT_GATEWAY_URL") or os.environ.get("EXTRCT_GATEWAY_URL", "")).strip()
            settings["egress_route"] = "gateway" if gateway else "direct"
            settings["allow_direct_egress"] = not gateway
            settings["gateway_url"] = gateway
    return _SPECS[provider](**settings), provider


def build_client_payload(defn: dict[str, Any], *, resolve_env: bool = True) -> dict[str, Any]:
    """Definition -> the exact Client payload the extract consumes. For OpenRouter,
    check_egress runs here — the same gate, the same wording, just earlier."""
    spec, provider = spec_from_definition(defn, resolve_env=resolve_env)
    if provider == "openrouter":
        check_egress(spec)
    payload: dict[str, Any] = {"provider": provider, "spec": dict(spec.__dict__),
                               "client_uid": defn.get("client_uid", "")}
    settings = defn.get("settings") or {}
    for key, default in ENVELOPE_FIELDS[provider].items():
        payload[key] = settings.get(key, default)
    return payload


def definition_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reverse lane: capture a live Client payload as a definition document — read the
    current client, mutate, re-emit. Forbidden keys are dropped, not carried."""
    provider = payload.get("provider")
    if provider not in _SPECS:
        raise ValueError(f"payload has unknown provider {provider!r}; known: {PROVIDERS}")
    settings = {k: v for k, v in dict(payload.get("spec") or {}).items()
                if k not in FORBIDDEN_CLIENT_KEYS}
    for key in ENVELOPE_FIELDS[provider]:
        if key in payload:
            settings[key] = payload[key]
    return client_definition(provider, settings)
