"""Built-in schema sets seeded into the registry.

`extrct_schema_builder_schema` describes a list of variable rows — that is, it is the schema
the Schema Builder itself consumes. An agent given this as its `output_type` produces rows
that feed straight into Registry `save_variables` or the Builder's `Variables (from Registry)`
port, which is what makes agent-authored schemas possible at all.

The `description` fields are load-bearing: they are sent to the model as prompt text, so they
are where the authoring RULES live. Every rule encoded below is one the builder enforces and
would otherwise raise on — teaching them up front is cheaper than a rejected round trip.

`extrct_client_schema` is the same idea for clients (client-def/1.0): an agent given it as
`output_type` produces the flat shorthand document that Client - Definition validates and
Registry `save_client` stores. Its descriptions teach the measured traps (family-specific
`format` support, num_gpu=0 CPU-forcing, the  egress gate, endpoint pinning).
"""

from __future__ import annotations

BUILDER_SCHEMA_SET = "extrct_schema_builder_schema"

SCHEMA_BUILDER_VARIABLES: list[dict] = [
    {
        "name": "variables",
        "type": "object",
        "is_list": True,
        "parent": "",
        "options": "",
        "constraints": "",
        "required": True,
        "ordinal": 0,
        "description": (
            "One entry per field in the schema being designed. Order matters only for display. "
            "A field that contains other fields must be declared as its own entry with type "
            "'object', and its children declared as separate entries whose 'parent' is its name."
        ),
    },
    {
        "name": "name",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": '{"pattern": "^[a-z][a-z0-9_]*$"}',
        "required": True,
        "ordinal": 0,
        "description": (
            "The key this field will have in the extracted JSON. snake_case, starting with a "
            "letter. MUST be unique across the entire schema, including across different "
            "parents — two fields both called 'name' is an error even if they sit in different "
            "objects."
        ),
    },
    {
        "name": "type",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "str,int,float,bool,date,datetime,object",
        "required": True,
        "constraints": "",
        "ordinal": 1,
        "description": (
            "The data type. Use 'object' ONLY for a container that holds other fields; an "
            "'object' entry with no children is an error. Use 'date' for a calendar date and "
            "'datetime' when a time of day is present. Prefer 'str' with an enum in 'options' "
            "over a free-text 'str' whenever the possible values are known."
        ),
    },
    {
        "name": "is_list",
        "type": "bool",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": "",
        "required": True,
        "ordinal": 2,
        "description": (
            "True if the source may contain MANY of this field rather than one. Combined with "
            "type 'object' this produces a list of objects, which is how you express repeated "
            "structured items such as several diagnoses, each with its own attributes."
        ),
    },
    {
        "name": "description",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": '{"min_length": 10}',
        "required": True,
        "ordinal": 3,
        "description": (
            "Instructions to the extracting model for THIS field. This is the main determinant "
            "of extraction quality, so be specific: say what counts, what does not, which value "
            "to prefer when the source gives several, and what to do when the information is "
            "absent. 'The ejection fraction' is weak; 'LV ejection fraction as a percentage, "
            "using the biplane Simpson value if more than one is reported' is useful."
        ),
    },
    {
        "name": "parent",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": "",
        "required": False,
        "ordinal": 4,
        "description": (
            "The 'name' of the entry this field belongs to. Leave empty or null for a top-level "
            "field. The referenced entry MUST exist in this same list and MUST have type "
            "'object'. Nesting under a non-object, or naming a parent that does not exist, is "
            "an error."
        ),
    },
    {
        "name": "options",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": "",
        "required": False,
        "ordinal": 5,
        "description": (
            "Comma-separated list of the only permitted values, e.g. 'current,past,suspected'. "
            "Leave empty for free text. Use this whenever the possible values are known: an "
            "enumerated field is enforced during generation and can be counted across "
            "documents, whereas free text produces 'current', 'currently' and 'ongoing' for the "
            "same concept. Prefer the source's own vocabulary over an invented scale."
        ),
    },
    {
        "name": "constraints",
        "type": "str",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": "",
        "required": False,
        "ordinal": 6,
        "description": (
            "A JSON object of validation rules, or empty. Permitted keys: ge, le, gt, lt "
            "(numeric bounds), pattern (regular expression), min_length, max_length (strings), "
            "min_items, max_items (lists). Example: {\"ge\": 0, \"le\": 100}. Any other key is "
            "an error. Do not invent constraints the source cannot support."
        ),
    },
    {
        "name": "required",
        "type": "bool",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": "",
        "required": True,
        "ordinal": 7,
        "description": (
            "True if this field must always be present. Set False for information that is "
            "genuinely often absent from the source — an optional field returns an explicit "
            "null rather than a guess, which is the honest answer when a document simply does "
            "not state something."
        ),
    },
    {
        "name": "ordinal",
        "type": "int",
        "is_list": False,
        "parent": "variables",
        "options": "",
        "constraints": '{"ge": 0}',
        "required": False,
        "ordinal": 8,
        "description": "Display order among siblings. Use 0, 1, 2 within each parent. Cosmetic only.",
    },
]

CLIENT_SCHEMA_SET = "extrct_client_schema"

