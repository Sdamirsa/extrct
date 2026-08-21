# Setup 03 — add a GPU host as a local engine (LAN cable or overlay)

How to put a second machine with a GPU (a workstation, a DGX, a gaming box) behind
extrct as a **local** engine: Ollama runs on the GPU host, your laptop runs the
library and the stack, and text never leaves machines you own. Two ways to wire
them; both end at the same place — a `base_url` you point extrct at.

## Option A — direct LAN cable

One Ethernet cable between laptop and GPU host. No router, no internet dependency,
lowest latency, and the traffic physically cannot leave the pair.

1. **Give both ends a static address on the link.** Pick any private subnet that
   does not collide with your other networks (example below uses `10.55.0.0/24`).
   - GPU host (Ubuntu): *Settings → Network → the wired interface → IPv4 → Manual*,
     address `10.55.0.2`, netmask `255.255.255.0`, gateway blank.
   - Laptop (Windows): *Settings → Network & Internet → Ethernet → IP assignment →
     Manual*, address `10.55.0.1`, same netmask, gateway blank.
2. **Name the host** so configs stay readable: add one line to the laptop's
   `C:\Windows\System32\drivers\etc\hosts` (edit as Administrator):

   ```
   10.55.0.2   gpu-host
   ```

3. **Make Ollama listen on the link.** By default Ollama binds `127.0.0.1` only.
   On the GPU host:

   ```bash
   sudo systemctl edit ollama
   ```

   ```ini
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   ```

   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart ollama
   ```

   `0.0.0.0` exposes Ollama on **every** interface and Ollama has **no
   authentication** — so pair it with a firewall rule that admits only the link:

   ```bash
   sudo ufw allow from 10.55.0.1 to any port 11434
   sudo ufw deny 11434
   ```

4. **Verify from the laptop** (PowerShell — `curl.exe`, not the `curl` alias):

   ```powershell
   curl.exe http://gpu-host:11434/api/version
   curl.exe http://gpu-host:11434/api/tags
   ```

   The first returns the Ollama version; the second lists the models pulled on the
   host. If it hangs: firewall on the host; if it refuses: `OLLAMA_HOST` not applied.

## Option B — an overlay network (Tailscale or similar)

A mesh VPN gives every device a stable name and address that survives moving
between networks — the cable's simplicity without the cable. Install the agent on
both machines, log both into the same tailnet, and the GPU host gets a stable
hostname; use it in `base_url` exactly like `gpu-host` above. Traffic is
end-to-end encrypted between your own devices. The trade-off: a coordination
service is now part of your setup, and round-trips depend on the route the mesh
finds — a direct LAN path when both are home, a relay when not.

## Point extrct at it

Everywhere the stack takes an Ollama URL, use the host you just named:

- **Library** — in a job YAML:

  ```yaml
  provider:
    provider: ollama
    model: gemma4:31b-it-q4_K_M
    base_url: http://gpu-host:11434
    num_ctx: 8192
  ```

- **Langflow canvas** — set `EXTRCT_OLLAMA_URL=http://gpu-host:11434` in
  `deploy/.env`, then `docker compose --profile prototype up -d` (env is read at
  container BOOT — never `restart`). From inside a container, `localhost` is the
  container itself; the named host works from everywhere.

- **Model probe** — measure what the host's models actually support and record it
  in the model registry (see `packages/extrct/examples/08_model_probe.py`):

  ```bash
  python examples/08_model_probe.py --provider ollama --model gemma4:31b-it-q4_K_M --base-url http://gpu-host:11434 --long
  ```

## Pull models on the host, not through the stack

Model pulls are tens of gigabytes; run them on the GPU host itself
(`ollama pull gemma4:31b-it-q4_K_M`) so the download does not cross your link
twice. Keep `keep_alive` generous (extrct's Ollama spec defaults to `30m`) — a
31B model takes real time to load into VRAM, and evicting it between calls wastes
most of your throughput.

## What never changes

No key is needed for any of this — Ollama is keyless, and extrct resolves
credentials from the environment only when a hosted provider needs them. Nothing
about the GPU host enters job configs' identity: `base_url` is deployment, not
content, so the same job hashes identically on any machine (see
[packages/extrct/docs/configuration.md](../../../packages/extrct/docs/configuration.md)).
