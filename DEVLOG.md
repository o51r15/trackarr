# Trackarr — Developer Log
_Last updated: 2026-06-29_

---

## Current architecture (PowerShell — to be replaced)

### One image, two roles

There is a single Docker image: `ghcr.io/o51r15/trackarr:latest`.

It contains:
- `trackarr-bridge.ps1` — the HTTP bridge that serves the GUI and handles all API requests
- `trackerping.ps1` — the core collection/ping/inject script, invoked by the bridge per run
- `trackerping` binary — the Python ping tool installed at `/usr/local/bin/trackerping`
- `tracker-discovery.ps1` — the source discovery script, invoked by the bridge per discovery run
- `trackarr-gui.html` — the single-file web GUI, served by the bridge at `/`

The image's default entrypoint starts the bridge:
```
pwsh -NonInteractive -ExecutionPolicy Bypass -File /app/trackarr-bridge.ps1
```

### What happens during a ping run

1. User clicks Run Now in the GUI (or scheduler fires)
2. Bridge calls `Start-Process powershell.exe -File trackerping.ps1` — detached child process
3. `trackerping.ps1` collects tracker URLs from all configured sources, writes to `active_raw.txt` in `$Cfg.dir`
4. The script calls `docker run --rm -v "$($Cfg.dir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt`
5. Docker creates a container from `$PingImage` (same image) with `trackerping` overriding the entrypoint
6. The ping container reads `active_raw.txt`, pings every tracker, writes survivors to `working_trackers.txt`, exits and is destroyed
7. `trackerping.ps1` reads `working_trackers.txt`, runs TCP latency measurement, emits `[TRACKER_RESULT]` lines to stdout, updates sleep state, injects into qBittorrent

### The ping container is not a service

The ping container is ephemeral. It does not run alongside the bridge. It is created and destroyed within a single run. There is no separate `local-trackerping` image — the ping container IS the main image invoked with `trackerping` as the command instead of the bridge entrypoint.

### Docker socket requirement

The bridge container needs the host Docker socket to spawn the ephemeral ping container:
```
-v /var/run/docker.sock:/var/run/docker.sock
```

### Volume layout

| Volume | Container path | Purpose |
|---|---|---|
| `./tracker-data` | `/app/tracker-data` | Persistent state: sleep.json, history.json, sources.json, source-cache.json |
| `$Cfg.dir` (user-set) | `/data` | Runtime files: active_raw.txt, working_trackers.txt, combined_raw.txt, trackerping.log |

`$Cfg.dir` must be set to `/data` for Docker deployments. The bridge mounts it into the ephemeral ping container at the same `/data` path.

---

## Ping modes (current)

| Mode | Mechanism | UDP |
|---|---|---|
| `docker-vpn` | `docker run --network=$docker_net` — ping container joins VPN network | Supported |
| `socks5` | `docker run -e ALL_PROXY=...` — ping container uses SOCKS5 proxy | Skipped (`--no-udp`) |
| `https-proxy` | `docker run -e HTTPS_PROXY=...` | Skipped (`--no-udp`) |
| `direct` | `docker run` with no network arg | Supported |

VPN security check (docker-vpn only): compares host external IP vs container external IP via ipify.org. Aborts if they match.

---

## File data flow (current)

```
tracker_urls.txt          → trackerping.ps1 reads raw URL sources
tracker-sources.json      → trackerping.ps1 + tracker-discovery.ps1 read/write
tracker-source-cache.json → trackerping.ps1 reads/writes GitHub SHA cache
combined_raw.txt          → trackerping.ps1 writes full deduplicated pool
active_raw.txt            → trackerping.ps1 writes active (non-sleeping) trackers
                          → ping container reads as input
working_trackers.txt      → ping container writes surviving trackers
                          → trackerping.ps1 reads for latency + qBT injection
tracker-sleep.json        → trackerping.ps1 reads/writes sleep/hibernate state
tracker-history.json      → bridge writes after each run (from [TRACKER_RESULT] stdout lines)
trackerping.log           → trackerping.ps1 appends all log lines
```

---

## Bridge job system (current)

Bridge runs scripts via `Start-Process powershell.exe` with stdout/stderr redirected to temp files.
Each run gets a random 8-char job ID. Bridge polls the process, streams stdout to GUI via `/job/{id}`.
On completion: `Update-TrackerHistory` parses `[TRACKER_RESULT]` lines, `Invoke-CompletionNotification` sends Pushover.

