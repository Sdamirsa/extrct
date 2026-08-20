# extrct stack deployment

Milestone **M-02** (dev environment bootstrap) and to-do ****. The one-operator bootstrap test this has
to pass: a fresh machine reaches a running stack in under half a day, with docker compose
only and no Kubernetes.

## Start

Docker Desktop must be running first — the whale icon in the tray, not just installed.

```powershell
cd deploy; powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1; docker compose up -d
```

The `-ExecutionPolicy Bypass` applies to that one invocation only and changes nothing on
the machine. Windows ships with the policy set to `Restricted`, so a bare
`.\bootstrap.ps1` is refused. If you would rather not type it every time, you can set
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once — that is a persistent change to
your security settings, so it is your call, not a prerequisite.

First run pulls ~4 GB and takes a few minutes; Langfuse runs ClickHouse migrations on
first boot, so give `langfuse-web` a minute before it answers.

| Service | URL | Login |
|---|---|---|
| Langfuse | http://localhost:3000 | sign up on first visit — the account is local |
| Label Studio | http://localhost:8080 | `LABEL_STUDIO_*` in `deploy/.env` |
| System DB (Postgres) | `localhost:5432` | `EXTRCT_DB_*` in `deploy/.env` |

Add the Langflow canvas:

```bash
docker compose --profile prototype up -d
```

| Service | URL | Login |
|---|---|---|
| Langflow | http://localhost:7860 | `LANGFLOW_SUPERUSER*` in `deploy/.env` |

Stop, or wipe everything including volumes:

```bash
docker compose --profile prototype down
```

```bash
docker compose --profile prototype down -v
```

## Wire Langflow traces into Langfuse

Optional, and worth doing once — it means prototype runs and real runs land in the same
trace store instead of a second silo.

1. Open Langfuse → create a project → copy its public and secret API keys.
2. Paste them into `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in `deploy/.env`.
3. `docker compose --profile prototype up -d` — Compose recreates only Langflow.

## Layout

```
deploy/
  compose.yaml                     root manifest — an `include:` list, nothing else
  bootstrap.ps1                    generates .env with random secrets
  .env.example                     every variable, documented
  components/
    core/compose.yaml              extrct-postgres + Label Studio        always on
    langfuse/compose.yaml          Langfuse v4, six services            always on
    langflow/compose.yaml          Langflow + its own Postgres          profiles: [prototype]
```

Adding a component = drop a folder in, add one line to `compose.yaml`. Gateway, eval, and
simclinic are stubbed as commented lines in the manifest; they need `packages/` first.

**Why `include:` and not stacked `-f` files.** With `-f a.yaml -f b.yaml`, relative paths
in *every* file resolve against the **first** file's directory — so a component file
referencing `../../../integrations/langflow/components` breaks the moment it is composed from a
different entry point. With `include:`, paths resolve against the included file's own
directory, which is what makes these files self-contained and movable. Keep `-f` overrides
for environment *variants* of the same stack (laptop vs. air-gapped hospital box), which is
what override files are actually for. Requires Compose **v2.20+** — check with
`docker compose version`.

Three things that bite when writing more of these:

- `include:` is unconditional. Optionality comes from `profiles:` on the services.
- Name collisions across included files **warn and refuse to merge**. Namespace everything
  per component (`langfuse-postgres`, not `postgres`) — this is why the Langfuse services
  are renamed from upstream.
- Services with no `profiles:` key always start, which is exactly why core and Langfuse are
  unprofiled and Langflow is not.

There are no custom Docker networks here, deliberately. Everything shares the project
default network so Langflow can reach Langfuse; isolation between containers on a
single-user workstation buys little, and the real control is that **every published port
binds to 127.0.0.1**. Nothing in this stack is reachable from another machine.

## Security posture

Telemetry audited at runtime on 2026-08-03, in the running containers rather than in YAML:

| Component | Control | Verified |
|---|---|---|
| Langfuse web | `TELEMETRY_ENABLED=false`, `NEXT_TELEMETRY_DISABLED=1` | yes |
| Langfuse worker | `TELEMETRY_ENABLED=false` | yes |
| Label Studio | `COLLECT_ANALYTICS=false` | yes |
| ClickHouse | `config.d` override, crash reports off | yes, in merged config |
| MinIO | `MINIO_UPDATE=off` | yes |
| Postgres, Redis | no telemetry in either project | n/a |
| Langflow | `DO_NOT_TRACK=true`, `LANGFLOW_NATIVE_TRACING=false` | configured, unverified until first run |

- **Langfuse**: `TELEMETRY_ENABLED` is hard-coded `"false"` in the component file rather
  than exposed as an env var. It is architectural, not a preference. Upstream
  defaults it to `true`.
- **ClickHouse**: stock `config.xml` ships `send_crash_reports.enabled = true` pointing at
  `https://crash.clickhouse.com/`. Found during the 2026-08-03 audit and overridden via
  `clickhouse-config.d/disable-telemetry.xml`. ClickHouse is where Langfuse stores spans,
  so this one matters more than its "anonymized stack traces" description suggests.
  **Re-check after every ClickHouse image bump** — a config.d override silently stops
  matching if upstream restructures the block:
  ```bash
  docker compose exec langfuse-clickhouse sh -c "sed -n '/<send_crash_reports>/,/<\/send_crash_reports>/p' /var/lib/clickhouse/preprocessed_configs/config.xml"
  ```
