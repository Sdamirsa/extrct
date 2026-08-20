"""Flow-to-flow calls as data — authored, validated, content-addressed (call-def/1.0).

One flow calls another over Langflow's own HTTP API. This module owns the handshake so
the canvas components stay thin: a call document is validated here, stamped with a
content uid here, turned into the exact wire request here, and the response is parsed
here. The Flow - Call component wires and reports; any Python caller gets the same
behaviour by importing these functions.

The transport facts below were read from the Langflow 1.11.0 source in the running
container (2026-08-14) — they are the contract this module is written against:

- ``POST /api/v2/workflows`` is one endpoint for both modes: ``mode="sync"`` runs
  inline and returns the outputs; ``mode="background"`` queues a bounded job and
  returns ``{job_id, status, links}``; ``GET /api/v2/workflows?job_id=`` returns
  status AND the reconstructed outputs after completion (the runner persists the
  result into a durable Job row — it survives client disconnects).
- Tweak-key matching differs BY LANE: the sync path matches node id OR display name
  (``process_tweaks`` builds both maps), but the background path matches ``vertex.id``
  ONLY — which is why ``execute_call`` resolves the delivery node to real node ids
  before sending (see ``build_request``).
- The v1 ``POST /api/v1/webhook/<flow>`` route finds trigger nodes by a
  case-sensitive substring — ``"Webhook" in node.id`` — and injects the raw request
  body into their input field literally named ``data``. That route is fire-and-forget
  (202, no job handle, unbounded ``asyncio.create_task``); the v2 background mode is
  the audited lane, v1 webhook is kept for external one-line callers.
- Sync component methods run under ``asyncio.to_thread`` (component.py:1397), so a
  blocking self-call from inside a flow cannot deadlock the single-process server.
  The worker pool is bounded (~24 threads on this box): depth-1 calls are safe,
  deep synchronous call chains are not a supported pattern.

Identity: ``call_uid`` names the AUTHORED call — target, mode, payload, delivery,
timeouts. Deployment never leaks into it: ``base_url`` and the API key are resolved
from the environment (``EXTRCT_LANGFLOW_URL`` / ``EXTRCT_LANGFLOW_API_KEY``) inside
``execute_call``, after the uid exists, and both raise on sight if authored into the
document. Same JSON, same call_uid, any machine.

Credentials cannot ride a call document (same red line as client-def): the API key
lives in ``deploy/.env``, reaches the container as environment, and is attached to the
request at send time only.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from .hashing import canonical_json, content_uid

CALL_MODEL_VERSION = "call-def/1.0"

MODES = ("wait", "post", "check")

# Deployment and credentials: never content, never authored. Each raises with the
# instruction of where the value actually lives.
FORBIDDEN_CALL_KEYS = {
    "api_key": "the API key lives in deploy/.env as EXTRCT_LANGFLOW_API_KEY and is attached at send time",
    "x_api_key": "the API key lives in deploy/.env as EXTRCT_LANGFLOW_API_KEY and is attached at send time",
    "authorization": "auth headers are built at send time from EXTRCT_LANGFLOW_API_KEY",
    "base_url": "the server address is deployment, not content — set EXTRCT_LANGFLOW_URL (or the component's Base URL override)",
}

# Field -> (type check, default). The document always normalizes to ALL fields filled,
# so a stored call replays identically even if defaults change later (replayability: the document
# is complete, not sparse).
_DEFAULTS: dict[str, Any] = {
    "mode": "wait",
    "target": "",
    "payload": {},
    "delivery_node": "Flow - Trigger",
    "delivery_field": "data",
    "input_value": "",
    "output_ids": [],
    "session_id": "",
    "timeout_s": 600.0,
    "job_id": "",
}

_STR_FIELDS = ("mode", "target", "delivery_node", "delivery_field", "input_value", "session_id", "job_id")


def _err(msg: str) -> ValueError:
    return ValueError(f"call-def: {msg}")


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def call_definition(raw: Any) -> dict:
    """Validate a call document, fill defaults, stamp ``call_uid``. Loud by design:
    an agent's typo must die at authoring time, not surface as a provider 404."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _err(f"document is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise _err(f"document must be a JSON object, got {type(raw).__name__}")

    doc = dict(raw)
    doc.pop("version", None)
    doc.pop("call_uid", None)  # always recomputed — the wire is never trusted

    for key in doc:
        low = str(key).lower().replace("-", "_")
        if low in FORBIDDEN_CALL_KEYS:
            raise _err(f"{key!r} may not be authored: {FORBIDDEN_CALL_KEYS[low]}")
    unknown = sorted(set(doc) - set(_DEFAULTS))
    if unknown:
        raise _err(f"unknown key(s) {unknown}; allowed: {sorted(_DEFAULTS)}")

    out: dict[str, Any] = {"version": CALL_MODEL_VERSION}
    for key, default in _DEFAULTS.items():
        value = doc.get(key, default)
        if value is None:
            value = default
        if key in _STR_FIELDS:
            if not isinstance(value, str):
                raise _err(f"{key}: expected a string, got {value!r}")
            value = value.strip()
        elif key == "payload":
            if isinstance(value, str):
                try:
                    value = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError as exc:
                    raise _err(f"payload: not valid JSON ({exc}): {value[:200]!r}") from None
            if not isinstance(value, dict):
                raise _err(f"payload: top level must be a JSON object, got {type(value).__name__}")
        elif key == "output_ids":
            if isinstance(value, str):
                value = [x.strip() for x in value.split(",") if x.strip()]
            if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                raise _err(f"output_ids: expected a list of strings, got {value!r}")
        elif key == "timeout_s":
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    raise _err(f"timeout_s: expected a number, got {value!r}") from None
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise _err(f"timeout_s: expected a positive number, got {value!r}")
            value = float(value)
        out[key] = value

    if out["mode"] not in MODES:
        raise _err(f"mode {out['mode']!r} not in {MODES}")
    if out["mode"] in ("wait", "post") and not out["target"]:
        raise _err("target is required for wait/post: a flow id (UUID) — endpoint names are v1-only, see build_request")
    if out["mode"] in ("wait", "post") and not is_uuid(out["target"]):
        raise _err(
            f"target {out['target']!r} is not a flow id (UUID). Measured on 1.11: the v2 endpoint "
            "returns 422 for endpoint names — they work only on the v1 webhook route. Copy the "
            "flow id from the flow's URL or API access pane."
        )
    if out["mode"] == "check" and not out["job_id"]:
        raise _err("job_id is required for check mode (it comes from a previous post-mode Result)")
    if out["mode"] in ("wait", "post") and not out["delivery_node"] and out["payload"]:
        raise _err("payload set but delivery_node empty: name the receiving node (default 'Flow - Trigger')")
    if out["mode"] in ("wait", "post") and not out["delivery_field"] and out["payload"]:
        raise _err("payload set but delivery_field empty: name the receiving field (default 'data' — "
                   "also what the v1 webhook route injects into)")

    out["call_uid"] = content_uid({k: v for k, v in out.items() if k != "version"})
    return out


