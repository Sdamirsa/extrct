"""Batch grid (batch-def/1.0) - the benchmark lane: inputs × configs × schemas.

Design decisions, each traced to measured behaviour:

- OpenRouter ONLY in v1: every provider config is PINNED to an endpoint tag -
  order+no-fallbacks pinning is also what disables OpenRouter's sticky routing, which
  would otherwise silently route a whole benchmark to whichever provider served the
  first row. Unpinned or egress-refused configs become config_error CELLS for that
  column, never a dead batch. (For other providers, `Extractor.extract_many` is the
  multi-document lane; the grid generalizes when a second provider earns a measured
  retry/pinning policy.)
- Cell identity needs no new scheme: run_uid = content_uid({request_uid, input_sha256})
  already separates all three axes. The optional input id is a JOIN LABEL in
  run_metadata - never hashed, so re-labelling never re-bills.
- One request path: apply_client_overrides -> pipeline.compose ->
  pipeline.execute_single. Never a third inline fork.
- runner.call_many drives execution: index-safe rows, per-item isolation, a progress
  hook.
- Retry set {429,500,502,503,524,529} with jittered backoff capped 60s, 5 attempts;
  Retry-After honored when present and never depended on - it is USUALLY ABSENT on
  OpenRouter 429s. 429s are normal operating condition; they are counted.
- stop_on_failure_rate circuit breaker: after min_sample completions, a failure
  fraction over the threshold stops DISPATCH (in-flight cells finish); undispatched
  cells become aborted_circuit_breaker rows - kept, absence is not evidence.
- Audit per cell: X-OpenRouter-Metadata is requested (the metadata rides the stored
  provider_response) and X-Generation-Id lands in the transport envelope -> run row.
- BATCH_CAP=2000 cells - deliberately above config.GRID_CAP=500: these functions are a
  conductor's import surface; an interactive caller is one thin consumer.
"""

from __future__ import annotations

from typing import Any, Callable

from . import config as runcfg
from . import pipeline, telemetry
from .hashing import content_uid, sha256_text
from .providers import get_provider, openrouter
from .storage import NullRunStore, RunStore, run_row_from_single

BATCH_MODEL_VERSION = "batch-def/1.0"

BATCH_CAP = 2000  # cells; above this belongs to a real conductor even with this module

# The measured OpenRouter batch retry policy (single-run default is narrower).
BATCH_RETRYABLE = frozenset({429, 500, 502, 503, 524, 529})
BATCH_BACKOFF_CAP_S = 60.0
BATCH_MAX_RETRIES = 5

SETTINGS_DEFAULTS: dict[str, Any] = {
    "concurrency": 8,            # engineering judgment, NOT vendor guidance (none exists)
    "max_retries": BATCH_MAX_RETRIES,
    "stop_on_failure_rate": 0.5,
    "min_sample": 10,
    "skip_completed": True,
    "run_tags": "batch",
    "log_to_db": True,
    "store_input_text": False,   # the privacy default: hash, don't keep
}

AUDIT_HEADERS = {"X-OpenRouter-Metadata": "enabled"}


def _err(msg: str) -> ValueError:
    return ValueError(f"batch-def: {msg}")


def rows_from(value: Any, list_key: str) -> list[dict]:
    """The one polymorphic list reader for all three axes: DataFrame-like -> records;
    dict -> its `list_key` list (or the dict itself as a single row); list -> as is.
    Loud on anything else."""
    if value is None:
        return []
    if hasattr(value, "to_dict") and hasattr(value, "columns"):
        return value.to_dict(orient="records")
    if isinstance(value, dict):
        inner = value.get(list_key)
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        return [value]
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    raise _err(f"{list_key}: expected a DataFrame, a dict, or a list of dicts - got {type(value).__name__}")


