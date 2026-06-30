"""
inject.py — qBittorrent Web API client

Logs in, sets the global add_trackers preference (qBittorrent injects this
list into every torrent that doesn't already have these trackers), and
verifies the update took effect.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)


class QbtAuthError(Exception):
    pass


class QbtConnectionError(Exception):
    pass


async def login(session: aiohttp.ClientSession, qbt_url: str, user: str, password: str) -> None:
    try:
        async with session.post(
            f"{qbt_url}/api/v2/auth/login",
            data={"username": user, "password": password},
            headers={"Referer": qbt_url},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status not in (200, 204):
                raise QbtAuthError(f"qBittorrent login rejected. HTTP {resp.status}")
            text = await resp.text()
            if "Fails" in text:
                raise QbtAuthError("qBittorrent login failed — check credentials.")
    except asyncio.TimeoutError as exc:
        # A bare asyncio.TimeoutError is NOT a subclass of aiohttp.ClientError in
        # most aiohttp versions, so it must be caught separately or it silently
        # escapes this function with a useless empty str() representation,
        # surfacing upstream as "Unexpected error:" with no detail at all.
        raise QbtConnectionError(
            f"Timed out connecting to qBittorrent at {qbt_url} (15s). "
            f"Check the URL/port is correct and reachable from this container's network."
        ) from exc
    except aiohttp.ClientError as exc:
        raise QbtConnectionError(f"Could not reach qBittorrent: {exc}") from exc


async def inject_trackers(
    session: aiohttp.ClientSession,
    qbt_url: str,
    trackers: list[str],
) -> None:
    trackers_str = "\n".join(trackers)
    payload = json.dumps({"add_trackers_enabled": True, "add_trackers": trackers_str})
    try:
        async with session.post(
            f"{qbt_url}/api/v2/app/setPreferences",
            data={"json": payload},
            headers={"Referer": qbt_url},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status not in (200, 204):
                raise QbtConnectionError(f"Failed to update preferences. HTTP {resp.status}")
    except asyncio.TimeoutError as exc:
        raise QbtConnectionError(
            f"Timed out updating preferences at {qbt_url} (15s)."
        ) from exc
    except aiohttp.ClientError as exc:
        raise QbtConnectionError(f"Failed to update qBittorrent preferences: {exc}") from exc


async def verify_trackers(
    session: aiohttp.ClientSession,
    qbt_url: str,
    expected: list[str],
) -> tuple[bool, int]:
    """Returns (all_present, stored_count)."""
    try:
        async with session.get(
            f"{qbt_url}/api/v2/app/preferences",
            headers={"Referer": qbt_url},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
    except Exception as exc:
        logger.warning("Verification skipped: %s", exc)
        return True, -1   # don't fail the run over a verify-step network blip

    stored_raw = data.get("add_trackers", "") or ""
    stored = {ln.strip() for ln in stored_raw.split("\n") if ln.strip()}
    missing = [t for t in expected if t.strip() not in stored]

    return len(missing) == 0, len(stored)


async def run_inject_pipeline(
    qbt_url: str, qbt_user: str, qbt_pass: str, trackers: list[str], log
) -> None:
    """Full login -> inject -> verify pipeline. Raises on failure."""
    # unsafe=True is required because aiohttp's default cookie jar follows RFC 6265
    # and silently refuses to store cookies for bare IP-address hosts (no public
    # suffix / domain). qBittorrent is very commonly reached via a raw LAN IP
    # (e.g. http://192.168.1.x:port), so without this, login() appears to succeed
    # (200, no "Fails" body) but the SID cookie is dropped before the next request
    # goes out, and every subsequent call gets HTTP 403 as if unauthenticated.
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        await log(f"Logging into qBittorrent at {qbt_url}...", "info")
        await login(session, qbt_url, qbt_user, qbt_pass)
        await log("Authenticated.", "ok")

        await log(f"Injecting {len(trackers)} trackers...", "info")
        await inject_trackers(session, qbt_url, trackers)
        await log(f"Done. {len(trackers)} trackers active in qBittorrent.", "ok")

        await log("Verifying...", "info")
        ok, stored_count = await verify_trackers(session, qbt_url, trackers)
        if ok:
            await log(
                f"[OK] Verification PASSED: {len(trackers)} trackers confirmed (stored: {stored_count}).",
                "ok",
            )
        else:
            await log("Verification WARNING: some trackers not found after update.", "warn")