def build_request(doc: dict, delivery_ids: list[str] | None = None) -> dict:
    """The exact wire request for a validated document: {method, path, params, json}.
    Pure — no environment, no network — so a stored call is replayable byte-for-byte.

    ``delivery_ids``: resolved NODE IDS for the payload tweak (from
    ``resolve_delivery``). This matters because the two server paths match tweak keys
    differently (measured 2026-08-14): sync matches node id OR display name
    (process_tweaks), but background/streaming builds from the DB and matches
    ``vertex.id`` ONLY (langflow/api/build.py:540) — a display-name key is silently
    dropped there and the trigger fires empty. Passing resolved ids makes both modes
    behave identically. Without them (offline/pure callers) the raw delivery_node
    rides as the key — fine for sync, wrong for post."""
    mode = doc["mode"]
    if mode == "check":
        return {"method": "GET", "path": "/api/v2/workflows", "params": {"job_id": doc["job_id"]}, "json": None}

    body: dict[str, Any] = {
        "flow_id": doc["target"],
        "mode": "sync" if mode == "wait" else "background",
        "input_value": doc["input_value"],
    }
    if doc["payload"]:
        # canonical_json, not json.dumps: identical payload -> identical wire bytes on
        # any machine, so replay comparisons stay exact. Every resolved node gets
        # the same body — mirroring the v1 webhook route's inject-into-every-trigger.
        keys = delivery_ids or [doc["delivery_node"]]
        wire_payload = canonical_json(doc["payload"])
        body["tweaks"] = {k: {doc["delivery_field"]: wire_payload} for k in keys}
    if doc["session_id"]:
        body["session_id"] = doc["session_id"]
    if doc["output_ids"] and mode == "wait":  # ignored by the server in background mode
        body["output_ids"] = doc["output_ids"]
    return {"method": "POST", "path": "/api/v2/workflows", "params": None, "json": body}


