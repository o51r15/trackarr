"""
network.py — VPN auto-detection

Runs once at container startup. Inspects the default network gateway to
determine whether this container is attached to a VPN-routed Docker network.

Logic:
  - Read default gateway from /proc/net/route (Linux only — container environment)
  - If gateway == 172.17.0.1  →  standard Docker bridge, no VPN
  - If gateway is any other private IP  →  custom Docker network, assumed VPN-routed
  - Fetch external IP from ipify.org and log it for confirmation

Result is stored in app.state and exposed via GET /api/network-mode.
It is set once at startup and never changes for the lifetime of the container.
If VPN is detected, the GUI hides proxy/direct options entirely.
If VPN is not detected, the GUI shows proxy/direct options and hides VPN references.
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Literal

import aiohttp

logger = logging.getLogger(__name__)

ConnectionMode = Literal["vpn", "direct", "proxy"]

# The default gateway on the standard Docker bridge network.
# Any other private gateway means the container is on a custom (VPN) network.
DOCKER_BRIDGE_GATEWAY = "172.17.0.1"


def _read_default_gateway() -> str | None:
    """
    Parse /proc/net/route for the default route (destination 0.0.0.0).
    Returns the gateway IP string, or None if unreadable.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:          # skip header
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                destination = fields[1]
                gateway_hex = fields[2]
                if destination == "00000000":        # 0.0.0.0 = default route
                    # Gateway is little-endian hex
                    gw_bytes = struct.pack("<L", int(gateway_hex, 16))
                    return socket.inet_ntoa(gw_bytes)
    except Exception as exc:
        logger.debug("Could not read /proc/net/route: %s", exc)
    return None


def _is_private_ip(ip: str) -> bool:
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return False
        return (
            parts[0] == 10
            or (parts[0] == 172 and 16 <= parts[1] <= 31)
            or (parts[0] == 192 and parts[1] == 168)
        )
    except ValueError:
        return False


async def _fetch_external_ip() -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.ipify.org",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return (await resp.text()).strip()
    except Exception as exc:
        logger.warning("Could not fetch external IP: %s", exc)
        return None


async def detect() -> dict:
    """
    Run VPN detection. Returns a dict suitable for the /api/network-mode response.

    Fields:
        mode          "vpn" | "direct" | "proxy"  — resolved mode
        vpn_detected  bool                          — True if VPN network found
        gateway       str | None                    — default gateway IP
        external_ip   str | None                    — container's external IP
    """
    gateway = _read_default_gateway()
    external_ip = await _fetch_external_ip()

    vpn_detected = (
        gateway is not None
        and gateway != DOCKER_BRIDGE_GATEWAY
        and _is_private_ip(gateway)
    )

    mode: ConnectionMode = "vpn" if vpn_detected else "direct"

    result = {
        "mode":         mode,
        "vpn_detected": vpn_detected,
        "gateway":      gateway,
        "external_ip":  external_ip,
    }

    if vpn_detected:
        logger.info(
            "VPN network detected — gateway=%s external_ip=%s — proxy/direct options disabled.",
            gateway, external_ip,
        )
    else:
        logger.info(
            "No VPN network detected — gateway=%s external_ip=%s — proxy/direct options available.",
            gateway, external_ip,
        )

    return result
