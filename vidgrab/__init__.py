"""vidgrab: a multi-connection, resumable download manager for files and video links."""

from .grab import GrabError, GrabOptions, GrabResult, grab
from .limiter import RateLimiter
from .manager import DownloadManager, Job
from .segmented import Cancelled, DownloadError

__all__ = [
    "Cancelled", "DownloadError", "DownloadManager", "GrabError", "GrabOptions", "GrabResult",
    "Job", "RateLimiter", "grab",
]
__version__ = "0.3.0"
