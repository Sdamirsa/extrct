# Setup 02 — the Langflow canvas stack (`deploy/`)

One operator, docker compose only, no Kubernetes. The authority for every detail —
logins, tracing wiring, telemetry audit, troubleshooting — is
[deploy/README.md](../../../deploy/README.md); this page is the ordered path through it.

## Prerequisites

- Docker Desktop **running** (tray whale idle; `docker info` answers), not merely installed.
- Compose **v2.20+** (`docker compose version`) — the manifest uses `include:`.

## Bring it up

```powershell
cd deploy
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1   # generates .env with random secrets
docker compose up -d                                                  # core: Postgres, Langfuse, Label Studio
docker compose --profile prototype up -d                              # + the Langflow canvas
```

First run pulls ~4 GB; Langfuse runs ClickHouse migrations on first boot, so give
`langfuse-web` a minute. Services and logins:
[deploy/README.md → Start](../../../deploy/README.md#start). Every published port binds
to `127.0.0.1` — nothing is reachable from another machine.

Optional but worth doing once: wire Langflow traces into Langfuse
([deploy/README.md → Wire Langflow traces](../../../deploy/README.md#wire-langflow-traces-into-langfuse)).

## The three gotchas that cost real hours

- **Langflow reads env at BOOT.** After editing `deploy/.env`:
  `docker compose --profile prototype up -d` — never `restart`.
- **Component edits need `--force-recreate langflow`.**
- **Moving a bind-mount source is silent.** Docker resolves host paths at container
  creation; a moved directory leaves a running container pointing at nothing, with no
  error anywhere. After any path change under `deploy/components/*/compose.yaml`:
  `--force-recreate` that service, then re-inspect with `docker inspect`.

## The data line

Langflow is treated as a remote-code-execution engine with a web UI (live CVE
record — see [integrations/langflow/README.md](../../../integrations/langflow/README.md)).
Synthetic and de-identified content only. **Never confidential text.**

## Verify

```bash
docker compose --project-directory deploy config --quiet   # manifest resolves
```

Then `/stack` (in a Claude Code session) checks containers, endpoints, telemetry,
and port bindings; component behaviour is verified *inside* the container, never by
reading docs ([integrations/langflow/README.md](../../../integrations/langflow/README.md)).
