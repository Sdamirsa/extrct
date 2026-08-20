"""Transport and concurrency. No Langflow imports anywhere in this package.

`call_many` is the reason this module exists outside a component: whatever ends up
conducting the sweep imports the same function the canvas does, so single-item and
batch cannot drift. See docs/extraction-stack/concurrency.md.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

# Concurrency is a property of the BACKEND, not of the caller. Ollama queues anything
# past OLLAMA_NUM_PARALLEL server-side, so oversubscribing adds latency variance and
# fires timeouts on requests that never started.
DEFAULT_CONCURRENCY = {"ollama": 4, "vllm": 16, "openrouter": 8}

RETRYABLE_STATUS = frozenset({429, 502, 504})


class TransportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, error_class: str = "transport"):
        super().__init__(message)
        self.status = status
        self.error_class = error_class


async def call_once(
    url: str,
    body: dict[str, Any],
    *,
    http: httpx.AsyncClient,
    headers: dict[str, str] | None = None,
    timeout_s: float = 600.0,
    max_retries: int = 2,
    retry_on_5xx: bool = False,
    retryable_status: frozenset | None = None,
    backoff_cap_s: float = 30.0,
) -> dict[str, Any]:
    """One HTTP call with bounded retry. Returns a transport envelope, never raises on 4xx/5xx.

    Retries are recorded, never hidden - they are part of the run's latency and cost.
    5xx is NOT retried by default: on OpenRouter a 503 is a routing rejection produced by
    `require_parameters`, and blindly retrying it is how a silent fallback happens.

    `retryable_status` overrides the default set for callers with a measured policy —
    the batch lane retries {429,500,502,503,524,529} per the 2026-08-17 OpenRouter
    research (Retry-After is USUALLY ABSENT on 429, so the jittered schedule never
    depends on it). Single-run callers keep the conservative default untouched.
    The envelope carries `generation_id` (OpenRouter's X-Generation-Id response
    header) when present — the per-row audit/reconciliation handle.
    """
    retryable_set = RETRYABLE_STATUS if retryable_status is None else retryable_status
    attempt = 0
    started = time.monotonic()
    last: dict[str, Any] = {}

    while True:
        attempt += 1
        try:
            resp = await http.post(url, json=body, headers=headers or {}, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - transport failures are data here
            last = {
                "http_status": None,
                "error_class": type(exc).__name__,
                "error": str(exc)[:400],
                "payload": None,
            }
            if attempt > max_retries:
                break
            await asyncio.sleep(min(_backoff(attempt), backoff_cap_s))
            continue

        retryable = resp.status_code in retryable_set or (retry_on_5xx and resp.status_code >= 500)
        gen_id = resp.headers.get("X-Generation-Id")
        if resp.status_code >= 400:
            last = {
                "http_status": resp.status_code,
                "error_class": "http_error",
                "error": resp.text[:400],
                "payload": None,
                "retry_after_s": _retry_after(resp),
            }
            if retryable and attempt <= max_retries:
                await asyncio.sleep(min(last.get("retry_after_s") or _backoff(attempt), backoff_cap_s))
                continue
            break

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            last = {
                "http_status": resp.status_code,
                "error_class": "decode_error",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "payload": None,
            }
            break

        last = {"http_status": resp.status_code, "error_class": None, "error": None, "payload": payload}
        if gen_id:
            last["generation_id"] = gen_id
        break

    last["latency_ms"] = int((time.monotonic() - started) * 1000)
    last["retries"] = attempt - 1
    return last


def _backoff(attempt: int) -> float:
    """Exponential with jitter. Jitter matters: without it a batch retries in lockstep."""
    return min(30.0, (2 ** (attempt - 1))) * (0.5 + random.random())  # noqa: S311 - not crypto


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def call_many(
    items: Sequence[Any],
    worker: Callable[[Any], Awaitable[dict[str, Any]]],
    *,
    concurrency: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Run `worker` over `items` with bounded concurrency and total failure isolation.

    Guarantees:
      - results[i] corresponds to items[i]. Completion order is nondeterministic under
        concurrency, so never rely on it - a wrong join here silently corrupts a sweep.
      - one item's exception never kills the batch; it becomes a result with an error.
      - cancellation propagates, so an aborted run does not leave requests in flight
        burning credits.
    """
    if concurrency < 1:
        msg = "concurrency must be >= 1"
        raise ValueError(msg)

    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any] | None] = [None] * len(items)
    done = 0
    lock = asyncio.Lock()

    async def run(index: int, item: Any) -> None:
        nonlocal done
        async with sem:
            try:
                results[index] = await worker(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                results[index] = {
                    "http_status": None,
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:400],
                    "payload": None,
                    "latency_ms": 0,
                    "retries": 0,
                }
        async with lock:
            done += 1
            if on_progress:
                on_progress(done, len(items))

    await asyncio.gather(*(run(i, it) for i, it in enumerate(items)))
    return [r if r is not None else {"error_class": "missing", "payload": None} for r in results]


async def get_json(url: str, *, http: httpx.AsyncClient, timeout_s: float = 15.0) -> Any:
    resp = await http.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()
