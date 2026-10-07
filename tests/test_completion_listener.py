from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import cancel, complete, get, row, running
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from agent_runtime.app import create_completion_app


@pytest.fixture
def completion(env):
    app = create_completion_app(env.app)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, backend=env.backend, app=app)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/executions"),
        ("GET", "/api/v1/executions"),
        ("GET", f"/api/v1/executions/{uuid4()}"),
        ("POST", f"/api/v1/executions/{uuid4()}/cancel"),
        ("GET", "/health"),
        ("GET", "/healthz"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("POST", f"/api/v1/executions/{uuid4()}/complete/"),
    ],
)
def test_ISO_2_completion_listener_has_no_control_routes(env, completion, method, path):
    execution_id = running(env)
    assert completion.client.request(method, path).status_code == 404
    assert complete(completion, execution_id).status_code == 200
    assert get(env, execution_id).json()["status"] == "SUCCEEDED"


def test_LC_9_ID_8_completion_listener_binding_validation_and_replay(env, completion):
    first, second = running(env), running(env)
    credential = env.backend.specs[f"exec-{first}"].credential
    before = dict(row(env, second))
    assert complete(completion, second, credential=credential).status_code == 401
    assert dict(row(env, second)) == before
    assert complete(completion, first, credential="wrong").status_code == 401
    assert (
        completion.client.post(
            f"/api/v1/executions/{first}/complete", json={"result": {}}
        ).status_code
        == 401
    )
    for result in ([1], "text", {"value": "é" * 32768}):
        assert complete(completion, first, result).status_code == 422
        assert row(env, first)["status"] == "RUNNING"
    response = complete(completion, first, {"b": 2, "a": 1})
    assert response.status_code == 200
    assert complete(completion, first, {"a": 1, "b": 2}).json() == response.json()
    assert complete(env, first, {"b": 2, "a": 1}).json() == response.json()
    assert complete(completion, first, {"a": 3}).status_code == 409
    assert cancel(env, second).status_code == 200
    assert complete(completion, second).status_code == 409


def test_LC_9_LC_10_completion_listener_lost_response(env, completion):
    execution_id = running(env)
    env.failpoints.arm("before_cleanup")
    assert complete(completion, execution_id).status_code == 500
    before = dict(row(env, execution_id))
    assert before["status"] == "SUCCEEDED"
    assert complete(completion, execution_id).status_code == 200
    assert dict(row(env, execution_id)) == before
    assert env.backend.list_owned() == []


def test_LC_7_completion_listener_database_failure(env, completion, monkeypatch):
    execution_id = running(env)
    original = env.db.engine.begin

    def unavailable():
        raise OperationalError("connection", {}, Exception("synthetic outage"))

    monkeypatch.setattr(env.db.engine, "begin", unavailable)
    response = complete(completion, execution_id)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UNAVAILABLE"
    monkeypatch.setattr(env.db.engine, "begin", original)
    assert complete(completion, execution_id).status_code == 200


@pytest.mark.parametrize("listener", ["full", "completion"])
@pytest.mark.parametrize("offset", [-0.000001, 0, 1])
def test_LC_8_ID_10_completion_obeys_database_deadline_without_reconciler(
    env, completion, listener, offset
):
    from datetime import timedelta

    execution_id = running(env)
    # Keep the credential valid while exercising the independent database guard.
    deadline = row(env, execution_id)["deadline_at"] - timedelta(seconds=10)
    with env.db.engine.begin() as conn:
        conn.execute(
            env.db.executions.update()
            .where(env.db.executions.c.id == execution_id)
            .values(deadline_at=deadline)
        )
    before = dict(row(env, execution_id))
    env.clock.set(deadline + timedelta(seconds=offset))
    response = complete(env if listener == "full" else completion, execution_id)
    if offset < 0:
        assert response.status_code == 200
        assert row(env, execution_id)["status"] == "SUCCEEDED"
    else:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DEADLINE_EXCEEDED"
        assert dict(row(env, execution_id)) == before
        assert env.backend.delete_calls == 0


