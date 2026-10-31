"""File categories, used to sort finished downloads into folders the way IDM does."""

from pathlib import Path

CATEGORIES = {
    "Video": {
        "mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "ts", "3gp", "wmv", "mpg", "mpeg",
    },
    "Audio": {"m4a", "mp3", "aac", "ogg", "opus", "wav", "flac", "wma"},
    "Compressed": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "iso"},
    "Documents": {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "csv", "odt", "rtf",
    },
    "Programs": {"exe", "msi", "dmg", "pkg", "deb", "rpm", "apk", "appimage"},
}
CATEGORY_NAMES = [*CATEGORIES, "Other"]
MEDIA_EXTS = CATEGORIES["Video"] | CATEGORIES["Audio"]


def extension(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def category_for(name: str) -> str:
    ext = extension(name)
    for category, exts in CATEGORIES.items():
        if ext in exts:
            return category
    return "Other"
