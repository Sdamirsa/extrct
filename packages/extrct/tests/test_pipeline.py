import json

import httpx
import pytest

from extrct import pipeline

from conftest import mock_http, ollama_payload

VALID = '{"lvef": 55.0, "severity": "mild"}'


# ------------------------------------------------------------------ composition
def test_compose_certainty_riders_upgrade_only(ollama_client_payload, echo_envelope):
    composed = pipeline.compose(ollama_client_payload, echo_envelope, None, {"enabled": True})
    spec = composed["client"]["spec"]
    assert spec["logprobs"] is True
    assert spec["top_logprobs"] == 3
    assert composed["composition"]["riders"] == {"logprobs": True, "top_logprobs": 3}
    # authored stronger setting is never downgraded
    strong = dict(ollama_client_payload)
    strong["spec"] = {**strong["spec"], "logprobs": True, "top_logprobs": 10}
    composed = pipeline.compose(strong, echo_envelope, None, {"enabled": True, "top_logprobs": 3})
    assert composed["client"]["spec"]["top_logprobs"] == 10
    assert composed["composition"]["riders"] == {}


def test_compose_injects_evidence_mirror(ollama_client_payload, echo_envelope):
    composed = pipeline.compose(ollama_client_payload, echo_envelope, {"enabled": True}, None)
    sent = composed["envelope_sent"]
    assert "_evidence" in sent["schema"]["properties"]
    assert sent["schema_uid"] != echo_envelope["schema_uid"]
    assert composed["composition"]["evidence_injected"] is True
    assert composed["composition"]["clean_schema_uid"] == echo_envelope["schema_uid"]
    # the input envelope is never mutated
    assert "_evidence" not in echo_envelope["schema"]["properties"]


def test_compose_untouched_without_configs(ollama_client_payload, echo_envelope):
    composed = pipeline.compose(ollama_client_payload, echo_envelope, None, None)
    assert composed["envelope_sent"] is echo_envelope
    assert composed["client"]["spec"]["logprobs"] is False


# ------------------------------------------------------------------ chunking
def test_make_chunks_identity_when_wrapper_off():
    chunks, doc, entry = pipeline.make_chunks("some text", None)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "some text"
    assert doc is None
    assert entry["status"] == "skipped"


def test_make_chunks_wraps_when_enabled():
    text = "para one. " * 100 + "\n\n" + "para two. " * 100
    chunks, doc, entry = pipeline.make_chunks(text, {"enabled": True, "max_chars": 400,
                                                     "overlap_chars": 50})
    assert entry["status"] == "ok"
    assert len(chunks) > 1
    assert entry["coverage_ok"] is True


def test_make_chunks_failure_is_recorded():
    chunks, doc, entry = pipeline.make_chunks("text", {"enabled": True, "max_chars": 10})
    assert chunks == []
    assert entry["status"] == "failed"
    assert "sane floor" in entry["reason"]


# ------------------------------------------------------------------ execute_single
async def test_execute_single_ok(ollama_client_payload, echo_envelope):
    def handler(req):
        body = json.loads(req.content)
        assert body["model"] == "test-model"
        assert body["format"] == echo_envelope["schema"]
        return httpx.Response(200, json=ollama_payload(VALID))

    async with mock_http(handler) as http:
        out = await pipeline.execute_single(ollama_client_payload, echo_envelope,
                                            "note text", http=http,
                                            ladder=("json_repair", "coerce"))
    assert out["final_status"] == "ok"
    assert out["obj"] == {"lvef": 55.0, "severity": "mild"}
    assert out["run_uid"] and len(out["run_uid"]) == 16
    assert out["ladder"]["attempts"][0]["layer"] == "request"


async def test_execute_single_truncation_gate_blocks_ladder(ollama_client_payload, echo_envelope):
    def handler(req):
        return httpx.Response(200, json=ollama_payload('{"lvef": 5', done_reason="length"))

    async with mock_http(handler) as http:
        out = await pipeline.execute_single(ollama_client_payload, echo_envelope,
                                            "note", http=http, ladder=("json_repair", "coerce"))
    assert out["final_status"] == "invalid"
    assert out["ladder"] is None  # the ladder never saw the truncated body
    assert "truncated_by_length" in out["response"]["silent_failure_flags"]


async def test_execute_single_transport_error_is_data(ollama_client_payload, echo_envelope):
    def handler(req):
        return httpx.Response(500, json={"error": "kaboom"})

    async with mock_http(handler) as http:
        out = await pipeline.execute_single(ollama_client_payload, echo_envelope,
                                            "note", http=http)
    assert out["final_status"] == "error"
    assert out["transport"]["http_status"] == 500


