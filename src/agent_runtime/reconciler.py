import hashlib
import math
import secrets

from sqlalchemy import select

from agent_runtime.backend import (
    BackendUnavailable,
    CreateOutcomeUnknown,
    CreateRejected,
    WorkloadSpec,
)
from agent_runtime.executions import TERMINAL, transition


def matches(workload, row):
    return (
        workload is not None
        and workload.labels.get("owner") == "agent-runtime"
        and workload.labels.get("execution_id") == str(row["id"])
    )


class Reconciler:
    def __init__(self, db, backend, failpoints, max_active_executions=3):
        self.db, self.backend, self.failpoints = db, backend, failpoints
        self.max_active_executions = max_active_executions

    def change(self, row, status, now, **values):
        with self.db.engine.begin() as conn:
            return transition(self.db, conn, row, status, now, **values)

    def cleanup(self, row):
        try:
            workload = self.backend.get(row["workload_name"])
            if matches(workload, row):
                self.backend.delete(row["workload_name"], workload.uid)
        except BackendUnavailable:
            pass

    def reconcile_once(self, now):
        try:
            workloads = self.backend.list_owned()
        except BackendUnavailable:
            workloads = None
        # Complete the authoritative lookup before making any backend changes.
        with self.db.engine.connect() as conn:
            tenants = conn.execute(select(self.db.tenants.c.id)).scalars().all()
            rows = []
            for tenant_id in tenants:
                rows.extend(
                    conn.execute(
                        select(self.db.executions).where(
                            self.db.executions.c.tenant_id == tenant_id,
                            self.db.executions.c.status.not_in(TERMINAL),
                        )
                    )
                    .mappings()
                    .all()
                )
        active = sum(row["status"] in {"PROVISIONING", "RUNNING"} for row in rows)
        for row in rows:
            if now >= row["deadline_at"]:
                changed = self.change(row, "TIMED_OUT", now)
                if changed:
                    self.cleanup(changed)
                continue
            if row["status"] == "PENDING":
                if workloads is None or active >= self.max_active_executions:
                    continue
                credential = secrets.token_urlsafe(32)
                claimed = self.change(
                    row,
                    "PROVISIONING",
                    now,
                    launch_attempted_at=now,
                    credential_hash=hashlib.sha256(credential.encode()).hexdigest(),
                )
                if not claimed:
                    continue
                active += 1
                self.failpoints.hit("after_claim")
                spec = WorkloadSpec(
                    str(row["id"]),
                    credential,
                    row["deadline_at"],
                    {"owner": "agent-runtime", "execution_id": str(row["id"])},
                    row["input"],
                    max(1, math.ceil((row["deadline_at"] - now).total_seconds())),
                )
                try:
                    uid = self.backend.create(row["workload_name"], spec)
                except CreateRejected:
                    self.change(claimed, "FAILED", now, failure_reason="LAUNCH_FAILED")
                except (CreateOutcomeUnknown, BackendUnavailable):
                    pass
                else:
                    self.failpoints.hit("after_create")
                    self.change(
                        claimed,
                        "RUNNING",
                        now,
                        workload_uid=uid,
                    )
                continue
            try:
                workload = self.backend.get(row["workload_name"])
            except BackendUnavailable:
                continue
            changed = None
            if row["status"] == "PROVISIONING":
                if matches(workload, row):
                    changed = self.change(row, "RUNNING", now, workload_uid=workload.uid)
                else:
                    changed = self.change(row, "FAILED", now, failure_reason="LAUNCH_UNKNOWN")
            elif (
                workload is None or workload.phase == "LOST" or workload.uid != row["workload_uid"]
            ):
                changed = self.change(row, "FAILED", now, failure_reason="POD_LOST")
            elif workload.phase in {"SUCCEEDED", "FAILED"}:
                changed = self.change(
                    row, "FAILED", now, failure_reason="EXITED_WITHOUT_COMPLETION"
                )
            if changed and changed["status"] in TERMINAL:
                self.cleanup(changed)
        if workloads is None:
            return
        for workload in workloads:
            execution_id = workload.labels.get("execution_id")
            if (
                workload.labels.get("owner") != "agent-runtime"
                or not execution_id
                or workload.name != f"exec-{execution_id}"
            ):
                continue
            # Fresh state covers both concurrent creates and late-visible terminal workloads.
            with self.db.engine.connect() as conn:
                record = None
                for tenant_id in tenants:
                    e = self.db.executions
                    record = (
                        conn.execute(
                            select(e).where(
                                e.c.tenant_id == tenant_id, e.c.workload_name == workload.name
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if record is not None:
                        break
            if record is None or (record["status"] in TERMINAL and matches(workload, record)):
                try:
                    self.backend.delete(workload.name, workload.uid)
                except BackendUnavailable:
                    return
