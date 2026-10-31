# vidgrab

A download manager for video links. Paste a link from YouTube, X, Reddit, TikTok, Instagram, a news site, a direct `.mp4`, or an HLS/DASH stream, and it saves the video.

## How it works

1. **Direct media links**: a link that serves a video file goes to the segmented downloader. This is the technique IDM uses:
   - The file is split into byte ranges, and each range is fetched on its own connection.
   - When a connection finishes, it takes over half of the biggest range still downloading.
   - Progress is saved to a `.vgstate` file beside the download. A stopped or crashed download resumes where it left off.
2. **Site pages and streams**: other links go through [yt-dlp](https://github.com/yt-dlp/yt-dlp)'s extractors, which cover thousands of sites.
   - Plain video and audio files found that way still use the segmented downloader, then get merged with FFmpeg.
   - HLS and DASH streams use yt-dlp's parallel fragment downloader.
3. **Unknown sites**: if no extractor recognizes the page, vidgrab scans it for `<video>` tags and `.mp4`, `.m3u8` and `.mpd` URLs.

**Not supported:**
- DRM-protected video (Netflix, Disney+, and so on).
- Videos that need an account to watch.

## Install

You need Python 3.10 or newer and FFmpeg on your PATH. For YouTube, you also need [deno](https://deno.com) or Node.js.

```bash
pip install -e ".[server]"
```

## Use

```bash
vidgrab "https://example.com/some-video" -o ~/Videos -c 8
```

```bash
vidgrab serve --port 8765
```

`vidgrab serve` opens the web UI at http://127.0.0.1:8765. It has a download queue with progress, speed, time left, pause, resume and retry.

In Python:

```python
from vidgrab import grab, DownloadManager

result = grab("https://...", "downloads", connections=8, max_height=1080)

manager = DownloadManager("downloads", max_parallel=3)
job = manager.add("https://...")  # runs in the background; poll manager.get(job.id)
```

To mount the HTTP API in another FastAPI app:

```python
from vidgrab.server import build_router

app.include_router(build_router(manager), prefix="/api/downloads")
```

## Running it on a server

By default, links that resolve to private or internal addresses (localhost, 10.x, 169.254.x and the like) are refused. This stops anyone using the server to reach its internal network. The check also runs on every redirect followed by the segmented downloader.

The CLI allows private addresses, since it only runs on your own machine. `vidgrab serve --allow-private` turns them on for the web UI.

```bash
docker build -t vidgrab .
```

```bash
docker run -p 8765:8765 -v "$PWD/downloads:/downloads" vidgrab
```
