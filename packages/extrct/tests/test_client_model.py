import pytest

from extrct.client_model import (
    build_client_payload,
    client_definition,
    definition_from_payload,
    parse_document,
    spec_from_definition,
)
from extrct.providers.openrouter import EgressRefused


def test_definition_fills_defaults_and_stamps_uid():
    doc = client_definition("ollama", {"model": "m", "num_ctx": 16384})
    assert doc["version"] == "client-def/1.0"
    assert doc["settings"]["num_ctx"] == 16384
    assert doc["settings"]["temperature"] == 0.0  # default filled and visible
    assert doc["settings"]["api_path"] == "/api/chat"  # envelope field present
    assert len(doc["client_uid"]) == 16


def test_definition_is_idempotent():
    a = client_definition("ollama", {"model": "m"})
    b = client_definition(a["provider"], a["settings"])
    assert a["client_uid"] == b["client_uid"]


def test_same_definition_same_uid_any_machine(monkeypatch):
    # deployment resolution happens AFTER stamping: env must not change identity
    monkeypatch.delenv("EXTRCT_GATEWAY_URL", raising=False)
    a = client_definition("openrouter", {"model": "m", "data_classification": "synthetic"})
    monkeypatch.setenv("EXTRCT_GATEWAY_URL", "http://gw:9000")
    b = client_definition("openrouter", {"model": "m", "data_classification": "synthetic"})
    assert a["client_uid"] == b["client_uid"]


def test_forbidden_keys_raise():
    with pytest.raises(ValueError, match="never part of a definition"):
        client_definition("openrouter", {"api_key": "sk-x"})
    with pytest.raises(ValueError, match="never part of a definition"):
        client_definition("openrouter", {"capability": {}})


def test_unknown_key_raises_with_allowed_list():
    with pytest.raises(ValueError, match="Allowed:"):
        client_definition("ollama", {"contexts": 4096})


def test_unknown_provider_raises_with_known():
    with pytest.raises(ValueError, match="known:"):
        client_definition("gpt4all", {})


def test_type_checking_coerces_strings_but_not_numbers_to_str():
    doc = client_definition("ollama", {"num_ctx": "16384", "temperature": "0.5"})
    assert doc["settings"]["num_ctx"] == 16384
    assert doc["settings"]["temperature"] == 0.5
    with pytest.raises(ValueError, match="not silently stringified"):
        client_definition("ollama", {"model": 42})


def test_vocab_violation_raises():
    with pytest.raises(ValueError, match="not one of"):
        client_definition("ollama", {"mode": "yaml"})


def test_parse_document_shapes():
    flat = {"provider": "ollama", "model": "m", "num_ctx": 4096}
    canonical = {"provider": "ollama", "settings": {"model": "m", "num_ctx": 4096}}
    p1, s1 = parse_document(flat)
    p2, s2 = parse_document(canonical)
    assert p1 == p2 == "ollama"
    assert s1 == s2
    row = {"name": "stored", "definition": canonical}
    p3, s3 = parse_document(row)
    assert (p3, s3) == (p1, s1)


def test_spec_from_definition_env_resolution(monkeypatch):
    doc = client_definition("openrouter", {"model": "m", "data_classification": "synthetic"})
    monkeypatch.setenv("EXTRCT_GATEWAY_URL", "http://gw:9000")
    spec, _ = spec_from_definition(doc)
    assert spec.egress_route == "gateway"
    assert spec.gateway_url == "http://gw:9000"
    monkeypatch.delenv("EXTRCT_GATEWAY_URL")
    spec, _ = spec_from_definition(doc)
    assert spec.egress_route == "direct"
    assert spec.allow_direct_egress is True  # explicit direct, recorded as such


def test_authored_egress_settings_never_touched(monkeypatch):
    monkeypatch.setenv("EXTRCT_GATEWAY_URL", "http://gw:9000")
    doc = client_definition("openrouter", {
        "model": "m", "data_classification": "synthetic",
        "egress_route": "direct", "allow_direct_egress": True})
    spec, _ = spec_from_definition(doc)
    assert spec.egress_route == "direct"  # authoring intent wins over env


def test_build_client_payload_runs_preflight(monkeypatch):
    monkeypatch.delenv("EXTRCT_GATEWAY_URL", raising=False)
    doc = client_definition("openrouter", {"model": "m"})  # no data_classification
    with pytest.raises(EgressRefused, match="data_classification"):
        build_client_payload(doc)


def test_definition_from_payload_round_trip():
    doc = client_definition("ollama", {"model": "m", "num_ctx": 16384})
    payload = build_client_payload(doc)
    doc2 = definition_from_payload(payload)
    assert doc2["client_uid"] == doc["client_uid"]


def test_quantizations_tuple_round_trip():
    doc = client_definition("openrouter", {
        "model": "m", "data_classification": "synthetic", "egress_route": "direct",
        "allow_direct_egress": True, "quantizations": ["fp8", "int4"]})
    assert doc["settings"]["quantizations"] == ["fp8", "int4"]  # pure JSON in the document
    spec, _ = spec_from_definition(doc)
    assert spec.quantizations == ("fp8", "int4")  # tuple on the frozen spec
