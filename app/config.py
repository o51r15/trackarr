"""
config.py — Configuration management

Sensitive values (credentials) come from environment variables only.
Non-sensitive settings are stored in /app/data/config.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DATA_DIR = Path("/app/data")
CONFIG_FILE = DATA_DIR / "config.json"


# ---------------------------------------------------------------------------
# Environment variables — sensitive values only
# ---------------------------------------------------------------------------

class Env(BaseSettings):
    """
    Reads from actual environment variables (set in docker-compose).
    All values default to empty string so the app starts without crashing
    if credentials are not yet configured.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    qbt_url:        str = ""
    qbt_user:       str = ""
    qbt_pass:       str = ""
    pushover_user:  str = ""
    pushover_token: str = ""
    github_token:   str = ""
    webhook_url:    str = ""
    ollama_url:     str = ""     # e.g. http://192.168.1.x:11434 — empty disables quality assessment entirely
    ollama_model:   str = ""     # e.g. gemma4:latest — must be explicitly set, no default model assumed
    vpn_container:  str = ""     # e.g. gluetun — name of the VPN container to route pings through


# ---------------------------------------------------------------------------
# File-based config — non-sensitive settings
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    """
    Stored in /app/data/config.json.
    Editable via the GUI Config tab or direct file edit.
    """
    history_days:        int         = 7
    latency_timeout_ms:  int         = 3000
    proxy_url:           str         = ""
    connection_mode:     str         = "direct"   # "direct" | "proxy" — vpn is auto-detected
    pushover_notify:     bool        = False
    webhook_notify:      bool        = False
    tracker_urls:        List[str]   = [
        "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt"
    ]

    @field_validator("connection_mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("direct", "proxy"):
            return "direct"
        return v

    @field_validator("history_days")
    @classmethod
    def clamp_history(cls, v: int) -> int:
        return max(1, min(v, 90))

    @field_validator("latency_timeout_ms")
    @classmethod
    def clamp_latency(cls, v: int) -> int:
        return max(500, min(v, 30_000))


def load_app_config() -> AppConfig:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return AppConfig.model_validate(data)
        except Exception as exc:
            logger.warning("Could not parse config.json (%s) — using defaults.", exc)
    return AppConfig()


def save_app_config(config: AppConfig) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Config saved to %s", CONFIG_FILE)


# Module-level singleton — loaded once at import, refreshed on save
env = Env()
