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
