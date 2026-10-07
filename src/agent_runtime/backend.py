from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol


@dataclass(frozen=True)
class WorkloadSpec:
    execution_id: str
    credential: str
    deadline_at: datetime
    labels: dict[str, str]
    input: dict
    active_deadline_seconds: int


@dataclass(frozen=True)
class Workload:
    name: str
    uid: str
    phase: str
    labels: dict[str, str]


class BackendUnavailable(Exception):
    pass


class CreateRejected(Exception):
    pass


class CreateOutcomeUnknown(Exception):
    pass


class WorkloadBackend(Protocol):
    def create(self, name: str, spec: WorkloadSpec) -> str: ...
    def get(self, name: str) -> Workload | None: ...
    def delete(self, name: str, uid: str | None = None) -> None: ...
    def list_owned(self) -> list[Workload]: ...


class FakeBackend:
    def __init__(self):
        self.workloads = {}
        self.specs = {}
        self.create_calls = 0
        self.delete_calls = 0
        self._serial = 0
        self._next_create = None
        self._unavailable = False
        self._lock = RLock()

    def lose_next_create_response(self):
        self._next_create = "lost"

    def fail_next_create_without_creating(self):
        self._next_create = "absent"

    def reject_next_create(self):
        self._next_create = "reject"

    def set_unavailable(self, value):
        self._unavailable = value

    def _check(self):
        if self._unavailable:
            raise BackendUnavailable()

    def create(self, name, spec):
        with self._lock:
            self.create_calls += 1
            self._check()
            mode, self._next_create = self._next_create, None
            if mode == "reject":
                raise CreateRejected()
            self.specs[name] = spec
            if mode == "absent":
                raise CreateOutcomeUnknown()
            if name in self.workloads:
                raise CreateOutcomeUnknown()
            self.appear(name, spec.labels)
            if mode == "lost":
                raise CreateOutcomeUnknown()
            return self.workloads[name].uid

    def get(self, name):
        with self._lock:
            self._check()
            return self.workloads.get(name)

    def delete(self, name, uid=None):
        with self._lock:
            self._check()
            self.delete_calls += 1
            workload = self.workloads.get(name)
            if workload and (uid is None or workload.uid == uid):
                self.workloads.pop(name)

    def list_owned(self):
        with self._lock:
            self._check()
            return [w for w in self.workloads.values() if w.labels.get("owner") == "agent-runtime"]

    def exit(self, name, code):
        with self._lock:
            w = self.workloads[name]
            self.workloads[name] = Workload(
                name, w.uid, "SUCCEEDED" if code == 0 else "FAILED", w.labels
            )

    def remove(self, name):
        with self._lock:
            self.workloads.pop(name, None)

    def add_unowned(self, name):
        self.appear(name, {})

    def appear(self, name, labels):
        with self._lock:
            self._serial += 1
            self.workloads[name] = Workload(name, f"fake-{self._serial}", "RUNNING", dict(labels))
