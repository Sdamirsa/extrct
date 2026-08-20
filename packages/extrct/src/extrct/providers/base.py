"""The provider contract — one small surface every backend implements.

A provider is a stateless adapter between the pipeline and one model backend. All the
knowledge about a backend (its spec dataclass, its wire format, its silent failure
modes, its closed vocabularies) lives inside the provider package; the pipeline, the
client-definition validator, and the config override machinery consult the REGISTRY
(`extrct.providers`) and never name a backend in an if/elif chain. Adding vLLM,
Cerebras, or Fireworks is therefore a new module plus one `register_provider()` call —
no core module changes (open/closed).

Design rules every implementation must keep:

- `build_request` / `read_response` / `request_record` are PURE — no I/O, fully
  testable. All I/O goes through `extrct.runner`.
- The spec dataclass is FROZEN so it can be hashed and reused; every sampling knob is
  a field, and knobs the backend silently ignores are refused or recorded, never
  dropped on the floor.
- `read_response` decides whether a 200 is actually a failure (truncation, context
  pressure, silent downgrade) and reports it in `silent_failure_flags` /
  `terminal_failure`. HTTP success is not extraction success.
- `request_record` redacts: input text becomes a sha256 + length, schemas become a
  `schema_uid` reference, credentials never appear (a key fingerprint at most).
- Credentials are resolved from the environment at send time (`headers()`), never
  stored on a spec that could ride a record or an export.

`capabilities` is a declared, honest set — used for documentation and preflight
warnings, never to silently change a request. Current vocabulary:

  structured_outputs   full JSON Schema enforcement
  json_object          json-only mode without a schema
  prompted             schema-in-prompt fallback
  logprobs             per-token logprobs of the OUTPUT
  top_logprobs         alternatives per output token
  prompt_logprobs      per-token logprobs of the INPUT (vLLM `prompt_logprobs`-style;
                       no built-in provider serves this yet — the field exists so the
                       roadmap providers declare it instead of inventing a new axis)
  usage_cost           per-call cost accounting in the response
  endpoint_pinning     pinning one serving endpoint for reproducible routing
  seed                 deterministic sampling seed
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Spec fields that must never be authored into definitions, configs, or sweep grids:
# credentials are resolved at send time; capability is fetched, never authored.
FORBIDDEN_CLIENT_KEYS = {"api_key", "capability"}


class Provider(ABC):
    """One backend. Subclasses are registered in `extrct.providers`."""

    #: registry key and the value of `provider` in every record and definition
    name: str = ""
    #: serving engine for logprob mask-state semantics (see xai.certainty.mask_state);
    #: equals `name` unless the provider routes to heterogeneous engines
    engine: str = ""
    #: the frozen spec dataclass this provider consumes
    spec_cls: type = None  # type: ignore[assignment]
    #: payload settings that are real but live OUTSIDE the spec dataclass
    #: (e.g. ollama's api_path), with their defaults
    envelope_fields: dict[str, str] = {}
    #: closed vocabularies per spec field, checked at definition time so a wrong member
    #: raises at authoring instead of surfacing as a provider 400 or a silent reroute
    allowed_values: dict[str, tuple] = {}
    #: measured sane parallelism for this backend (a property of the backend, not the caller)
    default_concurrency: int = 4
    #: honest declaration of what this backend can do (module docstring vocabulary)
    capabilities: frozenset = frozenset()

    # ------------------------------------------------------------------ construction
    def make_spec(self, spec_kwargs: dict[str, Any]):
        """kwargs -> frozen spec. Loud on unknown fields (dataclass raises)."""
        return self.spec_cls(**spec_kwargs)

    def resolve_deployment(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Resolve deployment-only settings from the environment — called AFTER the
        definition uid is stamped, so deployment never leaks into identity. Must leave
        explicitly authored values untouched (authoring intent is content). Default:
        nothing to resolve."""
        return settings

    # ------------------------------------------------------------------ request side
    def preflight(self, spec) -> None:
        """Raise BEFORE anything leaves the process (egress guards, missing pins).
        Default: nothing to check."""

    @abstractmethod
    def build_request(self, spec, text: str, schema: dict | None) -> tuple[dict, dict | None]:
        """(wire_body, resolution | None). Pure. `schema` accepts a bare JSON Schema or
        a `schema_envelope()`. `resolution` records emit/omit decisions when the
        provider makes any (None otherwise)."""

    @abstractmethod
    def request_record(self, spec, text: str, schema: dict | None, body: dict,
                       resolution: dict | None = None) -> dict:
        """The replay unit: redacted wire body, hashes, target, policy. Pure."""

    @abstractmethod
    def target_url(self, spec, envelope: dict[str, Any] | None = None) -> str:
        """Full URL for this call. `envelope` carries envelope_fields overrides."""

    def headers(self, spec) -> dict[str, str]:
        """Credentials resolved here, at the single place they are used. Default: none."""
        return {}

    def timeout_s(self, spec) -> float:
        return float(getattr(spec, "timeout_s", 600))

    # ------------------------------------------------------------------ response side
    @abstractmethod
    def read_response(self, spec, payload: dict, **kwargs) -> dict:
        """Turn a 200 body into a response record, and decide whether it is actually a
        failure. Must set `silent_failure_flags`, `terminal_failure`, `content.raw_text`,
        `usage`, and `logprobs` (provider-native shape; xai.certainty normalizes)."""
