"""
Multi-connection HTTP downloader (the IDM technique).

The file is preallocated and split into byte ranges, each fetched on its own connection and
written at its offset. When a connection finishes early it takes over half of the largest
range still in flight, so every connection stays busy until the end. Progress is checkpointed
to a sidecar file, so an interrupted download resumes where it stopped.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

CHUNK_SIZE = 256 * 1024
# A range is only split when both halves would be at least this big
MIN_SPLIT_BYTES = 1024 * 1024
RETRIES = 5
STATE_SUFFIX = ".vgstate"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)

ProgressFn = Callable[[int, Optional[int]], None]


class DownloadError(RuntimeError):
    pass


class Cancelled(DownloadError):
    pass


@dataclass(eq=False)
class Segment:
    start: int
    end: int  # inclusive
    written: int  # first byte not yet on disk
    pos: int = -1  # first byte not yet claimed by the worker (runtime only)

    def __post_init__(self):
        if self.pos < 0:
            self.pos = self.written


def make_client(connections: int, guard: Optional[Callable[[str], None]] = None) -> httpx.Client:
    hooks = {"request": [lambda request: guard(str(request.url))]} if guard else {}
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=60.0),
        limits=httpx.Limits(max_connections=connections + 4),
        headers={"User-Agent": USER_AGENT},
        event_hooks=hooks,
    )


def probe(client: httpx.Client, url: str, headers: Dict[str, str]) -> tuple:
    """Returns (size or None, supports_ranges, final_url) without downloading the body."""
    with client.stream("GET", url, headers={**headers, "Range": "bytes=0-0"}) as r:
        final_url = str(r.url)
        if r.status_code == 206:
            total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
            size = int(total) if total.isdigit() else None
            return size, size is not None, final_url
        if r.status_code == 200:
            length = r.headers.get("content-length", "")
            return (int(length) if length.isdigit() else None), False, final_url
        raise DownloadError(f"The server answered HTTP {r.status_code}.")


def download(
    url: str,
    dest: Path,
    *,
    connections: int = 8,
    headers: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
    on_progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
    max_bytes: Optional[int] = None,
) -> Path:
    dest = Path(dest)
    headers = dict(headers or {})
    on_progress = on_progress or (lambda done, total: None)
    cancel = cancel or threading.Event()
    own_client = client is None
    client = client or make_client(connections)
    try:
        size, ranged, final_url = probe(client, url, headers)
        if max_bytes and size and size > max_bytes:
            raise DownloadError(
                f"File is {size / 1e9:.1f} GB, over the {max_bytes / 1e9:.1f} GB limit."
            )
        if ranged and size:
            _download_ranges(client, final_url, dest, size, connections, headers, on_progress, cancel)
        else:
            _download_single(client, final_url, dest, size, headers, on_progress, cancel, max_bytes)
        return dest
    finally:
        if own_client:
            client.close()


def _download_single(client, url, dest, size, headers, on_progress, cancel, max_bytes):
    """For servers without range support: one connection, no resume."""
    part = dest.with_name(dest.name + ".part")
    done = 0
    with client.stream("GET", url, headers=headers) as r:
        if r.status_code >= 400:
            raise DownloadError(f"The server answered HTTP {r.status_code}.")
        with part.open("wb") as f:
            for chunk in r.iter_bytes(CHUNK_SIZE):
                if cancel.is_set():
                    raise Cancelled("Cancelled")
                f.write(chunk)
                done += len(chunk)
                if max_bytes and done > max_bytes:
                    raise DownloadError(f"File is over the {max_bytes / 1e9:.1f} GB limit.")
                on_progress(done, size)
    os.replace(part, dest)


def _load_state(state_path: Path, part: Path, size: int) -> Optional[List[Segment]]:
    try:
        state = json.loads(state_path.read_text())
        if state["size"] != size or part.stat().st_size != size:
            return None
        return [Segment(s, e, w) for s, e, w in state["segments"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _download_ranges(client, url, dest, size, connections, headers, on_progress, cancel):
    part = dest.with_name(dest.name + ".part")
    state_path = dest.with_name(dest.name + STATE_SUFFIX)
    segments = _load_state(state_path, part, size)
    if segments is None:
        count = max(1, min(connections, size // MIN_SPLIT_BYTES))
        step = size // count
        segments = [
            Segment(i * step, size - 1 if i == count - 1 else (i + 1) * step - 1, i * step)
            for i in range(count)
        ]
        with part.open("wb") as f:
            f.truncate(size)

    lock = threading.Lock()
    active = set()
    stop = threading.Event()  # set when any worker fails for good
    errors: List[BaseException] = []

    def save_state():
        with lock:
            rows = [[s.start, s.end, s.written] for s in segments]
        state_path.write_text(json.dumps({"size": size, "segments": rows}))

    def claim() -> Optional[Segment]:
        with lock:
            for seg in segments:
                if seg not in active and seg.written <= seg.end:
                    active.add(seg)
                    return seg
            # Nothing left unassigned: split the busiest range in half
            busiest = max(active, key=lambda s: s.end - s.pos, default=None)
            if busiest is None or busiest.end - busiest.pos + 1 < 2 * MIN_SPLIT_BYTES:
                return None
            mid = busiest.pos + (busiest.end - busiest.pos) // 2
            stolen = Segment(mid + 1, busiest.end, mid + 1)
            busiest.end = mid
            segments.append(stolen)
            active.add(stolen)
            return stolen

    def fetch(seg: Segment):
        failures = 0
        with part.open("r+b") as f:
            while True:
                with lock:
                    if seg.written > seg.end:
                        return
                    seg.pos = seg.written
                    offset_before = seg.pos
                    byte_range = f"bytes={seg.written}-{seg.end}"
                try:
                    with client.stream("GET", url, headers={**headers, "Range": byte_range}) as r:
                        if r.status_code != 206:
                            raise DownloadError(
                                f"The server stopped honouring byte ranges (HTTP {r.status_code})."
                            )
                        for chunk in r.iter_bytes(CHUNK_SIZE):
                            if cancel.is_set() or stop.is_set():
                                raise Cancelled("Cancelled")
                            # Claim under the lock: another worker may have shortened this range
                            with lock:
                                take = min(len(chunk), seg.end - seg.pos + 1)
                                offset = seg.pos
                                seg.pos += take
                            if take > 0:
                                f.seek(offset)
                                f.write(chunk[:take])
                                with lock:
                                    seg.written += take
                            if take < len(chunk):
                                break
                    if seg.pos > offset_before:
                        failures = 0
                        continue
                    error = "the server closed the connection without sending data"
                except httpx.HTTPError as exc:
                    error = str(exc)
                failures += 1
                if failures > RETRIES:
                    raise DownloadError(f"Connection kept failing: {error}")
                time.sleep(min(30, 2 ** failures))

    def worker():
        try:
            while not (cancel.is_set() or stop.is_set()):
                seg = claim()
                if seg is None:
                    return
                try:
                    fetch(seg)
                finally:
                    with lock:
                        active.discard(seg)
        except Cancelled:
            pass
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            errors.append(exc)
            stop.set()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, connections))]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
        save_state()
        with lock:
            done = sum(s.written - s.start for s in segments)
        on_progress(done, size)

    save_state()
    if errors:
        raise errors[0] if isinstance(errors[0], DownloadError) else DownloadError(str(errors[0]))
    if cancel.is_set():
        raise Cancelled("Cancelled")
    if any(s.written <= s.end for s in segments):
        raise DownloadError("Download ended with missing pieces; try again to resume.")
    os.replace(part, dest)
    state_path.unlink(missing_ok=True)
    on_progress(size, size)
