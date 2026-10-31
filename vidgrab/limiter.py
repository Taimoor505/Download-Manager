"""Shared bandwidth limit: every connection of every download draws from one token bucket."""

import threading
import time


class RateLimiter:
    def __init__(self, bytes_per_second: float = 0):
        self._rate = max(0.0, float(bytes_per_second))
        self._tokens = self._rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, bytes_per_second: float) -> None:
        with self._lock:
            self._rate = max(0.0, float(bytes_per_second))
            self._tokens = min(self._tokens, self._rate)

    def consume(self, n: int) -> None:
        """Blocks until `n` bytes fit under the limit. 0 means unlimited."""
        with self._lock:
            if self._rate <= 0:
                return
            now = time.monotonic()
            # At most one second of burst, so a long pause doesn't allow a spike
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            # Going negative books the bytes now; the caller sleeps off the debt below
            self._tokens -= n
            debt = -self._tokens / self._rate if self._tokens < 0 else 0.0
        if debt:
            time.sleep(debt)
