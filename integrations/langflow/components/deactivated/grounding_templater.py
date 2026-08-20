"""Posthoc Templater — compose the grounding task for the second extract, in one node.

Replaces the three-stock-node dance (Type Convert + Prompt Template + a shared Text
Input) with a single purpose-built step:

    [Run - Structured Extract #1] ─Extracted──> [Posthoc Templater] ─Grounding Task──┐
    source text ──────────────────────────────>                                      │
    [Prep - Schema Builder] ─Grounding Schema──────────> [Run - Structured Extract #2] <┘
    [Extract #2] ─Extracted ({"_evidence": ...})──> [XAI - Evidence Grounding]

Pure string composition — no LLM, no network. `_evidence` is stripped from the values
before composing (in case an evidence-ON extraction is wired), so the grounding model
sees only the values it must support. The default instruction carries the measured
lesson: models echo inferred values as their own "evidence" unless told to quote the
sentence they inferred FROM.
"""

import json

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

DEFAULT_INSTRUCTION = (
    "For EVERY extracted value, return the VERBATIM supporting quote from the source "
    "document - exact characters, no paraphrase, no reordering. Keep array order. If a "
    "value is inferred (a category, a status), quote the sentence you inferred it FROM - "
    "never repeat the extracted value itself. null if nothing supports it."
)


class GroundingPosthocTemplater(Component):
    display_name: str = "XAI - Evidence Grounding - Posthoc Templater"
    description: str = "Compose the grounding task text for a second Structured Extract run."
    documentation: str = "docs/extraction-stack/grounding-certainty-design.md"
    icon: str = "file-input"
    name: str = "grounding_posthoc_templater"

    inputs = [
        HandleInput(name="extracted", display_name="Extracted", required=True, input_types=["Data"],
                    info="Extract #1's Extracted output (the values to be grounded)."),
        MessageTextInput(name="source_text", display_name="Source Text", required=True,
                         info="The SAME text the extraction ran on."),
        MultilineInput(name="instruction", display_name="Instruction", value=DEFAULT_INSTRUCTION,
                       advanced=True,
                       info=("Appended after the source and the values. The default encodes the "
                             "measured trap: without 'never repeat the value', models echo "
                             "inferred values instead of quoting the justifying sentence.")),
    ]

    outputs = [
        Output(name="grounding_task", display_name="Grounding Task", method="build_task"),
    ]

    @staticmethod
    def _unwrap(value) -> dict:
        if isinstance(value, list):
            value = value[0] if value else None
        d = getattr(value, "data", value)
        return d if isinstance(d, dict) else {}

    def build_task(self) -> Message:
        values = {k: v for k, v in self._unwrap(self.extracted).items() if k != "_evidence"}
        source = str(self.source_text or "")
        if not values:
            msg = "Extracted is empty. Wire Extract #1's Extracted output."
            raise ValueError(msg)
        if not source.strip():
            msg = "Source Text is empty. Wire the SAME text the extraction ran on."
            raise ValueError(msg)
        task = ("SOURCE DOCUMENT:\n" + source
                + "\n\nEXTRACTED DATA:\n" + json.dumps(values, ensure_ascii=False)
                + "\n\n" + str(self.instruction or DEFAULT_INSTRUCTION).strip())
        self.status = f"{len(values)} top-level value group(s), {len(task)} chars"
        return Message(text=task)
