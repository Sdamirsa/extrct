"""XAI - Evidence Grounding — CONFIG AUTHOR for the router's grounding step (2026-08-14).

Hard cutover from the executor era: this node no longer aligns anything itself. It
authors the grounding section of the pipeline and hands it to Run - Structured
Extract's Grounding Config input; the router injects the evidence mirror into the
schema AT REQUEST TIME (killing the old R1 rule at the root — no more evidence-less
payloads meeting the aligner) and runs the alignment per chunk as a NON-BREAKING step
(a grounding failure is a recorded step, never a lost extraction). The alignment logic
itself is unchanged and lives in extrct.grounding / extrct.pipeline; post-hoc grounding
of STORED runs remains available through those functions.

Modes: inline and auto both mean "inject and align" (the router can always inject);
posthoc (a second grounding call) is next-wave in-router and is recorded as a skipped
step, never silently ignored. The pre-router executor node and the Posthoc Templater
are parked in deactivated/ — old dragged nodes keep their frozen behavior.

The Overrides input still takes the Flow - Controller's Grounding Config thread:
grounding.* keys apply over this node's widget values before the config is emitted, so
document-driven flows steer it exactly as before. VALUES are validated too since
2026-08-14 (flow_model.validate_section): owned_subset checks key NAMES only, and a
`grounding.mode='Inline'` typo from a Flow - Config table used to ride through — the node
reported "grounding ON | mode Inline" while the router skipped injection and the failure
blamed the model.
"""

from lfx.custom.custom_component.component import Component
from lfx.field_typing.range_spec import RangeSpec
from lfx.io import BoolInput, DropdownInput, FloatInput, HandleInput, Output
from lfx.schema.data import Data

from extrct.config import owned_subset
from extrct.flow_model import GROUNDING_KEYS, validate_section
from extrct.hashing import content_uid


class ExtrctGrounding(Component):
    display_name: str = "XAI - Evidence Grounding"
    description: str = "Author the grounding step: evidence injected at request time, every quote aligned per chunk."
    documentation: str = "docs/system-arch/extraction-stack/grounding-certainty-design.md"
    icon: str = "crosshair"
    name: str = "extrct_grounding"

    inputs = [
        BoolInput(name="enabled", display_name="Enabled", value=True,
                  info=("ON: every extracted value must come with a verbatim quote, and each quote "
                        "is checked word-for-word against the source document (per chunk). A quote "
                        "that cannot be found is flagged as unverified evidence. OFF: no evidence "
                        "is requested or checked; the run records grounding as skipped.")),
        DropdownInput(name="mode", display_name="Mode", options=["auto", "inline", "posthoc"],
                      value="auto",
                      info=("Leave on auto. auto and inline behave identically: the extraction is "
                            "asked to quote its evidence, and each quote is checked against the "
                            "source. posthoc (a second grounding call) is NOT yet available — "
                            "selecting it makes the router skip grounding for this run, and the "
                            "skip is recorded with that reason.")),
        FloatInput(name="fuzzy_threshold", display_name="Fuzzy Threshold", value=0.75, advanced=True,
                   range_spec=RangeSpec(min=0.5, max=1.0, step=0.05, step_type="float"),
                   info=("Minimum fraction of quote tokens that must match for a fuzzy alignment. "
                         "Quotes scoring below it are reported as unlocated (unverified evidence) — "
                         "lower the value to tolerate small wording differences. 0.75 is the "
                         "benchmarked default; 1.0 demands that EVERY quoted token be found in "
                         "order (it still allows words in between, so it is not exact-match).")),
        BoolInput(name="log_to_db", display_name="Annotate Run In Postgres", value=True, advanced=True,
                  info=("Saves the per-field grounding results onto each stored run row in "
                        "Postgres, so past runs can be queried by grounding outcome. It is an "
                        "annotation added after the run — it never changes the extraction. Safe to "
                        "leave ON.")),
        HandleInput(name="overrides", display_name="Overrides", required=False, input_types=["Data"],
                    info=("Optional: Flow - Controller's Grounding Config thread (grounding.* keys "
                          "apply over the widget values; unknown keys AND bad values raise). "
                          "Unwired = as set here.")),
    ]

    outputs = [
        Output(name="grounding_config", display_name="Grounding Config", method="build_config"),
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
            "mode": self.mode or "auto",
            "fuzzy_threshold": float(self.fuzzy_threshold or 0.75),
            "log_to_db": bool(self.log_to_db),
        }
        ov = self._unwrap(getattr(self, "overrides", None))
        config_uid = None
        applied: list[str] = []
        if ov:
            # Keys via owned_subset, VALUES via validate_section (types + vocabulary +
            # the [0.5, 1.0] fuzzy range) — a typo must die here, not in the router.
            sub = validate_section("grounding", owned_subset(ov, "grounding", GROUNDING_KEYS))
            cfg.update(sub)
            applied = sorted(sub)
            config_uid = ov.get("config_uid")
        payload = {"grounding": cfg, "config_uid": config_uid or content_uid({"grounding": cfg})}
        if not cfg["enabled"]:
            self.status = f"grounding OFF (recorded skipped) | {payload['config_uid']}"
            return Data(data=payload)
        mode = f"mode {cfg['mode']}"
        if cfg["mode"] == "posthoc":
            mode = "mode posthoc (WILL BE SKIPPED — not yet available; use auto)"
        ovnote = f" | overrides: {','.join(applied)}" if applied else ""
        self.status = (f"grounding ON | {mode} | fuzzy {cfg['fuzzy_threshold']} | "
                       f"{payload['config_uid']}{ovnote}")
        return Data(data=payload)
