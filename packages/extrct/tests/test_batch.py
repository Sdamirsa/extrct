import json

import httpx
import pytest

from extrct.batch import normalize_configs, normalize_inputs, normalize_schemas, plan_batch, run_batch
from extrct.storage.sqlite import SQLiteRunStore

from conftest import mock_http, openrouter_payload

VALID = json.dumps({"lvef": 55.0, "severity": None})


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")


# ------------------------------------------------------------------ normalization
def test_normalize_inputs_hash_identity_and_labels():
    rows = normalize_inputs([{"input_id": "a", "text": "one"}, {"text": "two"}])
    assert rows[0]["input_id"] == "a" and rows[1]["input_id"] is None
    assert all(len(r["input_sha256"]) == 64 for r in rows)
    with pytest.raises(ValueError, match="duplicate input_id"):
        normalize_inputs([{"input_id": "a", "text": "x"}, {"input_id": "a", "text": "y"}])
    with pytest.raises(ValueError, match="empty text"):
        normalize_inputs([{"input_id": "a", "text": " "}])


def test_normalize_configs_recomputes_untrusted_uids():
    out = normalize_configs([{"config": {"openrouter.temperature": 0.7}}])
    assert len(out[0]["config_uid"]) == 16
    out2 = normalize_configs([{"config": json.dumps({"openrouter.temperature": 0.7})}])
    assert out2[0]["config_uid"] == out[0]["config_uid"]


def test_normalize_schemas_shapes(echo_envelope):
    single = normalize_schemas(echo_envelope)
    listed = normalize_schemas([{"schema_uid": echo_envelope["schema_uid"],
                                 "envelope": echo_envelope}])
    assert single == listed


# ------------------------------------------------------------------ planning
def test_plan_batch_is_strict(openrouter_client_payload, echo_envelope):
    inputs = normalize_inputs([{"text": "note"}])
    configs = normalize_configs([{"config": {}}])
    schemas = normalize_schemas(echo_envelope)
    with pytest.raises(ValueError, match="OpenRouter-only"):
        plan_batch({"provider": "ollama", "spec": {}}, inputs, configs, schemas)
    with pytest.raises(ValueError, match="unknown setting"):
        plan_batch(openrouter_client_payload, inputs, configs, schemas, {"parallel": 4})
    with pytest.raises(ValueError, match="empty axis"):
        plan_batch(openrouter_client_payload, [], configs, schemas)
    with pytest.raises(ValueError, match="BATCH_CAP"):
        many = normalize_inputs([{"input_id": str(i), "text": f"t{i}"} for i in range(2001)])
        plan_batch(openrouter_client_payload, many, configs, schemas)


def test_plan_batch_marks_bad_configs_instead_of_dying(openrouter_client_payload, echo_envelope):
    inputs = normalize_inputs([{"text": "note"}])
    configs = normalize_configs([
        {"config": {}},                                  # inherits the pinned base: fine
        {"config": {"openrouter.endpoint_tag": ""}},     # unpins: refused per-config
    ])
    schemas = normalize_schemas(echo_envelope)
    manifest = plan_batch(openrouter_client_payload, inputs, configs, schemas)
    assert manifest["cells"] == 2
    assert len(manifest["bad_configs"]) == 1
    assert "endpoint" in [c for c in manifest["config_checks"] if not c["ok"]][0]["error"]
    assert manifest["batch_uid"]


# ------------------------------------------------------------------ running
async def test_run_batch_one_row_per_cell_always(openrouter_client_payload, echo_envelope, tmp_path):
    inputs = normalize_inputs([{"input_id": "n1", "text": "note one"},
                               {"input_id": "n2", "text": "note two"}])
    configs = normalize_configs([
        {"config": {"openrouter.temperature": 0.0}},
        {"config": {"openrouter.endpoint_tag": ""}},     # config_error column
    ])
    schemas = normalize_schemas(echo_envelope)
    store = SQLiteRunStore(str(tmp_path / "runs.sqlite"))

    async with mock_http(lambda req: httpx.Response(200, json=openrouter_payload(VALID))) as http:
        out = await run_batch(openrouter_client_payload, inputs, configs, schemas,
                              {"min_sample": 100}, http=http, store=store)

    rows, report = out["rows"], out["report"]
    assert len(rows) == 4                        # 2 inputs x 2 configs x 1 schema
    assert report["by_status"] == {"ok": 2, "config_error": 2}
    assert report["total_cost_usd"] > 0          # usage accounting flowed through
    ok_rows = [r for r in rows if r["final_status"] == "ok"]
    assert all(r["run_uid"] for r in ok_rows)
    assert all(r["generation_id"] is None for r in ok_rows)  # no header in the mock

    # evidence rows landed with the batch join labels
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        metas = [json.loads(m[0]) for m in
                 conn.execute("SELECT run_metadata FROM extraction_run").fetchall()]
    assert len(metas) == 2
    assert all(m["batch_uid"] == report["batch_uid"] for m in metas)
    assert {m["input_id"] for m in metas} == {"n1", "n2"}


async def test_run_batch_resume_never_rebills(openrouter_client_payload, echo_envelope, tmp_path):
    inputs = normalize_inputs([{"text": "note one"}, {"text": "note two"}])
    configs = normalize_configs([{"config": {}}])
    schemas = normalize_schemas(echo_envelope)
    store = SQLiteRunStore(str(tmp_path / "runs.sqlite"))
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=openrouter_payload(VALID))

    async with mock_http(handler) as http:
        first = await run_batch(openrouter_client_payload, inputs, configs, schemas,
                                {"min_sample": 100}, http=http, store=store)
        assert calls["n"] == 2
        second = await run_batch(openrouter_client_payload, inputs, configs, schemas,
                                 {"min_sample": 100}, http=http, store=store)

    assert calls["n"] == 2                       # nothing re-billed
    assert second["report"]["by_status"] == {"skipped_completed": 2}
    assert second["report"]["resumed"] == 2
    # resumed rows still carry the extracted object (refetched from the log)
    assert all(json.loads(r["extracted"]) == json.loads(VALID) for r in second["rows"])


async def test_circuit_breaker_stops_dispatch(openrouter_client_payload, echo_envelope):
    inputs = normalize_inputs([{"input_id": str(i), "text": f"note {i}"} for i in range(4)])
    configs = normalize_configs([{"config": {}}])
    schemas = normalize_schemas(echo_envelope)

    def handler(req):  # every response is a terminal failure (error on 200)
        return httpx.Response(200, json=openrouter_payload("", finish_reason="error"))

    logs = []
    async with mock_http(handler) as http:
        out = await run_batch(openrouter_client_payload, inputs, configs, schemas,
                              {"min_sample": 2, "stop_on_failure_rate": 0.4, "concurrency": 1},
                              http=http, log=logs.append)

    by = out["report"]["by_status"]
    assert out["report"]["circuit_breaker_tripped"] is True
    assert by.get("aborted_circuit_breaker", 0) >= 1     # kept as rows: absence is not evidence
    assert by.get("invalid", 0) >= 2
    assert any("CIRCUIT BREAKER" in m for m in logs)
