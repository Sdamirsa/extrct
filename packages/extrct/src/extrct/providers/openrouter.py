"""OpenRouter chat completions — capability resolution, request builder, response reader.

Text sent here LEAVES YOUR MACHINE. The egress guard raises rather than warns: every
spec must declare a `data_classification`, and there is deliberately no member of that
vocabulary that permits confidential text. Route through a gateway you control
(`egress_route="gateway"`) or accept direct egress explicitly.

Two behaviours were measured live and shape everything below:

1. `require_parameters: true` genuinely gates on capability. Pinned to an endpoint that
   declares `response_format` but not `structured_outputs`, a json_schema request returns
   404 "No endpoints found that can handle the requested parameters"; with the flag off,
   the same request returns 200. So the default of true converts a silent downgrade into
   a loud routing refusal.

2. `:free` variants have their OWN endpoint list. A model may expose 9 endpoints while
   its `:free` twin exposes 2 entirely different ones. A pin discovered from the paid
   slug 404s against the free id every time, so capability MUST be fetched with the
   exact id being sent, suffix included.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..hashing import canonical_json, content_uid, key_fingerprint, sha256_text
from ..runner import get_json
from .base import Provider

BASE_URL = "https://openrouter.ai/api/v1"

# Emission policy per sampling key.
AUTO, ALWAYS, NEVER = "auto", "always", "never"

# Closed vocabulary. OpenRouter does NOT validate this query param - a typo silently
# returns a different model set instead of an error.
CATALOGUE_FILTERS = (
    "response_format",
    "structured_outputs",
    "seed",
    "temperature",
    "max_tokens",
    "tools",
    "reasoning",
)

# What the author declares about the text being sent. Deliberately closed, and
# deliberately WITHOUT any member meaning "confidential but I'll allow it": text that
# must not leave the machine has no legal value here, so the guard cannot be waved off.
DATA_CLASSES = ("synthetic", "deidentified_aggregate", "public", "approved_for_egress")


class EgressRefused(RuntimeError):
    """Raised before any network call. The guard runs at spec time AND at send time."""


class CapabilityError(RuntimeError):
    """Raised when a model/endpoint cannot be pinned reproducibly."""


@dataclass(frozen=True)
class OpenRouterSpec:
    # --- egress guard (evaluated first, raises) ---
    data_classification: str = ""  # MUST be set; empty raises
    egress_route: str = "gateway"  # gateway | direct
    allow_direct_egress: bool = False
    gateway_url: str = ""
    store_input_text: bool = False

    # --- connection ---
    base_url: str = BASE_URL
    api_key: str = ""  # literal override only; normally empty — see resolve_key()
    api_key_env_var: str = "OPENROUTER_API_KEY"
    timeout_s: int = 120
    retry_on_429: bool = True
    retry_on_5xx: bool = False  # 503 is a routing rejection, not a transient error
    http_referer: str = ""
    attribution_title: str = ""

    # --- model & endpoint (pin both, always) ---
    model: str = ""  # the EXACT id sent, :free suffix included
    endpoint_tag: str = ""  # full tag from endpoints[].tag, never a bare slug
    require_endpoint_pin: bool = True

    # --- routing ---
    require_parameters: bool = True  # proven to gate on real capability
    allow_fallbacks: bool = False
    zdr: bool = True
    data_collection: str = "deny"
    quantizations: tuple[str, ...] = ()

    # --- sampling, tri-state ---
    send_seed: str = AUTO
    seed: int = 42
    send_temperature: str = AUTO
    temperature: float = 0.0
    send_top_p: str = NEVER
    top_p: float = 1.0
    max_output_tokens: int = 2048
    max_output_field: str = "auto"  # auto | max_tokens | max_completion_tokens

    # --- structured output ---
    mode: str = "json_schema"
    schema_name: str = "extract"
    # DISTINCT from the schema builder's `encoding`. This asks the PROVIDER to hard-enforce
    # and does not change the schema at all; encoding changes the schema and its uid.
    # Conflating them would hide an axis inside a flag.
    response_format_strict: bool = True
    schema_in_prompt: bool = False
    system_prompt: str = ""

    # --- logprobs: endpoint-dependent; declared in supported_parameters, gate on it ---
    logprobs: bool = False
    top_logprobs: int = 0

    # --- reasoning ---
    reasoning_control: str = "off"  # off | effort | budget
    reasoning_effort: str = ""
    reasoning_max_tokens: int = 0
    reasoning_exclude: bool = False

    # --- plugins ---
    response_healing: bool = False  # server-side repair; unlogged, so off by default

    # --- accounting ---
    # OpenRouter returns usage.cost ONLY when asked: without usage:{include:true} the
    # response carries token counts but no cost, so cost_usd logs as NULL. Accounting is
    # OpenRouter-side (not forwarded to the provider), so it cannot affect routing.
    usage_accounting: bool = True

    capability: dict = field(default_factory=dict)


def check_egress(spec: OpenRouterSpec) -> None:
    """Raise before anything leaves the process."""
    if spec.data_classification not in DATA_CLASSES:
        msg = (
            "data_classification must be one of "
            f"{DATA_CLASSES}. OpenRouter is outside your machine; no member of this "
            "vocabulary permits confidential text — that is the point."
        )
        raise EgressRefused(msg)
    if spec.egress_route == "direct" and not spec.allow_direct_egress:
        msg = "egress_route='direct' requires allow_direct_egress=True. Prefer a gateway you control."
        raise EgressRefused(msg)
    if spec.egress_route == "gateway" and not spec.gateway_url:
        msg = "egress_route='gateway' but gateway_url is empty. Set it, or accept 'direct' explicitly."
        raise EgressRefused(msg)


async def fetch_catalogue(
    *, http: httpx.AsyncClient, base_url: str = BASE_URL, zdr_only: bool = True, requires: tuple[str, ...] = ()
) -> list[dict]:
    """Public endpoint, CDN-cached 300s. Filters are for DISCOVERY only.

    Model-level `supported_parameters` is the UNION across providers, so it is authoritative
    for "this model can never accept X" and useless for "this request will honour X".
    """
    bad = [r for r in requires if r not in CATALOGUE_FILTERS]
    if bad:
        msg = f"unknown catalogue filter(s) {bad}; OpenRouter silently ignores typos here. Known: {CATALOGUE_FILTERS}"
        raise CapabilityError(msg)
    params = []
    if zdr_only:
        params.append("zdr=true")
    if requires:
        params.append("supported_parameters=" + ",".join(requires))
    url = f"{base_url}/models" + ("?" + "&".join(params) if params else "")
    return (await get_json(url, http=http))["data"]


async def fetch_endpoints(model_id: str, *, http: httpx.AsyncClient, base_url: str = BASE_URL) -> list[dict]:
    """MUST be called with the exact id being sent, ':free' suffix included.

    Measured: the free variant of a model exposes a different provider set entirely, and a
    pin taken from the paid slug 404s against it.
    """
    data = await get_json(f"{base_url}/models/{model_id}/endpoints", http=http)
    return (data.get("data") or {}).get("endpoints") or []


async def resolve_capability(
    model_id: str, endpoint_tag: str, *, http: httpx.AsyncClient, base_url: str = BASE_URL
) -> dict[str, Any]:
    """Pin one endpoint and return the capability record that gating reads."""
    endpoints = await fetch_endpoints(model_id, http=http, base_url=base_url)
    if not endpoints:
        msg = f"{model_id!r} exposes no endpoints. A ':free' variant often has its own, much smaller, set."
        raise CapabilityError(msg)
    chosen = next((e for e in endpoints if e.get("tag") == endpoint_tag), None)
    if chosen is None:
        tags = [e.get("tag") for e in endpoints]
        msg = f"endpoint {endpoint_tag!r} not found for {model_id!r}. Available: {tags}"
        raise CapabilityError(msg)
    supported = list(chosen.get("supported_parameters") or [])
    return {
        "source_url": f"{base_url}/models/{model_id}/endpoints",
        "model_id": model_id,
        "endpoint_tag": chosen.get("tag"),
        "provider_name": chosen.get("provider_name") or chosen.get("name"),
        "quantization": chosen.get("quantization"),
        "context_length": chosen.get("context_length"),
        "max_completion_tokens": chosen.get("max_completion_tokens"),
        "status": chosen.get("status"),
        "supported_parameters": supported,
        "supports_structured_outputs": "structured_outputs" in supported,
        "supports_response_format": "response_format" in supported,
        "gate_effective": True,
        "source_sha256": sha256_text(canonical_json(chosen)),
    }


def _emit(policy: str, key: str, capability: dict) -> bool:
    if policy == ALWAYS:
        return True
    if policy == NEVER:
        return False
    return key in (capability.get("supported_parameters") or [])


def build_request(spec: OpenRouterSpec, text: str, schema: dict | None) -> tuple[dict, dict]:
    """Return (wire_body, resolution). Pure. Call check_egress() before this.

    `schema` accepts a bare JSON Schema or a `schema_envelope()`.
    """
    from ..schema import unwrap_schema

    schema, encoding = unwrap_schema(schema)
    cap = spec.capability or {}
    resolution: dict[str, Any] = {"emitted": {}, "omitted": [], "notes": [], "schema_encoding": encoding}

    if spec.require_endpoint_pin and not spec.endpoint_tag:
        msg = "endpoint_tag is empty and require_endpoint_pin is on. Default routing re-ranks every ~5 min."
        raise CapabilityError(msg)

    messages: list[dict[str, str]] = []
    system = spec.system_prompt or ""
    # mode="prompted" IMPLIES the schema goes into the prompt (same trap as the Ollama
    # builder, fixed together: with defaults, prompted mode sent the schema nowhere).
    if (spec.schema_in_prompt or spec.mode == "prompted") and schema is not None:
        system = (system + "\n\nRespond with JSON matching this schema:\n" + canonical_json(schema)).strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": text})

    body: dict[str, Any] = {"model": spec.model, "messages": messages, "stream": False}

    # --- structured output ---
    if spec.mode == "json_schema":
        if schema is None:
            msg = "mode='json_schema' requires a schema"
            raise ValueError(msg)
        if cap and not cap.get("supports_structured_outputs"):
            resolution["notes"].append(
                "pinned endpoint does not declare structured_outputs; "
                "require_parameters=true will refuse to route (404) rather than downgrade"
            )
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": spec.schema_name, "strict": spec.response_format_strict, "schema": schema},
        }
    elif spec.mode == "json_object":
        body["response_format"] = {"type": "json_object"}

    # --- sampling, tri-state ---
    for policy, key, value in (
        (spec.send_seed, "seed", spec.seed),
        (spec.send_temperature, "temperature", spec.temperature),
        (spec.send_top_p, "top_p", spec.top_p),
    ):
        if _emit(policy, key, cap):
            body[key] = value
            resolution["emitted"][key] = f"{policy}->sent"
        else:
            resolution["omitted"].append(
                {
                    "param": key,
                    "policy": policy,
                    "supported": key in (cap.get("supported_parameters") or []),
                    # Omitted is NOT sent-at-default: the provider default is inherited.
                    "vendor_default_inherited": True,
                }
            )

    field_name = spec.max_output_field
    if field_name == "auto":
        supported = cap.get("supported_parameters") or []
        field_name = "max_completion_tokens" if "max_completion_tokens" in supported else "max_tokens"
    body[field_name] = spec.max_output_tokens
    resolution["emitted"][field_name] = f"auto->{field_name}"

    if spec.logprobs:
        body["logprobs"] = True
        if spec.top_logprobs > 0:
            body["top_logprobs"] = min(int(spec.top_logprobs), 20)
        resolution["emitted"]["logprobs"] = "requested"
        if cap and "logprobs" not in (cap.get("supported_parameters") or []):
            resolution["notes"].append(
                "pinned endpoint does not declare logprobs; require_parameters=true will refuse to route"
            )

    # --- reasoning: "not sending it" is not a no-reasoning baseline ---
    if spec.reasoning_control == "off":
        body["reasoning"] = {"enabled": False}
    elif spec.reasoning_control == "effort":
        body["reasoning"] = {"effort": spec.reasoning_effort, "exclude": spec.reasoning_exclude}
    elif spec.reasoning_control == "budget":
        body["reasoning"] = {"max_tokens": spec.reasoning_max_tokens, "exclude": spec.reasoning_exclude}

    # --- routing ---
    provider: dict[str, Any] = {
        "require_parameters": spec.require_parameters,
        "allow_fallbacks": spec.allow_fallbacks,
        "data_collection": spec.data_collection,
        "zdr": spec.zdr,
    }
    if spec.endpoint_tag:
        provider["only"] = [spec.endpoint_tag]
    if spec.quantizations:
        provider["quantizations"] = list(spec.quantizations)
    body["provider"] = provider

    if spec.response_healing:
        body["plugins"] = [{"id": "response-healing"}]
        resolution["notes"].append("response-healing ON: server-side repair is unlogged; json_repair stops being observable")

    if spec.usage_accounting:
        body["usage"] = {"include": True}
        resolution["emitted"]["usage.include"] = "accounting"

    return body, resolution


def resolve_key(spec: OpenRouterSpec, *, required: bool = True) -> str:
    """Resolve the credential at the last moment, at the single place it is used.

    A literal ``sk-`` key on the spec wins; anything else — empty, a variable NAME, a
    placeholder — falls through to the process environment. Resolving here rather than
    at authoring time means the key never travels through configs or records, and a
    stale value frozen somewhere is healed at send time. The failure this closes,
    measured live: a header that does not parse as Bearer credentials (for example
    ``Bearer OPENROUTER_API_KEY``, the variable name sent as the token) is answered with
    401 "Missing Authentication header" — which reads like a missing key, not a stale
    config.
    """
    literal = (spec.api_key or "").strip()
    if literal.startswith("sk-"):
        return literal
    env_name = (spec.api_key_env_var or "OPENROUTER_API_KEY").strip()
    key = os.environ.get(env_name, "").strip()
    if not key and required:
        msg = (
            f"No OpenRouter key: spec.api_key holds no literal key and {env_name} is empty "
            "in this process. Put it in your environment (e.g. via .env — see .env.example); "
            "keys never belong in YAML configs or definitions."
        )
        raise CapabilityError(msg)
    return key


def headers(spec: OpenRouterSpec) -> dict[str, str]:
    h = {"Authorization": f"Bearer {resolve_key(spec)}", "Content-Type": "application/json"}
    if spec.http_referer:
        h["HTTP-Referer"] = spec.http_referer
    if spec.attribution_title:
        h["X-OpenRouter-Title"] = spec.attribution_title
    return h


def target_url(spec: OpenRouterSpec) -> str:
    base = spec.gateway_url if (spec.egress_route == "gateway" and spec.gateway_url) else spec.base_url
    return f"{base.rstrip('/')}/chat/completions"


def request_record(spec: OpenRouterSpec, text: str, schema: dict | None, body: dict, resolution: dict) -> dict:
    from ..schema import unwrap_schema

    schema, encoding = unwrap_schema(schema)
    s_uid = content_uid(schema) if schema is not None else None
    redacted = dict(body)
    redacted["messages"] = [
        {"role": m["role"], "content_sha256": sha256_text(m["content"]), "content_chars": len(m["content"])}
        for m in body["messages"]
    ]
    if "response_format" in redacted:
        rf = dict(redacted["response_format"])
        if "json_schema" in rf:
            js = dict(rf["json_schema"])
            js["schema"] = {"__ref": f"schema_uid:{s_uid}"}
            rf["json_schema"] = js
        redacted["response_format"] = rf

    hashable = {"provider": "openrouter", "model": spec.model, "endpoint": spec.endpoint_tag,
                "schema_uid": s_uid, "wire": redacted}
    return {
        "record_version": "1.0.0",
        "request_uid": content_uid(hashable),
        "guard": {
            "data_classification": spec.data_classification,
            "egress_route": spec.egress_route,
            "gateway_url": spec.gateway_url or None,
            "store_input_text": spec.store_input_text,
            "wall_side": "outside",
        },
        "target": {
            "provider": "openrouter",
            "base_url": spec.base_url,
            "model_on_wire": spec.model,
            "endpoint_tag": spec.endpoint_tag,
            "endpoint_status_at_send": (spec.capability or {}).get("status"),
        },
        "capability": spec.capability or {"gate_effective": False},
        "schema": {"schema_uid": s_uid, "encoding": encoding, "mode": spec.mode,
                   "response_format_strict": spec.response_format_strict},
        "input": {
            "input_sha256": sha256_text(text),
            "input_chars": len(text),
            "input_text": text if spec.store_input_text else None,
        },
        "wire_body_redacted": redacted,
        "wire_body_sha256": sha256_text(canonical_json(body)),
        "headers_redacted": {
            "Authorization": "Bearer <sha256[:8]="
            + (key_fingerprint(resolve_key(spec, required=False)) or "unresolved")
            + ">"
        },
        "resolution": resolution,
        "policy": {
            "require_parameters": spec.require_parameters,
            "allow_fallbacks": spec.allow_fallbacks,
            "zdr": spec.zdr,
            "data_collection": spec.data_collection,
            "response_healing": spec.response_healing,
            "retry_on_5xx": spec.retry_on_5xx,
        },
    }


def read_response(spec: OpenRouterSpec, payload: dict) -> dict:
    choices = payload.get("choices") or [{}]
    choice = choices[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    usage = payload.get("usage") or {}
    served_provider = payload.get("provider")

    flags: list[str] = []
    if finish == "error" or choice.get("error"):
        flags.append("finish_reason_error_on_200")
    if finish == "length":
        flags.append("truncated_by_length")
    cap = spec.capability or {}
    if cap and spec.mode == "json_schema" and not cap.get("supports_structured_outputs"):
        flags.append("schema_mode_downgraded")
    if spec.response_healing:
        flags.append("plugin_repaired_server_side")
    if (usage.get("reasoning_tokens") or 0) > 0 and spec.reasoning_control == "off":
        flags.append("reasoning_forced_on")
    if cap.get("status") not in (None, 0):
        flags.append("endpoint_status_negative")

    # Attribution is a DISPLAY NAME only - measured, X-Provider-Name was absent and the
    # body carries "DeepInfra", which cannot distinguish google-vertex from
    # google-vertex/us-central1. Divergence detection is therefore best-effort.
    expected = (cap.get("provider_name") or "").lower()
    if expected and served_provider and expected not in str(served_provider).lower():
        flags.append("provider_diverged_from_pin")

    terminal = "truncated_by_length" in flags or "finish_reason_error_on_200" in flags
    return {
        "record_version": "1.0.0",
        "layer": "request",
        "served": {
            "provider_display_name": served_provider,
            "served_model": payload.get("model"),
            "generation_id": payload.get("id"),
            "attribution_fidelity": "display_name_only",
        },
        "finish": {"finish_reason": finish, "native_finish_reason": choice.get("native_finish_reason")},
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cost_usd": str(usage["cost"]) if usage.get("cost") is not None else None,
            "cost_details": usage.get("cost_details"),
        },
        "content": {"raw_sha256": sha256_text(content), "raw_chars": len(content), "raw_text": content},
        "silent_failure_flags": flags,
        "terminal_failure": terminal,
        "final_status_hint": "invalid" if terminal else "ok",
        # OpenAI shape: {"content": [{token, logprob, bytes, top_logprobs: [...]}]}.
        "logprobs": choice.get("logprobs"),
    }


async def fetch_zdr_pairs(*, http: httpx.AsyncClient, base_url: str = BASE_URL,
                          ) -> tuple[set[tuple[str, str]], str | None]:
    """((model_id, endpoint_tag) pairs, error | None) — the VISIBLE form.

    Membership must be resolved from this endpoint: passing `?zdr=true` to
    /models/{slug}/endpoints is SILENTLY IGNORED and returns the full list including
    non-ZDR endpoints. A filter that quietly does nothing is worse than no filter.

    The error is returned rather than swallowed: a failed ZDR fetch used to render as
    "this model has no ZDR endpoints" — a false negative on a privacy-relevant column.
    Absence is not evidence; the caller must be able to say "unknown".
    """
    try:
        data = await get_json(f"{base_url}/endpoints/zdr", http=http)
    except Exception as exc:  # noqa: BLE001 - absence must not block the UI, but must be visible
        return set(), f"{type(exc).__name__}: {str(exc)[:120]}"
    out: set[tuple[str, str]] = set()
    for item in (data.get("data") or []):
        mid = item.get("model_id") or item.get("id") or ""
        tag = item.get("tag") or item.get("endpoint_tag") or ""
        if mid and tag:
            out.add((mid, tag))
    return out, None


async def list_endpoints_annotated(
    model_id: str, *, http: httpx.AsyncClient, base_url: str = BASE_URL,
) -> list[dict[str, Any]]:
    """Endpoints for a model, each annotated with what it can actually do.

    Returned in a shape a table or dropdown can render directly, because the decision a
    user makes here depends entirely on capability - a tag alone tells you nothing about
    whether your schema will be honoured.

    `zdr` is None (not False) when the ZDR list could not be fetched, and `zdr_list_error`
    carries the reason on every row — unknown must never render as "no ZDR".
    """
    endpoints = await fetch_endpoints(model_id, http=http, base_url=base_url)
    zdr, zdr_err = await fetch_zdr_pairs(http=http, base_url=base_url)
    out = []
    for e in endpoints:
        sp = list(e.get("supported_parameters") or [])
        tag = e.get("tag") or ""
        out.append(
            {
                "tag": tag,
                "provider_name": e.get("provider_name") or e.get("name"),
                "quantization": e.get("quantization"),
                "context_length": e.get("context_length"),
                "max_completion_tokens": e.get("max_completion_tokens"),
                "status": e.get("status"),
                "supported_parameters": sp,
                "structured_outputs": "structured_outputs" in sp,
                "response_format": "response_format" in sp,
                "seed": "seed" in sp,
                "temperature": "temperature" in sp,
                "zdr": None if zdr_err else ((model_id, tag) in zdr),
                "zdr_list_error": zdr_err,
            }
        )
    return out


class OpenRouterProvider(Provider):
    """Registry adapter over this module's pure functions."""

    name = "openrouter"
    engine = "openrouter"  # heterogeneous serving engines behind one API
    spec_cls = OpenRouterSpec
    envelope_fields = {}
    allowed_values = {
        "mode": ("json_schema", "json_object", "prompted"),
        "egress_route": ("gateway", "direct"),
        "data_classification": tuple(DATA_CLASSES),
        "send_seed": (AUTO, ALWAYS, NEVER),
        "send_temperature": (AUTO, ALWAYS, NEVER),
        "send_top_p": (AUTO, ALWAYS, NEVER),
        "max_output_field": ("auto", "max_tokens", "max_completion_tokens"),
        "reasoning_control": ("off", "effort", "budget"),
    }
    default_concurrency = 8
    capabilities = frozenset({
        "structured_outputs", "json_object", "prompted", "logprobs", "top_logprobs",
        "seed", "usage_cost", "endpoint_pinning",
    })

    # Egress trio: resolved from the environment at payload-build time IF AND ONLY IF
    # all three still sit at their dataclass defaults (exactly the state check_egress
    # would refuse). Any explicit authoring wins and is never touched.
    _DEPLOYMENT_DEFAULTS = {"egress_route": "gateway", "gateway_url": "", "allow_direct_egress": False}

    def preflight(self, spec) -> None:
        check_egress(spec)  # raises before anything leaves the process

    def resolve_deployment(self, settings):
        untouched = all(settings.get(k, d) == d for k, d in self._DEPLOYMENT_DEFAULTS.items())
        if untouched:
            gateway = os.environ.get("EXTRCT_GATEWAY_URL", "").strip()
            settings = dict(settings)
            settings["egress_route"] = "gateway" if gateway else "direct"
            settings["allow_direct_egress"] = not gateway
            settings["gateway_url"] = gateway
        return settings

    def build_request(self, spec, text, schema):
        return build_request(spec, text, schema)

    def request_record(self, spec, text, schema, body, resolution=None):
        return request_record(spec, text, schema, body, resolution or {})

    def read_response(self, spec, payload, **kwargs):
        return read_response(spec, payload)

    def target_url(self, spec, envelope=None):
        return target_url(spec)

    def headers(self, spec):
        return headers(spec)
