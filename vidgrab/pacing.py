"""
Spaces out requests to sites that block bursts. YouTube flags an address that asks for
several videos within seconds, so page lookups there are at least SITE_GAPS apart.
"""

import threading
import time
from typing import Dict
from urllib.parse import urlsplit

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com")
SITE_GAPS = {"youtube": 8.0}  # seconds between page lookups


def site_key(url: str) -> str:
    """Groups hosts that share one rate limit: every YouTube domain counts as "youtube"."""
    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS):
        return "youtube"
    return host


class SitePacer:
    def __init__(self):
        self._next: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        key = site_key(url)
        gap = SITE_GAPS.get(key, 0.0)
        if not gap:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(key, 0.0))
            self._next[key] = start + gap  # reserve the slot before sleeping
        if start > now:
            time.sleep(start - now)
