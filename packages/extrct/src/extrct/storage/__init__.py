"""The run log — observability as a narrow, swappable interface.

A run row is EVIDENCE: it is written by the thing that produced the run, upserted
idempotently on its content-addressed run_uid, and never edited afterwards except
through the narrow `annotate_run` writer (which attaches deterministic derivations —
certainty, grounding — and can never touch `final_status`). The interface is deliberately
small so the backend is a swap, not a rewrite:

    ensure_schema  save_run  save_attempts  annotate_run
    find_completed  fetch_extracted  save_wrapping  save_merge_run

Backends:
  postgres  the reference backend (analysis joins, GIN-indexed tags, SQL views)
  sqlite    zero-setup local backend, same tables and semantics, stdlib only
  null      no-op (log_to_db off) — callers never need an `if store:` branch

Resume is the payoff of content addressing: `find_completed` makes a re-run of a
960-cell batch cost only the cells that never finished, because run identity derives
from (schema, model, input, config) rather than a sequence number.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

TERMINAL_STATUSES = ("ok", "repaired")


@runtime_checkable
class RunStore(Protocol):
    """What every backend implements. All methods are async; all are idempotent."""

    async def ensure_schema(self) -> None: ...
    async def save_run(self, record: dict[str, Any]) -> None: ...
    async def save_attempts(self, run_uid: str, attempts: list[dict]) -> None: ...
    async def annotate_run(self, run_uid: str, *, field_logprobs: dict | None = None,
                           field_grounding: dict | None = None) -> bool: ...
    async def find_completed(self, run_uids: list[str], statuses=TERMINAL_STATUSES) -> set[str]: ...
    async def fetch_extracted(self, run_uids: list[str]) -> dict[str, Any]: ...
    async def save_wrapping(self, doc: dict[str, Any], *, store_text: bool = False) -> int: ...
    async def save_merge_run(self, result: dict[str, Any], *, wrap_uid: str | None = None,
                             tags: list[str] | None = None) -> None: ...


class NullRunStore:
    """Logging off. Every write vanishes, every read says 'nothing stored'."""

    async def ensure_schema(self) -> None:
        return None

    async def save_run(self, record: dict[str, Any]) -> None:
        return None

    async def save_attempts(self, run_uid: str, attempts: list[dict]) -> None:
        return None

    async def annotate_run(self, run_uid: str, *, field_logprobs: dict | None = None,
                           field_grounding: dict | None = None) -> bool:
        return False

    async def find_completed(self, run_uids: list[str], statuses=TERMINAL_STATUSES) -> set[str]:
        return set()

    async def fetch_extracted(self, run_uids: list[str]) -> dict[str, Any]:
        return {}

    async def save_wrapping(self, doc: dict[str, Any], *, store_text: bool = False) -> int:
        return 0

    async def save_merge_run(self, result: dict[str, Any], *, wrap_uid: str | None = None,
                             tags: list[str] | None = None) -> None:
        return None


def open_store(cfg: dict[str, Any] | None) -> RunStore:
    """A storage section -> a store. Shapes:

        {"backend": "postgres", "dsn": "...");        # or dsn_env: VAR (default EXTRCT_PG_DSN)
        {"backend": "sqlite", "path": "runs.sqlite"}
        {"backend": "none"}                            # or cfg None

    Loud on an unknown backend or a missing DSN — a run that silently is not logged
    would violate the whole point of this package.
    """
    cfg = cfg or {}
    backend = str(cfg.get("backend") or "none").strip().lower()
    if backend in ("none", "off", ""):
        return NullRunStore()
    if backend == "sqlite":
        from .sqlite import SQLiteRunStore

        path = str(cfg.get("path") or "extrct_runs.sqlite")
        return SQLiteRunStore(path)
    if backend == "postgres":
        from .postgres import PostgresRunStore

        dsn = str(cfg.get("dsn") or "").strip()
        if not dsn:
            env_var = str(cfg.get("dsn_env") or "EXTRCT_PG_DSN").strip()
            dsn = os.environ.get(env_var, "").strip()
            if not dsn:
                msg = (f"storage backend 'postgres' but no DSN: set storage.dsn, or put it in "
                       f"the {env_var} environment variable")
                raise ValueError(msg)
        return PostgresRunStore(dsn)
    msg = f"unknown storage backend {backend!r}; known: none | sqlite | postgres"
    raise ValueError(msg)


from .record import run_row_from_single  # noqa: E402  (public re-export)

__all__ = [
    "NullRunStore",
    "RunStore",
    "TERMINAL_STATUSES",
    "open_store",
    "run_row_from_single",
]
