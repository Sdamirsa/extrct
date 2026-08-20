"""Client - Model Server — ONE client node for both providers (merged 2026-08-11).

Merges Client - Ollama and Client - OpenRouter at the COMPONENT layer only: the
package backends (extrct.ollama / extrct.openrouter) stay separate and untouched, the
spec builders below are copied verbatim from the two retired nodes, and the emitted
Client payload is byte-identical to what each old node produced — proven by the
equivalence test. One dropdown switches provider; flows swap Ollama <-> OpenRouter
without rewiring, which is what makes document- and sweep-driven switching clean.

Field visibility follows the provider dropdown via the sanctioned Registry pattern
(PROVIDER_FIELDS map + coverage test, membership-guarded put, is_refresh re-apply).
The options_metadata ban stands absolute — this component family crashed the canvas
twice. The Ollama model-list refresh (the ONE grandfathered network call in
update_build_config, strings only) runs ONLY when provider is ollama; the OpenRouter
side keeps its static-template discipline: discovery is the run-time Discovery output,
enforcement raises in build_client.

OpenRouter remains OUTSIDE THE WALL: synthetic or de-identified/aggregate content only; the egress gate and capability conflict checks are unchanged.

Two ordering lessons from the 2026-08-14 review wave:
- OVERRIDES ARE APPLIED BEFORE CAPABILITY IS RESOLVED. Capability must be fetched with
  the exact model/tag being SENT (openrouter.py: a ':free' variant has a disjoint endpoint
  list, and a pin from the paid slug 404s). Resolving first meant a sweep cell that
  overrode openrouter.model was gated, auto-emitted and RECORDED against the endpoint of
  the model it replaced.
- MODEL DIGEST IS RESOLVED AT BUILD TIME for Ollama. The refresh used to fetch digests
  into a write-only class cache while OllamaSpec.model_digest stayed None, so every run
  record and DB row lost the only pin tying a run to the actual model binary. The
  lookup is guarded: unreachable host -> digest None + a log line, never a dead node.
"""

import os

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DropdownInput, FloatInput, HandleInput, IntInput, MultilineInput, Output, StrInput,
)
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

from extrct import config as flowcfg
from extrct import ollama, openrouter
from extrct.hashing import key_fingerprint
from extrct.runner import get_json

_ENV_OLLAMA = (os.environ.get("EXTRCT_OLLAMA_URL") or os.environ.get("EXTRCT_OLLAMA_URL", "")).rstrip("/") or "http://your-gpu-host:11434"

# Which provider-specific inputs each provider actually reads. Shared fields are never
# toggled. The in-container test asserts these sets plus SHARED cover the template
# exactly, so adding a field without deciding its provider fails loudly.
PROVIDER_FIELDS: dict[str, set[str]] = {
    "ollama": {"base_url", "api_path", "num_ctx", "num_predict", "fail_on_truncation",
               "fail_on_context_pressure", "top_p", "top_k", "repeat_penalty", "think",
               "num_gpu", "keep_alive", "unload_before_run"},
    "openrouter": {"data_classification", "endpoint_tag", "require_endpoint_pin",
                   "require_parameters", "allow_fallbacks", "zdr", "data_collection",
                   "response_format_strict", "send_seed", "send_temperature", "send_top_p",
                   "max_output_tokens", "reasoning_control", "reasoning_effort",
                   "response_healing", "store_input_text", "api_key_env_var"},
}
SHARED_FIELDS = {"provider", "model", "mode", "seed", "temperature", "logprobs",
                 "top_logprobs", "system_prompt", "timeout_s"}
# Shown AND required travel together (a visible-but-empty gate field is just a slower
# version of the error the run-time guard raises).
REQUIRED_WHEN_SHOWN = {"data_classification"}

# The Model field's STATIC info. The Ollama refresh overwrites it with a live model count;
# that overwrite used to survive a switch to openrouter, leaving "5 model(s) on
# http://your-gpu-host:11434" as the tooltip on a slug-entry field — actively wrong at the exact
# moment the ':free' warning is needed. The provider switch puts this text back.
_MODEL_INFO = ("ollama: press refresh to list models pulled on the host (tag, e.g. "
               "'gemma4:31b-it-q8_0'). openrouter: type the EXACT slug incl. any ':free' "
               "suffix (e.g. 'google/gemma-4-26b-a4b-it') — a ':free' variant has its "
               "OWN endpoint list; run Discovery to inspect endpoints.")


