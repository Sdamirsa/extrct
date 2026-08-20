"""Ollama Chat Request — configuration only, no execution.

Emits a frozen client spec the extractor consumes. Native POST /api/chat, never /v1:
three measured /v1 response_format shapes return HTTP 200 with unconstrained prose, and /v1
cannot reach `options` at all. See docs/system-arch/extraction-stack/api-notes.md decision D-a.
"""

import os

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import (
    BoolInput, DropdownInput, FloatInput, HandleInput, IntInput, MessageTextInput,
    MultilineInput, Output, StrInput,
)
from lfx.schema.data import Data

from extrct import config as flowcfg
from extrct import ollama
from extrct.runner import get_json


class ExtrctOllamaClient(Component):
    display_name: str = "Client - Ollama"
    description: str = "Configure a native Ollama /api/chat call. Inside the wall."
    documentation: str = "docs/system-arch/extraction-stack/api-notes.md"
    icon: str = "hard-drive"
    name: str = "extrct_ollama_client"

    inputs = [
        HandleInput(
            name="overrides", display_name="Overrides", required=False, input_types=["Data"],
            info=("Optional: a Prep - Flow Config payload. Keys 'ollama.X' and 'client.X' "
                  "override this node's fields at the spec level (X must be a spec field — "
                  "typos raise). Toggles and dropdowns included: this is how flow variables "
                  "reach settings no wire can set. Unwired = this node behaves exactly as "
                  "its fields say."),
        ),
        DropdownInput(
            name="base_url",
            display_name="Base URL",
            info=("No /v1 suffix. 'spark' is the GPU host over the direct link (compose "
                  "extra_hosts); host.docker.internal is the laptop's own Ollama. The list "
                  "is editable (combobox) — type any other URL directly. Changing this "
                  "refreshes the model list."),
            options=list(dict.fromkeys([
                (os.environ.get("EXTRCT_OLLAMA_URL") or os.environ.get("EXTRCT_OLLAMA_URL", "")).rstrip("/") or "http://your-gpu-host:11434",
                "http://your-gpu-host:11434",
                "http://host.docker.internal:11434",
            ])),
            value=(os.environ.get("EXTRCT_OLLAMA_URL") or os.environ.get("EXTRCT_OLLAMA_URL", "")).rstrip("/") or "http://your-gpu-host:11434",
            combobox=True,
            real_time_refresh=True,
        ),
        DropdownInput(
            name="model",
            display_name="Model",
            info=("Populated from /api/tags on this host — only models already pulled appear. "
                  "The digest is the reproducibility anchor: a tag like 'qwen3:0.6b' can be "
                  "re-pointed upstream, a digest cannot. Use the refresh button after pulling."),
            options=[],
            value="",
            required=True,
            real_time_refresh=True,
            refresh_button=True,
        ),
        DropdownInput(
            name="api_path",
            display_name="API Path",
            info="/v1 is a HAZARD: malformed response_format returns 200 with prose, and options are unreachable.",
            options=["/api/chat", "/v1/chat/completions"],
            value="/api/chat",
            advanced=True,
        ),
        # --- context and length: the two silent failures ---
        IntInput(
            name="num_ctx",
            display_name="Context Window",
            info=(
                "NEVER leave implicit. Measured: input silently truncated 3641 -> 258 tokens at "
                "HTTP 200, giving a confident answer from text the model never saw."
            ),
            value=8192,
        ),
        IntInput(name="num_predict", display_name="Max Output Tokens", value=1024,
                 info=("Ceiling on the generated JSON. Too low returns HTTP 200 with a "
                       "structurally valid PREFIX — the grammar guarantees a well-formed start, "
                       "never a complete document — which Fail On Truncation catches.")),
        BoolInput(
            name="fail_on_truncation",
            display_name="Fail On Truncation",
            info="done_reason='length' returns 200 with an invalid JSON PREFIX. Off lets the repair ladder brace-balance a truncated record into a plausible wrong answer.",
            value=True,
        ),
        BoolInput(name="fail_on_context_pressure", display_name="Fail On Context Pressure", value=True,
                  info=("Fails the run when the prompt nearly fills the loaded window, verified "
                        "against /api/ps. Off, an over-long note is silently truncated and you get "
                        "a confident answer from text the model never read.")),
        # --- sampling: always emitted, never inherited ---
        IntInput(name="seed", display_name="Seed", value=42,
                 info=("Always sent. Determinism is not guaranteed even so — treat replay equality "
                       "as a measured rate; the run record hashes the response so you can measure it.")),
        FloatInput(
            name="temperature",
            display_name="Temperature",
            info="Baked Modelfile defaults differ per model (qwen3:0.6b ships 0.6, qwen3.5:9b ships 1.0), so every key is emitted explicitly.",
            value=0.0,
        ),
        FloatInput(name="top_p", display_name="Top P", value=1.0, advanced=True,
                   info="1.0 disables nucleus sampling. Models bake their own default (qwen3 ships 0.95), so it is sent explicitly."),
        IntInput(name="top_k", display_name="Top K", value=0, advanced=True,
                 info="0 disables top-k. qwen3 bakes 20, so leaving it implicit makes two models 'at default' incomparable."),
        FloatInput(name="repeat_penalty", display_name="Repeat Penalty", value=1.0, advanced=True,
                   info="1.0 is no penalty. Penalising repetition is actively harmful for structured output, where repeated keys and delimiters are required."),
        # --- logprobs ---
        BoolInput(name="logprobs", display_name="Token Logprobs", value=False,
                  info=("Returns each generated token's log-probability, with alternatives when "
                        "Top Logprobs > 0. Read them on Structured Extract's Logprobs output; the "
                        "verbatim payload also lands in provider_response in Postgres. Wire shape "
                        "is measured: boolean here, count separate — and note the current GPU host "
                        "path runs prompted mode, so these are unconstrained model probabilities.")),
        IntInput(name="top_logprobs", display_name="Top Logprobs", value=0, advanced=True,
                 range_spec=RangeSpec(min=0, max=20, step=1, step_type="int"),
                 info="Alternatives per position (0-20). Only sent when Token Logprobs is on."),
        # --- runtime ---
        DropdownInput(
            name="think",
            display_name="Thinking",
            info="OFF by default AGAINST the vendor default: it changes the answer (same seed gave CTA vs TTE) and burns output tokens before the grammar engages.",
            options=["false", "true", "low", "medium", "high"],
            value="false",
        ),
        IntInput(name="num_gpu", display_name="GPU Layers", value=-1, advanced=True,
                 # Explicit range_spec, or the frontend number widget refuses the minus sign
                 # and silently stores 0 = CPU-FORCED — the exact silent failure this field
                 # must never produce. Caught by the user 2026-08-06.
                 range_spec=RangeSpec(min=-1, max=999, step=1, step_type="int"),
                 info=("-1 = AUTO (recommended): the key is omitted and the serving host "
                       "offloads to GPU when it can — required for the GPU host, where 0 would "
                       "silently force a 27B model onto CPU. 0 = CPU, useful only on the "
                       "laptop whose CUDA backend is broken. Positive = explicit layer count. "
                       "The status line shows CPU-FORCED whenever 0 is in effect.")),
        StrInput(name="keep_alive", display_name="Keep Alive", value="30m", advanced=True,
                 info=("How long the model stays resident. Sent TOP-LEVEL — placing it inside "
                       "options silently does nothing. Longer avoids reload latency between runs.")),
        BoolInput(name="unload_before_run", display_name="Unload Before Run", value=False, advanced=True,
                  info="Correct for sweeps: stops one cell inheriting another cell's loaded context window."),
        IntInput(name="timeout_s", display_name="Timeout (s)", value=600, advanced=True,
                 info="Generous by default: a cold model load plus CPU inference on a large model can take minutes."),
        MultilineInput(name="system_prompt", display_name="System Prompt",
                       value="Extract structured data from the text.",
                       info=("Keep it about the TASK. Per-field instructions belong in each "
                             "variable's Description, where they travel with the schema and are "
                             "hashed with it — a system prompt is not part of the schema identity.")),
        DropdownInput(name="mode", display_name="Output Mode",
                      info=("json_schema sends the schema as `format` and is the only mode that "
                            "constrains generation — measured to enforce enums, required and "
                            "nesting, though NOT numeric ranges. json_object asks for 'some JSON'. "
                            "prompted puts the schema in the prompt and hopes."),
                      options=["json_schema", "json_object", "prompted"], value="json_schema"),
    ]

    outputs = [
        Output(name="client", display_name="Client", method="build_client", group_outputs=True),
        Output(name="capability", display_name="Capability Report", method="build_capability", group_outputs=True),
    ]

    _MODEL_CACHE: dict = {}

    async def update_build_config(self, build_config, field_value, field_name=None):
        """List models actually installed on this Ollama host.

        Mutation is in place (the return value is discarded) and every write is guarded by
        membership, because dotdict.__missing__ makes a typo'd key a silent no-op. Raising is
        the only loud channel, so an unreachable host or an empty model list raises rather
        than leaving an empty dropdown that looks like a UI glitch.
        """
        import httpx

        def put(name, key, value):
            if name in build_config and isinstance(build_config[name], dict):
                build_config[name][key] = value

        if field_name not in ("base_url", "model") and not build_config.get("is_refresh"):
            return build_config

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
            msg = (f"Cannot reach Ollama at {url}: {type(exc).__name__}. "
                   "From inside the container 'localhost' is the CONTAINER — use "
                   "host.docker.internal for the laptop, or the GPU host's LAN address. "
                   "Also check OLLAMA_HOST is 0.0.0.0 on the server and the port is open.")
            raise ValueError(msg) from exc

        models = tags.get("models") or []
        if not models:
            msg = (f"Ollama at {url} is running (v{version.get('version')}) but has NO models "
                   "pulled. Pull one on that host first, e.g.  ollama pull qwen3:0.6b  "
                   "then press refresh.")
            raise ValueError(msg)

        labels, meta = [], []
        for m in sorted(models, key=lambda x: x.get("name", "")):
            d = m.get("details") or {}
            bits = [b for b in (d.get("parameter_size"), d.get("quantization_level")) if b]
            labels.append(f"{m['name']}  [{', '.join(bits)}]" if bits else m["name"])
            meta.append({"name": m.get("name"), "digest": (m.get("digest") or "")[:12],
                         "family": d.get("family"), "parameter_size": d.get("parameter_size"),
                         "quantization_level": d.get("quantization_level"), "size_bytes": m.get("size")})
        self._MODEL_CACHE[url] = {m["name"]: m for m in meta}

        put("model", "options", labels)
        # options_metadata is deliberately NOT written: it is the banned pattern that
        # crashed the canvas twice via the OpenRouter client (see langflow-builder.md).
        # Plain string options have never crashed. Digests stay in _MODEL_CACHE server-side.
        put("model", "info", f"{len(models)} model(s) on {url} (Ollama v{version.get('version')}). "
                             "Only pulled models appear; pull on the host, then refresh.")
        current = str((build_config.get("model", {}) or {}).get("value") or "").split("  [")[0]
        if current and current not in self._MODEL_CACHE[url]:
            put("model", "value", "")
        return build_config

    def _spec(self) -> ollama.OllamaSpec:
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

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def build_client(self) -> Data:
        spec = self._spec()
        applied: list[str] = []
        ov = self._unwrap(getattr(self, "overrides", None))
        if ov:
            kwargs, applied = flowcfg.apply_client_overrides("ollama", spec.__dict__, ov)
            if applied:
                spec = ollama.OllamaSpec(**kwargs)
        gpu = "auto" if spec.num_gpu < 0 else (f"{spec.num_gpu} (CPU-FORCED!)" if spec.num_gpu == 0 else str(spec.num_gpu))
        self.status = f"{spec.model} @ ctx={spec.num_ctx} seed={spec.seed} think={spec.think} gpu={gpu}" + (
            f" | overrides: {','.join(applied)} ({ov.get('config_uid', 'no-uid')})" if applied else "")
        return Data(data={"provider": "ollama", "spec": spec.__dict__, "api_path": self.api_path,
                          **({"config_uid": ov.get("config_uid")} if applied and ov.get("config_uid") else {})})

    def build_capability(self) -> Data:
        spec = self._spec()
        risks = []
        if self.api_path != "/api/chat":
            risks += ["v1_silent_disable", "options_unreachable"]
        if self.think != "false":
            risks.append("thinking_changes_output")
        return Data(
            data={
                "provider": "ollama",
                "gate_effective": False,  # Ollama never rejects an unknown options key
                "enforcement_verified": {"enum": True, "required": True, "numeric_range": False},
                "numeric_range_note": "Ollama's grammar does not enforce minimum/maximum; the client-side validator is the only check.",
                "silent_failure_risk": risks,
                "resolved": {"num_ctx": spec.num_ctx, "seed": spec.seed, "temperature": spec.temperature},
            }
        )
