# Core pipeline modules
# collect.py   — source collection (raw URL lists, GitHub repos, scrapes, manual)
# ping.py      — async ping engine (UDP, HTTP/HTTPS, WS)
# latency.py   — TCP connect latency measurement
# inject.py    — qBittorrent API client
# sleep.py     — sleep/hibernate state with progressive backoff
# history.py   — 7-day tracker run history
# sources.py   — tracker source CRUD (GitHub repos, website scrapes, manual, discovery state)
# discovery.py — source discovery engine (well-known sources + rate-limited GitHub search)
# scheduler.py — internal async scheduler (daily/weekly/hourly/interval)
# run.py       — full pipeline orchestration (collect -> ping -> latency -> sleep -> inject)
#
# Phase 6+:
# notify.py    — Pushover + webhook notifications
