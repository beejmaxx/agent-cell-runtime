from datetime import UTC, datetime, timedelta
from threading import Lock


class Clock:
    def __init__(self, now: datetime | None = None):
        self._now = now
        self._lock = Lock()

    def now(self):
        with self._lock:
            return self._now if self._now is not None else datetime.now(UTC)

    def set(self, now: datetime):
        if now.tzinfo is None:
            raise ValueError("Clock requires timezone-aware time")
        with self._lock:
            self._now = now

    def advance(self, seconds: float):
        with self._lock:
            if self._now is None:
                raise ValueError("Set the test clock before advancing it")
            self._now += timedelta(seconds=seconds)
