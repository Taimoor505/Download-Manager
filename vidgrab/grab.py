"""
Turns any link into a media file on disk.

1. Direct media links (an .mp4 URL, a file server) go straight to the segmented downloader.
2. Everything else goes through yt-dlp's extractors, which know thousands of sites and also
   handle HLS (.m3u8) and DASH (.mpd) streams. Plain progressive formats it finds are still
   fetched by the segmented downloader; streams use yt-dlp's parallel fragment downloader.
3. If no extractor recognises a page, the page is scanned for video/stream URLs.

DRM-protected media is not supported and never will be.
"""

import html as html_lib
import mimetypes
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from . import segmented
from .netguard import BlockedURL, check_public_url
from .segmented import Cancelled, DownloadError

MEDIA_EXTS = {
    "mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "ts", "3gp",
    "m4a", "mp3", "aac", "ogg", "opus", "wav", "flac",
}
GENERIC_BINARY_TYPES = ("application/octet-stream", "binary/octet-stream", "application/mp4")
MAX_PAGE_BYTES = 3 * 1024 * 1024
MEDIA_URL_PATTERN = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\]+?\.(?:mp4|m4v|webm|mov|m3u8|mpd)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
MEDIA_TAG_PATTERN = re.compile(
    r"""<(?:video|source)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

Report = Callable[..., None]


class GrabError(DownloadError):
    pass


@dataclass
class GrabResult:
    path: Path
    title: str


def grab(
    url: str,
    out_dir: Path,
    *,
    connections: int = 8,
    max_height: Optional[int] = None,
    max_bytes: Optional[int] = None,
    allow_private: bool = False,
    report: Optional[Report] = None,
    cancel: Optional[threading.Event] = None,
    _depth: int = 0,
) -> GrabResult:
    """
    Downloads the media behind `url` into `out_dir`. `report(**fields)` receives phase
    ("resolving", "downloading", "merging"), title, downloaded and total as they change.
    """
    url = url.strip()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = report or (lambda **fields: None)
    cancel = cancel or threading.Event()
    guard = None if allow_private else check_public_url
    if not url.lower().startswith(("http://", "https://")):
        raise GrabError("Paste a full link starting with http:// or https://.")
    try:
        if guard:
            guard(url)
        report(phase="resolving")
        with segmented.make_client(connections, guard) as client:
            kind, info = _sniff(client, url)
            if kind == "file":
                dest = _unique_path(out_dir / info["filename"])
                report(phase="downloading", title=dest.stem)
                segmented.download(
                    info["url"], dest, connections=connections, client=client,
                    on_progress=lambda done, total: report(downloaded=done, total=total),
                    cancel=cancel, max_bytes=max_bytes,
                )
                return GrabResult(dest, dest.stem)

            try:
                return _grab_with_ytdlp(
                    url, out_dir, client, connections, max_height, max_bytes, report, cancel
                )
            except GrabError as exc:
                if kind != "page" or _depth > 0 or "Unsupported URL" not in str(exc):
                    raise
            # No extractor knows this site: look for media URLs in the page itself
            for media_url in _find_media_urls(info["html"], info["url"]):
                try:
                    return grab(
                        media_url, out_dir, connections=connections, max_height=max_height,
                        max_bytes=max_bytes, allow_private=allow_private, report=report,
                        cancel=cancel, _depth=_depth + 1,
                    )
                except (GrabError, BlockedURL):
                    continue
            raise GrabError("No downloadable video was found on this page.")
    except BlockedURL as exc:
        raise GrabError(str(exc))
    except Cancelled:
        raise
    except DownloadError as exc:
        raise exc if isinstance(exc, GrabError) else GrabError(str(exc))
    except httpx.HTTPError as exc:
        raise GrabError(f"Network error: {exc}")


def _sniff(client: httpx.Client, url: str) -> tuple:
    """Classifies a link as ("file", {url, filename}), ("page", {url, html}) or ("other", {})."""
    try:
        with client.stream("GET", url) as r:
            if r.status_code >= 400:
                return "other", {}
            final_url = str(r.url)
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            name = _filename(r.headers.get("content-disposition", ""), final_url)
            ext = Path(name).suffix.lower().lstrip(".")
            is_media = ctype.startswith(("video/", "audio/")) and "mpegurl" not in ctype
            if is_media or (ctype in GENERIC_BINARY_TYPES and ext in MEDIA_EXTS):
                if ext not in MEDIA_EXTS:
                    guessed = (mimetypes.guess_extension(ctype) or ".mp4").lstrip(".")
                    name = f"{name}.{guessed}"
                return "file", {"url": final_url, "filename": name}
            if ctype in ("text/html", "application/xhtml+xml"):
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                return "page", {"url": final_url, "html": body.decode(r.encoding or "utf-8", "replace")}
    except httpx.HTTPError:
        pass
    return "other", {}


def _grab_with_ytdlp(url, out_dir, client, connections, max_height, max_bytes, report, cancel):
    try:
        import yt_dlp
    except ImportError:
        raise GrabError("yt-dlp is not installed, so only direct media links can be downloaded.")

    def progress_hook(d):
        if cancel.is_set():
            raise Cancelled("Cancelled")
        if d.get("status") == "downloading":
            report(
                downloaded=d.get("downloaded_bytes") or 0,
                total=d.get("total_bytes") or d.get("total_bytes_estimate"),
            )

    def postprocessor_hook(d):
        if d.get("status") == "started" and d.get("postprocessor") == "Merger":
            report(phase="merging")

    if max_height:
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b"
    else:
        fmt = "bv*+ba/b"
    opts = {
        "format": fmt,
        "format_sort": ["res", "ext:mp4:m4a"],
        "merge_output_format": "mp4/mkv",
        "outtmpl": str(out_dir / "%(title).90B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "playlist_items": "1",
        "concurrent_fragment_downloads": connections,
        # YouTube needs a JS runtime to unlock full-speed formats; use whichever is installed
        "js_runtimes": {"deno": {}, "node": {}},
        "logger": _SilentLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }
    if max_bytes:
        opts["max_filesize"] = max_bytes

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise GrabError("This link is a playlist with no playable videos.")
                info = entries[0]
            title = info.get("title") or "video"
            report(phase="downloading", title=title)

            formats = info.get("requested_formats") or [info]
            # YouTube throttles plain range requests; its own downloader knows the workarounds
            if info.get("extractor_key") != "Youtube" and all(_is_progressive(f) for f in formats):
                path = _download_progressive(
                    ydl, info, formats, client, connections, max_bytes, report, cancel
                )
            else:
                ydl.process_info(info)
                downloads = info.get("requested_downloads") or []
                path = Path(downloads[-1].get("filepath") or ydl.prepare_filename(info)) if downloads \
                    else Path(ydl.prepare_filename(info))
            return GrabResult(path, title)
    except yt_dlp.utils.DownloadError as exc:
        if cancel.is_set():
            raise Cancelled("Cancelled")
        raise GrabError(_friendly_ytdlp_error(str(exc)))


class _SilentLogger:
    """yt-dlp prints errors to stderr even when quiet; they are raised to the caller instead."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _is_progressive(fmt: Dict) -> bool:
    return fmt.get("protocol") in ("http", "https") and bool(fmt.get("url")) and not fmt.get("fragments")


def _download_progressive(ydl, info, formats, client, connections, max_bytes, report, cancel) -> Path:
    """Fetches each format with the segmented downloader, then muxes video + audio."""
    final = Path(ydl.prepare_filename(info))
    if len(formats) == 2:
        exts = {f.get("ext") for f in formats}
        container = "mp4" if exts <= {"mp4", "m4a"} else "webm" if exts == {"webm"} else "mkv"
        final = final.with_suffix(f".{container}")
    if final.exists():
        return final

    done: Dict[int, int] = {}
    totals: Dict[int, Optional[int]] = {
        i: f.get("filesize") or f.get("filesize_approx") for i, f in enumerate(formats)
    }

    def progress_for(index):
        def on_progress(downloaded, total):
            done[index] = downloaded
            if total:
                totals[index] = total
            known = list(totals.values())
            report(downloaded=sum(done.values()), total=sum(known) if all(known) else None)
        return on_progress

    parts: List[Path] = []
    for i, fmt in enumerate(formats):
        part = final if len(formats) == 1 else final.with_suffix(f".f{fmt.get('format_id')}.{fmt.get('ext')}")
        segmented.download(
            fmt["url"], part, connections=connections, headers=fmt.get("http_headers") or {},
            client=client, on_progress=progress_for(i), cancel=cancel, max_bytes=max_bytes,
        )
        parts.append(part)

    if len(parts) == 2:
        report(phase="merging")
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(parts[0]), "-i", str(parts[1]),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(final),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GrabError(f"Could not merge video and audio: {proc.stderr.strip()[-300:]}")
        for part in parts:
            part.unlink(missing_ok=True)
    return final


def _friendly_ytdlp_error(message: str) -> str:
    message = ANSI_PATTERN.sub("", message).replace("ERROR: ", "").strip()
    lowered = message.lower()
    if "drm" in lowered:
        return "This video is DRM-protected, so it can't be downloaded."
    if "confirm you" in lowered and "bot" in lowered:
        return (
            "The site asked this server to sign in to prove it isn't a bot. "
            "Try again later, or download the file yourself and upload it."
        )
    if any(s in lowered for s in ("private video", "login", "logged-in", "logged in", "sign in", "--cookies")):
        return "This video needs an account to watch, so it can't be downloaded from here."
    # Drop yt-dlp's "[extractor] id:" prefix
    return re.sub(r"^\[[^\]]+\]\s*[^:]*:\s*", "", message, count=1) or "The download failed."


def _find_media_urls(page: str, base_url: str, limit: int = 5) -> List[str]:
    page = html_lib.unescape(page.replace("\\/", "/"))
    found = [urljoin(base_url, src) for src in MEDIA_TAG_PATTERN.findall(page)]
    found += [urljoin(base_url, m) for m in MEDIA_URL_PATTERN.findall(page)]
    unique = []
    for candidate in found:
        if candidate.startswith(("http://", "https://")) and candidate not in unique:
            unique.append(candidate)
    return unique[:limit]


def _filename(content_disposition: str, url: str) -> str:
    match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", content_disposition, re.IGNORECASE)
    if match:
        name = unquote(match.group(1))
    else:
        match = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.IGNORECASE)
        name = match.group(1) if match else unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "download"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path  # also the case when resuming: only .part/.vgstate exist so far
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise GrabError("Too many files with the same name in the download folder.")
