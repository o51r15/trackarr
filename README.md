# Trackarr

Automatically downloads, pings, and injects BitTorrent trackers into qBittorrent.
Supports VPN Docker networks, SOCKS5 proxies, HTTPS proxies, or direct connections.

## Features

- Multi-source tracker collection (raw URL lists, GitHub repos, website scrapes, manual entries)
- Selectable ping mode: Docker VPN network, SOCKS5 proxy, HTTPS proxy, or direct
- Latency measurement for all surviving trackers
- Automatic sleep/hibernate system with progressive backoff for repeated failures
- Per-tracker history with uptime percentages and trend indicators
- Source discovery engine (well-known aggregators + rate-limited GitHub search)
- Web GUI on port 7374 — all 5 tabs in one HTML file

---

## Installation

### Option 1 — Docker (recommended)

One image does everything. The bridge runs as a persistent container; when a ping
cycle runs, it spins up a second ephemeral instance of the same image to do the
pinging, then destroys it.

```
docker pull ghcr.io/o51r15/trackarr:latest
```

Create your config:
```
curl -o homelab-config.json https://raw.githubusercontent.com/o51r15/trackarr/master/homelab-config.example.json
```

Edit `homelab-config.json`, then run:
```
docker run -d \
  --name trackarr \
  -p 7374:7374 \
  -v ./homelab-config.json:/app/homelab-config.json \
  -v ./tracker-data:/app/tracker-data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/o51r15/trackarr:latest
```

Open http://localhost:7374.

> The Docker socket mount is required so the bridge can spin up ephemeral ping containers.

---

### Option 2 — Windows (run directly on host)

**Requirements:** PowerShell 5.1+, Docker Desktop, qBittorrent with Web API enabled.

1. Clone the repo
2. Copy `homelab-config.example.json` to `homelab-config.json` and fill in your settings
3. Encrypt your qBittorrent password:
   ```powershell
   ConvertFrom-SecureString (ConvertTo-SecureString "your_password" -AsPlainText -Force)
   ```
   Paste the output as `tp.pass` in `homelab-config.json`
4. Set `tp.dir` to the full path of the repo directory
5. Set `tp.script` to the full path of `trackerping.ps1`
6. Set `tp.pingImage` to `ghcr.io/o51r15/trackarr:latest` (or omit — that is the default)
7. Run the bridge:
   ```powershell
   powershell -ExecutionPolicy Bypass -File trackarr-bridge.ps1
   ```
8. Open http://localhost:7374

---

## Ping modes

Configured in the GUI under **Config → Ping Mode**, or as `tp.pingMode` in `homelab-config.json`.

| Mode | Description | UDP trackers |
|---|---|---|
| `docker-vpn` | Ping container joins a VPN Docker network (e.g. Gluetun). IP check confirms traffic exits through VPN. | Supported |
| `socks5` | Ping container uses a SOCKS5 proxy via `ALL_PROXY`. | **Skipped** — SOCKS5 cannot tunnel UDP |
| `https-proxy` | Ping container uses an HTTP CONNECT proxy via `HTTPS_PROXY`. | **Skipped** — HTTP proxy cannot tunnel UDP |
| `direct` | No VPN or proxy. Pings go out on the host network. | Supported |

Set `tp.proxyUrl` for proxy modes, e.g. `socks5://192.168.1.x:1080` or `http://192.168.1.x:3128`.

---

## File layout

```
trackarr/
├── ping/
│   ├── trackerping.py       Async ping binary — UDP, HTTP/HTTPS, WebSocket
│   └── requirements.txt     Python deps (bundled into the main Docker image)
├── trackerping.ps1          Core script — collect, ping, inject
├── tracker-discovery.ps1    Finds new tracker list sources
├── trackarr-bridge.ps1      HTTP bridge (serves GUI + API on port 7374)
├── trackarr-gui.html        Single-file web GUI
├── Dockerfile               Builds the single trackarr image (bridge + ping binary)
├── tracker_urls.txt         Raw .txt list URLs (one per line)
├── homelab-config.json      Your config (gitignored)
├── homelab-config.example.json  Config template
├── bridge-config.json       Bridge port config
└── tracker-data/            Runtime data (gitignored)
    ├── tracker-sources.json
    ├── tracker-source-cache.json
    ├── tracker-sleep.json
    └── tracker-history.json
```

---

## Roadmap

- [ ] Replace Windows DPAPI credential storage for Linux/Docker compatibility
- [ ] Scheduler built into the bridge