def normalize_inputs(rows: list[dict]) -> list[dict]:
    """[{input_id?, text}] -> [{input_id, text, input_sha256}]. The sha is identity;
    the id is a label (run_metadata only, never hashed)."""
    out = []
    for i, r in enumerate(rows):
        text = str(r.get("text") or r.get("input") or "").strip()
        if not text:
            raise _err(f"inputs[{i}]: empty text (columns seen: {sorted(r)})")
        out.append({"input_id": str(r.get("input_id") or r.get("id") or "").strip() or None,
                    "text": text, "input_sha256": sha256_text(text)})
    ids = [r["input_id"] for r in out if r["input_id"]]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise _err(f"duplicate input_id values {dupes} - ids must be unique to be a usable join label")
    return out


def normalize_configs(rows: list[dict]) -> list[dict]:
    """Sweep grid rows ({config_uid, config(json|dict)}) or bare config dicts
    -> [{config_uid, config}] with uids recomputed when absent (never trusted blind)."""
    import json as _json
    out = []
    for i, r in enumerate(rows):
        cfg = r.get("config", r if "config_uid" not in r else None)
        if isinstance(cfg, str):
            try:
                cfg = _json.loads(cfg)
            except _json.JSONDecodeError as exc:
                raise _err(f"configs[{i}].config: not valid JSON ({exc})") from None
        if not isinstance(cfg, dict):
            raise _err(f"configs[{i}]: no config object (columns seen: {sorted(r)})")
        cfg = {k: v for k, v in cfg.items() if k != "config_uid"}
        out.append({"config_uid": r.get("config_uid") or content_uid(cfg), "config": cfg})
    return out


def normalize_schemas(value: Any) -> list[dict]:
    """A single schema envelope, a list of envelopes, or rows of {schema_uid, envelope}
    -> [{schema_uid, envelope}]."""
    import json as _json
    if isinstance(value, dict) and "schema" in value:  # one bare envelope
        return [{"schema_uid": value.get("schema_uid"), "envelope": value}]
    rows = rows_from(value, "schemas")
    out = []
    for i, r in enumerate(rows):
        env = r.get("envelope", r if "schema" in r else None)
        if isinstance(env, str):
            try:
                env = _json.loads(env)
            except _json.JSONDecodeError as exc:
                raise _err(f"schemas[{i}].envelope: not valid JSON ({exc})") from None
        if not isinstance(env, dict) or "schema" not in env:
            raise _err(f"schemas[{i}]: no schema envelope (needs schema.schema_envelope output)")
        out.append({"schema_uid": r.get("schema_uid") or env.get("schema_uid"), "envelope": env})
    return out


def plan_batch(base_client: dict, inputs: list[dict], configs: list[dict],
               schemas: list[dict], settings: dict | None = None) -> dict:
    """Validate everything BEFORE anything runs and return the manifest - the declared
    half of the declared-vs-executed pair. Loud on: non-OpenRouter provider, empty
    axes, cap breach, unknown settings. A config missing its endpoint pin or refused by
    the egress gate becomes a per-config error marker - the RUN degrades per cell."""
    st = {**SETTINGS_DEFAULTS, **(settings or {})}
    unknown = sorted(set(st) - set(SETTINGS_DEFAULTS))
    if unknown:
        raise _err(f"unknown setting(s) {unknown}; allowed: {sorted(SETTINGS_DEFAULTS)}")
    if base_client.get("provider") != "openrouter":
        raise _err("the batch grid lane is OpenRouter-only in v1 (endpoint pinning and the "
                   "retry policy are OpenRouter-measured) - wire an OpenRouter client, or "
                   "use Extractor.extract_many for other providers")
    if not inputs or not configs or not schemas:
        raise _err(f"empty axis: inputs={len(inputs)}, configs={len(configs)}, schemas={len(schemas)}")

    cells = len(inputs) * len(configs) * len(schemas)
    if cells > BATCH_CAP:
        raise _err(f"{cells} cells exceeds BATCH_CAP={BATCH_CAP} "
                   f"({len(inputs)} inputs x {len(configs)} configs x {len(schemas)} schemas). "
                   "Split the run, or take it to a conductor - this module is its import surface.")

    config_checks = []
    for c in configs:
        entry = {"config_uid": c["config_uid"], "ok": True, "error": ""}
        try:
            kwargs, _ = runcfg.apply_client_overrides(
                "openrouter", dict(base_client.get("spec") or {}), c["config"])
            spec = openrouter.OpenRouterSpec(**kwargs)
            if not str(getattr(spec, "endpoint_tag", "") or "").strip():
                raise _err("no endpoint_tag: every batch config must PIN an endpoint - pinning "
                           "is also what disables sticky routing. Copy a tag from "
                           "openrouter.list_endpoints_annotated.")
            openrouter.check_egress(spec)
        except Exception as exc:  # noqa: BLE001 - recorded, the run degrades per column
            entry.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        config_checks.append(entry)

    body = {"version": BATCH_MODEL_VERSION,
            "inputs": [{"input_id": r["input_id"], "input_sha256": r["input_sha256"]} for r in inputs],
            "configs": [c["config_uid"] for c in configs],
            "schemas": [s["schema_uid"] for s in schemas],
            "settings": {k: st[k] for k in sorted(st)}}
    manifest = {
        **body,
        "batch_uid": content_uid(body),
        "cells": cells,
        "axis_sizes": {"inputs": len(inputs), "configs": len(configs), "schemas": len(schemas)},
        "config_checks": config_checks,
        "bad_configs": [c["config_uid"] for c in config_checks if not c["ok"]],
    }
    return manifest


