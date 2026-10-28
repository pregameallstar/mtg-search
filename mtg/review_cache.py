"""In-memory job cache for Deck Doctor reviews.

Review runs happen on a daemon thread; the browser polls progress and then fetches
the result. Jobs are capped so abandoned reviews cannot grow memory without bound.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

_CAP = 25
_lock = threading.Lock()
_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def create_job(*, total: int = 5) -> str:
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        _jobs[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "step": 0,
            "total": total,
            "label": "Queued…",
            "error": None,
            "result": None,
            "createdAt": now,
        }
        while len(_jobs) > _CAP:
            _jobs.popitem(last=False)
    return job_id


def update(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def get(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None


def remove(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)


def progress(job_id: str) -> dict[str, Any] | None:
    job = get(job_id)
    if job is None:
        return None
    return {
        "jobId": job_id,
        "status": job["status"],
        "step": job["step"],
        "total": job["total"],
        "label": job["label"],
        "error": job.get("error"),
    }
