"""
sources.py — Tracker source management (GitHub repos, website scrapes, manual entries)

Stored in /app/data/tracker-sources.json. This is what collect.py reads to
know which GitHub repos to crawl and which websites to scrape, on top of
the raw tracker_urls list (which lives in AppConfig).
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SOURCES_FILE = Path("/app/data/tracker-sources.json")


class GithubRepo(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    url: str
    label: str = ""


class WebsiteScrape(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    url: str
    label: str = ""


class DiscoveryCandidate(BaseModel):
    url:             str
    raw_url:         str = ""
    source_type:     str            # "raw_list" | "github_repo" | "website_scrape"
    label:           str = ""
    stars:           int | None = None
    last_commit:     str | None = None
    discovered_date: str = ""


class DiscoveryState(BaseModel):
    last_github_run:        str | None = None
    minimum_interval_days:  int = 7
    candidates:              list[DiscoveryCandidate] = []
    dismissed:                list[str] = []


class TrackerSources(BaseModel):
    github_repos:    list[GithubRepo]      = []
    website_scrape:  list[WebsiteScrape]   = []
    manual:          list[str]             = []
    discovery:       DiscoveryState         = DiscoveryState()


def load_sources() -> TrackerSources:
    if not SOURCES_FILE.exists():
        return TrackerSources()
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        return TrackerSources.model_validate(data)
    except Exception as exc:
        logger.warning("Could not parse tracker-sources.json: %s", exc)
        return TrackerSources()


def save_sources(sources: TrackerSources) -> None:
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(sources.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Sources saved: %d repos, %d scrapes, %d manual",
                len(sources.github_repos), len(sources.website_scrape), len(sources.manual))


def add_github_repo(url: str, label: str = "") -> TrackerSources:
    sources = load_sources()
    sources.github_repos.append(GithubRepo(url=url, label=label or url))
    save_sources(sources)
    return sources


def remove_github_repo(repo_id: str) -> TrackerSources:
    sources = load_sources()
    sources.github_repos = [r for r in sources.github_repos if r.id != repo_id]
    save_sources(sources)
    return sources


def add_website_scrape(url: str, label: str = "") -> TrackerSources:
    sources = load_sources()
    sources.website_scrape.append(WebsiteScrape(url=url, label=label or url))
    save_sources(sources)
    return sources


def remove_website_scrape(scrape_id: str) -> TrackerSources:
    sources = load_sources()
    sources.website_scrape = [s for s in sources.website_scrape if s.id != scrape_id]
    save_sources(sources)
    return sources


def set_manual(trackers: list[str]) -> TrackerSources:
    sources = load_sources()
    sources.manual = [t.strip() for t in trackers if t.strip()]
    save_sources(sources)
    return sources


# ---------------------------------------------------------------------------
# Discovery candidate management
# ---------------------------------------------------------------------------

def known_source_urls(sources: TrackerSources, tracker_urls: list[str]) -> set[str]:
    """All URLs already configured as sources, across every source type."""
    known = set(tracker_urls)
    known.update(r.url for r in sources.github_repos)
    known.update(s.url for s in sources.website_scrape)
    return known


def approve_candidate(candidate: DiscoveryCandidate) -> TrackerSources:
    """
    Moves a candidate from the pending list into the appropriate source type.
    Note: raw_list candidates are NOT added here — those go into AppConfig.tracker_urls,
    which the router endpoint handles directly since it has access to app.state.config.
    """
    sources = load_sources()
    if candidate.source_type == "github_repo":
        sources.github_repos.append(GithubRepo(url=candidate.url, label=candidate.label or candidate.url))
    elif candidate.source_type == "website_scrape":
        sources.website_scrape.append(WebsiteScrape(url=candidate.url, label=candidate.label or candidate.url))
    sources.discovery.candidates = [c for c in sources.discovery.candidates if c.url != candidate.url]
    save_sources(sources)
    return sources


def dismiss_candidate(url: str) -> TrackerSources:
    sources = load_sources()
    if url not in sources.discovery.dismissed:
        sources.discovery.dismissed.append(url)
    sources.discovery.candidates = [c for c in sources.discovery.candidates if c.url != url]
    save_sources(sources)
    return sources


def clear_dismissed() -> TrackerSources:
    sources = load_sources()
    sources.discovery.dismissed = []
    save_sources(sources)
    return sources
