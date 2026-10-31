"""
Command line:
  vidgrab URL...       download now, in this terminal
  vidgrab serve        web UI at http://127.0.0.1:8765
  vidgrab desktop      desktop app: native window, tray icon, clipboard catching
"""

import argparse
import sys
import threading
import time
from pathlib import Path

from .grab import GrabError, grab
from .limiter import RateLimiter
from .segmented import Cancelled

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _human(size) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _download(args) -> int:
    failures = 0
    limiter = RateLimiter(args.limit * 1024)
    for url in args.urls:
        state = {"phase": "", "title": "", "downloaded": 0, "total": None}
        started, last_draw = time.time(), 0.0

        def report(**fields):
            nonlocal last_draw
            state.update({k: v for k, v in fields.items() if v is not None})
            now = time.time()
            if now - last_draw < 0.25:
                return
            last_draw = now
            done, total = state["downloaded"], state["total"]
            speed = done / max(0.1, now - started)
            pct = f"{done / total * 100:5.1f}%" if total else "  ?  "
            label = (state["title"] or url)[:40]
            sys.stdout.write(
                f"\r{label:<40} {pct} {_human(done):>9} / {_human(total) if total else '?':<9} "
                f"{_human(speed)}/s {state['phase']:<12}"
            )
            sys.stdout.flush()

        cancel = threading.Event()
        try:
            result = grab(
                url, Path(args.dir), connections=args.connections, max_height=args.max_height,
                allow_private=True, proxy=args.proxy, limiter=limiter, report=report, cancel=cancel,
            )
            print(f"\nSaved {result.path}")
        except (GrabError, Cancelled) as exc:
            failures += 1
            print(f"\nFailed {url}: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            cancel.set()
            print("\nStopped. Run the same command again to resume.", file=sys.stderr)
            return 130
    return 1 if failures else 0


def _serve(args) -> int:
    import uvicorn

    from .server import create_app

    app = create_app(
        Path(args.data_dir) if args.data_dir else None,
        allow_private=args.allow_private,
        local_actions=args.host in LOOPBACK_HOSTS,
    )
    print(f"vidgrab running at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _desktop(args) -> int:
    from .desktop import run_desktop

    return run_desktop(port=args.port, data_dir=Path(args.data_dir) if args.data_dir else None)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        parser = argparse.ArgumentParser(prog="vidgrab serve", description="Start the web UI.")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--data-dir", help="Where settings and the queue are kept (default ~/.vidgrab)")
        parser.add_argument(
            "--allow-private", action="store_true",
            help="Allow links to private network addresses (only on a trusted machine)",
        )
        return _serve(parser.parse_args(argv[1:]))
    if argv and argv[0] == "desktop":
        parser = argparse.ArgumentParser(prog="vidgrab desktop", description="Start the desktop app.")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--data-dir", help="Where settings and the queue are kept (default ~/.vidgrab)")
        return _desktop(parser.parse_args(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="vidgrab", description="Download videos and files from links with parallel connections."
    )
    parser.add_argument("urls", nargs="+", help="Links to download")
    parser.add_argument("-o", "--dir", default=".", help="Download folder (default: current)")
    parser.add_argument("-c", "--connections", type=int, default=8, help="Connections per file")
    parser.add_argument("--max-height", type=int, help="Cap video resolution, e.g. 1080")
    parser.add_argument("--limit", type=int, default=0, help="Speed limit in KB/s (0 = unlimited)")
    parser.add_argument("--proxy", help="Proxy URL, e.g. socks5://127.0.0.1:1080")
    return _download(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
