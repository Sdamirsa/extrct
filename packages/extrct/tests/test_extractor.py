import json
import sqlite3

import httpx
import pytest

from extrct import Extractor

from conftest import mock_http, naive_tokens, ollama_payload

NOTE = "Echocardiography today. LVEF measured at 55 percent. Mild regurgitation noted."

CONTENT = json.dumps({
    "lvef": 55.0,
    "severity": "mild",
    "_evidence": {"lvef": "LVEF measured at 55 percent",
                  "severity": "Mild regurgitation noted"},
})


def _job(tmp_path, **over):
    base = {
        "provider": {"provider": "ollama", "model": "test-model", "base_url": "http://mock"},
        "schema": {
            "root_name": "echo",
            "variables": [
                {"name": "lvef", "type": "float", "constraints": {"ge": 0, "le": 100}},
                {"name": "severity", "type": "str",
                 "options": ["mild", "moderate", "severe"], "required": False},
            ],
        },
        "extract": {"run_tags": "suite"},
        "grounding": {"enabled": True},
        "certainty": {"enabled": True},
        "storage": {"backend": "sqlite", "path": str(tmp_path / "runs.sqlite")},
    }
    base.update(over)
    return base


def _handler(req):
    body = json.loads(req.content)
    # grounding config must have injected the evidence mirror into the sent schema
    assert "_evidence" in body["format"]["properties"]
    # certainty config must have ridden logprobs into the request
    assert body["logprobs"] is True
    return httpx.Response(200, json=ollama_payload(CONTENT, logprobs=naive_tokens(CONTENT)))


async def test_end_to_end_single_document(tmp_path):
    async with mock_http(_handler) as http:
        ex = Extractor(_job(tmp_path), http=http)
        result = await ex.extract(NOTE, input_id="note-1")

    assert result.status == "ok"
    assert result.ok
    assert result.data["lvef"] == 55.0
    assert result.data["severity"] == "mild"
    assert len(result.run_uids) == 1
    # grounding: every quote located in the source
    assert result.grounding["summary"]["grounding_clean"] is True
    # certainty: per-field statistics with engine semantics attached
    assert result.certainty["ok"] is True
    assert result.certainty["mask_state"] == "pre_mask"
    assert "lvef" in result.certainty["fields"]
    # declared plan vs executed steps
    assert result.record["steps"]["grounding"]["status"] == "ok"
    assert result.record["steps"]["certainty"]["status"] == "ok"
    assert result.record["steps"]["merge"]["status"] == "skipped"

    # --- the run log has everything ---
    db = tmp_path / "runs.sqlite"
    with sqlite3.connect(db) as conn:
        runs = conn.execute("SELECT run_uid, final_status, input_text, tags, run_metadata, "
                            "field_logprobs, field_grounding FROM extraction_run").fetchall()
    assert len(runs) == 1
    uid, status, input_text, tags, metadata, lp, gr = runs[0]
    assert uid == result.run_uids[0]
    assert status == "ok"
    assert input_text is None                     # privacy default: hash, don't keep
    assert "suite" in json.loads(tags)
    meta = json.loads(metadata)
    assert meta["input_id"] == "note-1"           # join label, never hashed
    assert meta["job_uid"] == result.job_uid
    assert json.loads(lp)["ok"] is True           # annotations attached
    assert json.loads(gr)["summary"]["grounding_clean"] is True


async def test_rerun_same_input_upserts_same_run(tmp_path):
    job = _job(tmp_path)
    async with mock_http(_handler) as http:
        ex = Extractor(job, http=http)
        a = await ex.extract(NOTE)
        b = await ex.extract(NOTE)
    assert a.run_uids == b.run_uids  # content-addressed identity
    with sqlite3.connect(tmp_path / "runs.sqlite") as conn:
        n = conn.execute("SELECT count(*) FROM extraction_run").fetchone()[0]
    assert n == 1  # idempotent upsert, not a duplicate row


