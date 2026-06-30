"""
quality_metrics.py — Deterministic tracker list quality metrics

These are computed in pure Python, never delegated to an LLM. Counting,
regex matching, and date math are things models get wrong or are needlessly
expensive/slow at — code does this reliably and instantly.

Used by quality_assessment.py to feed Ollama's qualitative judgment, and
exposed standalone in the discovery preview response regardless of whether
Ollama is configured at all.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from .collect import VALID_SCHEMES

PLACEHOLDER_HOST_PATTERN = re.compile(
    r"^(example|test|localhost|invalid|placeholder|changeme|yourtracker)",
    re.IGNORECASE,
)
# Hostnames that are long runs of random-looking alphanumerics with no dots/words —
# a weak heuristic signal, not proof, surfaced as a flag for the LLM/human to weigh
RANDOM_LOOKING_LABEL = re.compile(r"^[a-z0-9]{10,}$", re.IGNORECASE)


@dataclass
class DeterministicMetrics:
    total_lines: int = 0
    valid_tracker_lines: int = 0
    format_score: int = 0          # 0-100, % of lines that are well-formed tracker URLs
    protocol_counts: dict = field(default_factory=dict)   # {"udp": N, "http": N, "https": N, "ws": N}
    diversity_score: int = 0       # 0-100, penalizes single-protocol / single-domain lists
    overlap_pct: int = 0           # % of valid trackers already in the known pool
    freshness_days: int | None = None    # days since last commit (GitHub sources only)
    freshness_score: int | None = None   # 0-100, None if not applicable (raw lists/scrapes)
    red_flags: list[str] = field(default_factory=list)


def _is_suspicious_host(host: str) -> str | None:
    """Returns a red flag string if the host looks suspicious, else None."""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return f"Loopback address: {host}"
        if ip.is_private:
            return f"Private IP address: {host}"
        if ip.is_reserved or ip.is_link_local:
            return f"Reserved/link-local IP address: {host}"
        return None
    except ValueError:
        pass   # not an IP, it's a hostname

    if PLACEHOLDER_HOST_PATTERN.match(host):
        return f"Placeholder-looking hostname: {host}"

    label = host.split(".")[0] if "." in host else host
    if RANDOM_LOOKING_LABEL.match(label) and not any(c.isdigit() for c in label[:3]):
        return f"Hostname looks randomly generated: {host}"

    return None


def _is_suspicious_port(port: int | None) -> str | None:
    if port is None:
        return None
    if port == 0:
        return "Port 0 (invalid)"
    if port > 65535:
        return f"Port out of range: {port}"
    return None


def compute_metrics(
    content: str,
    known_trackers: set[str],
    last_commit_iso: str | None = None,
) -> DeterministicMetrics:
    """
    content: raw tracker list content (one URL per line, or scraped matches joined by newline)
    known_trackers: the current known-trackers cache, for overlap calculation
    last_commit_iso: ISO8601 timestamp of last commit, for GitHub sources only
    """
    m = DeterministicMetrics()

    lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
    m.total_lines = len(lines)
    if m.total_lines == 0:
        return m

    valid_lines = [ln for ln in lines if VALID_SCHEMES.match(ln)]
    m.valid_tracker_lines = len(valid_lines)
    m.format_score = round((m.valid_tracker_lines / m.total_lines) * 100) if m.total_lines else 0

    protocol_counts: dict[str, int] = {}
    hosts_seen: set[str] = set()
    flags: list[str] = []

    for url in valid_lines:
        p = urlparse(url)
        scheme = p.scheme.lower()
        protocol_counts[scheme] = protocol_counts.get(scheme, 0) + 1

        host = p.hostname or ""
        hosts_seen.add(host)

        host_flag = _is_suspicious_host(host)
        if host_flag and host_flag not in flags:
            flags.append(host_flag)

        port_flag = _is_suspicious_port(p.port)
        if port_flag and port_flag not in flags:
            flags.append(port_flag)

    m.protocol_counts = protocol_counts

    # Diversity: penalize single-protocol lists and lists dominated by one domain
    protocol_variety = len(protocol_counts)
    unique_host_ratio = len(hosts_seen) / max(m.valid_tracker_lines, 1)
    diversity = min(protocol_variety, 3) / 3 * 50 + min(unique_host_ratio, 1.0) * 50
    m.diversity_score = round(diversity)

    if unique_host_ratio < 0.2 and m.valid_tracker_lines >= 5:
        flags.append(f"Low domain diversity: {len(hosts_seen)} unique host(s) across {m.valid_tracker_lines} entries")

    overlap_count = sum(1 for url in valid_lines if url in known_trackers)
    m.overlap_pct = round((overlap_count / m.valid_tracker_lines) * 100) if m.valid_tracker_lines else 0

    if last_commit_iso:
        try:
            last_commit = datetime.fromisoformat(last_commit_iso.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - last_commit).days
            m.freshness_days = days
            # 0 days -> 100, 365+ days -> 0, linear in between
            m.freshness_score = max(0, round(100 - (days / 365) * 100))
            if days > 365:
                flags.append(f"No commits in {days} days")
        except ValueError:
            pass

    m.red_flags = flags
    return m
