import json
import sqlite3

import pytest

from extrct.storage import NullRunStore, open_store
from extrct.storage.sqlite import SQLiteRunStore
from extrct.wrapping import wrap_text


def _row(run_uid="r1", **over):
    base = {
        "run_uid": run_uid, "request_uid": "q1", "schema_uid": "s1",
        "schema_encoding": "strict_nullable", "provider": "ollama",
        "base_url": "http://mock", "model_on_wire": "m", "input_sha256": "abc",
        "input_chars": 10, "final_status": "ok", "attempts": 1,
        "layers_used": [], "silent_failure_flags": [], "extracted": {"x": 1},
        "tags": ["test"],
    }
    base.update(over)
    return base


@pytest.fixture
def store(tmp_path):
    return SQLiteRunStore(str(tmp_path / "runs.sqlite"))


async def test_ensure_schema_idempotent(store):
    await store.ensure_schema()
    await store.ensure_schema()  # second call must not raise


async def test_save_run_upsert_coalesce_semantics(store):
    await store.ensure_schema()
    await store.save_run(_row(extracted={"x": 1}, total_tokens=60))
    # partial re-save: encoding/extracted omitted -> must NOT be erased;
    # final_status legitimately progresses -> must be overwritten
    await store.save_run(_row(schema_encoding=None, extracted=None, total_tokens=None,
                              final_status="repaired", attempts=2))
    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT schema_encoding, extracted, total_tokens, final_status, attempts "
            "FROM extraction_run WHERE run_uid='r1'").fetchone()
    assert row[0] == "strict_nullable"          # survived the partial re-save
    assert json.loads(row[1]) == {"x": 1}
    assert row[2] == 60
    assert row[3] == "repaired" and row[4] == 2  # progressed


async def test_annotate_never_touches_final_status(store):
    await store.ensure_schema()
    await store.save_run(_row())
    ok = await store.annotate_run("r1", field_logprobs={"x": {"mean": 0.9}})
    assert ok is True
    assert await store.annotate_run("ghost", field_logprobs={}) is False
    assert await store.annotate_run("r1") is False  # nothing to set
    with sqlite3.connect(store.path) as conn:
        fs, lp = conn.execute(
            "SELECT final_status, field_logprobs FROM extraction_run WHERE run_uid='r1'").fetchone()
    assert fs == "ok"
    assert json.loads(lp)["x"]["mean"] == 0.9


async def test_find_completed_only_terminal_statuses(store):
    await store.ensure_schema()
    await store.save_run(_row("r1", final_status="ok"))
    await store.save_run(_row("r2", final_status="invalid"))
    await store.save_run(_row("r3", final_status="repaired"))
    done = await store.find_completed(["r1", "r2", "r3", "r4"])
    assert done == {"r1", "r3"}
    assert await store.find_completed([]) == set()


async def test_fetch_extracted_round_trip(store):
    await store.ensure_schema()
    await store.save_run(_row("r1", extracted={"lvef": 55.0}))
    out = await store.fetch_extracted(["r1", "missing"])
    assert out["r1"] == {"lvef": 55.0}
    assert "missing" not in out


async def test_attempts_saved_and_upserted(store):
    await store.ensure_schema()
    await store.save_run(_row())
    attempts = [{"attempt_no": 1, "layer": "request", "valid": False,
                 "validation_errors": [{"msg": "bad"}], "note": None},
                {"attempt_no": 2, "layer": "coerce", "valid": True,
                 "validation_errors": [], "note": "cast"}]
    await store.save_attempts("r1", attempts)
    await store.save_attempts("r1", attempts)  # idempotent
    with sqlite3.connect(store.path) as conn:
        n = conn.execute("SELECT count(*) FROM extraction_attempt WHERE run_uid='r1'").fetchone()[0]
    assert n == 2


async def test_wrapping_stored_hashes_always_text_on_request(store):
    await store.ensure_schema()
    doc = wrap_text("para one\n\npara two " * 40, "paragraph_pack", max_chars=300, overlap_chars=50)
    await store.save_wrapping(doc)  # default: no text
    with sqlite3.connect(store.path) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM text_chunk").fetchall()]
        shas = [r[0] for r in conn.execute("SELECT chunk_sha256 FROM text_chunk").fetchall()]
    assert all(t is None for t in texts)   # privacy default: hash, don't keep
    assert all(shas)
    await store.save_wrapping(doc, store_text=True)  # explicit opt-in fills text in
    with sqlite3.connect(store.path) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM text_chunk").fetchall()]
    assert all(t for t in texts)


async def test_merge_run_saved(store):
    from extrct.merging import merge_extractions

    await store.ensure_schema()
    result = merge_extractions([{"chunk_idx": 0, "extracted": {"x": 1}},
                                {"chunk_idx": 1, "extracted": {"x": 2}}])
    await store.save_merge_run(result, tags=["t"])
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT conflicts, major_conflicts FROM merge_run").fetchone()
    assert row == (1, 1)


async def test_views_exist(store):
    await store.ensure_schema()
    await store.save_run(_row("r1", final_status="invalid", error="boom"))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM v_failed").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v_run_report").fetchone()[0] == 1


def test_open_store_shapes(tmp_path, monkeypatch):
    assert isinstance(open_store(None), NullRunStore)
    assert isinstance(open_store({"backend": "none"}), NullRunStore)
    s = open_store({"backend": "sqlite", "path": str(tmp_path / "x.sqlite")})
    assert isinstance(s, SQLiteRunStore)
    monkeypatch.delenv("EXTRCT_PG_DSN", raising=False)
    with pytest.raises(ValueError, match="no DSN"):
        open_store({"backend": "postgres"})
    with pytest.raises(ValueError, match="unknown storage backend"):
        open_store({"backend": "mysql"})


async def test_null_store_swallows_everything():
    s = NullRunStore()
    await s.ensure_schema()
    await s.save_run({})
    assert await s.find_completed(["x"]) == set()
    assert await s.fetch_extracted(["x"]) == {}
    assert await s.annotate_run("x", field_logprobs={}) is False
