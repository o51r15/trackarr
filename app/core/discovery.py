"""
discovery.py — Tracker source discovery engine

Ported from tracker-discovery.ps1. Two phases on every run:

  1. Well-known aggregator sources — ALWAYS checked (cheap, no rate limit)
  2. GitHub API search — RATE-LIMITED, runs at most once per
     sources.discovery.minimum_interval_days (default 7)

New sources found are added to sources.discovery.candidates as pending
approval. Already-known, already-dismissed, or already-candidate URLs are
skipped silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp

from . import sources as sources_module
from .collect import VALID_SCHEMES
from .sources import DiscoveryCandidate, TrackerSources

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Trackarr-Discovery/2.0"

LogFn = Callable[[str, str], Awaitable[None]]

# (url, label, source_type) — checked on every discovery run regardless of rate limit
WELL_KNOWN_SOURCES = [
    ("https://newtrackon.com/api/all", "NewTrackon API (all)", "raw_list"),
    ("https://newtrackon.com/api/stable", "NewTrackon API (stable)", "raw_list"),
    ("https://trackers.run/s/wp_up_hp_hs_v4_v6.txt", "trackers.run", "raw_list"),
    ("https://cf.trackerslist.com/all.txt", "cf.trackerslist.com (all)", "raw_list"),
    ("https://cf.trackerslist.com/best.txt", "cf.trackerslist.com (best)", "raw_list"),
    (
        "https://raw.githubusercontent.com/DeSireFire/animeTrackerList/master/AT_all.txt",
        "DeSireFire/animeTrackerList",
        "raw_list",
    ),
    ("https://trackerslist.com", "trackerslist.com (website)", "website_scrape"),
    ("https://github.com/ngosang/trackerslist", "ngosang/trackerslist", "github_repo"),
    ("https://github.com/XIU2/TrackersListCollection", "XIU2/TrackersListCollection", "github_repo"),
]

GITHUB_SEARCH_TERMS = ["torrent+tracker+list", "bittorrent+announce+trackers+list"]


def is_tracker_list(content: str) -> bool:
    lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
    if not lines:
        return False
    tracker_lines = [ln for ln in lines if VALID_SCHEMES.match(ln)]
    return len(tracker_lines) >= 5 and (len(tracker_lines) / len(lines)) > 0.5


def _add_candidate(
    sources: TrackerSources,
    known_urls: set[str],
    candidate_urls: set[str],
    url: str,
    raw_url: str,
    source_type: str,
    label: str,
    stars: int | None = None,
    last_commit: str | None = None,
) -> bool:
    if url in known_urls or url in sources.discovery.dismissed or url in candidate_urls:
        return False
    candidate_urls.add(url)
    sources.discovery.candidates.append(
        DiscoveryCandidate(
            url=url,
            raw_url=raw_url or url,
            source_type=source_type,
            label=label,
            stars=stars,
            last_commit=last_commit,
            discovered_date=datetime.now(timezone.utc).isoformat(),
        )
    )
    return True


async def _check_well_known(
    session: aiohttp.ClientSession,
    sources: TrackerSources,
    known_urls: set[str],
    candidate_urls: set[str],
    log: LogFn,
) -> int:
    await log("=== Step 1: Checking well-known aggregator sources ===", "step")
    new_count = 0

    for url, label, source_type in WELL_KNOWN_SOURCES:
        if url in known_urls or url in sources.discovery.dismissed or url in candidate_urls:
            await log(f"  Skipping (already known): {label}", "info")
            continue

        if source_type in ("github_repo", "website_scrape"):
            if _add_candidate(sources, known_urls, candidate_urls, url, url, source_type, label):
                new_count += 1
                await log(f"[OK] New {source_type.replace('_', ' ')} candidate: {label}", "ok")
            continue

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as resp:
                content = await resp.text(errors="ignore")
            if is_tracker_list(content):
                if _add_candidate(sources, known_urls, candidate_urls, url, url, "raw_list", label):
                    new_count += 1
                    await log(f"[OK] New raw list candidate: {label}", "ok")
            else:
                await log(f"  Not a tracker list (content mismatch): {url}", "info")
        except Exception:
            await log(f"  Could not reach: {url}", "warn")

    return new_count


async def _check_github_search(
    session: aiohttp.ClientSession,
    sources: TrackerSources,
    known_urls: set[str],
    candidate_urls: set[str],
    github_token: str,
    log: LogFn,
) -> int:
    min_days = sources.discovery.minimum_interval_days or 7
    last_run = sources.discovery.last_github_run
    last_run_dt = None
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run)
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    days_since = (now - last_run_dt).total_seconds() / 86400 if last_run_dt else min_days + 1

    if days_since < min_days:
        days_left = int(min_days - days_since) + 1
        await log(f"=== Step 2: GitHub search skipped (next eligible in {days_left} day(s)) ===", "step")
        return 0

    await log(f"=== Step 2: GitHub API search (last run: {last_run or 'never'}) ===", "step")

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    new_count = 0
    repos_seen: set[str] = set()
    rate_limited = False

    for term in GITHUB_SEARCH_TERMS:
        if rate_limited:
            break
        try:
            async with session.get(
                "https://api.github.com/search/repositories",
                params={"q": term, "sort": "stars", "order": "desc", "per_page": "15"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (403, 429):
                    await log("GitHub API rate limit hit on search. Add a Personal Access Token.", "warn")
                    rate_limited = True
                    continue
                search_data = await resp.json()
        except Exception as exc:
            await log(f"GitHub search failed: {exc}", "warn")
            continue

        total = search_data.get("total_count", 0)
        await log(f"  GitHub search '{term}': {total} total results.", "info")

        for repo in search_data.get("items", []):
            repo_path = repo["full_name"]
            repo_url = f"https://github.com/{repo_path}"
            if repo_path in repos_seen:
                continue
            repos_seen.add(repo_path)

            if repo_url in known_urls or repo_url in sources.discovery.dismissed or repo_url in candidate_urls:
                await log(f"  Already known: {repo_path}", "info")
                continue

            try:
                async with session.get(
                    f"https://api.github.com/repos/{repo_path}/git/trees/HEAD",
                    params={"recursive": "1"}, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (403, 429):
                        await log(
                            "GitHub API rate limit hit. Add a Personal Access Token in Sources.", "warn"
                        )
                        rate_limited = True
                        break
                    tree = await resp.json()

                txt_files = [
                    item for item in tree.get("tree", [])
                    if item.get("type") == "blob" and item.get("path", "").endswith(".txt")
                ][:3]

                if not txt_files:
                    await log(f"  No .txt files found: {repo_path}", "info")
                    continue

                sample_url = f"https://raw.githubusercontent.com/{repo_path}/HEAD/{txt_files[0]['path']}"
                async with session.get(
                    sample_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    sample_content = await resp.text(errors="ignore")

                if is_tracker_list(sample_content):
                    if _add_candidate(
                        sources, known_urls, candidate_urls, repo_url, repo_url, "github_repo",
                        repo_path, stars=repo.get("stargazers_count"), last_commit=repo.get("pushed_at"),
                    ):
                        new_count += 1
                        await log(f"[OK] GitHub candidate: {repo_path} ({repo.get('stargazers_count', 0)} stars)", "ok")
                else:
                    await log(f"  Not a tracker list: {repo_path}", "info")
            except Exception as exc:
                await log(f"  Could not check {repo_path} - {exc}", "warn")

    sources.discovery.last_github_run = now.isoformat()
    await log(
        f"GitHub search complete. Repos checked: {len(repos_seen)}. Rate limited: {rate_limited}.",
        "info",
    )
    return new_count


async def run_discovery(tracker_urls: list[str], github_token: str, log: LogFn) -> TrackerSources:
    """
    Runs both discovery phases and persists results to tracker-sources.json.
    Returns the updated TrackerSources.
    """
    sources = sources_module.load_sources()
    known_urls = sources_module.known_source_urls(sources, tracker_urls)
    candidate_urls = {c.url for c in sources.discovery.candidates}

    async with aiohttp.ClientSession() as session:
        new_well_known = await _check_well_known(session, sources, known_urls, candidate_urls, log)
        new_github = await _check_github_search(
            session, sources, known_urls, candidate_urls, github_token, log
        )

    total_new = new_well_known + new_github
    await log(
        f"=== Discovery complete: {total_new} new candidate(s). "
        f"{len(sources.discovery.candidates)} total pending. ===",
        "ok",
    )

    sources_module.save_sources(sources)
    return sources
