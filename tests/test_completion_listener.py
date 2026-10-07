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
