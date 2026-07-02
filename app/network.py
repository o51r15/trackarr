"""
network.py — Connection mode detection and VPN IP verification

Runs once at container startup. Two modes:

VPN_CONTAINER is set (e.g. VPN_CONTAINER=gluetun):
  Pings will be routed through a temporary container sharing that container's
  network namespace. At startup we fetch both the host (Trackarr's own) IP
  and the VPN container's IP and store them. The run pipeline checks them
  before pinging and aborts if they match (VPN not protecting traffic).

VPN_CONTAINER is not set:
  Pings run in-process, direct or via SOCKS5/HTTP proxy per config.

This replaces the previous /sys/class/net tun/wg/tap interface detection,
which was unreliable because docker-compose bridge networks produce the same
interface patterns as real VPN networks on some hosts.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

import aiohttp

logger = logging.getLogger(__name__)


async def _fetch_ip(url: str = "https://api.ipify.org", timeout: float = 10.0) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return (await resp.text()).strip()
    except Exception as exc:
        logger.warning("Could not fetch external IP from %s: %s", url, exc)
        return None


def _fetch_ip_via_container(vpn_container: str) -> str | None:
    """
    Spawns a minimal alpine container on the VPN container's network namespace
    and fetches the external IP through it. This is exactly the same pattern
    as the original PS implementation. Returns None on failure.
    """
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                f"--network=container:{vpn_container}",
                "alpine",
                "wget", "--timeout=10", "-qO-", "https://api.ipify.org",
            ],
            capture_output=True, text=True, timeout=30,
        )
        ip = result.stdout.strip()
        return ip if ip else None
    except Exception as exc:
        logger.warning("Could not fetch VPN container IP via docker run: %s", exc)
        return None


async def detect(vpn_container: str = "") -> dict:
    """
    Detect connection mode and fetch IPs for display and verification.

    Returns:
        mode            "vpn" | "direct" | "proxy"
        vpn_detected    bool
        vpn_container   str — the configured container name, or ""
        host_ip         str | None — Trackarr's own external IP
        vpn_ip          str | None — IP seen through the VPN container, if configured
        external_ip     str | None — alias for host_ip, for GUI compatibility
        ips_match       bool | None — True if host_ip == vpn_ip (VPN not working)
    """
    host_ip = await _fetch_ip()

    if not vpn_container:
        logger.info(
            "No VPN_CONTAINER configured — direct/proxy mode. external_ip=%s", host_ip
        )
        return {
            "mode":          "direct",
            "vpn_detected":  False,
            "vpn_container": "",
            "host_ip":       host_ip,
            "vpn_ip":        None,
            "external_ip":   host_ip,
            "ips_match":     None,
        }

    logger.info("VPN_CONTAINER=%s — fetching VPN IP via ephemeral container...", vpn_container)
    vpn_ip = await asyncio.get_event_loop().run_in_executor(
        None, _fetch_ip_via_container, vpn_container
    )

    ips_match = (host_ip == vpn_ip) if (host_ip and vpn_ip) else None

    if vpn_ip is None:
        logger.warning(
            "Could not reach VPN container '%s'. Is it running?", vpn_container
        )
    elif ips_match:
        logger.warning(
            "CRITICAL: VPN IP (%s) matches host IP (%s) — VPN is NOT protecting traffic.",
            vpn_ip, host_ip,
        )
    else:
        logger.info(
            "VPN verified. Host IP: %s | VPN IP: %s", host_ip, vpn_ip
        )

    return {
        "mode":          "vpn",
        "vpn_detected":  True,
        "vpn_container": vpn_container,
        "host_ip":       host_ip,
        "vpn_ip":        vpn_ip,
        "external_ip":   vpn_ip or host_ip,
        "ips_match":     ips_match,
    }
