"""
scheduler.py — Internal async scheduler

No external dependency (no APScheduler/Celery). A simple asyncio background
task that wakes every CHECK_INTERVAL_SECONDS, checks all enabled schedules,
and fires any that are due.

Schedule types:
  daily     — fires once a day at `time` (HH:MM, UTC)
  weekly    — fires once a week on `day_of_week` (0=Monday..6=Sunday) at `time`
  hourly    — fires once an hour at `minute` (0-59)
  interval  — fires every `interval_minutes` minutes

Persisted to /app/data/schedules.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

SCHEDULES_FILE = Path("/app/data/schedules.json")
CHECK_INTERVAL_SECONDS = 30

TriggerFn = Callable[[], Awaitable[bool]]   # async () -> success: bool, runs a trackerping job to completion


class Schedule(BaseModel):
    id:               str = Field(default_factory=lambda: secrets.token_hex(4))
    name:             str = "TrackerPing"
    enabled:          bool = True
    frequency:        str = "daily"          # daily | weekly | hourly | interval
    time:             str = "03:00"          # HH:MM, UTC — used by daily/weekly
    day_of_week:      int = 0                # 0=Monday..6=Sunday — used by weekly
    interval_minutes: int = 60               # used by interval
    last_run:         str | None = None
    last_result:      str | None = None      # "success" | "failed" | None
    next_run:         str | None = None

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: str) -> str:
        if v not in ("daily", "weekly", "hourly", "interval"):
            return "daily"
        return v

    @field_validator("day_of_week")
    @classmethod
    def clamp_day(cls, v: int) -> int:
        return max(0, min(v, 6))

    @field_validator("interval_minutes")
    @classmethod
    def clamp_interval(cls, v: int) -> int:
        return max(5, min(v, 10_080))   # 5 min .. 7 days


def load_schedules() -> list[Schedule]:
    if not SCHEDULES_FILE.exists():
        return []
    try:
        data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        return [Schedule.model_validate(s) for s in data]
    except Exception as exc:
        logger.warning("Could not read schedules.json: %s", exc)
        return []


def save_schedules(schedules: list[Schedule]) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_FILE.write_text(
        json.dumps([s.model_dump() for s in schedules], indent=2),
        encoding="utf-8",
    )


def compute_next_run(schedule: Schedule, now: datetime) -> datetime:
    """Returns the next UTC datetime this schedule should fire, strictly after `now`."""
    if schedule.frequency == "interval":
        return now + timedelta(minutes=schedule.interval_minutes)

    if schedule.frequency == "hourly":
        candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return candidate

    # daily / weekly — both use `time` (HH:MM)
    try:
        hour, minute = (int(x) for x in schedule.time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 3, 0

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule.frequency == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # weekly
    days_ahead = (schedule.day_of_week - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def recalculate_next_runs(schedules: list[Schedule], now: datetime | None = None) -> list[Schedule]:
    """
    Called at startup. For every enabled schedule, recompute next_run.
    If next_run was already in the past (container was down), it stays due —
    the run loop will fire it on the very first check after startup.
    """
    now = now or datetime.now(timezone.utc)
    for s in schedules:
        if not s.enabled:
            s.next_run = None
            continue
        existing_next = _parse(s.next_run)
        if existing_next and existing_next > now:
            continue   # still valid, don't recompute
        s.next_run = compute_next_run(s, now).isoformat()
    return schedules


class Scheduler:
    """Owns the background asyncio loop. Started once at app startup."""

    def __init__(self, trigger_fn: TriggerFn):
        self._trigger_fn = trigger_fn
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started (check interval: %ds).", CHECK_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        # Recalculate on startup so missed schedules (container was down) fire promptly
        schedules = load_schedules()
        schedules = recalculate_next_runs(schedules)
        save_schedules(schedules)

        while not self._stop_event.is_set():
            try:
                await self._check_and_fire()
            except Exception:
                logger.exception("Scheduler loop iteration failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _check_and_fire(self) -> None:
        now = datetime.now(timezone.utc)
        schedules = load_schedules()
        dirty = False

        for s in schedules:
            if not s.enabled:
                continue
            next_run = _parse(s.next_run)
            if next_run is None:
                s.next_run = compute_next_run(s, now).isoformat()
                dirty = True
                continue

            if next_run <= now:
                logger.info("Schedule '%s' (%s) is due — firing.", s.name, s.id)
                s.last_run = now.isoformat()
                try:
                    success = await self._trigger_fn()
                    s.last_result = "success" if success else "failed"
                except Exception:
                    logger.exception("Scheduled run failed for '%s'", s.name)
                    s.last_result = "failed"
                s.next_run = compute_next_run(s, now).isoformat()
                dirty = True

        if dirty:
            save_schedules(schedules)
