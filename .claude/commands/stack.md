---
description: Verify the local stack is healthy — containers, endpoints, telemetry, port bindings
description-short: Health-check the extrct stack
---

Verify the running stack and report a compact table. Run the checks; do not infer from the
compose files.

1. **Containers** — `docker compose ps` from `deploy/`. Flag anything not running, restarting,
   or unhealthy.
2. **Endpoints** — HTTP status for Langfuse `:3000`, Label Studio `:8080`, and Langflow `:7860`
   if the prototype profile is up. A redirect (301/302/307) is healthy for login-gated UIs.
3. **Database** — `pg_isready` inside `extrct-postgres`.
4. **Telemetry** — read these **in the running containers**, not from YAML:
   `TELEMETRY_ENABLED` (langfuse-web, langfuse-worker), `COLLECT_ANALYTICS` (label-studio),
   `DO_NOT_TRACK` (langflow), and ClickHouse's merged config:
   `sed -n '/<send_crash_reports>/,/<\/send_crash_reports>/p' /var/lib/clickhouse/preprocessed_configs/config.xml`
5. **Port bindings** — every published port must be `127.0.0.1`. Any `0.0.0.0` is a finding,
   not a note.
6. **Model availability** — whether Ollama has any models pulled, and whether the container can
   reach it at `host.docker.internal:11434`.

Report only. Do not fix anything without asking — a wrong "fix" to a healthy stack costs more
than the finding. If everything passes, say so in one line plus the table.
