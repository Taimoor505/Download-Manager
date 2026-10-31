# vidgrab

A download manager in the style of IDM. Paste any link: a file (`.zip`, `.exe`, `.pdf`, `.mp4`...), a video page (YouTube, X, Reddit, TikTok, Instagram, news sites...), or an HLS/DASH stream. vidgrab saves it fast, sorts it into folders, and can pick up where it left off.

It comes in four forms:
- **Desktop app:** a native window, a tray icon, and catching of copied links.
- **Browser extension:** for Chrome and Edge.
- **Web UI and CLI.**
- **Python library:** ClipperX embeds it this way.

## Features

| | |
|---|---|
| **Multi-connection downloads** | Each file is split into byte ranges fetched in parallel (up to 32 connections). When a connection finishes, it takes over half of the biggest range still downloading. |
| **Resume** | Progress is saved to a `.vgstate` file beside the download. A paused, crashed or killed download continues from where it stopped. |
| **Any link** | Direct files go to the segmented downloader. Site pages and streams go through [yt-dlp](https://github.com/yt-dlp/yt-dlp)'s extractors. Unknown pages are scanned for video URLs. |
| **Categories** | Finished files are sorted into Video, Audio, Compressed, Documents, Programs and Other folders. |
| **Speed limiter** | One limit shared by all downloads. Changes apply immediately. |
| **Scheduler** | Mark downloads "Download later" and they run only inside a time window, such as 02:00–07:00. Anything still running when the window closes is paused until the next one. |
| **Queue** | Choose how many downloads run at once. Pause, resume and retry each one, or pause and resume all. The queue survives restarts. |
| **Site grabber** | Paste a page link to list every file it links to. Pick the ones you want, by type. |
| **Proxy** | HTTP, HTTPS or SOCKS5. |
| **Browser extension** | Detects media the page loads, adds right-click "Download with vidgrab", and sends the page address along for sites that check it. |
| **Desktop app** | Native window, tray icon (closing the window keeps it downloading), and copied video or file links show up under "Caught links". |

**Not supported:**
- DRM-protected video (Netflix, Disney+ and so on).
- Content that needs a site login.

## Desktop app (Windows)

To build `dist\vidgrab\vidgrab.exe` (a folder app; zip the folder to share it):

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[desktop]" pyinstaller
.\packaging\build_windows.ps1
```

The build bundles ffmpeg from your PATH. YouTube links also need [Node.js](https://nodejs.org) or [deno](https://deno.com) installed.

To run it from source instead:

```bash
vidgrab desktop
```

Settings and the queue live in `~/.vidgrab`. Downloads go to `~/Downloads/vidgrab` by default; you can change that in Settings.

## Browser extension (Chrome / Edge)

1. Open `chrome://extensions` (or `edge://extensions`) and turn on **Developer mode**.
2. Click **Load unpacked** and pick the `extension/` folder.
3. Keep the desktop app (or `vidgrab serve`) running.

Then:
- Click the toolbar icon to see the videos found on the current page.
- Right-click a link or a video and choose "Download with vidgrab".

## Web UI and CLI

```bash
pip install -e ".[server]"
```

```bash
vidgrab serve
```

That serves the web UI at http://127.0.0.1:8765. To download from the command line instead:

```bash
vidgrab "https://example.com/file.zip" -o ~/Downloads -c 16 --limit 2048
```

`--limit` is in KB/s.

## As a library

```python
from vidgrab import DownloadManager, grab

grab("https://...", "downloads", connections=8, max_height=1080)

manager = DownloadManager("downloads", max_parallel=3, sort_into_categories=True)
job = manager.add("https://...")  # runs in the background; poll manager.get(job.id)
```

To mount the API in another FastAPI app:

```python
from vidgrab.server import build_router

app.include_router(build_router(manager), prefix="/api/downloads")
```

## Security

- **Private addresses:** links that resolve to localhost, 10.x, 192.168.x, 169.254.x and similar are refused, including after redirects. This stops a vidgrab running on a server from being used to reach its internal network. The CLI allows them; `vidgrab serve --allow-private` turns them on for the UI.
- **Requests from websites:** the local API rejects write requests from other websites, so a page you visit can't queue downloads on your machine. The browser extension is allowed.
- **Extension privacy:** the extension keeps the media it detects in the browser. It sends a link to your local vidgrab only when you click.