`[TRACKER_RESULT]` format (stdout from trackerping.ps1, filtered from GUI log stream):
```
[TRACKER_RESULT] url=udp://tracker.example.com:6969 status=UP latency=42
[TRACKER_RESULT] url=udp://tracker.dead.com:80 status=DOWN latency=0
```

---

## Known bugs (current codebase)

### BUG-01 — `active_raw.txt` BOM (FIXED in trackarr)
`Out-File -Encoding UTF8` in PS 5.1 prepends BOM, corrupting first tracker URL read by ping container.
Fix: `[System.IO.File]::WriteAllLines($ActiveFile, @($ActiveTrackers), [System.Text.Encoding]::UTF8)`

### BUG-02 — Pushover regex never matches (FIXED in trackarr)
Notification function matched a log pattern that `trackerping.ps1` never emits. Every notification read "Run complete".
Fix: match actual patterns `Collection complete: (\d+)` and `(\d+) trackers passed` / `Done\. (\d+) trackers`.

### BUG-03 — `combined_raw.txt` path inconsistency (FIXED in trackarr)
`Update-TrackerHistory` used `$ScriptDir`, preview endpoint used `$cfg.tp.dir`. Now both use `$cfg.tp.dir`.

---

## Open issues (current codebase)

### DPAPI in Docker
`ConvertTo/From-SecureString` uses Windows DPAPI — unavailable on Linux. qBittorrent password, Pushover credentials, and GitHub token cannot be decrypted inside a Linux container. Docker deployment is not fully functional until this is resolved.

This is the primary driver for the Python rewrite.

---

## PS 5.1 compatibility notes (Windows source installs)

- `Out-File -Encoding UTF8` adds BOM — use `[System.IO.File]::WriteAllLines/WriteAllText`
- `ConvertFrom-Json` collapses single-item arrays — wrap with `@()`
- Em dashes in `.ps1` cause parse errors — use hyphens
- `$matches` is reserved — use `$regexMatches`
- `.Trim()` on null crashes — guard for null
- Inline `if` as parameter value is invalid — pre-compute to variable
- `Write-Output` in utility functions contaminates return values — use `Write-Trace` (file-only)

---
---

# Python Rewrite — Roadmap

## Locked decisions

| Decision | Choice |
|---|---|
| Deployment target | Container only. No Windows source install path. |
| Runtime | Python 3.12 |
| Web framework | FastAPI + Uvicorn |
| Ping execution | In-process async task. No Docker socket. No ephemeral containers. |
| VPN handling | Auto-detected from compose network at startup. Locks mode if VPN found. |
| Proxy/direct | Available only when VPN is not detected. |
| Scheduler | Internal. Built into the app. |
| Notifications | Pushover + generic webhook (both configurable, both optional). |
| Credentials | Environment variables. No encryption, no DPAPI, no platform dependency. |
| History retention | 7 days. Time-based cutoff, not run-count based. |
| Data format | JSON files. SQLite deferred to post-proof-of-concept. |
| Migration | None. Fresh install only. |
| Log streaming | SSE (Server-Sent Events). No polling. |

---

## What disappears in the rewrite

- All PowerShell scripts (`trackarr-bridge.ps1`, `trackerping.ps1`, `tracker-discovery.ps1`)
- Docker socket mount requirement
- Ephemeral ping container pattern
- File-based handoff (`active_raw.txt` → `working_trackers.txt`)
- `combined_raw.txt` intermediate file
- DPAPI credential encryption
- `tp.dir`, `tp.script`, `tp.pingImage` config fields
- Manual VPN network name configuration
- `[TRACKER_RESULT]` stdout parsing
- Temp log file polling job system

## What is retained and ported

- All tracker collection logic (URL lists, GitHub SHA cache, scraping, manual)
- Sleep/hibernate state and progressive backoff (watching → 48h → 7d)
- qBittorrent injection and verification
- Source discovery engine (well-known sources + rate-limited GitHub search)
- Candidate/approve/dismiss flow
- All 5 GUI tabs (minimal changes)
- Pushover notifications
- Per-tracker uptime, latency trend, history

---

## Project structure