def resolve_delivery(doc: dict, client: Any) -> list[str]:
    """Resolve delivery_node (node id, display name, or component type) to the target
    flow's REAL node ids, via one GET of the flow. Two jobs in one call: uniform tweak
    delivery across sync/background (see build_request), and a loud early check that
    the target flow actually contains the receiving node."""
    resp = client.get(f"/api/v1/flows/{doc['target']}")
    if resp.status_code == 404:
        raise _err(f"target flow {doc['target']!r} not found — {_hint_for_status(404, doc)}")
    if resp.status_code >= 400:
        raise _err(f"HTTP {resp.status_code} fetching target flow — {_hint_for_status(resp.status_code, doc)}")
    nodes = ((resp.json().get("data") or {}).get("nodes")) or []
    want = doc["delivery_node"]
    ids = []
    for node in nodes:
        data = node.get("data") or {}
        display = (data.get("node") or {}).get("display_name")
        if want in (node.get("id"), display, data.get("type")):
            ids.append(node["id"])
    if not ids:
        have = [f"{n.get('id')} ({((n.get('data') or {}).get('node') or {}).get('display_name')})"
                for n in nodes][:12]
        raise _err(
            f"target flow has no node matching {want!r} (by id, display name, or type). "
            f"Nodes present: {have}. Drag a Flow - Trigger into the target flow, or point "
            "delivery_node at the right node."
        )
    return sorted(ids)


def _hint_for_status(status: int, doc: dict) -> str:
    if status in (401, 403):
        return ("the API key was refused. Check EXTRCT_LANGFLOW_API_KEY in deploy/.env matches a key from "
                "Langflow Settings -> API Keys, and that the container was recreated after editing .env "
                "(env is read at boot).")
    if status == 404 and doc.get("mode") == "check":
        return "no job with that job_id (jobs are per-user; was it created with the same API key?)"
    if status == 404:
        return "flow not found — check the flow id (UUID) in the target field."
    if status == 422:
        return "the server rejected the request shape (mode/output_ids/protocol); the body above says which field."
    return "unexpected status; body above."


