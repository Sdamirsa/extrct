"""extrct — the shared extraction core.

Deliberately free of any `lfx`/Langflow import. Langflow components wrap it; whatever
conductor runs the sweep imports the same functions. One implementation, two callers.

See docs/extraction-stack/ for the design and the measured API behaviour it defends against.
"""

from . import batch, builtins, call_model, client_model, flow_model, merging, ollama, openrouter, pipeline, repair, storage, wrapping
from .call_model import (
    CALL_MODEL_VERSION,
    call_definition,
    execute_call,
    parse_trigger_payload,
)
from .client_model import (
    CLIENT_MODEL_VERSION,
    build_client_payload,
    client_definition,
    definition_from_payload,
    spec_from_definition,
)
from .hashing import canonical_json, content_uid, key_fingerprint, sha256_text
from .runner import DEFAULT_CONCURRENCY, TransportError, call_many, call_once, get_json
from .repair import DEFAULT_LADDER, LAYERS, coerce_to_schema, run_ladder, validate
from .schema import (
    ENCODINGS,
    SchemaError,
    Variable,
    build_schema,
    schema_envelope,
    schema_uid,
    to_pydantic_source,
    unwrap_schema,
)

# Backend modules expose identically-named build_request / read_response / request_record,
# so they are reached as `ollama.build_request(...)` / `openrouter.build_request(...)`
# rather than flattened here - the backend must always be explicit at the call site.
OllamaSpec = ollama.OllamaSpec
OpenRouterSpec = openrouter.OpenRouterSpec

__all__ = [
    "CALL_MODEL_VERSION",
    "batch",
    "CLIENT_MODEL_VERSION",
    "call_definition",
    "call_model",
    "execute_call",
    "parse_trigger_payload",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_LADDER",
    "LAYERS",
    "build_client_payload",
    "client_definition",
    "client_model",
    "definition_from_payload",
    "spec_from_definition",
    "OllamaSpec",
    "OpenRouterSpec",
    "ENCODINGS",
    "SchemaError",
    "TransportError",
    "Variable",
    "build_schema",
    "builtins",
    "call_many",
    "coerce_to_schema",
    "call_once",
    "canonical_json",
    "content_uid",
    "get_json",
    "key_fingerprint",
    "ollama",
    "openrouter",
    "pipeline",
    "repair",
    "run_ladder",
    "storage",
    "validate",
    "schema_envelope",
    "schema_uid",
    "unwrap_schema",
    "sha256_text",
    "to_pydantic_source",
]

__version__ = "0.1.0"