```
trackarr/
├── app/
│   ├── main.py              # FastAPI app, lifespan startup (VPN detect, scheduler init)
│   ├── config.py            # Pydantic settings model, load/save config.json, env var overrides
│   ├── network.py           # VPN auto-detection
│   ├── scheduler.py         # Internal async scheduler
│   ├── api/
│   │   ├── router.py        # All REST endpoints
│   │   └── jobs.py          # Async job manager: start, abort, SSE stream
│   └── core/
│       ├── collect.py       # Source collection pipeline
│       ├── ping.py          # Async ping engine (UDP, HTTP/HTTPS, WS)
│       ├── latency.py       # TCP latency measurement
│       ├── inject.py        # qBittorrent API client
│       ├── sleep.py         # Sleep/hibernate state
│       ├── history.py       # 7-day tracker run history
│       ├── discovery.py     # Source discovery
│       └── notify.py        # Pushover + webhook
├── static/
│   └── gui.html             # Single-file frontend
├── data/                    # Mounted volume (all persistent JSON + log)
├── Dockerfile
├── docker-compose.yml
├── docker-compose.vpn.example.yml
├── config.example.json
└── requirements.txt
```

---

## VPN detection (`network.py`)

Runs once at startup during FastAPI lifespan. Sets a global `connection_mode` that is read-only for the lifetime of the container. Result exposed via `GET /api/network-mode`.

**Detection logic:**
1. Check `/proc/net/route` for the default gateway
2. If default gateway is `172.17.0.1` (standard Docker bridge) — no VPN, proxy/direct available
3. If default gateway is any other private IP — container is on a custom Docker network, assumed VPN-routed
4. Confirm: fetch external IP via `https://api.ipify.org`. Store it. Log it.
5. Set `connection_mode = "vpn"` if custom gateway detected, else `"open"`

**GUI behaviour based on detected mode:**

- `vpn`: Config tab shows a green "VPN Detected" status banner. Proxy URL field and Direct option are not rendered at all.
- `open`: Config tab shows proxy URL field and mode selector (proxy / direct). VPN option is not rendered.

No manual override. The compose file determines the mode. If the user wants VPN, they connect the container to the VPN network in compose. If they don't, they get proxy/direct.

---

## Scheduler (`scheduler.py`)

Built into the app as an `asyncio` background task. No external library — implemented as a simple loop that checks pending jobs every 30 seconds.

**Schedule definition (stored in `data/schedules.json`):**
```json
[
  {
    "id": "abc123",
    "name": "Daily TrackerPing",
    "enabled": true,
    "type": "trackerping",
    "frequency": "daily",
    "time": "03:00",
    "last_run": "2026-06-28T03:00:00Z",
    "last_result": "success",
    "next_run": "2026-06-29T03:00:00Z"
  }
]
```

**Frequency types:** `daily` (at time), `weekly` (on day at time), `hourly`, `interval` (every N minutes).

**At startup:** recalculate `next_run` for all enabled schedules based on `last_run` and current time. If `next_run` is in the past and `last_run` was before the missed window, fire immediately.

**The loop:** every 30 seconds, check all enabled schedules. If `next_run <= now`, fire the job, update `last_run`, calculate new `next_run`.

**Exposed endpoints:** `GET /api/schedules`, `POST /api/schedules`, `PUT /api/schedules/{id}`, `DELETE /api/schedules/{id}`.

---

## Notifications (`notify.py`)

Both Pushover and webhook are optional and independent. Either, both, or neither can be configured.

**Pushover:** same as current — user key + API token, sends on run completion with tracker counts.

**Webhook:** `POST` to a user-configured URL with a JSON payload:
```json
{
  "event": "run_complete",
  "success": true,
  "fetched": 4521,
  "active": 892,
  "timestamp": "2026-06-29T03:04:12Z"
}
```
Events: `run_complete`, `run_failed`, `discovery_complete`. Webhook URL configured via env var `WEBHOOK_URL` or in `config.json`. Simple `aiohttp` POST, fire-and-forget with a timeout. No retry logic for v1.

---

## History (`history.py`)

**Retention:** 7 days. On every run completion, prune all history entries older than 7 days before writing.

**Storage:** `data/tracker-history.json` — same structure as current, just with time-based pruning replacing run-count pruning.

**Stale key pruning:** trackers not seen in the current pool are removed from history, same as current.

---

## Config (`config.py`)

All sensitive values come from environment variables. `config.json` holds non-sensitive settings only.

**Environment variables:**
```
QBT_URL          qBittorrent Web UI URL
QBT_USER         qBittorrent username
QBT_PASS         qBittorrent password (plain text)
PUSHOVER_USER    Pushover user key
PUSHOVER_TOKEN   Pushover API token
GITHUB_TOKEN     GitHub Personal Access Token (optional, raises API rate limit)
WEBHOOK_URL      Webhook endpoint URL (optional)
```

