from extrct.providers.ollama import (
    SAMPLING_KEYS,
    OllamaProvider,
    OllamaSpec,
    build_request,
    read_response,
    request_record,
)

from conftest import ollama_payload


def _spec(**kw):
    return OllamaSpec(model="test-model", base_url="http://mock", **kw)


def test_build_request_always_emits_every_sampling_key():
    body = build_request(_spec(), "text", {"type": "object", "properties": {}})
    for k in SAMPLING_KEYS:
        assert k in body["options"], f"{k} must be emitted explicitly (Modelfile inheritance trap)"
    assert "stop" not in body["options"]  # stop-truncation is indistinguishable from success
    assert body["think"] is False
    assert body["keep_alive"] == "30m"  # top-level, never inside options


def test_json_schema_mode_puts_schema_in_format(echo_envelope):
    body = build_request(_spec(), "text", echo_envelope)
    assert body["format"] == echo_envelope["schema"]


def test_json_schema_mode_without_schema_raises():
    import pytest

    with pytest.raises(ValueError, match="requires a schema"):
        build_request(_spec(), "text", None)


def test_prompted_mode_sends_schema_in_prompt_not_format(echo_envelope):
    body = build_request(_spec(mode="prompted"), "text", echo_envelope)
    assert "format" not in body
    assert "Respond with JSON matching this schema" in body["messages"][0]["content"]


def test_logprobs_top_level_boolean_with_count():
    body = build_request(_spec(logprobs=True, top_logprobs=5), "text",
                         {"type": "object", "properties": {}})
    assert body["logprobs"] is True
    assert body["top_logprobs"] == 5
    assert "logprobs" not in body["options"]  # inside options it is silently ignored


def test_num_gpu_auto_is_omitted():
    body = build_request(_spec(), "t", {"type": "object", "properties": {}})
    assert "num_gpu" not in body["options"]  # -1 = auto: let the serving host decide
    body = build_request(_spec(num_gpu=0), "t", {"type": "object", "properties": {}})
    assert body["options"]["num_gpu"] == 0


def test_request_record_redacts_input_and_schema(echo_envelope):
    spec = _spec()
    body = build_request(spec, "the secret note text", echo_envelope)
    rec = request_record(spec, "the secret note text", echo_envelope, body)
    assert rec["input"]["input_text"] is None  # hash, don't keep
    assert rec["input"]["input_chars"] == len("the secret note text")
    for m in rec["wire_body_redacted"]["messages"]:
        assert "content" not in m and "content_sha256" in m
    assert rec["wire_body_redacted"]["format"]["__ref"].startswith("schema_uid:")
    assert rec["schema"]["encoding"] == "strict_nullable"  # the envelope carried the axis


def test_request_uid_stable_across_machines(echo_envelope):
    spec = _spec()
    body = build_request(spec, "same text", echo_envelope)
    a = request_record(spec, "same text", echo_envelope, body)
    b = request_record(spec, "same text", echo_envelope, body)
    assert a["request_uid"] == b["request_uid"]


def test_read_response_truncation_is_terminal():
    out = read_response(_spec(), ollama_payload('{"partial": ', done_reason="length"))
    assert "truncated_by_length" in out["silent_failure_flags"]
    assert out["terminal_failure"] is True
    assert out["final_status_hint"] == "invalid"


def test_read_response_context_pressure_flag():
    out = read_response(_spec(), ollama_payload("{}", prompt_eval=7900), loaded_ctx=8192)
    assert "context_pressure" in out["silent_failure_flags"]
    assert out["terminal_failure"] is True


def test_read_response_ok_and_thinking_flag():
    out = read_response(_spec(), ollama_payload('{"x": 1}'))
    assert out["terminal_failure"] is False
    assert out["silent_failure_flags"] == []
    out = read_response(_spec(think="low"), ollama_payload('{"x": 1}'))
    assert "thinking_enabled" in out["silent_failure_flags"]


def test_provider_adapter_urls_and_capabilities():
    p = OllamaProvider()
    assert p.target_url(_spec()) == "http://mock/api/chat"
    assert p.target_url(_spec(), {"api_path": "/api/custom"}) == "http://mock/api/custom"
    assert "logprobs" in p.capabilities
    assert "prompt_logprobs" not in p.capabilities  # honest: not served yet
    assert p.engine == "ollama"
