import hashlib
import hmac
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from agent_runtime.db import canonical, digest
from agent_runtime.executions import TERMINAL, APIError, create_execution, owned, transition, view

router = APIRouter(prefix="/api/v1")


class CreateBody(BaseModel):
    model_config = ConfigDict(strict=True)
    agent: str
    grant_id: str
    operations: list[str]
    input: dict
    timeout_seconds: int = Field(ge=30, le=3600)

    @field_validator("input")
    @classmethod
    def input_size(cls, value):
        if len(canonical(value)) > 16 * 1024:
            raise ValueError("input exceeds 16 KiB")
        return value


class CompleteBody(BaseModel):
    model_config = ConfigDict(strict=True)
    result: dict

    @field_validator("result")
    @classmethod
    def result_size(cls, value):
        if len(canonical(value)) > 64 * 1024:
            raise ValueError("result exceeds 64 KiB")
        return value


def user(request, conn):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise APIError(401, "UNAUTHORIZED", "Human bearer token required")
    token = header[7:]
    return request.app.state.auth.authenticate(conn, token), token


def require_row(row):
    if row is None:
        raise APIError(404, "NOT_FOUND", "Execution not found")
    return row


@router.post("/executions", status_code=201)
def create(request: Request, body: CreateBody):
    s = request.app.state
    with s.db.engine.begin() as conn:
        principal, token = user(request, conn)
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise APIError(422, "INVALID_REQUEST", "Idempotency-Key is required")
        row, replay = create_execution(
            s.db, conn, principal, key, body.model_dump(), token, s.mw, s.clock.now()
        )
    return JSONResponse(
        jsonable_encoder(view(row)),
        status_code=201,
        headers={"Idempotent-Replay": "true"} if replay else {},
    )


@router.get("/executions/{execution_id}")
def get(request: Request, execution_id: UUID):
    s = request.app.state
    with s.db.engine.connect() as conn:
        principal, _ = user(request, conn)
        row = owned(s.db, conn, principal.tenant["id"], principal.account_id, execution_id)
        return view(require_row(row))


@router.post("/executions/{execution_id}/cancel")
def cancel(request: Request, execution_id: UUID):
    s = request.app.state
    with s.db.engine.begin() as conn:
        principal, _ = user(request, conn)
        row = require_row(
            owned(s.db, conn, principal.tenant["id"], principal.account_id, execution_id)
        )
        while row["status"] not in TERMINAL:
            changed = transition(s.db, conn, row, "CANCELLED", s.clock.now())
            row = changed or owned(
                s.db, conn, principal.tenant["id"], principal.account_id, execution_id
            )
        if row["status"] != "CANCELLED":
            raise APIError(409, "INVALID_STATE", "Execution is terminal")
    s.failpoints.hit("before_cleanup")
    s.reconciler.cleanup(row)
    return view(row)


@router.post("/executions/{execution_id}/complete")
def complete(request: Request, execution_id: UUID, body: CompleteBody):
    s = request.app.state
    header = request.headers.get("Authorization", "")
    if not header.startswith("Execution ") or not header[10:]:
        raise APIError(401, "UNAUTHORIZED", "Execution credential required")
    credential_hash = hashlib.sha256(header[10:].encode()).hexdigest()
    with s.db.engine.begin() as conn:
        # This temporary route derives identity from the path-bound execution credential.
        e = s.db.executions
        row = None
        for tenant_id in conn.execute(select(s.db.tenants.c.id)).scalars().all():
            row = (
                conn.execute(select(e).where(e.c.tenant_id == tenant_id, e.c.id == execution_id))
                .mappings()
                .first()
            )
            if row:
                break
        if (
            not row
            or not row["credential_hash"]
            or not hmac.compare_digest(row["credential_hash"], credential_hash)
        ):
            raise APIError(401, "UNAUTHORIZED", "Invalid execution credential")
        result_hash = digest(body.result)
        if row["status"] == "RUNNING":
            changed = transition(
                s.db,
                conn,
                row,
                "SUCCEEDED",
                s.clock.now(),
                result=body.result,
                result_hash=result_hash,
            )
            row = changed or owned(
                s.db, conn, row["tenant_id"], row["principal_account_id"], execution_id
            )
        if row["status"] != "SUCCEEDED":
            raise APIError(409, "INVALID_STATE", "Execution is not running")
        if row["result_hash"] != result_hash:
            raise APIError(409, "COMPLETION_CONFLICT", "Completion result differs")
    s.failpoints.hit("before_cleanup")
    s.reconciler.cleanup(row)
    return view(row)


def error_response(request, status, code, message):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.headers.get("X-Request-Id") or str(uuid4()),
            }
        },
    )
