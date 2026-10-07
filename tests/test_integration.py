import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from conftest import cancel, complete, create, get, reconcile
from fastapi.testclient import TestClient

from agent_runtime.app import create_app
from agent_runtime.backend import FakeBackend
from agent_runtime.clock import Clock
from agent_runtime.mockworkday import MockWorkday
from agent_runtime.seed import seed

pytestmark = pytest.mark.integration


@pytest.fixture
def live(db):
    base_url = os.getenv("MW_BASE_URL", "http://127.0.0.1:18080")
    seed(db, base_url)
    grants = []
    # Frozen test-admin time keeps repeated logins in the same rate-limit window.
    response = httpx.post(
        "http://127.0.0.1:8081/admin/clock",
        json={"advance_seconds": 3600},
        trust_env=False,
        timeout=5,
    )
    response.raise_for_status()
    with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as http:
        tokens = {}
        for name, tenant in (("alice", "acme"), ("bob", "acme"), ("dave", "globex")):
            response = http.post(
                "/oauth2/token",
                headers={"Host": f"{tenant}.mockworkday.local"},
                data={"grant_type": "password", "username": name, "password": f"pw-{name}"},
            )
            assert response.status_code == 200, "Mock Workday synthetic login failed"
            tokens[name] = response.json()["access_token"]

        def grant(owner="alice", tenant="acme", scopes=None, ttl=3600):
            headers = {
                "Host": f"{tenant}.mockworkday.local",
                "Authorization": f"Bearer {tokens[owner]}",
                "Idempotency-Key": str(uuid4()),
            }
            response = http.post(
                "/api/v1/delegation-grants",
                headers=headers,
                json={
                    "client_id": "hr-assistant",
                    "scopes": scopes or ["staffing"],
                    "ttl_seconds": ttl,
                },
            )
            assert response.status_code == 201, response.text
            value = response.json()
            grants.append((value["id"], headers))
            return value

        # Mock Workday test-admin time is frozen independently of wall-clock time.
        issued_at = jwt.decode(tokens["alice"], options={"verify_signature": False})["iat"]
        clock = Clock(datetime.fromtimestamp(issued_at, UTC))
        mw = MockWorkday(http)
        backend = FakeBackend()
        app = create_app(db=db, mw=mw, clock=clock, backend=backend, reconcile=False)
        with TestClient(app) as client:
            initial = grant()
            yield SimpleNamespace(
                db=db,
                app=app,
                client=client,
                clock=clock,
                backend=backend,
                token=tokens["alice"],
                tokens=tokens,
                grant=initial,
                new_grant=grant,
                http=http,
            )
        for grant_id, headers in grants:
            response = http.delete(f"/api/v1/delegation-grants/{grant_id}", headers=headers)
            assert response.status_code == 204, response.text


def test_ID_5_live_grant_ownership(live):
    denied = create(live, token=live.tokens["bob"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "GRANT_NOT_OWNED"
    owner = create(live)
    assert owner.status_code == 201
    assert cancel(live, owner.json()["id"]).status_code == 200


def test_ID_6_live_scope_deadline_revocation_and_expiry(live):
    assert (
        create(live, operations=["read_compensation"]).json()["error"]["code"]
        == "OPERATION_NOT_GRANTED"
    )
    short = live.new_grant(ttl=60)
    too_long = create(live, grant=short)
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "DEADLINE_EXCEEDS_GRANT"
    allowed = create(live, grant=short, timeout_seconds=30)
    assert allowed.status_code == 201
    assert cancel(live, allowed.json()["id"]).status_code == 200
    response = live.http.delete(
        f"/api/v1/delegation-grants/{live.grant['id']}",
        headers={"Host": "acme.mockworkday.local", "Authorization": f"Bearer {live.token}"},
    )
    assert response.status_code == 204
    inactive = create(live)
    assert inactive.status_code == 403
    assert inactive.json()["error"]["code"] == "GRANT_INACTIVE"
    live.clock.set(datetime.fromisoformat(short["expires_at"]))
    expired = create(live, grant=short, timeout_seconds=30)
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "GRANT_INACTIVE"


def test_HAPPY_live_create_complete_read_and_cleanup(live):
    response = create(live, key="live-happy")
    assert response.status_code == 201, response.text
    execution_id = response.json()["id"]
    assert create(live, key="live-happy").json()["id"] == execution_id
    reconcile(live)
    result = {"message": "synthetic end-to-end result"}
    response = complete(live, execution_id, result)
    assert response.status_code == 200
    assert get(live, execution_id).json()["result"] == result
    assert complete(live, execution_id, result).status_code == 200
    reconcile(live)
    assert live.backend.list_owned() == []


def test_ID_11_live_denies_other_principals(live):
    alice = create(live).json()["id"]
    for name, tenant in (("bob", "acme"), ("dave", "globex")):
        # A real grant-list request proves this caller is authenticated at the service.
        control = live.http.get(
            "/api/v1/delegation-grants",
            headers={
                "Host": f"{tenant}.mockworkday.local",
                "Authorization": f"Bearer {live.tokens[name]}",
            },
        )
        assert control.status_code == 200
        assert get(live, alice, live.tokens[name]).status_code == 404
        assert cancel(live, alice, live.tokens[name]).status_code == 404
    owner_view = get(live, alice)
    assert owner_view.status_code == 200
    assert owner_view.json()["status"] == "PENDING"
    assert cancel(live, alice).status_code == 200
