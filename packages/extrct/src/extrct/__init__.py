"""extrct — schema-first structured extraction with LLMs.

Three layers, each importable on its own:

  facade    `Extractor` / `ExtractionResult` — a YAML job file in, extractions out
  engine    schema, pipeline, repair, wrapping, merging, batch, config, client_model
  adapters  providers (registry: ollama, openrouter, yours), storage (postgres,
            sqlite, null), xai (certainty, grounding)

The standards every layer keeps: content-addressed identity (uids are hashes of
content), privacy by default (input text is hashed, never stored, unless explicitly
enabled), evidence over convenience (records of what was actually sent and received),
loud failure (silent downgrades become errors), and versioned contracts at every
boundary (*-def documents). See CLAUDE.md and docs/architecture.md.
"""

from . import (
    batch,
    client_model,
    config,
    hashing,
    job,
    merging,
    models,
    pipeline,
    providers,
    repair,
    runner,
    schema,
    storage,
    telemetry,
    wrapping,
    xai,
)
from .client_model import (
    CLIENT_MODEL_VERSION,
    build_client_payload,
    client_definition,
    definition_from_payload,
    spec_from_definition,
)
from .extractor import ExtractionResult, Extractor
from .hashing import canonical_json, content_uid, key_fingerprint, sha256_text
from .job import JOB_MODEL_VERSION, load_job, parse_job
from .models import (
    MODEL_REGISTRY_VERSION,
    feature_status,
    model_registry,
    models_for,
    register_model,
)
from .providers import (
    OllamaSpec,
    OpenRouterSpec,
    Provider,
    get_provider,
    provider_names,
    register_provider,
)
from .repair import DEFAULT_LADDER, LAYERS, coerce_to_schema, run_ladder, validate
from .runner import DEFAULT_CONCURRENCY, TransportError, call_many, call_once, get_json
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
from .storage import NullRunStore, RunStore, open_store
from .xai import field_confidence, ground_fields

__all__ = [
    "CLIENT_MODEL_VERSION",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_LADDER",
    "ENCODINGS",
    "ExtractionResult",
    "Extractor",
    "JOB_MODEL_VERSION",
    "LAYERS",
    "NullRunStore",
    "OllamaSpec",
    "OpenRouterSpec",
    "Provider",
    "RunStore",
    "SchemaError",
    "TransportError",
    "Variable",
    "batch",
    "build_client_payload",
    "build_schema",
    "call_many",
    "call_once",
    "canonical_json",
    "client_definition",
    "client_model",
    "coerce_to_schema",
    "config",
    "content_uid",
    "definition_from_payload",
    "field_confidence",
    "get_json",
    "get_provider",
    "ground_fields",
    "hashing",
    "job",
    "key_fingerprint",
    "load_job",
    "merging",
    "open_store",
    "parse_job",
    "pipeline",
    "provider_names",
    "providers",
    "register_provider",
    "repair",
    "run_ladder",
    "runner",
    "schema",
    "schema_envelope",
    "schema_uid",
    "sha256_text",
    "telemetry",
    "spec_from_definition",
    "storage",
    "to_pydantic_source",
    "unwrap_schema",
    "validate",
    "wrapping",
    "xai",
]

__version__ = "0.1.0"
