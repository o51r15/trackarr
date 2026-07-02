"""
ping_worker.py — Standalone ping worker, invoked inside an ephemeral container

Usage:
    python3 -m app.ping_worker <input_file> <output_file>

input_file:  one tracker URL per line
output_file: JSON array of {url, up, latency_ms} written on completion

This module is the entry point for the ephemeral ping container spawned by
ping.py when VPN_CONTAINER is set. The container runs on the VPN network
namespace (--network=container:<vpn_container>) so all outbound traffic
exits through the VPN tunnel automatically.

The main Trackarr process writes the input file, spawns this container,
waits for it to exit, then reads the output file. Temp files are cleaned
up by the caller after reading.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


async def run(input_path: str, output_path: str) -> None:
    # Import here after the module is fully initialized as part of the package
    from app.core.ping import ping_all
    from app.core.latency import measure_all

    urls = [
        ln.strip() for ln in Path(input_path).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]

    if not urls:
        Path(output_path).write_text("[]", encoding="utf-8")
        return

    ping_results = await ping_all(urls, no_udp=False, timeout=10.0)
    passed = [r.url for r in ping_results if r.up is True]
    latency_map = await measure_all(passed, timeout_ms=3000)

    results = []
    for r in ping_results:
        results.append({
            "url": r.url,
            "up": r.up,
            "latency_ms": latency_map.get(r.url) if r.up else None,
        })

    Path(output_path).write_text(json.dumps(results), encoding="utf-8")
    print(f"[ping_worker] Done. {len(passed)}/{len(urls)} passed.", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 -m app.ping_worker <input_file> <output_file>")
        sys.exit(1)
    asyncio.run(run(sys.argv[1], sys.argv[2]))
