"""User settings for the standalone app, stored as JSON in the data folder."""

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://")
# Browsers yt-dlp can read logins from; "" turns the feature off
LOGIN_BROWSERS = ("", "firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")


def default_data_dir() -> Path:
    return Path.home() / ".vidgrab"


def defaults() -> Dict[str, Any]:
    return {
        "download_dir": str(Path.home() / "Downloads" / "vidgrab"),
        "connections": 8,
        "max_parallel": 3,
        "speed_limit_kbps": 0,  # 0 = unlimited
        "sort_into_categories": True,
        "proxy": "",
        "max_height": 0,  # 0 = best available
        "schedule": {"enabled": False, "start": "02:00", "stop": "07:00"},
        "clipboard_monitor": True,  # desktop app only
        "auto_retry_blocks": True,  # wait and retry when a site says "confirm you're not a bot"
        # Opt-in: sign in to sites like YouTube with this browser's login. "" = never
        "browser_login": "",
    }


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._values = defaults()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self._values = self._validated({**self._values, **stored})
        except (OSError, ValueError, SettingsError):
            pass  # missing or broken file: start from defaults

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._values))

    def update(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            unknown = set(changes) - set(self._values)
            if unknown:
                raise SettingsError(f"Unknown setting: {', '.join(sorted(unknown))}")
            merged = {**self._values, **changes}
            if "schedule" in changes:
                merged["schedule"] = {**self._values["schedule"], **(changes["schedule"] or {})}
            self._values = self._validated(merged)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return json.loads(json.dumps(self._values))

    @staticmethod
    def _validated(v: Dict[str, Any]) -> Dict[str, Any]:
        def whole(name, low, high):
            try:
                value = int(v[name])
            except (TypeError, ValueError):
                raise SettingsError(f"{name} must be a whole number.")
            if not low <= value <= high:
                raise SettingsError(f"{name} must be between {low} and {high}.")
            return value

        out = dict(v)
        out["connections"] = whole("connections", 1, 32)
        out["max_parallel"] = whole("max_parallel", 1, 10)
        out["speed_limit_kbps"] = whole("speed_limit_kbps", 0, 10_000_000)
        out["max_height"] = whole("max_height", 0, 4320)
        out["download_dir"] = str(v["download_dir"]).strip()
        if not out["download_dir"]:
            raise SettingsError("Choose a download folder.")
        proxy = str(v.get("proxy") or "").strip()
        if proxy and not proxy.startswith(PROXY_SCHEMES):
            raise SettingsError("Proxy must start with http://, https:// or socks5://.")
        out["proxy"] = proxy
        out["sort_into_categories"] = bool(v["sort_into_categories"])
        out["clipboard_monitor"] = bool(v["clipboard_monitor"])
        out["auto_retry_blocks"] = bool(v["auto_retry_blocks"])
        browser = str(v.get("browser_login") or "").strip().lower()
        if browser not in LOGIN_BROWSERS:
            raise SettingsError(f"Browser login must be one of: {', '.join(b or 'off' for b in LOGIN_BROWSERS)}.")
        out["browser_login"] = browser
        schedule = dict(v["schedule"])
        for key in ("start", "stop"):
            if not TIME_PATTERN.match(str(schedule.get(key, ""))):
                raise SettingsError("Schedule times must look like 02:00 (24-hour clock).")
        out["schedule"] = {
            "enabled": bool(schedule.get("enabled")),
            "start": schedule["start"],
            "stop": schedule["stop"],
        }
        return out
