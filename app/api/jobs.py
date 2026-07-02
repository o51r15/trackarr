"""
jobs.py — Async job manager

Manages the lifecycle of TrackerPing and Discovery runs.
Each run is an asyncio Task with a unique 8-char ID, streamed live via SSE.

Job states: pending → running → done | failed | aborted
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from ..core import discovery as discovery_module
from ..core import notify as notify_module
from ..core.run import run_trackerping

logger = logging.getLogger(__name__)
router = APIRouter()


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"
    ABORTED = "aborted"


@dataclass
class Job:
    id:         str
    type:       str
    status:     JobStatus = JobStatus.PENDING
    started_at: float     = field(default_factory=time.time)
    ended_at:   float | None = None
    log_lines:  list[dict]   = field(default_factory=list)
    summary:    dict | None  = None
    task:       asyncio.Task | None = field(default=None, repr=False)
    _new_line_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "type":       self.type,
            "status":     self.status,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
            "summary":    self.summary,
        }


_jobs: dict[str, Job] = {}


def new_job_id() -> str:
    return secrets.token_hex(4)


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def create_job(job_type: str) -> Job:
    job = Job(id=new_job_id(), type=job_type)
    _jobs[job.id] = job
    return job


def prune_jobs(max_age_seconds: int = 1800) -> None:
    cutoff = time.time() - max_age_seconds
    stale = [
        jid for jid, j in _jobs.items()
        if j.status not in (JobStatus.PENDING, JobStatus.RUNNING)
        and (j.ended_at or j.started_at) < cutoff
    ]
    for jid in stale:
        del _jobs[jid]


async def _log_to_job(job: Job, message: str, level: str = "info") -> None:
    entry = {"ts": time.time(), "level": level, "msg": message}
    job.log_lines.append(entry)
    job._new_line_event.set()


def _describe_exception(exc: Exception) -> str:
    """
    Some exceptions (notably asyncio.TimeoutError) have an empty str() by
    design, which made "Unexpected error: {exc}" log lines render as
    "Unexpected error:" with nothing after it — useless for diagnosing a
    real connection timeout. Always include the exception type name, and
    only append str(exc) if it actually has content.
    """
    type_name = type(exc).__name__
    text = str(exc).strip()
    return f"{type_name}: {text}" if text else f"{type_name} (no further detail provided by the exception)"


async def _execute_trackerping(job: Job, app_state) -> None:
    job.status = JobStatus.RUNNING

    async def log(message: str, level: str = "info") -> None:
        await _log_to_job(job, message, level)
        logger.info("[job %s] %s", job.id, message)

    try:
        network_info = app_state.network_info
        config = app_state.config
        env = app_state.settings

        connection_mode = network_info["mode"] if network_info["vpn_detected"] else config.connection_mode

        summary = await run_trackerping(config, env, connection_mode, log, network_info)
        job.summary = {
            "fetched": summary.fetched,
            "active":  summary.active,
            "passed":  summary.passed,
            "success": summary.success,
            "error":   summary.error,
        }
        job.status = JobStatus.DONE if summary.success else JobStatus.FAILED
        await notify_module.notify_run_complete(
            config, env, summary.success, summary.fetched, summary.active, summary.passed, summary.error
        )
    except asyncio.CancelledError:
        job.status = JobStatus.ABORTED
        await log("Run aborted by user.", "warn")
        raise
    except Exception as exc:
        logger.exception("Job %s crashed", job.id)
        desc = _describe_exception(exc)
        await log(f"Unexpected error: {desc}", "error")
        job.status = JobStatus.FAILED
        job.summary = {"success": False, "error": desc}
        try:
            await notify_module.notify_run_complete(app_state.config, app_state.settings, False, error=desc)
        except Exception:
            pass
    finally:
        job.ended_at = time.time()
        job._new_line_event.set()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/run/trackerping")
async def start_trackerping(request: Request):
    prune_jobs()
    job = create_job("trackerping")
    job.task = asyncio.create_task(_execute_trackerping(job, request.app.state))
    return {"ok": True, "job_id": job.id}


async def trigger_scheduled_run(app_state) -> bool:
    """
    Used by the scheduler. Runs a trackerping job to completion (awaited, not
    fire-and-forget) so the scheduler can record a real success/failure result.
    The run is still visible in the job list/SSE stream like any other run.
    """
    prune_jobs()
    job = create_job("trackerping")
    job.task = asyncio.current_task()
    await _execute_trackerping(job, app_state)
    return job.status == JobStatus.DONE


async def _execute_discovery(job: Job, app_state) -> None:
    job.status = JobStatus.RUNNING

    async def log(message: str, level: str = "info") -> None:
        await _log_to_job(job, message, level)
        logger.info("[job %s] %s", job.id, message)

    try:
        config = app_state.config
        env = app_state.settings

        sources = await discovery_module.run_discovery(config.tracker_urls, env.github_token, log)
        job.summary = {
            "success": True,
            "candidates": len(sources.discovery.candidates),
        }
        job.status = JobStatus.DONE
        await notify_module.notify_discovery_complete(
            config, env, len(sources.discovery.candidates), True, None
        )
    except asyncio.CancelledError:
        job.status = JobStatus.ABORTED
        await log("Discovery aborted by user.", "warn")
        raise
    except Exception as exc:
        logger.exception("Discovery job %s crashed", job.id)
        desc = _describe_exception(exc)
        await log(f"Unexpected error: {desc}", "error")
        job.status = JobStatus.FAILED
        job.summary = {"success": False, "error": desc}
        try:
            await notify_module.notify_discovery_complete(
                app_state.config, app_state.settings, 0, False, desc
            )
        except Exception:
            pass
    finally:
        job.ended_at = time.time()
        job._new_line_event.set()


@router.post("/run/discovery")
async def start_discovery(request: Request):
    prune_jobs()
    job = create_job("discovery")
    job.task = asyncio.create_task(_execute_discovery(job, request.app.state))
    return {"ok": True, "job_id": job.id}


@router.get("/{job_id}")
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return {**job.to_dict(), "log_lines": job.log_lines}


@router.post("/{job_id}/abort")
async def abort_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    if job.task and not job.task.done():
        job.task.cancel()
    return {"ok": True}


@router.get("/{job_id}/stream")
async def stream_job(job_id: str):
    job = get_job(job_id)
    if not job:
        async def _not_found() -> AsyncGenerator[str, None]:
            yield "event: error\ndata: job not found\n\n"
        return StreamingResponse(_not_found(), media_type="text/event-stream")

    async def _stream() -> AsyncGenerator[str, None]:
        import json as _json

        sent = 0
        while True:
            job._new_line_event.clear()

            # Flush any lines not yet sent to this client
            while sent < len(job.log_lines):
                yield f"data: {_json.dumps(job.log_lines[sent])}\n\n"
                sent += 1

            if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                yield f"event: done\ndata: {_json.dumps(job.to_dict())}\n\n"
                break

            try:
                await asyncio.wait_for(job._new_line_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
