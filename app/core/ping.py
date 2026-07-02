"""
ping.py — Async tracker connectivity checker

Two modes:

IN-PROCESS (vpn_container is None):
  Pings run directly inside Trackarr using asyncio. Supports UDP BitTorrent
  protocol, HTTP/HTTPS announce, and WS/WSS. Proxy support via aiohttp-socks
  (SOCKS5) or aiohttp's native HTTP proxy kwarg.

CONTAINER (vpn_container is set):
  Pings are delegated to an ephemeral Docker container sharing the VPN
  container's network namespace. Trackarr writes tracker URLs to a temp
  input file, spawns the container (using the same image), waits for it
  to complete, reads the JSON output file, cleans up. All traffic exits
  through the VPN tunnel automatically, no proxy configuration needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import socket
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

CONNECT_MAGIC = 0x41727101980
MAX_CONCURRENCY = 150
DATA_DIR = Path("/app/data")
IMAGE_NAME = "ghcr.io/o51r15/trackarr:latest"

ANNOUNCE_PARAMS = {
    "info_hash": "%00" * 20,
    "peer_id": "-TR3000-000000000000",
    "port": "6881",
    "uploaded": "0",
    "downloaded": "0",
    "left": "0",
    "compact": "1",
}


@dataclass
class PingResult:
    url: str
    up: bool | None     # None = skipped (UDP under proxy mode)
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# In-process UDP ping
# ---------------------------------------------------------------------------

class _UDPPingProtocol(asyncio.DatagramProtocol):
    def __init__(self, packet: bytes, tid: int):
        self.packet = packet
        self.tid = tid
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.sendto(self.packet)  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self.result.done():
            return
        if len(data) >= 16:
            action, rtid, _ = struct.unpack(">IIQ", data[:16])
            self.result.set_result(action == 0 and rtid == self.tid)
        else:
            self.result.set_result(False)

    def error_received(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_result(False)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.result.done():
            self.result.set_result(False)


async def _ping_udp(url: str, timeout: float) -> bool:
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or 80
    tid = random.randint(0, 0xFFFFFFFF)
    pkt = struct.pack(">QII", CONNECT_MAGIC, 0, tid)
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        )
        if not infos:
            return False
        addr = infos[0][4]
        proto = _UDPPingProtocol(pkt, tid)
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(lambda: proto, remote_addr=addr),
            timeout=timeout,
        )
        try:
            return await asyncio.wait_for(proto.result, timeout=timeout)
        finally:
            transport.close()
    except Exception:
        return False


def _announce_url(url: str) -> str:
    target = url
    scheme = urlparse(url).scheme.lower()
    if scheme in ("ws", "wss"):
        target = url.replace("wss://", "https://").replace("ws://", "http://")
    base = target.rstrip("/")
    if not base.endswith("/announce"):
        base += "/announce"
    return base


async def _ping_http(
    session: aiohttp.ClientSession,
    url: str,
    timeout: float,
    http_proxy: str | None = None,
) -> bool:
    base = _announce_url(url)
    try:
        async with session.get(
            base,
            params=ANNOUNCE_PARAMS,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            ssl=False,
            proxy=http_proxy,
        ) as resp:
            return resp.status < 500
    except Exception:
        return False


async def _ping_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    no_udp: bool,
    timeout: float,
    http_proxy: str | None = None,
) -> PingResult:
    async with sem:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https", "ws", "wss"):
            return PingResult(url, await _ping_http(session, url, timeout, http_proxy))
        elif scheme == "udp":
            if no_udp:
                return PingResult(url, None)
            return PingResult(url, await _ping_udp(url, timeout))
        else:
            return PingResult(url, False)


def _build_connector(proxy_url: str | None) -> aiohttp.BaseConnector:
    if proxy_url and proxy_url.startswith("socks5"):
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(proxy_url, limit=MAX_CONCURRENCY, ssl=False)
    return aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENCY)


async def _ping_all_inprocess(
    urls: list[str],
    no_udp: bool = False,
    timeout: float = 10.0,
    proxy_url: str | None = None,
) -> list[PingResult]:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = _build_connector(proxy_url)
    http_proxy = proxy_url if proxy_url and proxy_url.startswith("http") else None

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _ping_one(session, sem, url, no_udp, timeout, http_proxy)
            for url in urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    clean: list[PingResult] = []
    for r, url in zip(results, urls):
        if isinstance(r, Exception):
            clean.append(PingResult(url, False))
        else:
            clean.append(r)
    return clean


# ---------------------------------------------------------------------------
# Container-based ping (VPN mode)
# ---------------------------------------------------------------------------

async def _ping_all_via_container(
    urls: list[str],
    vpn_container: str,
) -> list[PingResult]:
    """
    Delegates all pinging to an ephemeral container sharing the VPN
    container's network namespace. Input/output via temp files in /app/data.
    """
    job_id = secrets.token_hex(6)
    input_file  = DATA_DIR / f"ping_{job_id}_input.txt"
    output_file = DATA_DIR / f"ping_{job_id}_output.json"

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        input_file.write_text("\n".join(urls), encoding="utf-8")

        loop = asyncio.get_event_loop()
        returncode = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "docker", "run", "--rm",
                    f"--network=container:{vpn_container}",
                    "-v", "/app/data:/app/data",
                    IMAGE_NAME,
                    "python3", "-m", "app.ping_worker",
                    str(input_file), str(output_file),
                ],
                timeout=300,
            ).returncode,
        )

        if returncode != 0:
            logger.error("Ping container exited with code %d", returncode)
            return [PingResult(url, False) for url in urls]

        if not output_file.exists():
            logger.error("Ping container produced no output file")
            return [PingResult(url, False) for url in urls]

        raw = json.loads(output_file.read_text(encoding="utf-8"))
        return [
            PingResult(url=r["url"], up=r["up"], latency_ms=r.get("latency_ms"))
            for r in raw
        ]

    except Exception as exc:
        logger.exception("Container ping failed: %s", exc)
        return [PingResult(url, False) for url in urls]
    finally:
        input_file.unlink(missing_ok=True)
        output_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ping_all(
    urls: list[str],
    no_udp: bool = False,
    timeout: float = 10.0,
    proxy_url: str | None = None,
    vpn_container: str | None = None,
) -> list[PingResult]:
    """
    Pings every tracker URL. Routes through an ephemeral Docker container
    on the VPN network if vpn_container is set, otherwise runs in-process.
    """
    if vpn_container:
        return await _ping_all_via_container(urls, vpn_container)
    return await _ping_all_inprocess(urls, no_udp, timeout, proxy_url)
