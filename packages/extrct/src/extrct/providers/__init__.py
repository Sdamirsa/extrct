"""The provider registry — how backends plug in without touching core.

Everything downstream (the pipeline, client definitions, config overrides, the batch
lane) resolves backends through `get_provider(name)`. To add one:

    from extrct.providers import register_provider
    from extrct.providers.base import Provider

    class VllmProvider(Provider):
        name = "vllm"
        ...

    register_provider(VllmProvider())

See docs/providers.md for the full extension guide (including the roadmap providers:
vLLM, Cerebras, Fireworks — input logprobs and sampled token probabilities).
"""

from __future__ import annotations

from .base import FORBIDDEN_CLIENT_KEYS, Provider
from .ollama import OllamaProvider, OllamaSpec
from .openrouter import OpenRouterProvider, OpenRouterSpec

_REGISTRY: dict[str, Provider] = {}


def register_provider(provider: Provider, *, replace: bool = False) -> Provider:
    """Add a backend to the registry. Loud on name collisions unless `replace`."""
    name = (provider.name or "").strip()
    if not name:
        msg = f"{type(provider).__name__} has no name; set the `name` class attribute"
        raise ValueError(msg)
    if provider.spec_cls is None:
        msg = f"provider {name!r} has no spec_cls; every provider needs a frozen spec dataclass"
        raise ValueError(msg)
    if name in _REGISTRY and not replace:
        msg = f"provider {name!r} is already registered ({type(_REGISTRY[name]).__name__}); pass replace=True to swap it"
        raise ValueError(msg)
    _REGISTRY[name] = provider
    return provider


def get_provider(name: str) -> Provider:
    """Resolve a backend by name. Loud on unknown, listing what IS registered."""
    p = _REGISTRY.get(name)
    if p is None:
        msg = f"unknown provider {name!r}; registered: {provider_names()}"
        raise ValueError(msg)
    return p


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# The built-ins. Anything else registers itself the same way.
register_provider(OllamaProvider())
register_provider(OpenRouterProvider())

__all__ = [
    "FORBIDDEN_CLIENT_KEYS",
    "OllamaProvider",
    "OllamaSpec",
    "OpenRouterProvider",
    "OpenRouterSpec",
    "Provider",
    "get_provider",
    "provider_names",
    "register_provider",
]