class ExtrctModelClient(Component):
    display_name: str = "Provider - Model Server"
    description: str = "One client for both providers: Ollama (inside the wall) and OpenRouter (outside - )."
    documentation: str = "docs/extraction-stack/client-model.md"
    icon: str = "server"
    name: str = "extrct_model_client"

    inputs = [
        DropdownInput(
            name="provider", display_name="Provider",
            options=["ollama", "openrouter"], value="ollama",
            real_time_refresh=True,
            info=("ollama = local/GPU host serving, inside the wall. openrouter = OUTSIDE THE "
                  "WALL: synthetic or de-identified content only, and Data "
                  "Classification becomes required. Switching re-renders the fields; "
                  "your entered values are kept."),
        ),
        # --- shared -------------------------------------------------------------------
        DropdownInput(
            name="model", display_name="Model",
            options=[], value="", required=True, combobox=True,
            real_time_refresh=True, refresh_button=True,
            info=_MODEL_INFO,
        ),
        DropdownInput(
            name="mode", display_name="Output Mode",
            options=["json_schema", "json_object", "prompted"], value="json_schema",
            info=("json_schema is the only mode that constrains generation - on Ollama "
                  "enforcement is per MODEL FAMILY (gemma4 honors it; qwen3.5/3.6 silently "
                  "ignore it - probe first); on OpenRouter, Require Parameters converts "
                  "silent downgrades into loud refusals. json_object asks for 'some JSON'. "
                  "prompted puts the schema in the prompt and hopes."),
        ),
        # Edited per extraction task, so it sits above the set-once experiment knobs.
        MultilineInput(name="system_prompt", display_name="System Prompt",
                       value="Extract structured data from the text.",
                       info=("Keep it about the TASK. Per-field instructions belong in each "
                             "variable's Description, where they travel with the schema and "
                             "are hashed with it.")),
        IntInput(name="seed", display_name="Seed", value=42,
                 info=("Determinism is not guaranteed by any provider - treat replay equality "
                       "as a measured rate; the run record hashes the response so you can "
                       "measure it. OpenRouter emission follows Send Seed.")),
        FloatInput(name="temperature", display_name="Temperature", value=0.0,
                   info=("0.0 for extraction: transcription against a schema, not generation. "
                         "Always sent to Ollama (baked Modelfile defaults differ per model); "
                         "OpenRouter emission follows Send Temperature.")),
        BoolInput(name="logprobs", display_name="Token Logprobs", value=False,
                  info=("Per-token log-probabilities, with alternatives when Top Logprobs > 0 "
                        "- the input to certainty scoring. OpenRouter: ENDPOINT-dependent, "
                        "check the logprobs column in Discovery; build fails loudly if the "
                        "pin lacks it. Ollama: measured wire shape is boolean here + count "
                        "separate.")),
        # Visible beside Token Logprobs on purpose: the two are ONE decision. Hidden, it
        # produced logprobs without alternatives and silently degraded enum certainty.
        IntInput(name="top_logprobs", display_name="Top Logprobs", value=0,
                 range_spec=RangeSpec(min=0, max=20, step=1, step_type="int"),
                 info="Alternatives per position (0-20). 3+ required for enum-posterior certainty. Only sent when Token Logprobs is on."),
        IntInput(name="timeout_s", display_name="Timeout (s)", value=600, advanced=True,
                 info=("Generous by default: cold model loads and reasoning models can spend "
                       "minutes before the first token. Queue wait at the server counts "
                       "against this too.")),
        # --- ollama only --------------------------------------------------------------
        DropdownInput(
            name="base_url", display_name="Base URL",
            options=list(dict.fromkeys([_ENV_OLLAMA, "http://your-gpu-host:11434",
                                        "http://host.docker.internal:11434"])),
            value=_ENV_OLLAMA, combobox=True, real_time_refresh=True,
            info=("Ollama server, no /v1 suffix. 'spark' is the GPU host over the direct link; "
                  "host.docker.internal is the laptop. Editable - type any URL. Changing "
                  "this refreshes the model list."),
        ),
        DropdownInput(name="api_path", display_name="API Path",
                      options=["/api/chat", "/v1/chat/completions"], value="/api/chat",
                      advanced=True,
                      info="/v1 is a HAZARD: malformed response_format returns 200 with prose, and options are unreachable."),
        IntInput(name="num_ctx", display_name="Context Window", value=8192,
                 info=("Set it comfortably above your longest prompt plus expected output, and "
                       "check Run - Long Text's chunk size against it. NEVER leave implicit: "
                       "measured, input was silently truncated 3641 -> 258 tokens at HTTP 200, "
                       "giving a confident answer from text the model never saw.")),
        IntInput(name="num_predict", display_name="Max Output Tokens (Ollama)", value=1024,
                 info=("Ceiling on the generated JSON. Too low returns HTTP 200 with a valid "
                       "PREFIX - Fail On Truncation catches it.")),
        BoolInput(name="fail_on_truncation", display_name="Fail On Truncation", value=True,
                  info="done_reason='length' returns 200 with an invalid JSON PREFIX. Off lets the repair ladder brace-balance a truncated record into a plausible wrong answer."),
        BoolInput(name="fail_on_context_pressure", display_name="Fail On Context Pressure", value=True,
                  info=("Fails the run when the prompt nearly fills the loaded window, "
                        "verified against /api/ps.")),
        FloatInput(name="top_p", display_name="Top P", value=1.0, advanced=True,
                   info="1.0 disables nucleus sampling. Sent explicitly - models bake their own defaults."),
        IntInput(name="top_k", display_name="Top K", value=0, advanced=True,
                 info="0 disables top-k. Sent explicitly - qwen3 bakes 20."),
        FloatInput(name="repeat_penalty", display_name="Repeat Penalty", value=1.0, advanced=True,
                   info="1.0 = no penalty. Penalising repetition is harmful for structured output."),
        DropdownInput(name="think", display_name="Thinking",
                      options=["false", "true", "low", "medium", "high"], value="false",
                      info=("OFF by default AGAINST the vendor default: it changes the answer "
                            "and burns output tokens before the grammar engages.")),
        IntInput(name="num_gpu", display_name="GPU Layers", value=-1, advanced=True,
                 range_spec=RangeSpec(min=-1, max=999, step=1, step_type="int"),
                 info=("-1 = AUTO (recommended; required for the GPU host - 0 would silently force "
                       "a 27B model onto CPU). 0 = CPU (laptop only). Positive = explicit "
                       "layer count. Status shows CPU-FORCED whenever 0 is in effect.")),
        StrInput(name="keep_alive", display_name="Keep Alive", value="30m", advanced=True,
                 info="How long the model stays resident. Sent TOP-LEVEL - inside options it silently does nothing."),
        BoolInput(name="unload_before_run", display_name="Unload Before Run", value=False, advanced=True,
                  info="Correct for sweeps: stops one cell inheriting another cell's loaded context window."),
        # --- openrouter only ----------------------------------------------------------
        DropdownInput(
            name="data_classification", display_name="Data Classification",
            options=["", *openrouter.DATA_CLASSES], value="", show=False,
            info=("REQUIRED for OpenRouter. Empty refuses the call. No option permits raw "
                  "note text. Egress route is env-driven: EXTRCT_GATEWAY_URL set "
                  "routes via the  gateway; empty goes direct. Stamped into every run "
                  "record."),
        ),
        StrInput(name="endpoint_tag", display_name="Endpoint Tag", value="", show=False,
                 info=("The full tag from the Discovery output (e.g. 'deepinfra/fp8'), pasted "
                       "here. Deliberately NOT a live dropdown - edit-time discovery crashed "
                       "the canvas twice. build re-checks the pin at run time and fails "
                       "loudly on a conflict.")),
        BoolInput(name="require_endpoint_pin", display_name="Require Endpoint Pin", value=True, show=False,
                  info="Default routing re-ranks every ~5 minutes, so an unpinned run is not reproducible."),
        BoolInput(name="require_parameters", display_name="Require Parameters", value=True,
                  show=False, advanced=True,
                  info=("Measured: converts a silent capability downgrade into a loud routing "
                        "refusal (404).")),
        BoolInput(name="allow_fallbacks", display_name="Allow Fallbacks", value=False,
                  show=False, advanced=True,
                  info="Vendor default true lets a pinned run execute elsewhere at a different quantization and still return 200."),
        BoolInput(name="zdr", display_name="Zero Data Retention", value=True, show=False,
                  info=("Requests endpoints that do not retain your prompts. STRICTLY STRONGER "
                        "than Data Collection (training only). Discovery has a zdr column.")),
        DropdownInput(name="data_collection", display_name="Data Collection",
                      options=["deny", "allow"], value="deny", show=False,
                      info="Vendor default 'allow' permits providers that TRAIN on inputs."),
        BoolInput(name="response_format_strict", display_name="Provider Strict Enforcement",
                  value=True, show=False, advanced=True,
                  info="DISTINCT from the Schema Builder's Encoding: asks the provider to hard-enforce; does not change the schema."),
        DropdownInput(name="send_seed", display_name="Send Seed",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER],
                      value=openrouter.AUTO, advanced=True, show=False,
                      info="'auto' emits only if the pinned endpoint declares it; 'always' against a non-declaring endpoint fails loudly here."),
        DropdownInput(name="send_temperature", display_name="Send Temperature",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER],
                      value=openrouter.AUTO, advanced=True, show=False,
                      info="'auto' emits only if the pinned endpoint declares temperature (51/338 models do not accept it)."),
        DropdownInput(name="send_top_p", display_name="Send Top P",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER],
                      value=openrouter.NEVER, advanced=True, show=False,
                      info="'never' by default: inert at temperature 0, and every extra key narrows the routing pool."),
        IntInput(name="max_output_tokens", display_name="Max Output Tokens (OpenRouter)",
                 value=2048, show=False,
                 info=("Emitted as max_tokens or max_completion_tokens depending on the pinned "
                       "endpoint's declaration. Too low truncates mid-JSON and fails rather "
                       "than returning a fragment.")),
        DropdownInput(name="reasoning_control", display_name="Reasoning",
                      options=["off", "effort", "budget"], value="off", show=False,
                      info=("'off' SENDS {enabled:false}: 69/338 models reason by default, so "
                            "omitting the key is not a no-reasoning baseline. 'effort' reads the "
                            "Reasoning Effort field (under Advanced); 'budget' support is per model.")),
        StrInput(name="reasoning_effort", display_name="Reasoning Effort", value="",
                 advanced=True, show=False,
                 info="Only used when Reasoning is 'effort'. Valid values are PER MODEL - check reasoning.supported_efforts."),
        BoolInput(name="response_healing", display_name="Server-Side Response Healing",
                  value=False, advanced=True, show=False,
                  info="OFF by default: it repairs JSON invisibly and unlogged, recording 'ok' where 'repaired' is true."),
        BoolInput(name="store_input_text", display_name="Store Input Text", value=False,
                  advanced=True, show=False,
                  info=". OFF stores only a sha256 of the input. Turning it on is a decision-log event, not a convenience."),
        StrInput(name="api_key_env_var", display_name="API Key Env Var",
                 value="OPENROUTER_API_KEY", advanced=True, show=False,
                 info=("NAME of the container env var holding the key - never the key itself. "
                       "Read at send time; only a sha256 prefix reaches a run record.")),
        # --- plumbing last: optional sweep wiring never pushes Provider/Model down ------
        HandleInput(
            name="overrides", display_name="Overrides", required=False, input_types=["Data"],
            info=("Optional: a Flow - Controller thread or Flow - Config payload. Keys "
                  "'<provider>.X' and 'client.X' override this node's fields at the spec "
                  "level for the ACTIVE provider (typos raise; api_key never overridable). "
                  "Applied BEFORE the egress and capability guards. Unwired = this node "
                  "behaves exactly as its fields say."),
        ),
    ]

    outputs = [
        # "Provider" matches the receiving input on both run nodes — the researcher wires
        # this edge daily. Internal name / method frozen (S2).
        Output(name="client", display_name="Provider", method="build_client", group_outputs=True),
        Output(name="discovery", display_name="Discovery", method="build_discovery", group_outputs=True),
        Output(name="capability", display_name="Capability Report", method="build_capability", group_outputs=True),
    ]

    async def update_build_config(self, build_config, field_value, field_name=None):
        """Two duties, strictly separated: (1) provider switch toggles field visibility -
        local state only, the Registry pattern; (2) the Ollama model-list refresh - the
        ONE grandfathered network call, strings only, and it never runs for openrouter.
        Mutation is in place and membership-guarded (dotdict.__missing__ makes typos
        silent). options_metadata is never written."""
        def put(name, key, value):
            if name in build_config and isinstance(build_config[name], dict):
                build_config[name][key] = value

        provider = field_value if field_name == "provider" else (
            (build_config.get("provider", {}) or {}).get("value") or "ollama")

        if field_name == "provider" or build_config.get("is_refresh"):
            for f in PROVIDER_FIELDS["ollama"] | PROVIDER_FIELDS["openrouter"]:
                shown = f in PROVIDER_FIELDS.get(provider, set())
                put(f, "show", shown)
                if f in REQUIRED_WHEN_SHOWN:
                    put(f, "required", shown)
            if provider != "ollama":
                # Undo any Ollama model-count text left on the Model field.
                put("model", "info", _MODEL_INFO)

        if provider != "ollama":
            return build_config  # openrouter stays static: discovery is the run-time output
        if field_name not in ("base_url", "model") and not build_config.get("is_refresh"):
            return build_config

        import httpx

        url = (build_config.get("base_url", {}) or {}).get("value") or ""
        if field_name == "base_url":
            url = field_value or url
        url = str(url).rstrip("/")
        if not url:
            return build_config
        try:
            async with httpx.AsyncClient() as http:
                tags = await get_json(f"{url}/api/tags", http=http, timeout_s=15)
                version = await get_json(f"{url}/api/version", http=http, timeout_s=10)
        except Exception as exc:
            msg = (f"Cannot reach Ollama at {url}: {type(exc).__name__}. From inside the "
                   "container 'localhost' is the CONTAINER - use host.docker.internal for "
                   "the laptop, or 'spark' for the GPU host.")
            raise ValueError(msg) from exc
        models = tags.get("models") or []
        if not models:
            msg = (f"Ollama at {url} is running (v{version.get('version')}) but has NO "
                   "models pulled. Pull one on that host, then press refresh.")
            raise ValueError(msg)
        # Strings only, no digest cache: the digest that matters is resolved at build time
        # against the model actually selected (a class-attr cache was write-only anyway).
        labels = []
        for m in sorted(models, key=lambda x: x.get("name", "")):
            d = m.get("details") or {}
            bits = [b for b in (d.get("parameter_size"), d.get("quantization_level")) if b]
            labels.append(f"{m['name']}  [{', '.join(bits)}]" if bits else m["name"])
        put("model", "options", labels)
        put("model", "info", f"{len(models)} model(s) on {url} (Ollama v{version.get('version')}).")
        return build_config

    # --- spec builders: copied VERBATIM from the retired per-provider nodes -----------
    def _ollama_spec(self) -> ollama.OllamaSpec:
        return ollama.OllamaSpec(
            base_url=(self.base_url or "").rstrip("/"),
            model=str(self.model or "").split("  [")[0],
            mode=self.mode,
            num_ctx=int(self.num_ctx),
            num_predict=int(self.num_predict),
            seed=int(self.seed),
            temperature=float(self.temperature),
            top_p=float(self.top_p),
            top_k=int(self.top_k),
            repeat_penalty=float(self.repeat_penalty),
            num_gpu=int(self.num_gpu),
            logprobs=bool(self.logprobs),
            top_logprobs=int(self.top_logprobs or 0),
            think=self.think,
            keep_alive=self.keep_alive,
            unload_before_run=bool(self.unload_before_run),
            fail_on_truncation=bool(self.fail_on_truncation),
            fail_on_context_pressure=bool(self.fail_on_context_pressure),
            system_prompt=self.system_prompt or "",
            timeout_s=int(self.timeout_s),
        )

    def _openrouter_spec(self, capability: dict | None = None) -> openrouter.OpenRouterSpec:
        gateway = (os.environ.get("EXTRCT_GATEWAY_URL") or os.environ.get("EXTRCT_GATEWAY_URL", "")).strip()
        return openrouter.OpenRouterSpec(
            data_classification=self.data_classification or "",
            egress_route="gateway" if gateway else "direct",
            allow_direct_egress=not gateway,
            gateway_url=gateway,
            store_input_text=bool(self.store_input_text),
            api_key="",
            api_key_env_var=str(getattr(self, "api_key_env_var", "") or "OPENROUTER_API_KEY").strip(),
            model=(self.model or "").strip(),
            endpoint_tag=str(self.endpoint_tag or "").split("  [")[0].strip(),
            require_endpoint_pin=bool(self.require_endpoint_pin),
            require_parameters=bool(self.require_parameters),
            allow_fallbacks=bool(self.allow_fallbacks),
            zdr=bool(self.zdr),
            data_collection=self.data_collection,
            mode=self.mode,
            response_format_strict=bool(self.response_format_strict),
            send_seed=self.send_seed, seed=int(self.seed),
            send_temperature=self.send_temperature, temperature=float(self.temperature),
            send_top_p=self.send_top_p,
            logprobs=bool(self.logprobs),
            top_logprobs=int(self.top_logprobs or 0),
            max_output_tokens=int(self.max_output_tokens),
            reasoning_control=self.reasoning_control,
            reasoning_effort=self.reasoning_effort or "",
            response_healing=bool(self.response_healing),
            system_prompt=self.system_prompt or "",
            timeout_s=int(self.timeout_s),
            capability=capability or {},
        )

    async def _capability(self, model: str | None = None, tag: str | None = None) -> dict:
        """Capability for the model/tag that will ACTUALLY be sent (post-overrides), never
        for the widget values — a ':free' variant has a disjoint endpoint list."""
        model = self.model if model is None else model
        tag = self.endpoint_tag if tag is None else tag
        tag = str(tag or "").split("  [")[0].strip()
        if not tag:
            return {}
        import httpx

        async with httpx.AsyncClient() as http:
            return await openrouter.resolve_capability(str(model or "").strip(), tag, http=http)

    def _effective_kwargs(self, provider: str, ov: dict) -> tuple[dict, list[str]]:
        """Spec kwargs with the Overrides thread applied — BEFORE capability resolution and
        before the egress guard, which is what the Overrides info text promises."""
        base = self._ollama_spec() if provider == "ollama" else self._openrouter_spec()
        kwargs = dict(base.__dict__)
        if not ov:
            return kwargs, []
        return flowcfg.apply_client_overrides(provider, kwargs, ov)

    async def _ollama_digest(self, base_url: str, model: str) -> str | None:
        """The selected model's digest from a fresh /api/tags — the replayability pin to the binary.
        Guarded: an unreachable host logs and returns None, it never breaks the node."""
        if not base_url or not model:
            return None
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                tags = await get_json(f"{base_url}/api/tags", http=http, timeout_s=10)
            for m in tags.get("models") or []:
                if m.get("name") == model:
                    return (m.get("digest") or "") or None
            self.log(f"model {model!r} is not pulled on {base_url}; model_digest stays NULL")
        except Exception as exc:  # noqa: BLE001 - a missing pin must not break authoring
            self.log(f"could not resolve model_digest from {base_url} "
                     f"({type(exc).__name__}); the run record will carry model_digest=NULL")
        return None

    def _check_conflicts(self, spec: openrouter.OpenRouterSpec, cap: dict) -> None:
        if not cap:
            return
        supported = cap.get("supported_parameters") or []
        if spec.mode == "json_schema" and not cap.get("supports_structured_outputs"):
            msg = (f"{cap.get('endpoint_tag')} does not declare 'structured_outputs', so a "
                   f"json_schema request is refused (404) by Require Parameters. Run "
                   f"Discovery and pick a tag with structured_outputs=True, or change "
                   f"Output Mode.")
            raise ValueError(msg)
        for policy, key in ((spec.send_seed, "seed"), (spec.send_temperature, "temperature"),
                            (spec.send_top_p, "top_p")):
            if policy == openrouter.ALWAYS and key not in supported:
                msg = (f"Send {key} is 'always' but {cap.get('endpoint_tag')} does not "
                       f"declare {key!r}; this run would FAIL TO ROUTE. Set 'auto' or pick "
                       f"an endpoint that declares {key!r}.")
                raise ValueError(msg)
        if spec.logprobs and "logprobs" not in supported:
            msg = (f"Token Logprobs is on but {cap.get('endpoint_tag')} does not declare "
                   f"'logprobs'; this run would fail to route. Pick a tag with "
                   f"logprobs=True in Discovery.")
            raise ValueError(msg)

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    # --- outputs ----------------------------------------------------------------------
    async def build_client(self) -> Data:
        ov = self._unwrap(getattr(self, "overrides", None))
        if self.provider == "ollama":
            kwargs, applied = self._effective_kwargs("ollama", ov)
            if "model_digest" not in applied:  # an authored digest is never stomped
                digest = await self._ollama_digest(kwargs.get("base_url"), kwargs.get("model"))
                if digest:
                    kwargs["model_digest"] = digest
            spec = ollama.OllamaSpec(**kwargs)
            gpu = "auto" if spec.num_gpu < 0 else (f"{spec.num_gpu} (CPU-FORCED!)" if spec.num_gpu == 0 else str(spec.num_gpu))
            dig = spec.model_digest[:12] if spec.model_digest else "UNKNOWN"
            self.status = (f"ollama | {spec.model} @ ctx={spec.num_ctx} seed={spec.seed} "
                           f"mode={spec.mode} think={spec.think} gpu={gpu} | digest={dig}") + (
                f" | overrides: {','.join(applied)} ({ov.get('config_uid', 'no-uid')})" if applied else "")
            return Data(data={"provider": "ollama", "spec": spec.__dict__, "api_path": self.api_path,
                              **({"config_uid": ov.get("config_uid")} if applied and ov.get("config_uid") else {})})

        # Overrides FIRST, then capability for the model/tag actually being sent.
        kwargs, applied = self._effective_kwargs("openrouter", ov)
        cap = await self._capability(model=kwargs.get("model"), tag=kwargs.get("endpoint_tag"))
        kwargs["capability"] = cap
        spec = openrouter.OpenRouterSpec(**kwargs)
        openrouter.check_egress(spec)
        self._check_conflicts(spec, cap)
        key = openrouter.resolve_key(spec)
        source = f"env:{spec.api_key_env_var}"
        self.status = (
            f"openrouter | {spec.model} @ {spec.endpoint_tag or 'UNPINNED'} | mode={spec.mode} | "
            f"{spec.data_classification} | egress:{spec.egress_route} | "
            f"key:{source}:{key_fingerprint(key)}"
            + (f" | overrides: {','.join(applied)} ({ov.get('config_uid', 'no-uid')})" if applied else "")
        )
        return Data(data={"provider": "openrouter", "spec": spec.__dict__, "key_source": source,
                          **({"config_uid": ov.get("config_uid")} if applied and ov.get("config_uid") else {})})

    async def build_discovery(self) -> DataFrame:
        """Run-time discovery for the ACTIVE provider: OpenRouter endpoints, or the
        models installed on the Ollama host. Data path only - never the template."""
        import httpx

        if self.provider == "ollama":
            url = (self.base_url or "").rstrip("/")
            try:
                async with httpx.AsyncClient() as http:
                    tags = await get_json(f"{url}/api/tags", http=http, timeout_s=15)
            except Exception as exc:  # same actionable message as the refresh path (S9)
                msg = (f"Cannot reach Ollama at {url}: {type(exc).__name__}. From inside the "
                       "container 'localhost' is the CONTAINER - use host.docker.internal for "
                       "the laptop, or 'spark' for the GPU host.")
                raise ValueError(msg) from exc
            rows = []
            for m in sorted(tags.get("models") or [], key=lambda x: x.get("name", "")):
                d = m.get("details") or {}
                rows.append({"name": m.get("name"), "digest": (m.get("digest") or "")[:12],
                             "family": d.get("family") or "", "parameter_size": d.get("parameter_size") or "",
                             "quantization": d.get("quantization_level") or "",
                             "size_gb": round((m.get("size") or 0) / 1e9, 2)})
            self.status = f"{len(rows)} model(s) on {url}"
            return DataFrame(rows or [{"note": f"no models pulled on {url}"}])

        model = (self.model or "").strip()
        if not model:
            msg = "Set Model first - the exact id, ':free' suffix included."
            raise ValueError(msg)
        async with httpx.AsyncClient() as http:
            eps = await openrouter.list_endpoints_annotated(model, http=http)
        # zdr is None (not False) when the ZDR list itself could not be fetched — a
        # transient failure must not render as "this model has no ZDR endpoints".
        zdr_err = next((e.get("zdr_list_error") for e in eps if e.get("zdr_list_error")), None)
        rows = [{
            "tag": e.get("tag") or "",
            "provider": e.get("provider_name") or "",
            "structured_outputs": bool(e.get("structured_outputs")),
            "response_format": bool(e.get("response_format")),
            "zdr": None if e.get("zdr") is None else bool(e.get("zdr")),
            "seed": bool(e.get("seed")),
            "temperature": bool(e.get("temperature")),
            "logprobs": "logprobs" in (e.get("supported_parameters") or []),
            "quantization": e.get("quantization") or "",
            "context_length": e.get("context_length") or 0,
            "max_completion_tokens": e.get("max_completion_tokens") or 0,
            "status": e.get("status") if isinstance(e.get("status"), int) else 0,
            "supported_parameters": ",".join(e.get("supported_parameters") or []),
        } for e in eps]
        if not rows:  # absence is not evidence — say it, like the ollama branch does
            self.status = (f"{model}: NO endpoints exposed — nothing to pin. A ':free' variant "
                           "has its own, much smaller, set; check the exact slug.")
            return DataFrame([{"note": f"no endpoints exposed for {model!r} — check the exact "
                                       "slug (':free' variants have their own endpoint list)"}])
        schema_n = sum(1 for r in rows if r["structured_outputs"])
        zdr_txt = (f"ZDR LIST UNAVAILABLE ({zdr_err}) — the zdr column is UNKNOWN, not false"
                   if zdr_err else f"{sum(1 for r in rows if r['zdr']) } ZDR")
        self.status = (f"{len(rows)} endpoint(s) for {model}: {schema_n} schema-capable, "
                       f"{zdr_txt}. Copy a tag into Endpoint Tag.")
        return DataFrame(rows)

    async def build_capability(self) -> Data:
        """The audit view of what a run WOULD do — so it applies the same Overrides thread
        build_client applies; otherwise 'resolved' contradicts the client actually used."""
        ov = self._unwrap(getattr(self, "overrides", None))
        if self.provider == "ollama":
            kwargs, applied = self._effective_kwargs("ollama", ov)
            spec = ollama.OllamaSpec(**kwargs)
            risks = []
            if self.api_path != "/api/chat":
                risks += ["v1_silent_disable", "options_unreachable"]
            if spec.think != "false":
                risks.append("thinking_changes_output")
            self.status = ("ollama | enum+required enforced, numeric ranges NOT enforced | "
                           f"risks: {','.join(risks) if risks else 'none'}"
                           + (f" | overrides: {','.join(applied)}" if applied else ""))
            return Data(data={
                "provider": "ollama",
                "gate_effective": False,
                "enforcement_verified": {"enum": True, "required": True, "numeric_range": False},
                "numeric_range_note": "Ollama's grammar does not enforce minimum/maximum; the client-side validator is the only check.",
                "silent_failure_risk": risks,
                "resolved": {"num_ctx": spec.num_ctx, "seed": spec.seed, "temperature": spec.temperature},
                "overrides_applied": applied,
            })
        kwargs, applied = self._effective_kwargs("openrouter", ov)
        cap = await self._capability(model=kwargs.get("model"), tag=kwargs.get("endpoint_tag"))
        if not cap:
            self.status = "no endpoint_tag pinned - capability gating unavailable"
            return Data(data={"note": "no endpoint_tag pinned; capability gating is unavailable",
                              "overrides_applied": applied})
        self.status = (f"{cap['endpoint_tag']} | structured_outputs={cap['supports_structured_outputs']}"
                       + (f" | overrides: {','.join(applied)}" if applied else ""))
        return Data(data={**cap, "overrides_applied": applied})
