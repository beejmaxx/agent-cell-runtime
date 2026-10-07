from threading import Lock


class SimulatedCrash(Exception):
    pass


class Failpoints:
    def __init__(self):
        self._armed = set()
        self._lock = Lock()

    def arm(self, name):
        if name not in {"after_claim", "after_create", "before_cleanup"}:
            raise ValueError(name)
        with self._lock:
            self._armed.add(name)

    def hit(self, name):
        with self._lock:
            if name in self._armed:
                self._armed.remove(name)
                raise SimulatedCrash(name)
