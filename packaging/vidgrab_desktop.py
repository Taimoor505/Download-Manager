"""Entry point for the packaged desktop app (PyInstaller). Accepts --port and --data-dir."""

import os
import sys

# A windowed .exe has no console: give libraries that print or log somewhere harmless to write
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from vidgrab.cli import main  # noqa: E402

sys.exit(main(["desktop", *sys.argv[1:]]))
