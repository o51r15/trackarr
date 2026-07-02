"""
main.py — FastAPI application entry point

Startup sequence:
  1. Run VPN detection (network.detect())
  2. Load app config from /app/data/config.json
  3. Start the internal scheduler
  4. Mount API router
  5. Serve static GUI

Connection mode is set once at startup and never changes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_app_config, env
from .core.scheduler import Scheduler
from .network import detect
from .api.jobs import trigger_scheduled_run
from .api.router import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- Startup --------------------------------------------------------------
    logger.info("Trackarr v2 starting up...")

    network_info = await detect(vpn_container=env.vpn_container)
    app.state.network_info   = network_info
    app.state.connection_mode = network_info["mode"]

    app.state.config   = load_app_config()
    app.state.settings = env

    scheduler = Scheduler(trigger_fn=lambda: trigger_scheduled_run(app.state))
    scheduler.start()
    app.state.scheduler = scheduler

    logger.info("Ready. mode=%s  external_ip=%s", network_info["mode"], network_info["external_ip"])

    yield

    # -- Shutdown ---------------------------------------------------------------
    scheduler.stop()
    logger.info("Trackarr shutting down.")


app = FastAPI(
    title="Trackarr",
    version="2.0.0",
    description="Automated BitTorrent tracker management for qBittorrent",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.include_router(router, prefix="/api")


@app.get("/", include_in_schema=False)
async def serve_gui():
    return FileResponse(STATIC_DIR / "gui.html")
