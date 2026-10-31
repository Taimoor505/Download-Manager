"""
Desktop app: runs the vidgrab server in the background, shows the UI in a native window,
and sits in the system tray. Closing the window hides it; Quit in the tray menu exits.
While it runs it watches the clipboard and catches copied video and file links.
"""

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .categories import CATEGORIES

# Pages on these sites are video pages even without a file extension in the link
VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv", "tiktok.com",
    "instagram.com", "facebook.com", "fb.watch", "x.com", "twitter.com", "reddit.com",
    "v.redd.it", "streamable.com", "bilibili.com", "soundcloud.com", "rumble.com",
)
FILE_EXTS = set().union(*CATEGORIES.values()) | {"m3u8", "mpd"}


def looks_downloadable(text: str) -> bool:
    """True for a single copied link to a known video site or a file type vidgrab sorts."""
    text = text.strip()
    if not text.lower().startswith(("http://", "https://")) or len(text) > 2000 or any(c.isspace() for c in text):
        return False
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS):
        return True
    return parts.path.rsplit(".", 1)[-1].lower() in FILE_EXTS if "." in parts.path else False


def _use_bundled_ffmpeg() -> None:
    # The packaged .exe ships ffmpeg.exe next to itself
    if getattr(sys, "frozen", False):
        for folder in (Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).parent):
            candidate = folder / "ffmpeg.exe"
            if candidate.exists():
                os.environ.setdefault("VIDGRAB_FFMPEG", str(candidate))
                return


def _already_running(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url}/api/info", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


class ClipboardWatcher(threading.Thread):
    def __init__(self, app, on_caught):
        super().__init__(name="vidgrab-clipboard", daemon=True)
        self.app = app
        self.on_caught = on_caught
        self.stopped = threading.Event()

    def run(self) -> None:
        import pyperclip

        try:
            last = pyperclip.paste()  # don't catch whatever was copied before the app started
        except pyperclip.PyperclipException:
            return
        while not self.stopped.wait(1.0):
            try:
                text = pyperclip.paste()
            except pyperclip.PyperclipException:
                continue
            if text == last:
                continue
            last = text
            if not self.app.state.settings["clipboard_monitor"] or not looks_downloadable(text):
                continue
            manager = self.app.state.manager
            url = text.strip()
            if any(job.url == url for job in manager.list()):
                continue
            manager.add(url, status="pending")
            self.on_caught(url)


def run_desktop(port: int = 8765, data_dir: Optional[Path] = None) -> int:
    import pystray
    import uvicorn
    import webview

    from .icon import draw
    from .server import create_app

    base_url = f"http://127.0.0.1:{port}"
    if _already_running(base_url):
        webbrowser.open(base_url)  # second launch: show the running app instead of failing
        return 0

    _use_bundled_ffmpeg()
    app = create_app(data_dir, local_actions=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name="vidgrab-server", daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        print(f"vidgrab could not start on port {port}; is something else using it?", file=sys.stderr)
        return 1

    state = {"quitting": False, "told_about_tray": False}
    window = webview.create_window("vidgrab", base_url, width=1120, height=760, min_size=(420, 480))

    def show_window(*_):
        window.show()
        window.restore()

    def quit_app(*_):
        state["quitting"] = True
        watcher.stopped.set()
        tray.stop()
        server.should_exit = True
        window.destroy()

    def on_closing():
        if state["quitting"]:
            return True
        window.hide()
        if not state["told_about_tray"]:
            state["told_about_tray"] = True
            tray.notify("vidgrab keeps downloading in the tray. Right-click the icon to quit.", "vidgrab")
        return False  # keep running in the tray instead of exiting

    window.events.closing += on_closing

    def toggle_clipboard(*_):
        settings = app.state.settings
        settings.update({"clipboard_monitor": not settings["clipboard_monitor"]})

    tray = pystray.Icon(
        "vidgrab",
        draw(64),
        "vidgrab",
        menu=pystray.Menu(
            pystray.MenuItem("Open vidgrab", show_window, default=True),
            pystray.MenuItem("Pause all", lambda *_: app.state.manager.pause_all()),
            pystray.MenuItem("Resume all", lambda *_: app.state.manager.resume_all()),
            pystray.MenuItem(
                "Catch copied links",
                toggle_clipboard,
                checked=lambda _item: app.state.settings["clipboard_monitor"],
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    tray.run_detached()

    watcher = ClipboardWatcher(
        app, lambda url: tray.notify(f"Open vidgrab and press Start now to download it.\n{url[:120]}", "Link caught")
    )
    watcher.start()

    webview.start()  # blocks until the window is destroyed (Quit)
    if not state["quitting"]:
        quit_app()
    return 0