- **Label Studio**: `COLLECT_ANALYTICS=false`.
- **Not covered by this stack: Docker Desktop's own analytics.** It maintains
  `%LOCALAPPDATA%\Docker\run\userAnalyticsOtlpHttp.sock` and reports usage to Docker Inc.
  independently of anything here. Turn it off in Settings → General if that matters, but
  note it observes Docker, not container payloads.
- **None of the above is *enforced*.** Every row in that table is a setting the vendor
  honours by convention. Real enforcement would be a firewall egress allowlist plus a
  per-run trace audit, and that is **not built here**. Until it is, treat this as a
  clean configuration rather than a guarantee, and keep confidential text out of the
  stack.
- **Langflow**: treat as a remote-code-execution engine with a web UI — see
  [`integrations/langflow/README.md`](../integrations/langflow/README.md) and the
  header of `components/langflow/compose.yaml` for the CVE record. Pinned to `1.11.0`;
  **1.9.2 is the hard floor** and `:latest` is never acceptable. CVE-2026-33017's
  changelog claimed a fix that was never implemented, so check advisories rather than
  release notes before any bump.
- **Data line**: Langflow sees synthetic and de-identified content only.
  **Never confidential text.**

## Troubleshooting

**`bootstrap.ps1 cannot be loaded because running scripts is disabled on this system.`**
Default Windows execution policy. Use the `powershell -NoProfile -ExecutionPolicy Bypass
-File .\bootstrap.ps1` form above.

**`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`.**
Docker Desktop is not running. Start it and wait for the tray whale to stop animating;
`docker info` succeeding is the real readiness signal. On this machine Docker Desktop is a
per-user install at
`C:\Users\sdami\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`, not under
Program Files.

**Docker Desktop starts but the daemon never answers, with
`initializing Inference manager: listening on unix://.../dockerInference: remove ...: The
file cannot be accessed by the system`.** Docker Model Runner is wedged on a stale socket.
`%LOCALAPPDATA%\Docker\run\` holds 0-byte reparse points (`dockerInference`,
`dockerEthernetVfkit`) left by a previous session, and the new process cannot remove them.
Nothing to do with this repo — Docker Desktop cannot reach the point of serving containers.

Easiest fix, since extrct never uses Model Runner (models are served by vLLM and Ollama
behind the gateway): **Settings → AI → turn off Docker AI / Model Runner → Apply &
Restart.** Equivalent to setting `"EnableDockerAI": false` in
`%APPDATA%\Docker\settings-store.json` while Docker Desktop is fully stopped.

If it persists, clear the stale sockets:

```powershell
Get-Process "Docker Desktop","com.docker.backend" -EA SilentlyContinue | Stop-Process -Force
wsl --shutdown
Remove-Item "$env:LOCALAPPDATA\Docker\run\dockerInference","$env:LOCALAPPDATA\Docker\run\dockerEthernetVfkit" -Force -EA SilentlyContinue
Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
```

**Editing `bootstrap.ps1`: keep it ASCII-only.** Windows PowerShell 5.1 reads BOM-less
`.ps1` files as ANSI, so an em dash or smart quote arrives as mojibake and can break
parsing outright. The generated `.env` is written UTF-8 **without** a BOM for the mirror
image of this reason: PS 5.1's default UTF-8 adds a BOM, and docker compose would fold it
into the first variable name.

**`langfuse-web` restarts or 500s on first boot.** ClickHouse migrations. Watch with
`docker compose logs -f langfuse-web` and wait for the migration lines to finish.

**Port already in use.** 5432 is the usual one — a local Postgres install. Change the host
side only, e.g. `"127.0.0.1:5433:5432"` in `components/core/compose.yaml`.

**`docker compose` doesn't recognise `include`.** You're below v2.20. Update Docker Desktop.

**Langflow won't log in.** 1.11.x images set `LANGFLOW_AUTO_LOGIN=false` at build time, so
`LANGFLOW_SUPERUSER_PASSWORD` must be set — `bootstrap.ps1` does this. If you changed the
password after the first boot, it won't take: the superuser already exists in
`langflow-db`. Either log in with the old one or `docker compose down -v`.

**Regenerating `.env` breaks Langfuse.** `LANGFUSE_ENCRYPTION_KEY` decrypts stored data.
Rotating it makes existing traces unreadable — `bootstrap.ps1` refuses to overwrite without
`-Force` for this reason.
