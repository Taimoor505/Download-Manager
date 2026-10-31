"""Download queue: runs grab() jobs in the background, tracks progress, survives restarts."""

import gc
import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

from .categories import category_for
from .grab import GrabError, GrabOptions, RateLimited, grab
from .limiter import RateLimiter
from .pacing import SitePacer, site_key
from .segmented import Cancelled

# queued: waiting for a free slot. scheduled: waiting for the scheduler's window.
# pending: caught from the clipboard, waiting for the user to start it.
# waiting: the site pushed back (bot check); retried automatically at retry_at.
WAITING = ("queued", "scheduled", "pending")
ACTIVE = ("queued", "running")
STOPPED = ("paused", "error", "canceled", "scheduled", "pending", "waiting")
# Backoff after each consecutive block from a site, then give up and show the error
RETRY_DELAYS = (10 * 60, 20 * 60, 40 * 60, 60 * 60)
TICK_SECONDS = 10


@dataclass
class Job:
    id: str
    url: str
    connections: int
    status: str = "queued"  # queued, running, paused, done, error, canceled, scheduled, pending
    phase: str = ""  # resolving, downloading, merging
    title: str = ""
    filename: str = ""
    category: str = ""
    referer: str = ""
    downloaded: int = 0
    total: Optional[int] = None
    speed: float = 0.0  # bytes per second
    eta: Optional[float] = None  # seconds
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    retry_at: Optional[float] = None  # when a "waiting" job tries again
    attempts: int = 0  # consecutive blocks from the site
    signed_in: bool = False  # the browser extension sent a session for this job
    path: Optional[Path] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = str(self.path) if self.path else None
        data["speed"] = round(self.speed)
        data["eta"] = round(self.eta) if self.eta is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        job = cls(**{k: v for k, v in data.items() if k in known})
        job.path = Path(job.path) if job.path else None
        return job


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
        media_only: bool = False,
        sort_into_categories: bool = False,
        proxy: Optional[str] = None,
        speed_limit: float = 0,  # bytes per second, shared by every download; 0 = unlimited
        state_file: Optional[Path] = None,
        auto_retry_rate_limits: bool = False,
        cookies_browser: Optional[str] = None,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_parallel = max_parallel
        self.connections = connections
        self.max_height = max_height
        self.max_bytes = max_bytes
        self.allow_private = allow_private
        self.media_only = media_only
        self.sort_into_categories = sort_into_categories
        self.proxy = proxy
        self.limiter = RateLimiter(speed_limit)
        self.state_file = Path(state_file) if state_file else None
        self.auto_retry_rate_limits = auto_retry_rate_limits
        self.cookies_browser = cookies_browser
        self.pacer = SitePacer()
        self._jobs: Dict[str, Job] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._running: set = set()
        # Browser-extension sessions live only in memory and are dropped when the job ends
        self._session_cookies: Dict[str, List[dict]] = {}
        self._site_cooldown: Dict[str, float] = {}  # site -> time it may be tried again
        self._lock = threading.RLock()
        self._load()
        threading.Thread(target=self._tick_forever, name="vidgrab-retry", daemon=True).start()

    # ---- configuration -------------------------------------------------------------

    def configure(self, **changes) -> None:
        """Applies new settings; a new limit or parallel count takes effect immediately."""
        with self._lock:
            for key, value in changes.items():
                if key == "speed_limit":
                    self.limiter.set_rate(value)
                elif key == "out_dir":
                    self.out_dir = Path(value)
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                elif hasattr(self, key):
                    setattr(self, key, value)
                else:
                    raise AttributeError(key)
        self._dispatch()

    # ---- queue operations ----------------------------------------------------------

    def add(
        self,
        url: str,
        connections: Optional[int] = None,
        status: str = "queued",
        referer: str = "",
        cookies: Optional[List[dict]] = None,
    ) -> Job:
        if status not in WAITING:
            raise ValueError(f"New downloads can't start as {status!r}.")
        conns = max(1, min(32, connections or self.connections))
        job = Job(id=uuid.uuid4().hex[:12], url=url.strip(), connections=conns,
                  status=status, referer=referer.strip(), signed_in=bool(cookies))
        with self._lock:
            self._jobs[job.id] = job
            if cookies:
                self._session_cookies[job.id] = list(cookies)
        self._save()
        self._dispatch()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def pause(self, job_id: str) -> Job:
        return self._stop(self._require(job_id), "paused")

    def cancel(self, job_id: str) -> Job:
        return self._stop(self._require(job_id), "canceled")

    def resume(self, job_id: str) -> Job:
        job = self._require(job_id)
        with self._lock:
            if job.status == "waiting":
                # "Retry now": the user overrides the site's cooldown
                self._site_cooldown.pop(site_key(job.url), None)
            if job.status in STOPPED:
                job.status, job.error, job.retry_at = "queued", "", None
        self._save()
        self._dispatch()
        return job

    def pause_all(self) -> None:
        for job in self.list():
            if job.status in ACTIVE:
                self._stop(job, "paused")

    def resume_all(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                if job.status in ("paused", "error"):
                    job.status, job.error = "queued", ""
        self._save()
        self._dispatch()

    def start_scheduled(self) -> None:
        with self._lock:
            started = [j for j in self._jobs.values() if j.status == "scheduled"]
            for job in started:
                job.status = "queued"
        if started:
            self._save()
            self._dispatch()

    def stop_for_schedule(self) -> None:
        """The window closed: everything still downloading goes back to waiting for it."""
        for job in self.list():
            if job.status in ACTIVE:
                self._stop(job, "scheduled")

    def remove(self, job_id: str, delete_file: bool = False) -> None:
        job = self._require(job_id)
        if job.status in ACTIVE:
            self._stop(job, "canceled")
        with self._lock:
            self._jobs.pop(job_id, None)
            self._session_cookies.pop(job_id, None)
        self._save()
        if delete_file and job.path and job.status == "done":
            job.path.unlink(missing_ok=True)

    def clear_finished(self) -> None:
        with self._lock:
            for job_id in [j.id for j in self._jobs.values() if j.status == "done"]:
                self._jobs.pop(job_id)
        self._save()

    # ---- internals -----------------------------------------------------------------

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _stop(self, job: Job, status: str) -> Job:
        with self._lock:
            if job.status == "running":
                job.status = status  # the worker sees the event and leaves the status alone
                self._cancel[job.id].set()
            elif job.status in (*WAITING, "waiting") or (job.status == "paused" and status == "canceled"):
                job.status, job.retry_at = status, None
        self._save()
        return job

    def _dispatch(self) -> None:
        with self._lock:
            waiting = sorted(
                (j for j in self._jobs.values() if j.status == "queued"), key=lambda j: j.created_at
            )
            now = time.time()
            for job in waiting:
                if len(self._running) >= self.max_parallel:
                    break
                if self._site_cooldown.get(site_key(job.url), 0) > now:
                    continue  # this site is blocking us; asking again would extend the block
                cancel = threading.Event()
                self._cancel[job.id] = cancel
                self._running.add(job.id)
                job.status = "running"
                threading.Thread(
                    target=self._run, args=(job, cancel), name=f"vidgrab-{job.id}", daemon=True
                ).start()

    def _options(self, job: Job) -> GrabOptions:
        return GrabOptions(
            connections=job.connections,
            max_height=self.max_height or None,
            max_bytes=self.max_bytes,
            allow_private=self.allow_private,
            media_only=self.media_only,
            proxy=self.proxy or None,
            limiter=self.limiter,
            headers={"Referer": job.referer} if job.referer else {},
            pacer=self.pacer,
            cookies_browser=self.cookies_browser or None,
            cookies=self._session_cookies.get(job.id, []),
        )

    def _run(self, job: Job, cancel: threading.Event) -> None:
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
            result = grab(job.url, self.out_dir, options=self._options(job), report=report, cancel=cancel)
            path = self._file_into_category(result.path) if self.sort_into_categories else result.path
            job.path = path
            job.filename = path.name
            job.category = category_for(path.name)
            job.title = job.title or result.title
            if path.exists():
                job.downloaded = job.total = path.stat().st_size
            job.status = "done"
            job.finished_at = time.time()
            job.attempts, job.retry_at = 0, None
            self._session_cookies.pop(job.id, None)
        except Cancelled:
            interrupted = True  # status was already set by _stop
        except RateLimited as exc:
            self._back_off(job, str(exc))
        except GrabError as exc:
            job.status, job.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            job.status, job.error = "error", f"Unexpected error: {exc}"
        finally:
            job.speed, job.eta, job.phase = 0.0, None, ""
            with self._lock:
                self._running.discard(job.id)
            self._save()
        if interrupted:
            # yt-dlp leaves its .part file open inside a reference cycle when interrupted;
            # collect it (outside the except block, once the traceback is gone) so the file
            # can be resumed or deleted
            gc.collect()
        self._dispatch()

    def _back_off(self, job: Job, message: str) -> None:
        """The site blocked us: wait (longer each time) instead of failing, unless out of tries."""
        if not self.auto_retry_rate_limits or job.attempts >= len(RETRY_DELAYS):
            job.status, job.error = "error", message
            return
        delay = RETRY_DELAYS[job.attempts]
        job.attempts += 1
        job.retry_at = time.time() + delay
        job.status = "waiting"
        job.error = f"{message.removesuffix(' Try again later.')} Retrying automatically."
        with self._lock:
            # Hold every job for this site, so the queue doesn't keep knocking during the block
            site = site_key(job.url)
            self._site_cooldown[site] = max(self._site_cooldown.get(site, 0), job.retry_at)

    def _tick_forever(self) -> None:
        while True:
            time.sleep(TICK_SECONDS)
            now = time.time()
            with self._lock:
                due = [j for j in self._jobs.values()
                       if j.status == "waiting" and j.retry_at and j.retry_at <= now]
                for job in due:
                    job.status, job.retry_at = "queued", None
            if due:
                self._save()
            # Also starts queued jobs whose site cooldown just ended
            self._dispatch()

    def _file_into_category(self, path: Path) -> Path:
        folder = self.out_dir / category_for(path.name)
        if path.parent == folder or not path.exists():
            return path
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / path.name
        n = 1
        while dest.exists():
            dest = folder / f"{path.stem} ({n}){path.suffix}"
            n += 1
        shutil.move(str(path), dest)
        return dest

    def _save(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            rows = [job.to_dict() for job in self._jobs.values()]
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
            tmp.replace(self.state_file)

    def _load(self) -> None:
        if not self.state_file:
            return
        try:
            rows = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in rows:
            try:
                job = Job.from_dict(row)
            except TypeError:
                continue
            # Nothing runs across a restart; unfinished work waits for the user to resume
            if job.status in ACTIVE:
                job.status = "paused"
            if job.status == "waiting" and job.retry_at:
                site = site_key(job.url)
                self._site_cooldown[site] = max(self._site_cooldown.get(site, 0), job.retry_at)
            job.speed, job.eta, job.phase = 0.0, None, ""
            job.signed_in = False  # extension sessions are never written to disk
            self._jobs[job.id] = job
