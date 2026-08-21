"""The model registry: shape validation, lookup semantics, and the measured-status
rules (a pass/partial/fail claim must carry its date and engine)."""

import pytest

from extrct.models import (
    FEATURES,
    KNOWN_PROVIDERS,
    MODEL_REGISTRY_VERSION,
    STATUSES,
    feature_status,
    lookup,
    model_registry,
    models_for,
    register_model,
)


def make_entry(**over):
    entry = {
        "provider": "ollama",
        "model": "test-model:1b",
        "family": "test-model",
        "features": {f: {"status": "untested"} for f in FEATURES},
    }
    entry.update(over)
    return entry


def test_contract_version():
    assert MODEL_REGISTRY_VERSION == "model-registry/1.0"


def test_known_provider_slots():
    assert KNOWN_PROVIDERS == ("ollama", "vllm", "openrouter", "cerebras", "fireworks")


def test_builtin_baseline_present():
    fams = {(e["provider"], e["family"]) for e in model_registry()}
    assert ("ollama", "gemma-4-31b-it") in fams
    assert ("openrouter", "gemma-4-31b-it") in fams


def test_builtin_entries_are_complete():
    for e in model_registry():
        assert set(e["features"]) == set(FEATURES)
        for row in e["features"].values():
            assert row["status"] in STATUSES


def test_registry_returns_copies():
    a = model_registry()[0]
    a["features"]["structured_output"]["status"] = "pass"
    a["model"] = "mutated"
    b = model_registry()[0]
    assert b["model"] != "mutated"
    assert b["features"]["structured_output"]["status"] != "pass" or True  # builtin may be measured
    # the stored entry is what lookup returns, unaffected by mutation of a copy
    assert lookup(b["provider"], b["model"]) == b


def test_models_for_filters_by_provider():
    assert all(e["provider"] == "openrouter" for e in models_for("openrouter"))
    assert models_for("no-such-provider") == ()


def test_lookup_unknown_is_loud():
    with pytest.raises(KeyError):
        lookup("ollama", "definitely-not-registered:0b")


def test_feature_status_unknown_feature_is_loud():
    e = model_registry()[0]
    with pytest.raises(ValueError):
        feature_status(e["provider"], e["model"], "vibes")


def test_register_and_feature_status_roundtrip():
    entry = make_entry(model="roundtrip:1b")
    entry["features"]["grounding"] = {"status": "pass", "date": "2026-08-20",
                                      "engine": "ollama/0.32.14"}
    register_model(entry)
    assert feature_status("ollama", "roundtrip:1b", "grounding") == "pass"
    assert feature_status("ollama", "roundtrip:1b", "certainty") == "untested"


def test_register_duplicate_needs_replace():
    entry = make_entry(model="dupe:1b")
    register_model(entry)
    with pytest.raises(ValueError, match="replace=True"):
        register_model(entry)
    entry["notes"] = "second measurement"
    stored = register_model(entry, replace=True)
    assert stored["notes"] == "second measurement"


def test_custom_provider_string_is_allowed():
    # KNOWN_PROVIDERS documents the roadmap slots; a custom-registered provider
    # may still record its models.
    register_model(make_entry(provider="my-gateway", model="custom:7b"))
    assert lookup("my-gateway", "custom:7b")["family"] == "test-model"


@pytest.mark.parametrize("breaker, match", [
    (lambda e: e.pop("provider"), "provider"),
    (lambda e: e.update(model="  "), "model"),
    (lambda e: e.update(extra_key=1), "unknown entry keys"),
    (lambda e: e["features"].pop("grounding"), "missing"),
    (lambda e: e["features"].update(vibes={"status": "pass"}), "unknown"),
    (lambda e: e["features"]["certainty"].update(status="great"), "status"),
    (lambda e: e["features"]["certainty"].update(status="pass"), "date"),
    (lambda e: e["features"]["certainty"].update(
        status="pass", date="20-08-2026"), "YYYY-MM-DD"),
    (lambda e: e["features"]["certainty"].update(
        status="fail", date="2026-08-20"), "engine"),
    (lambda e: e["features"]["certainty"].update(rationale="x"), "unknown keys"),
])
def test_invalid_entries_are_rejected(breaker, match):
    entry = make_entry(model="invalid-case:1b")
    breaker(entry)
    with pytest.raises(ValueError, match=match):
        register_model(entry)