# The client analog of the builder schema: an agent given THIS as its output_type produces
# a flat client-def/1.0 shorthand document — exactly what Client - Definition accepts and
# what Registry save_client stores. Only the consequential fields appear here; every other
# spec field may also be given as a top-level key, and Client - Definition rejects unknown
# keys loudly with the full allowed list, so the schema teaches rather than gates.
CLIENT_SCHEMA_VARIABLES: list[dict] = [
    {
        "name": "provider", "type": "str", "is_list": False, "parent": "",
        "options": "ollama,openrouter", "constraints": "", "required": True, "ordinal": 0,
        "description": (
            "Which backend serves the call. 'ollama' is inside the wall (local/GPU host serving). "
            "'openrouter' is OUTSIDE the wall: only synthetic or de-identified/aggregate "
            "content may ever be sent to it, and data_classification MUST then be set. "
            "Any other setting of the chosen provider's spec may be added as an extra "
            "top-level key; unknown or misspelled keys are rejected with the allowed list, "
            "so never invent field names."
        ),
    },
    {
        "name": "model", "type": "str", "is_list": False, "parent": "",
        "options": "", "constraints": "", "required": True, "ordinal": 1,
        "description": (
            "The EXACT model id as served. Ollama: the tag as pulled, e.g. "
            "'gemma4:31b-it-q8_0'. OpenRouter: the exact slug INCLUDING any ':free' suffix "
            "- free variants expose entirely different endpoint lists than the paid slug, "
            "so the suffix is part of the identity, never decoration."
        ),
    },
    {
        "name": "base_url", "type": "str", "is_list": False, "parent": "",
        "options": "", "constraints": "", "required": False, "ordinal": 2,
        "description": (
            "Ollama only: the server URL, e.g. 'http://your-gpu-host:11434'. Omit to accept the "
            "deployment default. Never set for openrouter."
        ),
    },
    {
        "name": "mode", "type": "str", "is_list": False, "parent": "",
        "options": "json_schema,json_object,prompted", "constraints": "", "required": False, "ordinal": 3,
        "description": (
            "Structured-output mode. 'json_schema' asks the server to enforce the schema "
            "during generation - but enforcement is a MODEL-FAMILY property on Ollama "
            "(gemma4 honors it; qwen3.5/3.6 silently ignore it and return prose at HTTP "
            "200), so a new family must be probed before this mode is trusted. 'prompted' "
            "embeds the schema in the prompt instead and relies on the repair ladder. "
            "Default json_schema."
        ),
    },
    {
        "name": "num_ctx", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 512}', "required": False, "ordinal": 4,
        "description": (
            "Ollama only: context window in tokens. Too small does NOT error - the input "
            "is silently truncated and the extraction quietly degrades, so size it to the "
            "longest document plus the schema. Default 8192."
        ),
    },
    {
        "name": "num_predict", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 1}', "required": False, "ordinal": 5,
        "description": (
            "Ollama only: maximum generated tokens. A truncated generation is a failed "
            "extraction; large schemas need room. Default 1024."
        ),
    },
    {
        "name": "temperature", "type": "float", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 0}', "required": False, "ordinal": 6,
        "description": (
            "Sampling temperature. Extraction wants determinism: keep 0.0 unless the run "
            "is deliberately exploring variance. Default 0.0."
        ),
    },
    {
        "name": "seed", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": "", "required": False, "ordinal": 7,
        "description": "Sampling seed, for reproducibility across identical calls. Default 42.",
    },
    {
        "name": "num_gpu", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": -1}', "required": False, "ordinal": 8,
        "description": (
            "Ollama only: GPU offload layers. -1 (default) means AUTO - the key is omitted "
            "and the server offloads to GPU when it can. 0 forces CPU: never send 0 to a "
            "GPU host, it silently serves a 27B model from CPU for the whole keep_alive."
        ),
    },
    {
        "name": "timeout_s", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 1}', "required": False, "ordinal": 9,
        "description": (
            "HTTP timeout in seconds. Large local models on first load can need minutes; "
            "defaults are 600 (ollama) / 120 (openrouter)."
        ),
    },
    {
        "name": "data_classification", "type": "str", "is_list": False, "parent": "",
        "options": "synthetic,deidentified_aggregate", "constraints": "", "required": False, "ordinal": 10,
        "description": (
            "REQUIRED whenever provider is 'openrouter' (the egress gate refuses without "
            "it). Declares what the input text is. There is NO option that permits raw "
            "clinical note text - OpenRouter is outside the wall, full stop."
        ),
    },
    {
        "name": "endpoint_tag", "type": "str", "is_list": False, "parent": "",
        "options": "", "constraints": "", "required": False, "ordinal": 11,
        "description": (
            "OpenRouter only: the full endpoint tag to pin, from the client's Endpoints "
            "discovery output - never a bare provider slug. With require_parameters left "
            "at its default (true), a capability gap becomes a loud 404 instead of a "
            "silent downgrade. Definitions carry no fetched capability, so pinning here "
            "is what makes them reproducible."
        ),
    },
    {
        "name": "max_output_tokens", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 1}', "required": False, "ordinal": 12,
        "description": "OpenRouter only: maximum generated tokens. Default 2048.",
    },
    {
        "name": "logprobs", "type": "bool", "is_list": False, "parent": "",
        "options": "", "constraints": "", "required": False, "ordinal": 13,
        "description": (
            "Request per-token log-probabilities (the input to certainty scoring). "
            "Support is per endpoint/engine; MTP models emit first-token-only logprobs "
            "and are unsuitable for logprob work. Default false."
        ),
    },
    {
        "name": "top_logprobs", "type": "int", "is_list": False, "parent": "",
        "options": "", "constraints": '{"ge": 0, "le": 20}', "required": False, "ordinal": 14,
        "description": (
            "How many alternative tokens to return per position. Enum-posterior certainty "
            "REFUSES at 0 because no alternatives are visible - use 3 or more when "
            "certainty scores are wanted. Default 0."
        ),
    },
]

BUILTIN_SCHEMA_SETS: dict[str, list[dict]] = {
    BUILDER_SCHEMA_SET: SCHEMA_BUILDER_VARIABLES,
    CLIENT_SCHEMA_SET: CLIENT_SCHEMA_VARIABLES,
}
