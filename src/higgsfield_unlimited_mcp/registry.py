"""In-memory registry tracking all jobs submitted in this MCP session.

Persists across tool calls within a single server process, so the LLM can
fire-and-forget submissions and check back later by job_id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import JobNotFoundError

log = logging.getLogger(__name__)


@dataclass
class JobRecord:
    job_id: str
    prompt: str
    model: str
    resolution: str
    aspect_ratio: str
    width: int
    height: int
    batch_size: int
    submitted_at: float = field(default_factory=time.time)
    status: str = "submitted"          # submitted | active | completed | failed | timeout
    completed_at: Optional[float] = None
    last_status_payload: Optional[dict] = None
    output_paths: list[Path] = field(default_factory=list)
    output_urls: list[str] = field(default_factory=list)
    error: Optional[str] = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "prompt": self.prompt,
            "model": self.model,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "batch_size": self.batch_size,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "completed_at": self.completed_at,
            "duration_seconds": (
                round(self.completed_at - self.submitted_at, 1)
                if self.completed_at else None
            ),
            "output_paths": [str(p) for p in self.output_paths],
            "output_urls": self.output_urls,
            "error": self.error,
            "tags": self.tags,
        }


class JobRegistry:
    """Concurrency-safe registry plus a global semaphore for slot limits."""

    def __init__(self, max_concurrent: int):
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active_count = 0

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def active_count(self) -> int:
        return self._active_count

    async def register(self, record: JobRecord) -> None:
        async with self._lock:
            self._jobs[record.job_id] = record
            log.debug("Registered job %s", record.job_id)

    async def update(self, job_id: str, **fields) -> JobRecord:
        async with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(f"Job {job_id} is not in the registry")
            record = self._jobs[job_id]
            for k, v in fields.items():
                setattr(record, k, v)
            return record

    async def get(self, job_id: str) -> JobRecord:
        async with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(f"Job {job_id} is not in the registry")
            return self._jobs[job_id]

    async def list(self, status: Optional[str] = None) -> list[JobRecord]:
        async with self._lock:
            if status is None:
                return list(self._jobs.values())
            return [j for j in self._jobs.values() if j.status == status]

    async def remove(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)

    async def mark_active(self) -> None:
        async with self._lock:
            self._active_count += 1

    async def mark_inactive(self) -> None:
        async with self._lock:
            self._active_count = max(0, self._active_count - 1)

    def snapshot(self) -> dict:
        """Quick stats — for status tools."""
        counts: dict[str, int] = {}
        for j in self._jobs.values():
            counts[j.status] = counts.get(j.status, 0) + 1
        return {
            "total_jobs": len(self._jobs),
            "by_status": counts,
            "active_now": self._active_count,
            "max_concurrent": self._max_concurrent,
            "available_slots": self._max_concurrent - self._active_count,
        }