def parse_wire(doc: dict, http_status: int, body: Any) -> dict:
    """Normalize a v2 workflows response. Raises on HTTP errors with a what-to-DO hint.
    Statuses come back LOWERCASE (measured 2026-08-14: 'queued', 'in_progress',
    'completed', 'failed') — compare accordingly."""
    if http_status >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        # A FAILED job answers the status GET with HTTP 500 + code JOB_FAILED (and a
        # timed-out one with 408 + EXECUTION_TIMEOUT) — measured 2026-08-14. That is a
        # legitimate ANSWER to a check, not a malformed call: surface it as a result the
        # flow can branch on, with the target's own error detail carried in errors.
        if isinstance(detail, dict) and detail.get("code") in ("JOB_FAILED", "EXECUTION_TIMEOUT"):
            return {
                "kind": "result",
                "status": "failed" if detail["code"] == "JOB_FAILED" else "timed_out",
                "flow_id": detail.get("flow_id"),
                "job_id": detail.get("job_id"),
                "session_id": None,
                "output_text": None,
                "output_reason": "failed",
                "outputs": None,
                "errors": [detail.get("error_detail") or detail.get("message")],
            }
        raise _err(f"HTTP {http_status} from Langflow: {str(detail)[:300]} — {_hint_for_status(http_status, doc)}")
    if not isinstance(body, dict):
        raise _err(f"unexpected non-JSON-object response: {str(body)[:200]!r}")

    kind = body.get("object")
    if kind == "job":
        return {
            "kind": "job",
            "job_id": str(body.get("job_id", "")),
            "status": body.get("status"),
            "flow_id": body.get("flow_id"),
            "links": body.get("links", {}),
            "errors": body.get("errors", []),
        }
    if kind == "response":
        output = body.get("output") or {}
        return {
            "kind": "result",
            "status": body.get("status"),
            "flow_id": body.get("flow_id"),
            "job_id": body.get("job_id"),
            "session_id": body.get("session_id"),
            "output_text": output.get("text"),
            "output_reason": output.get("reason"),
            "outputs": body.get("outputs"),
            "errors": body.get("errors", []),
        }
    raise _err(f"unrecognized response shape (object={kind!r}, keys={sorted(body)[:12]})")


def resolve_deployment(base_url: str | None = None, api_key: str | None = None) -> tuple[str, str]:
    """Environment resolution, AFTER any uid is stamped (deployment is not content).
    Missing key raises with the full setup instruction — a silent unauthenticated call
    would just be a confusing 403 later."""
    base = (base_url or "").strip() or os.environ.get("EXTRCT_LANGFLOW_URL", "").strip() or "http://localhost:7860"
    key = (api_key or "").strip() or os.environ.get("EXTRCT_LANGFLOW_API_KEY", "").strip()
    if not key:
        raise _err(
            "no API key. Create one in Langflow Settings -> API Keys, add "
            "EXTRCT_LANGFLOW_API_KEY=<key> to deploy/.env, then "
            "`docker compose --profile prototype up -d` (env is read at boot; restart is not enough)."
        )
    return base.rstrip("/"), key


def execute_call(
    doc: dict,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: Any = None,
) -> dict:
    """Send one validated call. Returns the parsed result plus transport facts
    (http_status, latency_ms, base_url). ``transport`` is injectable (httpx transport
    object) so tests never need a live server — same pattern as the runner."""
    import httpx  # deferred: everything above works without httpx installed

    base, key = resolve_deployment(base_url, api_key)
    started = time.monotonic()
    delivery_ids: list[str] | None = None
    try:
        with httpx.Client(base_url=base, timeout=doc["timeout_s"], transport=transport,
                          headers={"x-api-key": key}) as client:
            if doc["mode"] in ("wait", "post") and doc["payload"]:
                delivery_ids = resolve_delivery(doc, client)
            req = build_request(doc, delivery_ids)
            resp = client.request(req["method"], req["path"], params=req["params"], json=req["json"])
    except httpx.TimeoutException as exc:
        # ReadTimeout subclasses HTTPError — without this branch a slow wait-mode run
        # would be misreported as "unreachable" with container-networking advice.
        raise _err(
            f"timed out after {doc['timeout_s']}s ({type(exc).__name__}). In wait mode the "
            "connection stays open for the target flow's WHOLE run — raise timeout_s, or "
            "switch to post + check for long-running targets."
        ) from None
    except httpx.HTTPError as exc:
        raise _err(
            f"Langflow API unreachable at {base}: {type(exc).__name__}: {exc}. From inside the "
            "langflow container the server is http://localhost:7860; from the host it is "
            "http://127.0.0.1:7860."
        ) from None
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = resp.text
    result = parse_wire(doc, resp.status_code, body)
    result.update({"http_status": resp.status_code, "latency_ms": latency_ms, "base_url": base,
                   "call_uid": doc.get("call_uid"), "delivery_ids": delivery_ids})
    return result


