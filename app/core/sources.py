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


class TrackerSources(BaseModel):
    github_repos:    list[GithubRepo]      = []
    website_scrape:  list[WebsiteScrape]   = []
    manual:          list[str]             = []


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