@pytest.mark.parametrize("guard", ["deadline_at", "credential_expires_at"])
def test_LC_8_ID_10_completion_rechecks_deadline_after_row_lock(env, completion, guard):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import event, select, text

    execution_id = running(env)
    e = env.db.executions
    deadline = row(env, execution_id)["deadline_at"] - timedelta(seconds=10)
    with env.db.engine.begin() as conn:
        conn.execute(e.update().where(e.c.id == execution_id).values({guard: deadline}))
    before = dict(row(env, execution_id))
    attempting = Event()
    pids = []

    def waiting_query(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            pids.append(conn.connection.driver_connection.info.backend_pid)
            attempting.set()

    with env.db.engine.begin() as owner:
        owner.execute(select(e).where(e.c.id == execution_id).with_for_update())
        event.listen(env.db.engine, "before_cursor_execute", waiting_query)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(complete, completion, execution_id)
            try:
                assert attempting.wait(5)
                with env.db.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as observer:
                    until = monotonic() + 5
                    while monotonic() < until:
                        if (
                            observer.scalar(
                                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                                {"pid": pids[0]},
                            )
                            == "Lock"
                        ):
                            break
                        sleep(0.01)
                    else:
                        pytest.fail("Completion never blocked on the row lock")
                env.clock.set(deadline)
            finally:
                owner.commit()
                event.remove(env.db.engine, "before_cursor_execute", waiting_query)
            response = future.result(timeout=5)
    assert response.status_code == (409 if guard == "deadline_at" else 401)
    assert response.json()["error"]["code"] == (
        "DEADLINE_EXCEEDED" if guard == "deadline_at" else "CREDENTIAL_EXPIRED"
    )
    assert dict(row(env, execution_id)) == before
    assert env.backend.delete_calls == 0


def test_LC_8_ID_10_deadline_crossed_between_check_and_update(env, completion):
    from datetime import UTC, datetime, timedelta
    from time import sleep

    from sqlalchemy import event

    from agent_runtime.clock import Clock

    execution_id = running(env)
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    with env.db.engine.begin() as conn:
        conn.execute(
            env.db.executions.update()
            .where(env.db.executions.c.id == execution_id)
            .values(deadline_at=deadline)
        )
    before = dict(row(env, execution_id))
    # Production clock: SQL must not reuse a timestamp sampled before this stall.
    env.app.state.clock = completion.app.state.clock = Clock()
    stalled = []

    def delay_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE executions"):
            stalled.append(True)
            sleep(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.05)

    event.listen(env.db.engine, "before_cursor_execute", delay_update)
    try:
        response = complete(completion, execution_id)
    finally:
        event.remove(env.db.engine, "before_cursor_execute", delay_update)
    assert stalled == [True]
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEADLINE_EXCEEDED"
    assert dict(row(env, execution_id)) == before
    assert env.backend.delete_calls == 0


def test_LC_8_LC_9_ID_10_receipt_replay_rejected_after_deadline(env, completion):
    from datetime import timedelta

    execution_id = running(env)
    deadline = row(env, execution_id)["deadline_at"] - timedelta(seconds=10)
    with env.db.engine.begin() as conn:
        conn.execute(
            env.db.executions.update()
            .where(env.db.executions.c.id == execution_id)
            .values(deadline_at=deadline)
        )
    assert complete(completion, execution_id).status_code == 200
    before = dict(row(env, execution_id))
    deletes = env.backend.delete_calls
    env.clock.set(deadline)
    response = complete(completion, execution_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEADLINE_EXCEEDED"
    assert dict(row(env, execution_id)) == before
    assert env.backend.delete_calls == deletes


@pytest.mark.parametrize("listener", ["full", "completion"])
@pytest.mark.parametrize("offset", [-0.000001, 0, 1])
def test_LC_8_ID_10_credential_expiry_without_reconciler(env, completion, listener, offset):
    from datetime import timedelta

    execution_id = running(env)
    issued = row(env, execution_id)
    assert issued["credential_expires_at"] == issued["deadline_at"]
    # Leave the database deadline in the future to isolate credential validity.
    expiry = issued["credential_expires_at"] - timedelta(seconds=10)
    with env.db.engine.begin() as conn:
        conn.execute(
            env.db.executions.update()
            .where(env.db.executions.c.id == execution_id)
            .values(credential_expires_at=expiry)
        )
    before = dict(row(env, execution_id))
    assert before["status"] == "RUNNING"
    env.clock.set(expiry + timedelta(seconds=offset))
    response = complete(env if listener == "full" else completion, execution_id)
    if offset < 0:
        assert response.status_code == 200
        assert row(env, execution_id)["status"] == "SUCCEEDED"
    else:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "CREDENTIAL_EXPIRED"
        assert dict(row(env, execution_id)) == before
        assert env.backend.delete_calls == 0


@pytest.mark.parametrize("status", ["RUNNING", "SUCCEEDED", "CANCELLED"])
def test_ID_10_credential_expiry_precedes_state_handling(env, completion, status):
    execution_id = running(env)
    if status == "SUCCEEDED":
        assert complete(completion, execution_id).status_code == 200
    elif status == "CANCELLED":
        assert cancel(env, execution_id).status_code == 200
    before = dict(row(env, execution_id))
    deletes = env.backend.delete_calls
    env.clock.set(before["credential_expires_at"])
    response = complete(completion, execution_id)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CREDENTIAL_EXPIRED"
    assert dict(row(env, execution_id)) == before
    assert env.backend.delete_calls == deletes


def test_ID_10_existing_schema_credentials_without_expiry_fail_closed(env, completion):
    from agent_runtime.db import Database

    execution_id = running(env)
    with env.db.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE executions DROP COLUMN credential_expires_at")
    upgraded = Database(str(env.db.engine.url))
    try:
        upgraded.initialize()
        upgraded.initialize()
        before = dict(row(env, execution_id))
        assert before["credential_expires_at"] is None
        response = complete(completion, execution_id)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "CREDENTIAL_EXPIRED"
        assert dict(row(env, execution_id)) == before
        assert env.backend.delete_calls == 0
    finally:
        upgraded.engine.dispose()
