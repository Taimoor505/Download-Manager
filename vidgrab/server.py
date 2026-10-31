"""HTTP API for a DownloadManager, plus the standalone app (web UI, settings, scheduler)."""

import platform
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .categories import CATEGORY_NAMES
from .grab import GrabError
from .grabber import find_files
from .manager import DownloadManager, Job
from .scheduler import Scheduler
from .settings import Settings, SettingsError, default_data_dir

STATIC_DIR = Path(__file__).parent / "static"
EXTENSION_ORIGIN = r"^(chrome|moz)-extension://[a-zA-Z0-9-]+$"


class NewDownload(BaseModel):
    url: str
    connections: Optional[int] = None
    start: Literal["now", "later"] = "now"
    referer: str = ""


class NewDownloads(BaseModel):
    urls: List[str]
    start: Literal["now", "later"] = "now"
    referer: str = ""


class GrabRequest(BaseModel):
    url: str
    categories: List[str] = []


def build_router(manager: DownloadManager) -> APIRouter:
    router = APIRouter()

    def find(job_id: str) -> Job:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown download.")
        return job

    def status_for(start: str) -> str:
        return "scheduled" if start == "later" else "queued"

    @router.post("")
    def add_download(payload: NewDownload):
        if not payload.url.strip():
            raise HTTPException(status_code=400, detail="Paste a link first.")
        job = manager.add(payload.url, payload.connections, status_for(payload.start), payload.referer)
        return job.to_dict()

    @router.post("/batch")
    def add_downloads(payload: NewDownloads):
        urls = [u.strip() for u in payload.urls if u.strip()]
        if not urls:
            raise HTTPException(status_code=400, detail="No links to add.")
        return [manager.add(u, None, status_for(payload.start), payload.referer).to_dict() for u in urls]

    @router.get("")
    def list_downloads():
        return [job.to_dict() for job in manager.list()]

    @router.post("/pause-all")
    def pause_all():
        manager.pause_all()
        return {"ok": True}

    @router.post("/resume-all")
    def resume_all():
        manager.resume_all()
        return {"ok": True}

    @router.post("/clear-finished")
    def clear_finished():
        manager.clear_finished()
        return {"ok": True}

    @router.get("/{job_id}")
    def get_download(job_id: str):
        return find(job_id).to_dict()

    @router.post("/{job_id}/pause")
    def pause_download(job_id: str):
        find(job_id)
        return manager.pause(job_id).to_dict()

    @router.post("/{job_id}/resume")
    def resume_download(job_id: str):
        find(job_id)
        return manager.resume(job_id).to_dict()

    @router.post("/{job_id}/cancel")
    def cancel_download(job_id: str):
        find(job_id)
        return manager.cancel(job_id).to_dict()

    @router.delete("/{job_id}")
    def remove_download(job_id: str, delete_file: bool = False):
        find(job_id)
        manager.remove(job_id, delete_file=delete_file)
        return {"removed": job_id}

    @router.get("/{job_id}/file")
    def download_file(job_id: str):
        job = find(job_id)
        if job.status != "done" or not job.path or not job.path.exists():
            raise HTTPException(status_code=409, detail="This download isn't finished.")
        return FileResponse(job.path, filename=job.path.name)

    return router


def apply_settings(manager: DownloadManager, values: dict) -> None:
    manager.configure(
        out_dir=values["download_dir"],
        connections=values["connections"],
        max_parallel=values["max_parallel"],
        speed_limit=values["speed_limit_kbps"] * 1024,
        sort_into_categories=values["sort_into_categories"],
        proxy=values["proxy"],
        max_height=values["max_height"],
    )


def reveal_in_folder(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif system == "Darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def create_app(
    data_dir: Optional[Path] = None,
    *,
    allow_private: bool = False,
    local_actions: bool = True,
) -> FastAPI:
    """
    The standalone app. `local_actions` enables "show in folder", which opens a file
    manager on the machine running the server, so it is only on when that's the user's own.
    """
    data_dir = Path(data_dir or default_data_dir())
    settings = Settings(data_dir / "settings.json")
    manager = DownloadManager(
        settings["download_dir"], allow_private=allow_private, state_file=data_dir / "jobs.json"
    )
    apply_settings(manager, settings.as_dict())
    scheduler = Scheduler(manager, lambda: settings["schedule"])

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="vidgrab", description="Multi-connection download manager.", lifespan=lifespan)
    app.state.manager, app.state.settings, app.state.scheduler = manager, settings, scheduler

    # The browser extension calls the API from its own origin
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=EXTENSION_ORIGIN,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def block_cross_site_writes(request: Request, call_next):
        # Without this any website you visit could POST to localhost and queue downloads.
        # Browsers always send Origin on cross-site POSTs; local tools (curl, the app) don't.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            same_site = urlsplit(origin).netloc == request.headers.get("host")
            from_extension = origin.startswith(("chrome-extension://", "moz-extension://"))
            if not (same_site or from_extension):
                return JSONResponse({"detail": "Requests from other websites are blocked."}, 403)
        return await call_next(request)

    app.include_router(build_router(manager), prefix="/api/downloads")

    @app.get("/api/info")
    def info():
        return {
            "download_dir": str(manager.out_dir),
            "categories": CATEGORY_NAMES,
            "local_actions": local_actions,
        }

    @app.get("/api/settings")
    def get_settings():
        return settings.as_dict()

    @app.put("/api/settings")
    def put_settings(changes: dict):
        try:
            values = settings.update(changes)
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        apply_settings(manager, values)
        scheduler.tick()
        return values

    @app.post("/api/grabber")
    def grab_links(payload: GrabRequest):
        try:
            return find_files(
                payload.url, payload.categories, allow_private=allow_private, proxy=settings["proxy"]
            )
        except GrabError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/downloads/{job_id}/reveal")
    def reveal(job_id: str):
        job = manager.get(job_id)
        if not local_actions:
            raise HTTPException(status_code=403, detail="Not available on a remote server.")
        if job is None or not job.path or not job.path.exists():
            raise HTTPException(status_code=404, detail="The file isn't on disk anymore.")
        reveal_in_folder(job.path)
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