RESPONSE_MODEL_VERSION = "flow-response/1.0"

# The canonical order sections appear in the envelope — deterministic bytes.
RESPONSE_SECTIONS = ("extracted", "certainty", "grounding", "report", "merged")


def _section_uids(section: Any) -> list[str]:
    """Collect run_uid values from one section, tolerating the flat-combine artifact
    where a key union turned a scalar into a list of duplicates."""
    if not isinstance(section, dict):
        return []
    raw = section.get("run_uid")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v) for v in values if isinstance(v, str) and v]


def build_flow_response(sections: dict, extra: list | None = None) -> dict:
    """One canonical envelope for a flow's answer — the outbound half of the trigger
    handshake. Built for the caller on the other side of a Flow - Call, not for a
    human: sections stay NAMESPACED (a flat key-union of extracted + certainty +
    grounding is how run_uid became a list of duplicates on a real canvas,
    2026-08-14), run_uid is reconciled to ONE scalar or refused loudly (two runs in
    one response is a wiring bug, not a merge case), and serialization is
    canonical_json — deterministic bytes, no markdown fence.

    ``sections``: {name: dict | None} — None/empty sections are simply absent, and
    the ``present`` list declares what made it in (absence is not evidence, independent measurement).
    ``extra``: anything else, kept as a list under sections.extra.
    """
    known = {}
    for name in RESPONSE_SECTIONS:
        value = sections.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise _err(f"flow-response: section {name!r} must be a JSON object, got {type(value).__name__}")
        if value:
            known[name] = value
    extras = [x for x in (extra or []) if x]
    if not known and not extras:
        raise _err("flow-response: nothing to respond with — wire at least one section (Extracted, "
                   "Certainty, Grounding, Report, Merged) or an Extra input")

    uids = []
    for section in list(known.values()) + [x for x in extras if isinstance(x, dict)]:
        for uid in _section_uids(section):
            if uid not in uids:
                uids.append(uid)
    if len(uids) > 1:
        raise _err(f"flow-response: sections carry DIFFERENT run_uids {uids} — one response must "
                   "describe one run. Check the wiring: some input comes from another run.")

    flagged = sorted(
        name for name, section in known.items()
        if section.get("ok") is False or section.get("error")
    )

    body: dict[str, Any] = {
        "version": RESPONSE_MODEL_VERSION,
        "run_uid": uids[0] if uids else None,
        "present": sorted(known) + (["extra"] if extras else []),
        "flagged": flagged,
        "sections": {**{k: known[k] for k in RESPONSE_SECTIONS if k in known},
                     **({"extra": extras} if extras else {})},
    }
    body["response_uid"] = content_uid(body)
    return body


def parse_trigger_payload(raw: Any, *, allow_empty: bool = False) -> dict:
    """The trigger side of the handshake. STRICTER than the stock Webhook component on
    purpose: stock silently converts invalid JSON into {"payload": "<raw text>"}, so a
    malformed sender fails invisibly downstream; this raises with the actual parse
    error. Empty means not-triggered — allowed only when the flow says so."""
    text = str(raw or "").strip()
    if not text:
        if allow_empty:
            return {}
        raise _err(
            "no payload. This node receives JSON one of two ways: "
            "POST /api/v1/webhook/<flow> with the JSON as the raw body, or "
            "POST /api/v2/workflows with tweaks {\"Flow - Trigger\": {\"data\": \"<json>\"}}. "
            "For a canvas dry-run, paste JSON into the Payload field or set Allow Empty."
        )
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _err(
            f"payload is not valid JSON ({exc}). First 200 chars: {text[:200]!r}. "
            "(The stock Webhook component would silently wrap this as {'payload': text}; "
            "this trigger refuses instead so the sender's bug is visible here.)"
        ) from None
    if not isinstance(obj, dict):
        raise _err(f"payload top level must be a JSON object, got {type(obj).__name__}")
    return obj
