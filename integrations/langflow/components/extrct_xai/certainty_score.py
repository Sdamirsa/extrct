"""XAI - Certainty Score — CONFIG AUTHOR for the router's certainty step (2026-08-14).

Hard cutover from the executor era: this node no longer computes statistics itself. It
authors the certainty section and hands it to Run - Structured Extract's Certainty
Config input. Two things happen there:

- REQUEST RIDERS (killing the old R2/R3 rules at the root): certainty-enabled merges
  logprobs=true and top_logprobs>=N into the client spec, UPGRADE-ONLY — a client
  authored stronger is never downgraded. An under-provisioned client can no longer
  produce an all-UNAVAILABLE certainty table.
- Per-chunk scoring as a NON-BREAKING step: the router runs extrct.confidence on each
  chunk's provider payload (engine and request mode come from its own request record —
  no DB round-trip) and records failure without losing the extraction.

The statistics themselves are unchanged (extrct.confidence). The pre-router executor
node is parked in deactivated/; old dragged nodes keep their frozen behavior. The
Overrides input still takes the Flow - Controller's Certainty Config thread — and since
2026-08-14 its VALUES are validated too (flow_model.validate_section), not only its key
names, which is what owned_subset checks.
"""

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import BoolInput, HandleInput, IntInput, Output
from lfx.schema.data import Data

from extrct.config import owned_subset
from extrct.flow_model import CERTAINTY_KEYS, validate_section
from extrct.hashing import content_uid


class ExtrctCertaintyScore(Component):
    display_name: str = "XAI - Certainty Score"
    description: str = "Author the certainty step: logprob riders at request time, per-variable scoring per chunk."
    documentation: str = "docs/system-arch/extraction-stack/grounding-certainty-design.md"
    icon: str = "gauge"
    name: str = "extrct_certainty_score"

    inputs = [
        BoolInput(name="enabled", display_name="Enabled", value=True,
                  info=("ON: the router asks the provider for token probabilities (logprobs) with "
                        "every request — raising the provider's settings if needed, never lowering "
                        "them — and scores every variable per chunk. OFF: the step is recorded as "
                        "skipped and requests go out unchanged.")),
        BoolInput(name="enum_posterior", display_name="Enum Option Probabilities", value=True,
                  info=("For multiple-choice (enum) variables, report a probability for each answer "
                        "option, computed from the model's alternatives for the first answer token. "
                        "Leave ON — this is the primary certainty statistic for enums. If two "
                        "options start with the same token the score is refused with a stated "
                        "reason instead of being reported wrong.")),
        IntInput(name="top_logprobs", display_name="Top Logprobs (minimum)", value=3, advanced=True,
                 range_spec=RangeSpec(min=1, max=10, step=1, step_type="int"),
                 info=("How many alternative tokens the provider must return per position. The "
                       "router raises the provider's setting to at least this and never lowers it. "
                       "Keep it at 3 or higher — enum option probabilities need at least 3 "
                       "alternatives.")),
        BoolInput(name="log_to_db", display_name="Annotate Run In Postgres", value=True, advanced=True,
                  info=("ON: each run row in Postgres gets the per-variable certainty scores "
                        "(field_logprobs) attached, so results can be audited later. OFF: the "
                        "scores appear only in the run output, not in the database.")),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: Flow - Controller's Certainty Config thread (certainty.* keys "
                          "apply over the widget values; unknown keys AND bad values raise). "
                          "Unwired = as set here.")),
    ]

    outputs = [
        Output(name="certainty_config", display_name="Certainty Config", method="build_config"),
    ]

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def build_config(self) -> Data:
        cfg = {
            "enabled": bool(self.enabled),
            "enum_posterior": bool(self.enum_posterior),
            "top_logprobs": int(self.top_logprobs or 3),
            "log_to_db": bool(self.log_to_db),
        }
        ov = self._unwrap(getattr(self, "overrides", None))
        config_uid = None
        applied: list[str] = []
        if ov:
            # Keys via owned_subset, VALUES via validate_section (types + vocabulary) —
            # a typo must die here, not silently change what the router requests.
            sub = validate_section("certainty", owned_subset(ov, "certainty", CERTAINTY_KEYS))
            cfg.update(sub)
            applied = sorted(sub)
            config_uid = ov.get("config_uid")
        payload = {"certainty": cfg, "config_uid": config_uid or content_uid({"certainty": cfg})}
        ovnote = f" | overrides: {','.join(applied)}" if applied else ""
        if not cfg["enabled"]:
            # enum_posterior / top_logprobs describe things that will not happen.
            self.status = f"certainty OFF (recorded skipped) | {payload['config_uid']}{ovnote}"
            return Data(data=payload)
        self.status = (f"certainty ON | enum options {cfg['enum_posterior']} | "
                       f"top_logprobs>={cfg['top_logprobs']} | {payload['config_uid']}{ovnote}")
        return Data(data=payload)
