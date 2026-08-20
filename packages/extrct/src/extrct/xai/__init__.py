"""XAI modules — analysis over what a run already produced, never part of producing it.

Both are pure functions of stored evidence (the provider payload, the schema, the
source text), so every number is re-derivable after the fact and the extraction run is
never perturbed by its own explanation:

  certainty   per-field confidence from token logprobs (mean / joint / min /
              first_token / margin, plus an enum option posterior)
  grounding   verbatim evidence-quote alignment against the source text
"""

from . import certainty, grounding
from .certainty import field_confidence, mask_state, normalize_logprobs
from .grounding import ALIGNER_VERSION, align, ground_fields, tokenize

__all__ = [
    "ALIGNER_VERSION",
    "align",
    "certainty",
    "field_confidence",
    "ground_fields",
    "grounding",
    "mask_state",
    "normalize_logprobs",
    "tokenize",
]