**`config.json` (non-sensitive):**
```json
{
  "history_days": 7,
  "latency_timeout_ms": 3000,
  "proxy_url": "",
  "connection_mode": "direct",
  "pushover_notify": false,
  "webhook_notify": false,
  "tracker_urls": [
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt"
  ]
}
```

`connection_mode` in config is only used when VPN is not auto-detected. When VPN is detected, it overrides whatever is in config.

---

## Ping engine (`ping.py`, `latency.py`)

The existing `ping/trackerping.py` is the base. Key changes:

- Runs as a native `asyncio` task inside the app process — no subprocess, no Docker, no file I/O for handoff
- Yields results as Python objects directly to the caller
- Results flow into `history.record()` and the SSE log stream without a `[TRACKER_RESULT]` parsing step
- `latency.py` ports the PS runspace pool latency check to `asyncio.gather` with `asyncio.open_connection`
- `--no-udp` flag becomes a boolean parameter passed to the ping function when proxy mode is active

---

## Log streaming (SSE)

Each run exposes a stream endpoint: `GET /api/jobs/{id}/stream` (text/event-stream).

The GUI replaces its `setInterval` poll with:
```javascript
const es = new EventSource(`${BRIDGE}/api/jobs/${jobId}/stream`);
es.onmessage = e => appendLog(JSON.parse(e.data));
es.addEventListener('done', () => { es.close(); finishRun(); });
```

The backend yields log lines as they're produced by the pipeline functions. No temp log files, no position tracking, no polling interval latency.

---

## Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY static/ ./static/
COPY config.example.json .
RUN mkdir -p /app/data
EXPOSE 7374
VOLUME ["/app/data"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7374"]
```

No PowerShell. No Python ping subdirectory. No Docker socket. ~200MB smaller than the current image.

---

## docker-compose examples

**VPN (e.g. Gluetun):**
```yaml
services:
  trackarr:
    image: ghcr.io/o51r15/trackarr:latest
    network_mode: "service:gluetun"
    volumes:
      - ./data:/app/data
    environment:
      - QBT_URL=http://192.168.1.x:8080
      - QBT_USER=admin
      - QBT_PASS=yourpassword
    depends_on:
      - gluetun

  gluetun:
    image: qmcgaw/gluetun
    # ... gluetun config
```
VPN auto-detected. Proxy/direct options hidden in GUI.

**Proxy or direct:**
```yaml
services:
  trackarr:
    image: ghcr.io/o51r15/trackarr:latest
    ports:
      - "7374:7374"
    volumes:
      - ./data:/app/data
    environment:
      - QBT_URL=http://192.168.1.x:8080
      - QBT_USER=admin
      - QBT_PASS=yourpassword
      - WEBHOOK_URL=https://hooks.example.com/trackarr
```
No VPN network. Proxy/direct options shown in GUI.

---

## Build phases

### Phase 1 — Foundation
- Project skeleton, FastAPI app
- Config loading (Pydantic + env vars)
- VPN detection at startup
- Static file serving (GUI placeholder)
- `GET /api/network-mode`, `GET /api/ping`

### Phase 2 — Core pipeline
- Source collection (`collect.py`)
- Ping engine (`ping.py`, `latency.py`)
- Sleep/hibernate state (`sleep.py`)
- qBittorrent client (`inject.py`)
- Full run: collect → ping → latency → sleep update → inject → verify

### Phase 3 — API and streaming
- Job manager (`jobs.py`)
- SSE log streaming
- All REST endpoints (config, tracker URLs, sources, sleep, history)
- Tracker history with 7-day pruning

### Phase 4 — Scheduler
- Internal async scheduler (`scheduler.py`)
- Schedule CRUD endpoints
- GUI Scheduler tab

### Phase 5 — Source discovery
- Discovery engine (`discovery.py`) ported from `tracker-discovery.ps1`
- All Discovery tab endpoints

### Phase 6 — Notifications
- Pushover (`notify.py`)
- Webhook

### Phase 7 — GUI updates
- Connection mode status panel in Config tab (VPN detected vs open)
- Replace polling job log with SSE
- Remove Docker-specific config fields (network name, script path, ping image)
- Update Scheduler tab if needed

### Phase 8 — Packaging and CI
- Final Dockerfile
- docker-compose examples
- Update CI workflow (image is now Python-only, ~200MB lighter)
- Update README for container-only install
