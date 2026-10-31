"""Download queue: runs grab() jobs in the background and tracks their progress."""

import gc
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .grab import GrabError, grab
from .segmented import Cancelled

ACTIVE_STATUSES = ("queued", "running")


@dataclass
class Job:
    id: str
    url: str
    connections: int
    status: str = "queued"  # queued, running, paused, done, error, canceled
    phase: str = ""  # resolving, downloading, merging
    title: str = ""
    filename: str = ""
    downloaded: int = 0
    total: Optional[int] = None
    speed: float = 0.0  # bytes per second
    eta: Optional[float] = None  # seconds
    error: str = ""
    created_at: float = field(default_factory=time.time)
    path: Optional[Path] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("path")
        data["speed"] = round(self.speed)
        data["eta"] = round(self.eta) if self.eta is not None else None
        return data


class DownloadManager:
    def __init__(
        self,
        out_dir: Path,
        *,
        max_parallel: int = 3,
        connections: int = 8,
        max_height: Optional[int] = None,
        max_bytes: Optional[int] = None,
        allow_private: bool = False,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.connections = connections
        self.max_height = max_height
        self.max_bytes = max_bytes
        self.allow_private = allow_private
        self._jobs: Dict[str, Job] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="vidgrab")

    def add(self, url: str, connections: Optional[int] = None) -> Job:
        conns = max(1, min(32, connections or self.connections))
        job = Job(id=uuid.uuid4().hex[:12], url=url.strip(), connections=conns)
        with self._lock:
            self._jobs[job.id] = job
        self._start(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def pause(self, job_id: str) -> Job:
        return self._stop(job_id, "paused")

    def cancel(self, job_id: str) -> Job:
        return self._stop(job_id, "canceled")

    def resume(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status in ("paused", "error", "canceled"):
            job.error = ""
            job.status = "queued"
            self._start(job)
        return job

    def remove(self, job_id: str, delete_file: bool = False) -> None:
        job = self._require(job_id)
        if job.status in ACTIVE_STATUSES:
            self._stop(job_id, "canceled")
        with self._lock:
            self._jobs.pop(job_id, None)
        if delete_file and job.path:
            job.path.unlink(missing_ok=True)

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _stop(self, job_id: str, status: str) -> Job:
        job = self._require(job_id)
        if job.status in ACTIVE_STATUSES:
            job.status = status  # the worker sees the event and leaves the status alone
            self._cancel[job_id].set()
        return job

    def _start(self, job: Job) -> None:
        cancel = threading.Event()
        self._cancel[job.id] = cancel
        self._pool.submit(self._run, job, cancel)

    def _run(self, job: Job, cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        job.status = "running"
        samples = []  # (time, bytes) over the last few seconds for a steady speed

        def report(phase=None, title=None, downloaded=None, total=None):
            if phase:
                job.phase = phase
                if phase == "downloading":
                    samples.clear()
            if title:
                job.title = title
            if total is not None:
                job.total = total
            if downloaded is not None:
                job.downloaded = downloaded
                now = time.time()
                samples.append((now, downloaded))
                while len(samples) > 2 and now - samples[0][0] > 4:
                    samples.pop(0)
                span = samples[-1][0] - samples[0][0]
                if span > 0.3:
                    job.speed = max(0.0, (samples[-1][1] - samples[0][1]) / span)
                if job.speed > 0 and job.total:
                    job.eta = max(0.0, (job.total - downloaded) / job.speed)

        interrupted = False
        try:
            result = grab(
                job.url, self.out_dir, connections=job.connections, max_height=self.max_height,
                max_bytes=self.max_bytes, allow_private=self.allow_private, report=report,
                cancel=cancel,
            )
            job.path = result.path
            job.filename = result.path.name
            job.title = job.title or result.title
            if result.path.exists():
                job.downloaded = job.total = result.path.stat().st_size
            job.status = "done"
        except Cancelled:
            interrupted = True  # status was already set by _stop
        except GrabError as exc:
            job.status, job.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            job.status, job.error = "error", f"Unexpected error: {exc}"
        finally:
            job.speed, job.eta, job.phase = 0.0, None, ""
        if interrupted:
            # yt-dlp leaves its .part file open inside a reference cycle when interrupted;
            # collect it (outside the except block, once the traceback is gone) so the file
            # can be resumed or deleted
            gc.collect()
