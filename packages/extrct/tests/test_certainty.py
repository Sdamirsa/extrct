import math

from extrct.schema import build_schema
from extrct.xai.certainty import field_confidence, mask_state, normalize_logprobs

from conftest import ollama_payload, openrouter_payload, tokens_for

SCHEMA = build_schema([
    {"name": "severity", "type": "str", "options": ["mild", "moderate", "severe"]},
    {"name": "lvef", "type": "float"},
])

CONTENT = '{"severity": "mild", "lvef": 55}'
# concat of these tokens must equal CONTENT exactly — the invariant under test
PIECES = [('{"', -0.01), ("severity", -0.02), ('": ', -0.01), ('"mild', -0.10),
          ('", ', -0.01), ('"lvef', -0.02), ('": ', -0.01), ("55", -0.30), ("}", -0.01)]
TOP = {'"mild': [('"mild', -0.10), ('"moderate', -2.5), ('"severe', -3.0)]}


def test_field_confidence_statistics():
    out = field_confidence(ollama_payload(CONTENT, logprobs=tokens_for(PIECES, TOP)), SCHEMA)
    assert out["ok"] is True
    sev = out["fields"]["severity"]
    assert sev["value_text"] == "mild"
    assert math.isclose(sev["mean"], math.exp(-0.10), rel_tol=1e-4)
    lvef = out["fields"]["lvef"]
    assert lvef["value_text"] == "55"
    assert math.isclose(lvef["joint"], math.exp(-0.30), rel_tol=1e-4)
    assert math.isclose(lvef["min"], math.exp(-0.30), rel_tol=1e-4)


def test_enum_posterior_over_option_set():
    out = field_confidence(ollama_payload(CONTENT, logprobs=tokens_for(PIECES, TOP)), SCHEMA)
    post = out["fields"]["severity"]["enum_posterior"]
    assert post["available"] is True
    assert set(post["posterior"]) == {"mild", "moderate", "severe"}
    assert post["posterior"]["mild"] > post["posterior"]["moderate"] > post["posterior"]["severe"]
    assert abs(sum(post["posterior"].values()) - 1.0) < 1e-6


def test_enum_posterior_refused_without_alternatives():
    out = field_confidence(ollama_payload(CONTENT, logprobs=tokens_for(PIECES)), SCHEMA)
    post = out["fields"]["severity"]["enum_posterior"]
    assert post["available"] is False
    assert "top_logprobs" in post["reason"].lower() or "sampled" in post["reason"].lower()


def test_no_logprobs_is_an_error_record():
    out = field_confidence(ollama_payload(CONTENT), SCHEMA)
    assert out["ok"] is False
    assert "logprobs" in out["error"]


def test_concat_mismatch_refuses_alignment():
    bad = tokens_for([('{"sev', -0.1), ("WRONG", -0.1)])
    out = field_confidence(ollama_payload(CONTENT, logprobs=bad), SCHEMA)
    assert out["ok"] is False
    assert "token_concat_mismatch" in out["error"]


def test_thinking_prefix_tolerated_as_suffix_alignment():
    prefixed = tokens_for([("think... ", -0.5)] + PIECES)
    out = field_confidence(ollama_payload(CONTENT, logprobs=prefixed), SCHEMA)
    assert out["ok"] is True
    assert out["diagnostics"]["invariant"].startswith("suffix")
    assert out["fields"]["severity"]["value_text"] == "mild"


def test_mtp_prefix_only_named_and_refused():
    partial = tokens_for(PIECES[:3])  # tokens cover only a prefix of the content
    out = field_confidence(ollama_payload(CONTENT, logprobs=partial), SCHEMA)
    assert out["ok"] is False
    assert "MTP" in out["error"]


def test_sentinel_logprobs_excluded_and_counted():
    pieces = list(PIECES)
    pieces[7] = ("55", -9999.0)  # vLLM-style masked sentinel on the lvef token
    out = field_confidence(ollama_payload(CONTENT, logprobs=tokens_for(pieces, TOP)), SCHEMA)
    lvef = out["fields"]["lvef"]
    assert lvef["masked_tokens"] == 1
    assert "mean" not in lvef  # no usable token left


def test_truncated_json_is_an_error_record_not_a_crash():
    cut = '{"severity": "mil'
    toks = tokens_for([('{"', -0.1), ("severity", -0.1), ('": ', -0.1), ('"mil', -0.1)])
    out = field_confidence(ollama_payload(cut, logprobs=toks), SCHEMA)
    assert out["ok"] is False
    assert "value spans" in out["error"]


def test_normalize_handles_openai_shape():
    lp = {"content": [{"token": "a", "logprob": -0.1, "top_logprobs": [{"token": "a", "logprob": -0.1}]}]}
    payload = openrouter_payload("a", logprobs=lp)
    toks = normalize_logprobs(payload)
    assert toks and toks[0]["token"] == "a"
    assert toks[0]["top"] == [("a", -0.1)]


def test_mask_state_rules():
    assert mask_state("ollama", "json_schema") == "pre_mask"
    assert mask_state("vllm", "json_schema") == "post_mask"
    assert mask_state("ollama", "prompted") == "unmasked"
    assert mask_state("openrouter", "json_schema") == "unknown_provider_engine"
