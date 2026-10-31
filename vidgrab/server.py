"""HTTP API for a DownloadManager, plus the standalone web UI."""

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .manager import DownloadManager, Job

STATIC_DIR = Path(__file__).parent / "static"


class NewDownload(BaseModel):
    url: str
    connections: Optional[int] = None


def build_router(manager: DownloadManager) -> APIRouter:
    router = APIRouter()

    def find(job_id: str) -> Job:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown download.")
        return job

    @router.post("")
    def add_download(payload: NewDownload):
        if not payload.url.strip():
            raise HTTPException(status_code=400, detail="Paste a link first.")
        return manager.add(payload.url, payload.connections).to_dict()

    @router.get("")
    def list_downloads():
        return [job.to_dict() for job in manager.list()]

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


def create_app(
    out_dir: Optional[Path] = None, connections: int = 8, allow_private: bool = False
) -> FastAPI:
    out_dir = Path(out_dir or os.environ.get("VIDGRAB_DIR") or Path.home() / "Downloads" / "vidgrab")
    manager = DownloadManager(out_dir, connections=connections, allow_private=allow_private)
    app = FastAPI(title="vidgrab", description="Multi-connection video download manager.")
    app.include_router(build_router(manager), prefix="/api/downloads")

    @app.get("/api/info")
    def info():
        return {"download_dir": str(manager.out_dir), "connections": manager.connections}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