async def test_multi_chunk_merge_surfaces_conflict(tmp_path):
    para1 = "First study section. LVEF measured at 55 percent. " + "Filler sentence. " * 30
    para2 = "Later addendum section. LVEF re-measured at 60 percent. " + "More filler. " * 30
    text = para1 + "\n\n" + para2

    def handler(req):
        body = json.loads(req.content)
        user = body["messages"][-1]["content"]
        lvef = 55.0 if "55 percent" in user else 60.0
        content = json.dumps({"lvef": lvef, "severity": None})
        return httpx.Response(200, json=ollama_payload(content))

    job = _job(tmp_path,
               grounding={"enabled": False}, certainty={"enabled": False},
               wrapper={"enabled": True, "max_chars": 600, "overlap_chars": 0})
    async with mock_http(handler) as http:
        ex = Extractor(job, http=http)
        result = await ex.extract(text)

    assert result.status == "ok"
    assert len(result.chunks) >= 2
    assert result.merge is not None
    assert result.record["steps"]["merge"]["needs_manual"] >= 1  # 55 vs 60: major, surfaced
    conflict = result.merge["conflicts"][0]
    assert {c["value"] for c in conflict["candidates"]} == {55.0, 60.0}
    # wrapping + merge landed in the log
    with sqlite3.connect(tmp_path / "runs.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM text_wrapping").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM merge_run").fetchone()[0] == 1
        n_runs = conn.execute("SELECT count(*) FROM extraction_run").fetchone()[0]
    assert n_runs == len(result.chunks)  # one evidence row per model call


async def test_extract_many_isolation_and_order(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        body = json.loads(req.content)
        if "explode" in body["messages"][-1]["content"]:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=ollama_payload(CONTENT, logprobs=naive_tokens(CONTENT)))

    async with mock_http(handler) as http:
        ex = Extractor(_job(tmp_path), http=http)
        results = await ex.extract_many([NOTE, "please explode", NOTE + " again"],
                                        input_ids=["a", "b", "c"], concurrency=2)
    assert [r.status for r in results] == ["ok", "error", "ok"]
    assert results[1].error  # the failure is data, not a dead batch


async def test_status_aggregation_partial(tmp_path):
    def handler(req):
        body = json.loads(req.content)
        user = body["messages"][-1]["content"]
        if "55 percent" in user:
            return httpx.Response(200, json=ollama_payload(json.dumps({"lvef": 55.0, "severity": None})))
        return httpx.Response(200, json=ollama_payload('{"lvef": 5', done_reason="length"))

    para1 = "LVEF measured at 55 percent. " + "Filler. " * 60
    para2 = "Truncation trigger section. " + "More filler. " * 60
    job = _job(tmp_path, grounding={"enabled": False}, certainty={"enabled": False},
               wrapper={"enabled": True, "max_chars": 600, "overlap_chars": 0})
    async with mock_http(handler) as http:
        ex = Extractor(job, http=http)
        result = await ex.extract(para1 + "\n\n" + para2)
    assert result.status == "partial"
    statuses = {c["final_status"] for c in result.chunks}
    assert "ok" in statuses and "invalid" in statuses


async def test_no_storage_backend_still_works(tmp_path):
    job = _job(tmp_path)
    job.pop("storage")
    async with mock_http(_handler) as http:
        ex = Extractor(job, http=http)
        result = await ex.extract(NOTE)
    assert result.ok


async def test_missing_http_client_is_loud(tmp_path):
    ex = Extractor(_job(tmp_path))
    with pytest.raises(RuntimeError, match="async with"):
        await ex.extract(NOTE)


def test_from_yaml(tmp_path):
    import yaml

    cfg = tmp_path / "job.yaml"
    cfg.write_text(yaml.safe_dump(_job(tmp_path)), encoding="utf-8")
    ex = Extractor.from_yaml(str(cfg))
    assert ex.job["job_uid"]
    assert ex.provider.name == "ollama"
