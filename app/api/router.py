"""
router.py — All REST API endpoints

Phases implemented here grow as the project progresses.
Phase 1: ping, network-mode, config read/write.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import AppConfig, load_app_config, save_app_config, env
from ..core import history as history_module
from ..core import scheduler as scheduler_module
from ..core import sleep as sleep_module
from ..core import sources as sources_module
from . import jobs as job_router

logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(job_router.router, prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Health / meta
# ---------------------------------------------------------------------------

@router.get("/ping", tags=["meta"])
async def ping():
    return {"ok": True, "version": "2.0.0"}


@router.get("/network-mode", tags=["meta"])
async def network_mode(request: Request):
    """
    Returns the connection mode detected at startup.
    mode:         "vpn" | "direct" | "proxy"
    vpn_detected: bool — if True, GUI should hide proxy/direct options
    gateway:      str  — container's default gateway IP
    external_ip:  str  — container's external IP as seen from the internet
    """
    return request.app.state.network_info


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@router.get("/config", tags=["config"])
async def get_config(request: Request):
    """
    Returns non-sensitive app config.
    Sensitive values (credentials) are returned as masked strings
    so the GUI can indicate whether they are set without exposing them.
    """
    cfg: AppConfig = request.app.state.config
    e = env  # module-level env singleton

    return {
        **cfg.model_dump(),
        # Credential presence indicators — never return actual values
        "qbt_url":       e.qbt_url        or "",
        "qbt_user":      e.qbt_user       or "",
        "qbt_pass":      "********"  if e.qbt_pass        else "",
        "pushover_user": "********"  if e.pushover_user   else "",
        "pushover_token": "********" if e.pushover_token  else "",
        "github_token":  "********"  if e.github_token    else "",
        "webhook_url":   e.webhook_url    or "",
    }


@router.post("/config", tags=["config"])
async def post_config(request: Request):
    """
    Save non-sensitive config to /app/data/config.json.
    Credentials are NOT accepted here — they must be set as env vars.
    """
    body = await request.json()

    # Strip any credential fields the client may have sent — they live in env only
    for key in ("qbt_url", "qbt_user", "qbt_pass", "pushover_user", "pushover_token", "github_token", "webhook_url"):
        body.pop(key, None)

    try:
        config = AppConfig.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    save_app_config(config)
    request.app.state.config = config
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tracker history
# ---------------------------------------------------------------------------

@router.get("/tracker-history", tags=["trackers"])
async def get_tracker_history():
    return history_module.load_history()


# ---------------------------------------------------------------------------
# Sleep / hibernate state
# ---------------------------------------------------------------------------

@router.get("/tracker-sleep", tags=["trackers"])
async def get_tracker_sleep():
    state = sleep_module.load_sleep_state()
    return {url: entry.__dict__ for url, entry in state.items()}


@router.post("/tracker-sleep/wake", tags=["trackers"])
async def wake_tracker(request: Request):
    body = await request.json()
    url = body.get("url")
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=422)
    state = sleep_module.load_sleep_state()
    state.pop(url, None)
    sleep_module.save_sleep_state(state)
    return {"ok": True}


@router.post("/tracker-sleep/wake-all", tags=["trackers"])
async def wake_all_trackers():
    sleep_module.save_sleep_state({})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tracker sources (GitHub repos, website scrapes, manual entries)
# ---------------------------------------------------------------------------

@router.get("/tracker-sources", tags=["trackers"])
async def get_tracker_sources():
    return sources_module.load_sources().model_dump()


@router.post("/tracker-sources/github-repos", tags=["trackers"])
async def add_github_repo(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=422)
    sources = sources_module.add_github_repo(url, body.get("label", ""))
    return {"ok": True, "sources": sources.model_dump()}


@router.delete("/tracker-sources/github-repos/{repo_id}", tags=["trackers"])
async def delete_github_repo(repo_id: str):
    sources = sources_module.remove_github_repo(repo_id)
    return {"ok": True, "sources": sources.model_dump()}


@router.post("/tracker-sources/website-scrapes", tags=["trackers"])
async def add_website_scrape(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=422)
    sources = sources_module.add_website_scrape(url, body.get("label", ""))
    return {"ok": True, "sources": sources.model_dump()}


@router.delete("/tracker-sources/website-scrapes/{scrape_id}", tags=["trackers"])
async def delete_website_scrape(scrape_id: str):
    sources = sources_module.remove_website_scrape(scrape_id)
    return {"ok": True, "sources": sources.model_dump()}


@router.post("/tracker-sources/manual", tags=["trackers"])
async def set_manual_trackers(request: Request):
    body = await request.json()
    trackers = body.get("trackers", [])
    if not isinstance(trackers, list):
        return JSONResponse({"ok": False, "error": "trackers must be a list"}, status_code=422)
    sources = sources_module.set_manual(trackers)
    return {"ok": True, "sources": sources.model_dump()}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@router.get("/schedules", tags=["scheduler"])
async def list_schedules():
    return [s.model_dump() for s in scheduler_module.load_schedules()]


@router.post("/schedules", tags=["scheduler"])
async def create_schedule(request: Request):
    body = await request.json()
    try:
        new_schedule = scheduler_module.Schedule.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    schedules = scheduler_module.load_schedules()
    schedules.append(new_schedule)
    schedules = scheduler_module.recalculate_next_runs(schedules)
    scheduler_module.save_schedules(schedules)
    return {"ok": True, "schedule": new_schedule.model_dump()}


@router.put("/schedules/{schedule_id}", tags=["scheduler"])
async def update_schedule(schedule_id: str, request: Request):
    body = await request.json()
    schedules = scheduler_module.load_schedules()
    idx = next((i for i, s in enumerate(schedules) if s.id == schedule_id), None)
    if idx is None:
        return JSONResponse({"ok": False, "error": "schedule not found"}, status_code=404)

    body["id"] = schedule_id   # id is immutable
    try:
        updated = scheduler_module.Schedule.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    # Force next_run to be recalculated since frequency/time may have changed
    updated.next_run = None
    schedules[idx] = updated
    schedules = scheduler_module.recalculate_next_runs(schedules)
    scheduler_module.save_schedules(schedules)
    return {"ok": True, "schedule": schedules[idx].model_dump()}


@router.delete("/schedules/{schedule_id}", tags=["scheduler"])
async def delete_schedule(schedule_id: str):
    schedules = scheduler_module.load_schedules()
    remaining = [s for s in schedules if s.id != schedule_id]
    if len(remaining) == len(schedules):
        return JSONResponse({"ok": False, "error": "schedule not found"}, status_code=404)
    scheduler_module.save_schedules(remaining)
    return {"ok": True}