def _cell_id(inp: dict, config_uid: str, schema_uid: str | None) -> str:
    left = inp["input_id"] or inp["input_sha256"][:8]
    return f"{left}|{config_uid}|{schema_uid or 'schema?'}"


async def run_batch(base_client: dict, inputs: list[dict], configs: list[dict],
                    schemas: list[dict], settings: dict | None = None, *,
                    http: Any, store: RunStore | None = None,
                    on_progress: Callable[[int, int], None] | None = None,
                    log: Callable[[str], None] | None = None) -> dict:
    """Execute the grid. Returns {manifest, rows, report}. One row per cell, ALWAYS -
    ok, failed, config_error, skipped_completed, or aborted_circuit_breaker.

    Traced as one `extrct.batch` root span (per-cell `chat` spans nest under it via
    the shared request path); identities and counts only, never text."""
    result = None
    with telemetry.span("extrct.batch") as sp:
        result = await _run_batch_impl(base_client, inputs, configs, schemas, settings,
                                       http=http, store=store, on_progress=on_progress, log=log)
        report = result["report"]
        telemetry.set_attrs(sp, {
            "extrct.batch_uid": report["batch_uid"],
            "extrct.cells": report["cells"],
            "extrct.cells.resumed": report["resumed"],
            "extrct.cost_usd": report["total_cost_usd"],
            "extrct.retries": report["total_retries"],
            "extrct.circuit_breaker_tripped": report["circuit_breaker_tripped"],
            **{f"extrct.cells.{k}": v for k, v in report["by_status"].items()},
        })
    return result