async def test_execute_single_unknown_provider_raises(echo_envelope):
    async with mock_http(lambda r: httpx.Response(200, json={})) as http:
        with pytest.raises(ValueError, match="registered:"):
            await pipeline.execute_single({"provider": "ghost", "spec": {}},
                                          echo_envelope, "x", http=http)


async def test_run_uid_changes_with_input_and_config(ollama_client_payload, echo_envelope):
    def handler(req):
        return httpx.Response(200, json=ollama_payload(VALID))

    async with mock_http(handler) as http:
        a = await pipeline.execute_single(ollama_client_payload, echo_envelope, "text A", http=http)
        b = await pipeline.execute_single(ollama_client_payload, echo_envelope, "text B", http=http)
        c = await pipeline.execute_single(ollama_client_payload, echo_envelope, "text A", http=http)
    assert a["run_uid"] != b["run_uid"]     # input separates identity
    assert a["run_uid"] == c["run_uid"]     # same request x input = same run


# ------------------------------------------------------------------ analysis steps
def test_try_grounding_reports_and_offsets(echo_envelope):
    extracted = {"lvef": 55.0, "_evidence": {"lvef": "LVEF at 55 percent"}}
    report, entry = pipeline.try_grounding(extracted, "Note: LVEF at 55 percent today.",
                                           {"enabled": True}, offset=100)
    assert entry["status"] == "ok"
    assert report["fields"]["lvef"]["start"] >= 100  # shifted to document coordinates
    assert entry["grounding_clean"] is True


def test_try_grounding_missing_evidence_is_failed_step():
    report, entry = pipeline.try_grounding({"lvef": 55.0}, "src", {"enabled": True})
    assert report is None
    assert entry["status"] == "failed"
    assert "_evidence" in entry["reason"]


def test_try_grounding_posthoc_recorded_as_skipped():
    _, entry = pipeline.try_grounding({}, "src", {"enabled": True, "mode": "posthoc"})
    assert entry["status"] == "skipped"
    assert "posthoc" in entry["reason"]


def test_try_certainty_engine_and_mask_state(echo_envelope):
    from conftest import tokens_for

    content = '{"x": 1}'
    payload = ollama_payload(content, logprobs=tokens_for([('{"', -0.1), ("x", -0.1),
                                                           ('": ', -0.1), ("1", -0.1),
                                                           ("}", -0.1)]))
    report, entry = pipeline.try_certainty(payload, None, {"enabled": True},
                                           engine="ollama", request_mode="json_schema")
    assert entry["status"] == "ok"
    assert report["mask_state"] == "pre_mask"
    assert entry["mask_state"] == "pre_mask"


def test_try_certainty_no_logprobs_failed_step():
    report, entry = pipeline.try_certainty(ollama_payload("{}"), None, {"enabled": True},
                                           engine="ollama", request_mode="json_schema")
    assert entry["status"] == "failed"
    assert "logprobs" in entry["reason"]


def test_steps_disabled_are_skipped_with_reason():
    _, g = pipeline.try_grounding({}, "s", {"enabled": False})
    _, c = pipeline.try_certainty({}, None, None, engine="e", request_mode=None)
    assert g["status"] == "skipped" and "enabled=false" in g["reason"]
    assert c["status"] == "skipped" and "no certainty config" in c["reason"]


def test_try_merge_identity_and_failure_paths(echo_envelope):
    single = [{"chunk_idx": 0, "extracted": {"x": 1}}]
    result, entry = pipeline.try_merge(single, None, echo_envelope)
    assert result is None and entry["status"] == "skipped"
    multi = [{"chunk_idx": 0, "extracted": {"x": 1}}, {"chunk_idx": 1, "extracted": {"x": 2}}]
    result, entry = pipeline.try_merge(multi, None, echo_envelope)
    assert entry["status"] == "ok"
    assert entry["needs_manual"] == 1  # the tie is a major conflict, surfaced
    dead = [{"chunk_idx": 0, "extracted": None}, {"chunk_idx": 1, "extracted": None}]
    result, entry = pipeline.try_merge(dead, None, echo_envelope)
    assert result is None and entry["status"] == "failed"


def test_assemble_record_declared_vs_executed():
    plan = pipeline.plan_steps(None, {"enabled": True}, None)
    steps = {"wrap": pipeline.step_entry("skipped", "no wrapper config wired"),
             "request": pipeline.step_entry("ok"),
             "grounding": pipeline.step_entry("failed", "model ignored the mirror"),
             "merge": pipeline.step_entry("skipped", "single chunk -> identity")}
    rec = pipeline.assemble_record(plan, steps, chunk_count=1, run_uids=["abc"],
                                   composition={"riders": {}})
    assert rec["ok"] is True                       # analysis failure is not breaking
    assert rec["failed_recorded"] == ["grounding"]
    assert rec["pipeline_uid"]
