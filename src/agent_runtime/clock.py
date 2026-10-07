from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import func, literal


class Clock:
    def __init__(self, now: datetime | None = None):
        self._now = now
        self._lock = Lock()

    def now(self):
        with self._lock:
            return self._now if self._now is not None else datetime.now(UTC)

    def sql_now(self):
        # PostgreSQL transaction time is frozen; expiry needs the current wall clock.
        with self._lock:
            return func.clock_timestamp() if self._now is None else literal(self._now)

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
