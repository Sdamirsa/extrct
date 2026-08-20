import asyncio

import httpx

from extrct.runner import call_many, call_once

from conftest import json_response, mock_http


async def test_call_once_success_envelope():
    async with mock_http(lambda req: json_response({"ok": True})) as http:
        out = await call_once("http://mock/x", {}, http=http)
    assert out["http_status"] == 200
    assert out["payload"] == {"ok": True}
    assert out["retries"] == 0
    assert out["error_class"] is None


async def test_call_once_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return json_response({"ok": True})

    async with mock_http(handler) as http:
        out = await call_once("http://mock/x", {}, http=http, max_retries=2, backoff_cap_s=0.01)
    assert out["http_status"] == 200
    assert out["retries"] == 1  # retries are recorded, never hidden


async def test_call_once_does_not_retry_5xx_by_default():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, json={"error": "routing rejection"})

    async with mock_http(handler) as http:
        out = await call_once("http://mock/x", {}, http=http, max_retries=3, backoff_cap_s=0.01)
    assert calls["n"] == 1
    assert out["http_status"] == 503
    assert out["error_class"] == "http_error"


async def test_call_once_retryable_status_override():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={})
        return json_response({"ok": True})

    async with mock_http(handler) as http:
        out = await call_once("http://mock/x", {}, http=http, max_retries=5,
                              retryable_status=frozenset({429, 503}), backoff_cap_s=0.01)
    assert out["http_status"] == 200
    assert out["retries"] == 2


async def test_call_once_generation_id_header_captured():
    def handler(req):
        return json_response({"ok": True}, headers={"X-Generation-Id": "gen-42"})

    async with mock_http(handler) as http:
        out = await call_once("http://mock/x", {}, http=http)
    assert out["generation_id"] == "gen-42"


async def test_call_once_decode_error_is_data():
    async with mock_http(lambda req: httpx.Response(200, text="not json")) as http:
        out = await call_once("http://mock/x", {}, http=http)
    assert out["error_class"] == "decode_error"
    assert out["payload"] is None


async def test_call_many_preserves_order_and_isolates_failures():
    async def worker(item):
        if item == 2:
            raise RuntimeError("boom")
        await asyncio.sleep(0.01 * (3 - item))  # finish out of order on purpose
        return {"item": item}

    out = await call_many([0, 1, 2, 3], worker, concurrency=4)
    assert out[0]["item"] == 0 and out[1]["item"] == 1 and out[3]["item"] == 3
    assert out[2]["error_class"] == "RuntimeError"  # one failure never kills the batch


async def test_call_many_progress_and_concurrency_bound():
    active = {"now": 0, "max": 0}
    seen = []

    async def worker(item):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return {"item": item}

    out = await call_many(list(range(8)), worker, concurrency=2,
                          on_progress=lambda d, t: seen.append((d, t)))
    assert len(out) == 8
    assert active["max"] <= 2
    assert seen[-1] == (8, 8)
