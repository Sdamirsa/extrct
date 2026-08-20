import pytest

from extrct.providers.openrouter import (
    CapabilityError,
    EgressRefused,
    OpenRouterSpec,
    build_request,
    check_egress,
    headers,
    read_response,
    request_record,
    resolve_key,
    target_url,
)

from conftest import openrouter_payload


def _spec(**kw):
    base = {"model": "test/model", "endpoint_tag": "test/model@TestServe",
            "data_classification": "synthetic", "egress_route": "direct",
            "allow_direct_egress": True}
    base.update(kw)
    return OpenRouterSpec(**base)


# ---------------------------------------------------------------- egress guard
def test_missing_data_classification_refused():
    with pytest.raises(EgressRefused, match="data_classification"):
        check_egress(OpenRouterSpec())


def test_direct_egress_needs_explicit_consent():
    with pytest.raises(EgressRefused, match="allow_direct_egress"):
        check_egress(OpenRouterSpec(data_classification="synthetic", egress_route="direct"))


def test_gateway_route_needs_url():
    with pytest.raises(EgressRefused, match="gateway_url is empty"):
        check_egress(OpenRouterSpec(data_classification="synthetic"))


def test_gateway_url_switches_target():
    spec = _spec(egress_route="gateway", gateway_url="http://gw.local:9000")
    assert target_url(spec) == "http://gw.local:9000/chat/completions"
    assert target_url(_spec()).startswith("https://openrouter.ai")


# ---------------------------------------------------------------- request build
def test_unpinned_endpoint_raises(echo_envelope):
    with pytest.raises(CapabilityError, match="endpoint_tag is empty"):
        build_request(_spec(endpoint_tag=""), "text", echo_envelope)


def test_body_shape_and_routing_block(echo_envelope):
    body, resolution = build_request(_spec(), "text", echo_envelope)
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == echo_envelope["schema"]
    prov = body["provider"]
    assert prov["require_parameters"] is True  # loud refusal beats silent downgrade
    assert prov["allow_fallbacks"] is False
    assert prov["only"] == ["test/model@TestServe"]
    assert body["reasoning"] == {"enabled": False}  # not-sending is not a baseline
    assert body["usage"] == {"include": True}  # cost is only returned when asked
    assert resolution["schema_encoding"] == "strict_nullable"


def test_tristate_sampling_auto_respects_capability(echo_envelope):
    cap = {"supported_parameters": ["seed"], "gate_effective": True}
    body, resolution = build_request(_spec(capability=cap), "t", echo_envelope)
    assert body["seed"] == 42
    assert "temperature" not in body  # auto + unsupported -> omitted, and recorded
    omitted = {o["param"] for o in resolution["omitted"]}
    assert "temperature" in omitted
    assert all(o["vendor_default_inherited"] for o in resolution["omitted"])


def test_tristate_always_overrides_capability(echo_envelope):
    body, _ = build_request(_spec(send_temperature="always"), "t", echo_envelope)
    assert body["temperature"] == 0.0


def test_max_output_field_auto(echo_envelope):
    cap = {"supported_parameters": ["max_completion_tokens"]}
    body, _ = build_request(_spec(capability=cap), "t", echo_envelope)
    assert body["max_completion_tokens"] == 2048
    body, _ = build_request(_spec(), "t", echo_envelope)
    assert body["max_tokens"] == 2048


def test_logprobs_note_when_endpoint_lacks_them(echo_envelope):
    cap = {"supported_parameters": ["response_format"], "gate_effective": True}
    body, resolution = build_request(_spec(logprobs=True, top_logprobs=3, capability=cap),
                                     "t", echo_envelope)
    assert body["logprobs"] is True
    assert any("logprobs" in n for n in resolution["notes"])


# ---------------------------------------------------------------- credentials
def test_resolve_key_literal_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key")
    assert resolve_key(_spec(api_key="sk-literal")) == "sk-literal"
    assert resolve_key(_spec()) == "sk-env-key"


def test_resolve_key_variable_name_never_sent(monkeypatch):
    # the measured 401 trap: a variable NAME on the spec must fall through to env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key")
    assert resolve_key(_spec(api_key="OPENROUTER_API_KEY")) == "sk-env-key"


def test_resolve_key_missing_raises_with_instructions(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(CapabilityError, match="No OpenRouter key"):
        resolve_key(_spec())


def test_headers_carry_bearer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key")
    h = headers(_spec(attribution_title="extrct"))
    assert h["Authorization"] == "Bearer sk-env-key"
    assert h["X-OpenRouter-Title"] == "extrct"


# ---------------------------------------------------------------- records
def test_request_record_redaction_and_privacy(monkeypatch, echo_envelope):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    spec = _spec()
    body, resolution = build_request(spec, "secret text", echo_envelope)
    rec = request_record(spec, "secret text", echo_envelope, body, resolution)
    assert rec["input"]["input_text"] is None  # store_input_text off by default
    assert "secret" not in str(rec["wire_body_redacted"])
    js = rec["wire_body_redacted"]["response_format"]["json_schema"]
    assert js["schema"]["__ref"].startswith("schema_uid:")
    assert rec["guard"]["wall_side"] == "outside"
    assert "unresolved" in rec["headers_redacted"]["Authorization"]


def test_request_record_stores_text_only_on_explicit_flag(echo_envelope):
    spec = _spec(store_input_text=True)
    body, resolution = build_request(spec, "keep me", echo_envelope)
    rec = request_record(spec, "keep me", echo_envelope, body, resolution)
    assert rec["input"]["input_text"] == "keep me"


# ---------------------------------------------------------------- response read
def test_read_response_ok_with_cost():
    out = read_response(_spec(), openrouter_payload('{"x": 1}'))
    assert out["terminal_failure"] is False
    assert out["usage"]["cost_usd"] == "0.00021"
    assert out["served"]["generation_id"] == "gen-123"


def test_finish_reason_error_on_200_is_terminal():
    out = read_response(_spec(), openrouter_payload("", finish_reason="error"))
    assert "finish_reason_error_on_200" in out["silent_failure_flags"]
    assert out["terminal_failure"] is True


def test_truncation_is_terminal():
    out = read_response(_spec(), openrouter_payload('{"x"', finish_reason="length"))
    assert out["terminal_failure"] is True


def test_schema_downgrade_flagged_when_capability_says_no():
    cap = {"supported_parameters": ["response_format"], "supports_structured_outputs": False,
           "gate_effective": True}
    out = read_response(_spec(capability=cap), openrouter_payload('{"x": 1}'))
    assert "schema_mode_downgraded" in out["silent_failure_flags"]


def test_reasoning_forced_on_flagged():
    payload = openrouter_payload('{"x": 1}')
    payload["usage"]["reasoning_tokens"] = 128
    out = read_response(_spec(reasoning_control="off"), payload)
    assert "reasoning_forced_on" in out["silent_failure_flags"]


def test_provider_divergence_best_effort():
    cap = {"provider_name": "PinnedServe", "supports_structured_outputs": True,
           "supported_parameters": ["structured_outputs"], "status": 0}
    out = read_response(_spec(capability=cap), openrouter_payload('{"x": 1}', provider="OtherServe"))
    assert "provider_diverged_from_pin" in out["silent_failure_flags"]
    assert out["terminal_failure"] is False  # divergence is a flag, not a kill
