"""vidgrab: a multi-connection, resumable downloader for video links from almost any site."""

from .grab import GrabError, GrabResult, grab
from .manager import DownloadManager, Job
from .segmented import Cancelled, DownloadError

__all__ = [
    "Cancelled", "DownloadError", "DownloadManager", "GrabError", "GrabResult", "Job", "grab",
]
__version__ = "0.1.0"
