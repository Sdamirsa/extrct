"""Model registry: which (provider, model) pairs are measured against which extrct
features, and what the measurement found.

Contract: `model-registry/1.0` - additive changes keep the version; anything that
changes meaning bumps it.

An entry is a plain dict:

    {"provider": "ollama",               # provider name (see KNOWN_PROVIDERS)
     "model": "gemma4:31b-it-q4_K_M",    # exact id the provider serves, quant included
     "family": "gemma-4-31b-it",         # provider-neutral name for cross-provider joins
     "features": {                       # one row per FEATURES member, all present
         "structured_output": {"status": "pass", "date": "2026-08-20",
                               "engine": "ollama/0.32.14"},
         ...},
     "notes": "free text"}

Statuses are claims about MEASUREMENTS, not endorsements: `pass` / `partial` /
`fail` require an actual probe run (`examples/08_model_probe.py` runs the battery
and prints a paste-ready entry; `partial` carries its caveat in the row's note), and
`untested` is the honest default. A measured status MUST carry `date` (YYYY-MM-DD)
and `engine` (what served the model, with its version) - the same rule that keeps
docs/providers.md trustworthy: the measurement is encoded next to the claim it
justifies, so a stale claim is visibly stale.

`register_model()` extends the registry at runtime without core changes, mirroring
`register_provider()`. Deployment values (base URLs, keys, hostnames) never appear
here - entries name models, not hosts.
"""

from __future__ import annotations

import copy
import re
from typing import Any

MODEL_REGISTRY_VERSION = "model-registry/1.0"

# The provider slots the stack plans for. Entries may name other (custom-registered)
# providers; these five are the ones the roadmap tracks.
KNOWN_PROVIDERS = ("ollama", "vllm", "openrouter", "cerebras", "fireworks")

# The extrct features a model can be probed against. `structured_output` includes
# hierarchical schemas (objects and lists of objects via the `parent` column);
# `enum_posterior` additionally needs the provider to surface top-k alternatives.
FEATURES = ("structured_output", "certainty", "enum_posterior", "grounding", "long_text")

STATUSES = ("pass", "partial", "fail", "untested")

_MEASURED = ("pass", "partial", "fail")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ENTRY_KEYS = {"provider", "model", "family", "features", "notes"}
_FEATURE_KEYS = {"status", "date", "engine", "note"}


