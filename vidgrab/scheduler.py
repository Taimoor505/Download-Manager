"""Starts "Download later" jobs inside the scheduled window and pauses them when it closes."""

import threading
from datetime import datetime
from typing import Callable, Dict, Optional

TICK_SECONDS = 20


def in_window(now: str, start: str, stop: str) -> bool:
    """All times are "HH:MM". A window like 23:00-06:00 runs overnight."""
    if start == stop:
        return True
    if start < stop:
        return start <= now < stop
    return now >= start or now < stop


class Scheduler:
    def __init__(self, manager, get_schedule: Callable[[], Dict], clock: Optional[Callable[[], str]] = None):
        self.manager = manager
        self.get_schedule = get_schedule
        self.clock = clock or (lambda: datetime.now().strftime("%H:%M"))
        self._was_open = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="vidgrab-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        schedule = self.get_schedule()
        is_open = schedule["enabled"] and in_window(self.clock(), schedule["start"], schedule["stop"])
        if is_open:
            self.manager.start_scheduled()  # also picks up jobs added while the window is open
        elif self._was_open:
            self.manager.stop_for_schedule()
        self._was_open = is_open

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(TICK_SECONDS)
