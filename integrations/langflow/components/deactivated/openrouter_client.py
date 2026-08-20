"""OpenRouter Client — configuration only, STATIC UI, no edit-time magic.

REWRITTEN 2026-08-06 after a day of frontend crashes. The previous version discovered
endpoints inside update_build_config and wrote the result into the node template as
dropdown options plus options_metadata; every saved node then carried that payload, and
reopening the flow — or clicking the endpoint dropdown — killed the whole canvas with the
full-screen "unexpected error" page. Root evidence: a controlled bisection (built-ins-only
flow saved/reopened clean; adding this component made save/reopen crash), and a
saved-vs-fresh template diff showing the endpoint dropdown payload as the only material
difference across five crashing flows. The 08-05 crash from nested options_metadata was
the same organ failing; scalars-only was necessary but not sufficient (the surviving
payload carried booleans, which the working Ollama dropdown's metadata never has).

Design rules now, for this component and its successors:

  1. NOTHING mutates the node template. No update_build_config, no options_metadata, no
     refresh buttons, no dynamic show/hide. What you drag is what the canvas renders.
  2. Discovery is a RUN-time output. `Endpoints` returns the annotated endpoint table as a
     DataFrame through the normal data path, which cannot touch the canvas renderer.
     Inspect it, pick a tag, paste it into Endpoint Tag.
  3. Enforcement stays at run time, in build_client: capability is resolved live for the
     pinned endpoint and conflicts RAISE — a raise renders as a red node error, never a
     dead canvas.
  4. Egress is env-driven, not UI-driven: EXTRCT_GATEWAY_URL set → gateway route (M-04);
     empty → direct. The  data-classification gate remains a required field, and the
     egress route actually used is recorded in every run record.

OUTSIDE THE WALL. Synthetic or de-identified/aggregate content only.
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

from extrct import openrouter
from extrct.hashing import key_fingerprint


class ExtrctOpenRouterClient(Component):
    display_name: str = "Client - OpenRouter"
    description: str = "Configure an OpenRouter call. OUTSIDE THE WALL — synthetic/de-identified content only."
    documentation: str = "docs/extraction-stack/api-notes.md"
    icon: str = "globe"
    name: str = "extrct_openrouter_client"

    inputs = [
        HandleInput(
            name="overrides", display_name="Overrides", required=False, input_types=["Data"],
            info=("Optional: a Prep - Flow Config payload. Keys 'openrouter.X' and 'client.X' "
                  "override this node's fields at the spec level (X must be a spec field — "
                  "typos raise; api_key is never overridable). Applied BEFORE the egress and "
                  "capability guards, so guards validate the overridden config. Unwired = "
                  "this node behaves exactly as its fields say."),
        ),
        # --- egress guard. Route selection is env-driven; see module docstring ---
        DropdownInput(
            name="data_classification",
            display_name="Data Classification",
            info=("REQUIRED. Empty refuses the call. No option permits raw note text. "
                  "Egress route is not a UI choice: EXTRCT_GATEWAY_URL set in the container "
                  "environment routes via the  gateway; empty goes direct (the gateway "
                  "does not exist yet, M-04). Whichever applies is stamped into the run record."),
            options=["", *openrouter.DATA_CLASSES],
            value="",
            required=True,
        ),
        # --- model & endpoint pin ---
        StrInput(
            name="model",
            display_name="Model",
            info=("The EXACT id sent, ':free' suffix included — a ':free' variant has its OWN "
                  "endpoint list, so a tag found on the paid slug 404s against it. To see what "
                  "this model offers, run the Endpoints output below and inspect the table."),
            value="google/gemma-4-26b-a4b-it",
            required=True,
        ),
        StrInput(
            name="endpoint_tag",
            display_name="Endpoint Tag",
            info=("The full tag from the Endpoints output (e.g. 'deepinfra/fp8'), pasted here. "
                  "Deliberately NOT a live dropdown: edit-time discovery wrote its results into "
                  "the saved node and crashed the canvas on reopen — twice, differently. The "
                  "Endpoints table shows which tags honour a json_schema request and which are "
                  "ZDR; build_client re-checks the pin against the live endpoint at run time "
                  "and fails loudly on a conflict."),
            value="",
        ),
        BoolInput(name="require_endpoint_pin", display_name="Require Endpoint Pin",
                  info="Default routing re-ranks every ~5 minutes, so an unpinned run is not reproducible.",
                  value=True),
        # --- routing: four non-vendor defaults ---
        BoolInput(name="require_parameters", display_name="Require Parameters",
                  info=("Measured: converts a silent capability downgrade into a loud routing "
                        "refusal (404). Pinned to an endpoint without structured_outputs, a "
                        "json_schema request refuses to route instead of returning prose."),
                  value=True),
        BoolInput(name="allow_fallbacks", display_name="Allow Fallbacks",
                  info="Vendor default true lets a pinned run execute elsewhere at a different quantization and still return 200.",
                  value=False),
        BoolInput(name="zdr", display_name="Zero Data Retention",
                  info=("Requests endpoints that do not retain your prompts. STRICTLY STRONGER "
                        "than Data Collection, which covers training only - a provider can "
                        "decline to train and still retain for abuse-scanning. ORs with your "
                        "OpenRouter account settings. The Endpoints table has a zdr column."),
                  value=True),
        DropdownInput(name="data_collection", display_name="Data Collection",
                      info="Vendor default 'allow' permits providers that TRAIN on inputs. Covers training only, hence ZDR too.",
                      options=["deny", "allow"], value="deny"),
        # --- structured output ---
        DropdownInput(name="mode", display_name="Output Mode",
                      info=("json_schema sends the full schema and is the only mode that can "
                            "constrain generation. json_object asks for 'some JSON' with no "
                            "shape. prompted puts the schema in the prompt and hopes. 30 of 338 "
                            "models accept a json_schema request shape while silently falling "
                            "back to unconstrained JSON - which is what Require Parameters "
                            "exists to prevent."),
                      options=["json_schema", "json_object", "prompted"], value="json_schema"),
        BoolInput(name="response_format_strict", display_name="Provider Strict Enforcement",
                  info="DISTINCT from the Schema Builder's Encoding: this asks the provider to hard-enforce and does not change the schema.",
                  value=True),
        # --- sampling: tri-state ---
        DropdownInput(name="send_seed", display_name="Send Seed",
                      info="'auto' emits only if the pinned endpoint declares it. 'always' on an endpoint without it makes Require Parameters refuse to route - build_client checks and fails on the canvas.",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER], value=openrouter.AUTO, advanced=True),
        IntInput(name="seed", display_name="Seed",
                 info="Sent only when Send Seed resolves to emit. Determinism is not guaranteed by any provider, so treat replay equality as a measured RATE rather than an assumed property - the run record hashes the response so you can measure it.",
                 value=42),
        DropdownInput(name="send_temperature", display_name="Send Temperature",
                      info="'auto' emits only if the pinned endpoint declares temperature. 51 of 338 models - every OpenAI reasoning model and the Anthropic thinking models - do not accept it; build_client checks 'always' against the pin and fails on the canvas instead of failing to route.",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER], value=openrouter.AUTO, advanced=True),
        FloatInput(name="temperature", display_name="Temperature",
                   info="0.0 for extraction: the task is transcription against a schema, not generation. Sent only when Send Temperature resolves to emit.",
                   value=0.0),
        DropdownInput(name="send_top_p", display_name="Send Top P",
                      info="'never' by default: inert at temperature 0, and every extra key narrows the routing pool.",
                      options=[openrouter.AUTO, openrouter.ALWAYS, openrouter.NEVER], value=openrouter.NEVER, advanced=True),
        # --- logprobs ---
        BoolInput(name="logprobs", display_name="Token Logprobs", value=False,
                  info=("Returns each generated token's log-probability, with alternatives when "
                        "Top Logprobs > 0. ENDPOINT-DEPENDENT: check the logprobs column in the "
                        "Endpoints output — build_client fails loudly if the pin lacks it. Under "
                        "strict json_schema, probabilities are conditioned on the grammar mask; "
                        "interpret per-field confidence accordingly.")),
        IntInput(name="top_logprobs", display_name="Top Logprobs", value=0, advanced=True,
                 range_spec=RangeSpec(min=0, max=20, step=1, step_type="int"),
                 info="Alternatives per position (0-20). Only sent when Token Logprobs is on."),
        IntInput(name="max_output_tokens", display_name="Max Output Tokens",
                 info="Emitted as max_tokens or max_completion_tokens depending on what the pinned endpoint declares - the same model can want different key names on different endpoints, and hardcoding either silently prunes half of them under Require Parameters. Too low truncates mid-JSON and the run fails rather than returning a fragment.",
                 value=2048),
        # --- reasoning ---
        DropdownInput(name="reasoning_control", display_name="Reasoning",
                      info="'off' SENDS {enabled:false}: 69/338 models reason by default, so omitting the key is not a no-reasoning baseline.",
                      options=["off", "effort", "budget"], value="off"),
        StrInput(name="reasoning_effort", display_name="Reasoning Effort",
                 info="Only used when Reasoning is 'effort'. Valid values are PER MODEL - low/medium/high is not universal, and tuples like ['xhigh','high'] or ['max','high','low'] exist. Check the model's reasoning.supported_efforts before setting it.",
                 value="", advanced=True),
        # --- plugins ---
        BoolInput(name="response_healing", display_name="Server-Side Response Healing",
                  info="OFF by default: it repairs JSON invisibly and unlogged, which would record 'ok' where 'repaired' is true.",
                  value=False, advanced=True),
        MultilineInput(name="system_prompt", display_name="System Prompt",
                       info="Sent as the first message. Keep it about the TASK; per-field instructions belong in each variable's Description, where they travel with the schema and are hashed with it.",
                       value="Extract structured data from the text."),
        IntInput(name="timeout_s", display_name="Timeout (s)",
                 info="Per-request ceiling. Raise it for reasoning models, which can spend minutes before emitting a first token.",
                 value=120, advanced=True),
        BoolInput(name="store_input_text", display_name="Store Input Text",
                  info=". OFF stores only a sha256 of the input in extraction_run, so the log stays clean even if real note text is processed by mistake. Turning it on is a decision-log event, not a convenience.",
                  value=False, advanced=True),
        StrInput(
            name="api_key_env_var",
            display_name="API Key Env Var",
            info=("NAME of the container environment variable holding the key — never the key "
                  "itself. Read at send time inside the extractor, so the secret never enters "
                  "the flow, the canvas data, or an export; only a sha256 prefix reaches a run "
                  "record. Set the variable in deploy/.env, then RECREATE the container "
                  "(docker compose --profile prototype up -d, not restart)."),
            value="OPENROUTER_API_KEY",
            advanced=True,
        ),
    ]

    outputs = [
        Output(name="client", display_name="Client", method="build_client", group_outputs=True),
        Output(name="endpoints", display_name="Endpoints", method="build_endpoints", group_outputs=True),
        Output(name="capability", display_name="Capability Report", method="build_capability", group_outputs=True),
    ]

    def _spec(self, capability: dict | None = None) -> openrouter.OpenRouterSpec:
        # Egress is env-driven (M-04): EXTRCT_GATEWAY_URL set -> gateway; empty -> direct.
        gateway = (os.environ.get("EXTRCT_GATEWAY_URL") or os.environ.get("EXTRCT_GATEWAY_URL", "")).strip()
        return openrouter.OpenRouterSpec(
            data_classification=self.data_classification or "",
            egress_route="gateway" if gateway else "direct",
            allow_direct_egress=not gateway,
            gateway_url=gateway,
            store_input_text=bool(self.store_input_text),
            # api_key stays empty on purpose: extrct.openrouter.resolve_key() reads
            # the environment at SEND time, so the secret never rides the canvas as data.
            api_key="",
            api_key_env_var=str(getattr(self, "api_key_env_var", "") or "OPENROUTER_API_KEY").strip(),
            model=(self.model or "").strip(),
            # ".split('  [')" tolerates a label pasted from the old dropdown era.
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

    async def _capability(self) -> dict:
        tag = str(self.endpoint_tag or "").split("  [")[0].strip()
        if not tag:
            return {}
        import httpx

        async with httpx.AsyncClient() as http:
            return await openrouter.resolve_capability((self.model or "").strip(), tag, http=http)

    def _check_conflicts(self, spec: openrouter.OpenRouterSpec, cap: dict) -> None:
        """The guards that used to live in the (removed) dynamic UI. Raising here renders
        as a red node error on the canvas — loud, local, and harmless to the renderer."""
        if not cap:
            return
        supported = cap.get("supported_parameters") or []
        if spec.mode == "json_schema" and not cap.get("supports_structured_outputs"):
            msg = (f"{cap.get('endpoint_tag')} does not declare 'structured_outputs', so a "
                   f"json_schema request is refused (404) by Require Parameters. That refusal "
                   f"is CORRECT — the alternative is a silent downgrade to unconstrained JSON. "
                   f"Run the Endpoints output and pick a tag with structured_outputs=True, or "
                   f"change Output Mode.")
            raise ValueError(msg)
        for policy, key in ((spec.send_seed, "seed"), (spec.send_temperature, "temperature"),
                            (spec.send_top_p, "top_p")):
            if policy == openrouter.ALWAYS and key not in supported:
                msg = (f"Send {key} is 'always' but {cap.get('endpoint_tag')} does not declare "
                       f"{key!r}; with Require Parameters on, this run would FAIL TO ROUTE. "
                       f"Set it to 'auto', or pick an endpoint that declares {key!r}.")
                raise ValueError(msg)
        if spec.logprobs and "logprobs" not in supported:
            msg = (f"Token Logprobs is on but {cap.get('endpoint_tag')} does not declare "
                   f"'logprobs'; this run would fail to route. Run the Endpoints output and "
                   f"pick a tag with logprobs=True (e.g. most bf16 endpoints declare it).")
            raise ValueError(msg)

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    async def build_client(self) -> Data:
        cap = await self._capability()
        spec = self._spec(cap)
        applied: list[str] = []
        ov = self._unwrap(getattr(self, "overrides", None))
        if ov:
            kwargs, applied = flowcfg.apply_client_overrides("openrouter", spec.__dict__, ov)
            if applied:
                spec = openrouter.OpenRouterSpec(**kwargs)
        openrouter.check_egress(spec)  # fail on the canvas, not mid-batch
        self._check_conflicts(spec, cap)
        # Resolve now so a missing env var fails HERE, on the canvas, not mid-batch. The
        # fingerprint makes a 401 unambiguous about which credential was actually used -
        # its absence cost two long debugging sessions.
        key = openrouter.resolve_key(spec)
        source = f"env:{spec.api_key_env_var}"
        self.status = (
            f"{spec.model} @ {spec.endpoint_tag or 'UNPINNED'} | {spec.data_classification} "
            f"| egress:{spec.egress_route} | key:{source}:{key_fingerprint(key)}"
            + (f" | overrides: {','.join(applied)} ({ov.get('config_uid', 'no-uid')})" if applied else "")
        )
        return Data(data={"provider": "openrouter", "spec": spec.__dict__, "key_source": source,
                          **({"config_uid": ov.get("config_uid")} if applied and ov.get("config_uid") else {})})

    async def build_endpoints(self) -> DataFrame:
        """Live endpoint discovery as DATA, replacing the dropdown that crashed the canvas.

        Run this output, read the table, paste the chosen tag into Endpoint Tag. All
        endpoints are listed — filtering happens in your head, not silently here — with
        the columns the decision actually needs: schema capability, ZDR, quantization.
        """
        model = (self.model or "").strip()
        if not model:
            msg = "Set Model first — the exact id, ':free' suffix included."
            raise ValueError(msg)
        import httpx

        async with httpx.AsyncClient() as http:
            eps = await openrouter.list_endpoints_annotated(model, http=http)
        rows = [{
            "tag": e.get("tag") or "",
            "provider": e.get("provider_name") or "",
            "structured_outputs": bool(e.get("structured_outputs")),
            "response_format": bool(e.get("response_format")),
            "zdr": bool(e.get("zdr")),
            "seed": bool(e.get("seed")),
            "temperature": bool(e.get("temperature")),
            "logprobs": "logprobs" in (e.get("supported_parameters") or []),
            "quantization": e.get("quantization") or "",
            "context_length": e.get("context_length") or 0,
            "max_completion_tokens": e.get("max_completion_tokens") or 0,
            "status": e.get("status") if isinstance(e.get("status"), int) else 0,
            "supported_parameters": ",".join(e.get("supported_parameters") or []),
        } for e in eps]
        schema_n = sum(1 for r in rows if r["structured_outputs"])
        zdr_n = sum(1 for r in rows if r["zdr"])
        self.status = (f"{len(rows)} endpoint(s) for {model}: {schema_n} schema-capable, "
                       f"{zdr_n} ZDR. Copy a tag into Endpoint Tag.")
        return DataFrame(rows)

    async def build_capability(self) -> Data:
        cap = await self._capability()
        if not cap:
            return Data(data={"note": "no endpoint_tag pinned; capability gating is unavailable"})
        self.status = f"{cap['endpoint_tag']} | structured_outputs={cap['supports_structured_outputs']}"
        return Data(data=cap)
