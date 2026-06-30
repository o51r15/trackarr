"""
run.py — Full TrackerPing pipeline orchestration

Ties together collect -> ping -> latency -> sleep update -> inject -> verify.
This is what a "Run Now" click or scheduled trigger actually executes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from . import collect, history, inject, latency, ping, sleep
from . import sources as sources_module
from ..config import AppConfig, Env

logger = logging.getLogger(__name__)

CACHE_FILE = Path("/app/data/tracker-source-cache.json")

LogFn = Callable[[str, str], Awaitable[None]]


@dataclass
class RunSummary:
    fetched: int = 0
    active: int = 0
    passed: int = 0
    success: bool = False
    error: str | None = None
    results: list[dict] = field(default_factory=list)   # [{url, status, latency_ms}, ...]


async def run_trackerping(
    config: AppConfig,
    env: Env,
    connection_mode: str,      # "vpn" | "direct" | "proxy" — resolved from network detection + config
    log: LogFn,
) -> RunSummary:
    summary = RunSummary()

    if not env.qbt_url or not env.qbt_user:
        summary.error = "qBittorrent credentials not configured (QBT_URL / QBT_USER / QBT_PASS env vars)."
        await log(summary.error, "error")
        return summary

    # ---------------------------------------------------------------------
    # 1. Collect
    # ---------------------------------------------------------------------
    sources = sources_module.load_sources()
    github_repos = [
        collect.GithubRepoSource(id=r.id, url=r.url, label=r.label)
        for r in sources.github_repos
    ]
    website_scrapes = [
        collect.WebsiteScrapeSource(id=s.id, url=s.url, label=s.label)
        for s in sources.website_scrape
    ]
    manual_trackers = sources.manual

    async with aiohttp.ClientSession() as session:
        collection = await collect.collect_all(
            session=session,
            tracker_urls=config.tracker_urls,
            github_repos=github_repos,
            website_scrapes=website_scrapes,
            manual_trackers=manual_trackers,
            github_token=env.github_token,
            cache_file=CACHE_FILE,
            log=log,
        )

    summary.fetched = len(collection.trackers)
    if summary.fetched == 0:
        summary.error = "No trackers collected."
        await log(summary.error, "error")
        return summary

    # ---------------------------------------------------------------------
    # 1.5 Sleep/hibernate filtering
    # ---------------------------------------------------------------------
    sleep_state = sleep.load_sleep_state()
    sleep_state = {
        k: v for k, v in sleep_state.items() if k in collection.trackers
    }  # prune unknowns

    dormant = sleep.get_dormant_set(sleep_state)
    active_trackers = collection.trackers - dormant
    summary.active = len(active_trackers)

    sleep_counts = sleep.counts(sleep_state)
    await log(
        f"Active: {summary.active} | Sleeping (48h): {sleep_counts['sleeping']} | "
        f"Hibernating (7d): {sleep_counts['hibernating']}",
        "info",
    )

    if not active_trackers:
        await log("[OK] All trackers are sleeping or hibernating. Skipping ping.", "ok")
        sleep.save_sleep_state(sleep_state)
        summary.success = True
        return summary

    # ---------------------------------------------------------------------
    # 2. Ping
    # ---------------------------------------------------------------------
    no_udp = connection_mode == "proxy"
    proxy_url = config.proxy_url if connection_mode == "proxy" else None

    if connection_mode == "proxy" and proxy_url:
        await log(f"Using proxy: {proxy_url}", "info")
        await log("NOTE: UDP trackers are skipped - proxies cannot tunnel UDP traffic.", "warn")
    elif connection_mode == "vpn":
        await log("Pinging via VPN-routed network.", "info")
    else:
        await log("Pinging directly (no VPN or proxy).", "info")

    await log(f"Starting ping tests for {len(active_trackers)} trackers...", "step")
    ping_results = await ping.ping_all(
        urls=list(active_trackers),
        no_udp=no_udp,
        timeout=10.0,
        proxy_url=proxy_url,
    )

    passed = {r.url for r in ping_results if r.up is True}
    skipped = {r.url for r in ping_results if r.up is None}
    tested = active_trackers - skipped   # only count tested ones for sleep state

    summary.passed = len(passed)
    await log(
        f"{len(passed)} trackers passed ping. {len(skipped)} skipped (UDP under proxy mode).",
        "ok",
    )

    if not passed:
        summary.error = "No trackers survived the ping test."
        await log(summary.error, "error")
        return summary

    # ---------------------------------------------------------------------
    # 3.5 Latency measurement
    # ---------------------------------------------------------------------
    await log(f"Measuring latency (timeout: {config.latency_timeout_ms}ms)...", "info")
    latency_map = await latency.measure_all(list(passed), config.latency_timeout_ms)
    await log(f"Latency complete for {len(passed)} trackers.", "ok")

    for t in active_trackers:
        if t in passed:
            lat = latency_map.get(t)
            summary.results.append({"url": t, "status": "UP", "latency_ms": lat})
        elif t not in skipped:
            summary.results.append({"url": t, "status": "DOWN", "latency_ms": None})

    history.record_run(summary.results, collection.trackers, config.history_days)

    # ---------------------------------------------------------------------
    # 3.7 Update sleep state
    # ---------------------------------------------------------------------
    sleep_state = sleep.update_after_run(sleep_state, tested, passed)
    sleep.save_sleep_state(sleep_state)
    new_counts = sleep.counts(sleep_state)
    await log(
        f"Sleep state saved. Sleeping: {new_counts['sleeping']} | "
        f"Hibernating: {new_counts['hibernating']}",
        "info",
    )

    # ---------------------------------------------------------------------
    # 4-6. qBittorrent inject + verify
    # ---------------------------------------------------------------------
    try:
        await inject.run_inject_pipeline(
            qbt_url=env.qbt_url,
            qbt_user=env.qbt_user,
            qbt_pass=env.qbt_pass,
            trackers=list(passed),
            log=log,
        )
    except (inject.QbtAuthError, inject.QbtConnectionError) as exc:
        summary.error = str(exc)
        await log(str(exc), "error")
        return summary

    summary.success = True
    await log("SCRIPT_FINISHED_SUCCESSFULLY", "ok")
    return summary
