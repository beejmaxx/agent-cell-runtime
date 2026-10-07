from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, insert, select, update

from agent_runtime.db import advisory_lock, digest
from agent_runtime.seed import SCOPES

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
FIELDS = (
    "id",
    "agent",
    "status",
    "failure_reason",
    "operations",
    "input",
    "deadline_at",
    "created_at",
    "finished_at",
    "result",
)


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def view(row):
    return {key: row[key] for key in FIELDS}


def owned(db, conn, tenant_id, principal, execution_id):
    e = db.executions
    return (
        conn.execute(
            select(e).where(
                e.c.tenant_id == tenant_id,
                e.c.principal_account_id == principal,
                e.c.id == execution_id,
            )
        )
        .mappings()
        .first()
    )


def transition(db, conn, row, status, now, **values):
    e = db.executions
    if status in TERMINAL:
        values["finished_at"] = now
    return (
        conn.execute(
            update(e)
            .where(
                e.c.tenant_id == row["tenant_id"],
                e.c.principal_account_id == row["principal_account_id"],
                e.c.id == row["id"],
                e.c.status == row["status"],
            )
            .values(status=status, **values)
            .returning(e)
        )
        .mappings()
        .first()
    )


def create_execution(db, conn, principal, key, body, token, mw, now):
    tenant_id, account_id = principal.tenant["id"], principal.account_id
    advisory_lock(conn, tenant_id, account_id, key)
    i = db.idempotency
    scope = (
        i.c.tenant_id == tenant_id,
        i.c.principal_account_id == account_id,
        i.c.idem_key == key,
    )
    record = conn.execute(select(i).where(*scope)).mappings().first()
    request_hash = digest(body)
    if record and record["created_at"] > now - timedelta(hours=24):
        if record["request_hash"] != request_hash:
            raise APIError(422, "IDEMPOTENCY_KEY_REUSED", "Key is bound to a different request")
        return owned(db, conn, tenant_id, account_id, record["execution_id"]), True
    if record:
        conn.execute(delete(i).where(*scope))
    a = db.agents
    agent = (
        conn.execute(select(a).where(a.c.tenant_id == tenant_id, a.c.name == body["agent"]))
        .mappings()
        .first()
    )
    if not agent or any(not op or op not in agent["operations"] for op in body["operations"]):
        raise APIError(422, "INVALID_REQUEST", "Unknown agent or operation")
    grants = mw.grants(principal.tenant, token)
    grant = next((g for g in grants if g["id"] == body["grant_id"]), None)
    if grant is None:
        raise APIError(403, "GRANT_NOT_OWNED", "Grant is not owned by the caller")
    if grant["revoked_at"] is not None or grant["expires_at"] <= now:
        raise APIError(403, "GRANT_INACTIVE", "Grant is inactive")
    if grant["client_id"] != agent["client_id"]:
        raise APIError(403, "GRANT_CLIENT_MISMATCH", "Grant belongs to another client")
    if any(SCOPES[op] not in grant["scopes"] for op in body["operations"]):
        raise APIError(403, "OPERATION_NOT_GRANTED", "An operation exceeds the grant scopes")
    deadline = now + timedelta(seconds=body["timeout_seconds"])
    if deadline > grant["expires_at"]:
        raise APIError(422, "DEADLINE_EXCEEDS_GRANT", "Deadline exceeds grant expiry")
    execution_id = uuid4()
    row = (
        conn.execute(
            insert(db.executions)
            .values(
                id=execution_id,
                tenant_id=tenant_id,
                principal_account_id=account_id,
                agent=body["agent"],
                grant_id=body["grant_id"],
                operations=body["operations"],
                input=body["input"],
                status="PENDING",
                workload_name=f"exec-{execution_id}",
                deadline_at=deadline,
                created_at=now,
            )
            .returning(db.executions)
        )
        .mappings()
        .one()
    )
    conn.execute(
        insert(i).values(
            tenant_id=tenant_id,
            principal_account_id=account_id,
            idem_key=key,
            request_hash=request_hash,
            execution_id=execution_id,
            created_at=now,
        )
    )
    return row, False