def _validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        msg = f"model-registry entry must be a dict, got {type(entry).__name__}"
        raise ValueError(msg)
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        msg = f"unknown entry keys {sorted(unknown)}; allowed: {sorted(_ENTRY_KEYS)}"
        raise ValueError(msg)
    for key in ("provider", "model", "family"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            msg = f"entry.{key} must be a non-empty string"
            raise ValueError(msg)
    feats = entry.get("features")
    if not isinstance(feats, dict):
        msg = "entry.features must be a dict with one row per feature"
        raise ValueError(msg)
    missing = set(FEATURES) - set(feats)
    extra = set(feats) - set(FEATURES)
    if missing or extra:
        msg = (f"entry.features must cover exactly {FEATURES}; "
               f"missing {sorted(missing)}, unknown {sorted(extra)}")
        raise ValueError(msg)
    for name, row in feats.items():
        if not isinstance(row, dict):
            msg = f"features[{name!r}] must be a dict"
            raise ValueError(msg)
        odd = set(row) - _FEATURE_KEYS
        if odd:
            msg = f"features[{name!r}] has unknown keys {sorted(odd)}"
            raise ValueError(msg)
        status = row.get("status")
        if status not in STATUSES:
            msg = f"features[{name!r}].status must be one of {STATUSES}, got {status!r}"
            raise ValueError(msg)
        if status in _MEASURED:
            date = row.get("date")
            if not isinstance(date, str) or not _DATE_RE.match(date):
                msg = (f"features[{name!r}]: measured status {status!r} requires "
                       f"date 'YYYY-MM-DD', got {date!r}")
                raise ValueError(msg)
            engine = row.get("engine")
            if not isinstance(engine, str) or not engine.strip():
                msg = (f"features[{name!r}]: measured status {status!r} requires "
                       "engine (what served the model, with version)")
                raise ValueError(msg)
    if "notes" in entry and not isinstance(entry["notes"], str):
        msg = "entry.notes must be a string"
        raise ValueError(msg)


def _untested() -> dict[str, dict[str, str]]:
    return {f: {"status": "untested"} for f in FEATURES}


# Built-in baseline: the gemma-4-31b-it family, one entry per provider that can
# serve it today. Measured rows come from examples/08_model_probe.py runs; anything
# still "untested" is exactly that.
_BUILTIN: tuple[dict[str, Any], ...] = (
    {
        "provider": "ollama",
        "model": "gemma4:31b-it-q4_K_M",
        "family": "gemma-4-31b-it",
        "features": {
            "structured_output": {"status": "pass", "date": "2026-08-21",
                                  "engine": "ollama/0.32.14"},
            "certainty": {"status": "pass", "date": "2026-08-21",
                          "engine": "ollama/0.32.14", "note": "mask_state=pre_mask"},
            "enum_posterior": {"status": "pass", "date": "2026-08-21",
                               "engine": "ollama/0.32.14"},
            "grounding": {"status": "pass", "date": "2026-08-21",
                          "engine": "ollama/0.32.14", "note": "4 exact, 0 fuzzy"},
            "long_text": {"status": "pass", "date": "2026-08-21",
                          "engine": "ollama/0.32.14", "note": "2 chunks merged"},
        },
        "notes": ("measured by examples/08_model_probe.py --long; baseline quant - "
                  "bf16/q8_0/qat tags exist for the same family"),
    },
    {
        "provider": "openrouter",
        "model": "google/gemma-4-31b-it",
        "family": "gemma-4-31b-it",
        "features": _untested(),
        "notes": "a :free variant exists; probe pending OPENROUTER_API_KEY",
    },
)

_REGISTRY: dict[tuple[str, str], dict[str, Any]] = {}
for _e in _BUILTIN:
    _validate_entry(_e)
    _REGISTRY[(_e["provider"], _e["model"])] = _e


def model_registry() -> tuple[dict[str, Any], ...]:
    """Every entry, as deep copies - mutating a result never touches the registry."""
    return tuple(copy.deepcopy(e) for e in _REGISTRY.values())


def models_for(provider: str) -> tuple[dict[str, Any], ...]:
    """Entries for one provider, deep-copied."""
    return tuple(copy.deepcopy(e) for e in _REGISTRY.values() if e["provider"] == provider)


def lookup(provider: str, model: str) -> dict[str, Any]:
    """One entry, deep-copied. Unknown (provider, model) raises KeyError - loudly,
    so a typo cannot read as 'untested'."""
    key = (provider, model)
    if key not in _REGISTRY:
        msg = f"no model-registry entry for {key!r}; registered: {sorted(_REGISTRY)}"
        raise KeyError(msg)
    return copy.deepcopy(_REGISTRY[key])


def feature_status(provider: str, model: str, feature: str) -> str:
    """The measured status for one feature of one model."""
    if feature not in FEATURES:
        msg = f"unknown feature {feature!r}; features: {FEATURES}"
        raise ValueError(msg)
    return lookup(provider, model)["features"][feature]["status"]


def register_model(entry: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    """Add an entry (validated). Duplicate (provider, model) is refused unless
    `replace=True` - overwriting a measurement should be deliberate. Returns a copy
    of what was stored."""
    _validate_entry(entry)
    key = (entry["provider"], entry["model"])
    if key in _REGISTRY and not replace:
        msg = f"{key!r} already registered; pass replace=True to overwrite"
        raise ValueError(msg)
    _REGISTRY[key] = copy.deepcopy(entry)
    return copy.deepcopy(entry)
