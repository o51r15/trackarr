"""
jobs.py — Async job manager

Manages the lifecycle of TrackerPing and Discovery runs.
Each run is an asyncio Task with a unique 8-char ID.

Job states: pending → running → done | failed | aborted

Phase 1: data structures and stub endpoints only.
Phase 3: full run implementation with SSE streaming.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

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
    type:       str                        # "trackerping" | "discovery"
    status:     JobStatus = JobStatus.PENDING
    started_at: float     = field(default_factory=time.time)
    ended_at:   float | None = None
    log_lines:  list[dict]   = field(default_factory=list)
    task:       asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "type":       self.type,
            "status":     self.status,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
        }


# In-memory store — jobs are short-lived and don't need persistence
_jobs: dict[str, Job] = {}


def new_job_id() -> str:
    return secrets.token_hex(4)   # 8 hex chars


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def create_job(job_type: str) -> Job:
    job = Job(id=new_job_id(), type=job_type)
    _jobs[job.id] = job
    return job


def prune_jobs(max_age_seconds: int = 1800) -> None:
    """Remove completed jobs older than max_age_seconds."""
    cutoff = time.time() - max_age_seconds
    stale = [
        jid for jid, j in _jobs.items()
        if j.status not in (JobStatus.PENDING, JobStatus.RUNNING)
        and (j.ended_at or j.started_at) < cutoff
    ]
    for jid in stale:
        del _jobs[jid]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{job_id}")
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "job not found"}, 404
    return {
        **job.to_dict(),
        "log_lines": job.log_lines,
    }


@router.post("/{job_id}/abort")
async def abort_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    if job.task and not job.task.done():
        job.task.cancel()
        job.status = JobStatus.ABORTED
        job.ended_at = time.time()
    return {"ok": True}


@router.get("/{job_id}/stream")
async def stream_job(job_id: str):
    """
    SSE stream for a job's log output.
    Phase 3 will yield real log lines as they're produced.
    Phase 1 stub: returns job status and closes.
    """
    job = get_job(job_id)
    if not job:
        async def _not_found() -> AsyncGenerator[str, None]:
            yield "event: error\ndata: job not found\n\n"
        return StreamingResponse(_not_found(), media_type="text/event-stream")

    async def _stream() -> AsyncGenerator[str, None]:
        # Phase 1: yield existing lines then close
        for line in job.log_lines:
            yield f"data: {line}\n\n"
            await asyncio.sleep(0)
        if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
            yield f"event: done\ndata: {job.status}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