async def _run_batch_impl(base_client: dict, inputs: list[dict], configs: list[dict],
                          schemas: list[dict], settings: dict | None = None, *,
                          http: Any, store: RunStore | None = None,
                          on_progress: Callable[[int, int], None] | None = None,
                          log: Callable[[str], None] | None = None) -> dict:
    from .runner import call_many

    log = log or (lambda _m: None)
    manifest = plan_batch(base_client, inputs, configs, schemas, settings)
    st = {**SETTINGS_DEFAULTS, **(settings or {})}
    store = store if store is not None else NullRunStore()
    log_to_db = bool(st["log_to_db"]) and not isinstance(store, NullRunStore)
    bad = set(manifest["bad_configs"])
    checks = {c["config_uid"]: c for c in manifest["config_checks"]}

    # ---- assemble cells (deterministic order: input-major, then config, then schema)
    cells: list[dict] = []
    for inp in inputs:
        for c in configs:
            for s in schemas:
                cells.append({"input": inp, "config": c, "schema": s,
                              "cell_id": _cell_id(inp, c["config_uid"], s["schema_uid"])})

    # ---- prep: effective client per config (once per config, not per cell)
    eff_clients: dict[str, dict] = {}
    for c in configs:
        if c["config_uid"] in bad:
            continue
        kwargs, _ = runcfg.apply_client_overrides(
            "openrouter", dict(base_client.get("spec") or {}), c["config"])
        eff_clients[c["config_uid"]] = {**base_client, "spec": kwargs}

    # ---- resume: rebuild each viable cell's run_uid without calling anything
    prov = get_provider("openrouter")
    prefetched: dict[str, dict] = {}
    if st["skip_completed"] and log_to_db:
        try:
            uid_by_cell: dict[str, str] = {}
            for cell in cells:
                cu = cell["config"]["config_uid"]
                if cu in bad:
                    continue
                composed = pipeline.compose(eff_clients[cu], cell["schema"]["envelope"], None, None)
                spec = prov.make_spec(dict(composed["client"]["spec"] or {}))
                body, res = prov.build_request(spec, cell["input"]["text"], composed["envelope_sent"])
                rec = prov.request_record(spec, cell["input"]["text"], composed["envelope_sent"], body, res)
                uid_by_cell[cell["cell_id"]] = content_uid(
                    {"request_uid": rec["request_uid"], "input": rec["input"]["input_sha256"]})
            done = await store.find_completed(list(uid_by_cell.values()))
            stored = await store.fetch_extracted(sorted(done)) if done else {}
            for cid, uid in uid_by_cell.items():
                if uid in done:
                    prefetched[cid] = {"run_uid": uid, "extracted": stored.get(uid)}
            if prefetched:
                log(f"resume: {len(prefetched)} cell(s) already completed - refetched, not re-billed")
        except Exception as exc:  # noqa: BLE001 - resume is an optimization, never a blocker
            log(f"resume check failed ({type(exc).__name__}: {exc}); running all cells")

    if log_to_db:
        try:
            await store.ensure_schema()  # ONCE per batch, not per cell
        except Exception as exc:  # noqa: BLE001
            log(f"ensure_schema failed ({type(exc).__name__}); cell logging may fail too")

    # ---- circuit breaker state
    breaker = {"completed": 0, "failed": 0, "tripped": False}
    tags = [t.strip() for t in str(st["run_tags"]).split(",") if t.strip()]

    async def worker(cell: dict) -> dict:
        cu = cell["config"]["config_uid"]
        su = cell["schema"]["schema_uid"]
        row = {"cell_id": cell["cell_id"], "input_id": cell["input"]["input_id"],
               "input_sha256": cell["input"]["input_sha256"], "config_uid": cu,
               "schema_uid": su, "batch_uid": manifest["batch_uid"], "run_uid": None,
               "final_status": None, "error": "", "extracted": None, "cost_usd": None,
               "latency_ms": None, "generation_id": None, "http_status": None, "retries": 0}
        if cu in bad:
            row.update({"final_status": "config_error", "error": checks[cu]["error"]})
            return row
        if cell["cell_id"] in prefetched:
            pre = prefetched[cell["cell_id"]]
            import json as _json
            row.update({"final_status": "skipped_completed", "run_uid": pre["run_uid"],
                        "extracted": _json.dumps(pre["extracted"], ensure_ascii=False)
                        if pre["extracted"] else None})
            return row
        if breaker["tripped"]:
            row.update({"final_status": "aborted_circuit_breaker",
                        "error": f"failure rate exceeded {st['stop_on_failure_rate']} "
                                 f"after {breaker['completed']} cells"})
            return row

        try:
            composed = pipeline.compose(eff_clients[cu], cell["schema"]["envelope"], None, None)
            single = await pipeline.execute_single(
                composed["client"], composed["envelope_sent"], cell["input"]["text"], http=http,
                ladder=("json_repair", "coerce"), max_retries=int(st["max_retries"]),
                extra_headers=AUDIT_HEADERS, retryable_status=BATCH_RETRYABLE,
                backoff_cap_s=BATCH_BACKOFF_CAP_S)
        except Exception as exc:  # noqa: BLE001 - one bad cell is one row
            row.update({"final_status": "config_error", "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            return row

        import json as _json
        usage = (single["response"] or {}).get("usage", {})
        cost = usage.get("cost_usd")
        row.update({
            "run_uid": single["run_uid"], "final_status": single["final_status"],
            "error": str(single["transport"].get("error") or "")[:300],
            "extracted": _json.dumps(single["obj"], ensure_ascii=False) if single["obj"] else None,
            "cost_usd": float(cost) if cost not in (None, "") else None,
            "latency_ms": single["transport"].get("latency_ms"),
            "generation_id": single["transport"].get("generation_id"),
            "http_status": single["transport"].get("http_status"),
            "retries": single["transport"].get("retries", 0),
        })

        if log_to_db:
            try:
                metadata = {"batch_uid": manifest["batch_uid"], "cell_id": cell["cell_id"],
                            "config_uid": cu,
                            **({"input_id": cell["input"]["input_id"]}
                               if cell["input"]["input_id"] else {})}
                await store.save_run(run_row_from_single(
                    single, cell["schema"]["envelope"], tags=tags or None,
                    store_input_text=bool(st["store_input_text"]),
                    input_text=cell["input"]["text"], run_metadata=metadata))
                if single["ladder"]:
                    await store.save_attempts(single["run_uid"], single["ladder"]["attempts"])
            except Exception as exc:  # noqa: BLE001 - logging must never lose a cell
                log(f"cell {cell['cell_id']}: DB logging failed ({type(exc).__name__}: {exc})")

        ok = single["final_status"] in ("ok", "repaired")
        breaker["completed"] += 1
        if not ok:
            breaker["failed"] += 1
        if (not breaker["tripped"] and breaker["completed"] >= int(st["min_sample"])
                and breaker["failed"] / breaker["completed"] > float(st["stop_on_failure_rate"])):
            breaker["tripped"] = True
            log(f"CIRCUIT BREAKER: {breaker['failed']}/{breaker['completed']} failed "
                f"(> {st['stop_on_failure_rate']}) - undispatched cells will be aborted")
        return row

    rows = await call_many(cells, worker, concurrency=max(1, min(int(st["concurrency"]), 16)),
                           on_progress=on_progress)
    # call_many wraps a worker exception into a transport-ish dict; keep row shape stable.
    for i, r in enumerate(rows):
        if "cell_id" not in r:
            rows[i] = {"cell_id": cells[i]["cell_id"], "input_id": cells[i]["input"]["input_id"],
                       "input_sha256": cells[i]["input"]["input_sha256"],
                       "config_uid": cells[i]["config"]["config_uid"],
                       "schema_uid": cells[i]["schema"]["schema_uid"],
                       "batch_uid": manifest["batch_uid"], "run_uid": None,
                       "final_status": "error", "error": str(r.get("error"))[:300],
                       "extracted": None, "cost_usd": None, "latency_ms": None,
                       "generation_id": None, "http_status": None, "retries": 0}

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["final_status"]] = by_status.get(r["final_status"], 0) + 1
    report = {
        "batch_uid": manifest["batch_uid"], "cells": len(rows), "by_status": by_status,
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows if r["cost_usd"]), 8),
        "total_retries": sum(r["retries"] for r in rows),
        "circuit_breaker_tripped": breaker["tripped"],
        "resumed": sum(1 for r in rows if r["final_status"] == "skipped_completed"),
        "bad_configs": manifest["bad_configs"],
        "run_uids": [r["run_uid"] for r in rows if r["run_uid"]],
        "tags": tags,
    }
    return {"manifest": manifest, "rows": rows, "report": report}
