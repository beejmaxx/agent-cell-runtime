from datetime import timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from conftest import cancel, create, get, row
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select


def counts(env):
    with env.db.engine.connect() as conn:
        return tuple(
            conn.scalar(select(func.count()).select_from(table))
            for table in (env.db.executions, env.db.idempotency)
        )


def test_ID_5_grant_ownership(env):
    response = create(env, token=env.issuer.token("bob"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "GRANT_NOT_OWNED"
    assert counts(env) == (0, 0)
    assert create(env).status_code == 201
    assert create(env, grant={"id": str(uuid4())}).json()["error"]["code"] == "GRANT_NOT_OWNED"


@pytest.mark.parametrize(
    "changes,body,status,code",
    [
        ({"scopes": ["absence"]}, {}, 403, "OPERATION_NOT_GRANTED"),
        ({"revoked_at": "2029-12-31T00:00:00Z"}, {}, 403, "GRANT_INACTIVE"),
        ({"expires_at": "2030-01-01T00:00:00Z"}, {}, 403, "GRANT_INACTIVE"),
        ({"expires_at": "2029-12-31T23:59:59Z"}, {}, 403, "GRANT_INACTIVE"),
        ({"client_id": "another-client"}, {}, 403, "GRANT_CLIENT_MISMATCH"),
        ({"expires_at": "2030-01-01T00:00:30Z"}, {}, 422, "DEADLINE_EXCEEDS_GRANT"),
    ],
)
def test_ID_6_grant_constraints(env, changes, body, status, code):
    grant = env.issuer.grant(**changes)
    response = create(env, grant=grant, **body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert counts(env) == (0, 0)
    assert create(env).status_code == 201


def test_ID_6_deadline_equal_to_expiry(env):
    grant = env.issuer.grant(expires_at=(env.clock.now() + timedelta(seconds=30)).isoformat())
    assert create(env, grant=grant, timeout_seconds=30).status_code == 201


@pytest.mark.parametrize(
    "bad",
    [
        "unavailable",
        {},
        {"data": []},
        [None],
        [{"id": "x"}],
        [
            {
                "id": "x",
                "client_id": "hr-assistant",
                "scopes": "staffing",
                "expires_at": "2030-01-01T01:00:00Z",
                "revoked_at": None,
            }
        ],
        [
            {
                "id": "x",
                "client_id": "hr-assistant",
                "scopes": ["staffing"],
                "expires_at": "invalid",
                "revoked_at": None,
            }
        ],
    ],
)
def test_ID_6_fails_closed_without_idempotency_record(env, bad):
    if bad == "unavailable":
        env.issuer.unavailable = True
    else:
        env.issuer.malformed = bad
    response = create(env, key="retry")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GRANT_CHECK_UNAVAILABLE"
    assert counts(env) == (0, 0)
    env.issuer.unavailable, env.issuer.malformed = False, None
    assert create(env, key="retry").status_code == 201


def test_ID_11_tenant_principal_routes_and_replay(env):
    alice = create(env, key="shared").json()["id"]
    dave_token = env.issuer.token("dave", "globex")
    dave_grant = env.issuer.grant("dave", "globex")
    dave_response = create(env, token=dave_token, grant=dave_grant, key="shared")
    assert dave_response.status_code == 201
    dave = dave_response.json()["id"]
    bob_token = env.issuer.token("bob")
    bob_grant = env.issuer.grant("bob")
    bob_response = create(env, token=bob_token, grant=bob_grant, key="shared")
    assert bob_response.status_code == 201
    globex_alice_token = env.issuer.token("alice", "globex")
    # Identical sub, key, and body isolate the tenant component of idempotency.
    globex_alice_grant = env.issuer.grant("alice", "globex", id=env.grant["id"])
    globex_alice_response = create(
        env, token=globex_alice_token, grant=globex_alice_grant, key="shared"
    )
    assert globex_alice_response.status_code == 201
    globex_alice = globex_alice_response.json()["id"]
    assert "Idempotent-Replay" not in globex_alice_response.headers
    assert len({alice, dave, bob_response.json()["id"], globex_alice}) == 4
    for token, target in (
        (bob_token, alice),
        (dave_token, alice),
        (env.token, dave),
        (globex_alice_token, alice),
        (env.token, globex_alice),
    ):
        assert get(env, target, token).status_code == 404
        assert cancel(env, target, token).status_code == 404
    for token, target in (
        (env.token, alice),
        (dave_token, dave),
        (globex_alice_token, globex_alice),
    ):
        assert row(env, target)["status"] == "PENDING"
        assert get(env, target, token).status_code == 200
        assert cancel(env, target, token).status_code == 200
    assert create(env, token=dave_token, grant=dave_grant, key="shared").json()["id"] == dave
    assert (
        create(env, token=bob_token, grant=bob_grant, key="shared").json()["id"]
        == bob_response.json()["id"]
    )
    assert create(env, key="shared").json()["id"] == alice
    assert (
        create(env, token=globex_alice_token, grant=globex_alice_grant, key="shared").json()["id"]
        == globex_alice
    )
    assert get(env, str(uuid4())).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"typ": "delegated"},
        {"typ": "integration"},
        {"aud": "somewhere-else"},
        {"iss": "https://unknown.mockworkday.local"},
        {"sub": ""},
        {"exp": 0},
        {"exp": float("nan")},
        {"nbf": 9999999999},
    ],
)
def test_ID_11_human_authentication(env, changes):
    assert create(env, token=env.issuer.token(**changes)).status_code == 401
    assert counts(env) == (0, 0)
    assert create(env).status_code == 201


def test_ID_11_auth_signature_algorithm_cache_and_clock(env):
    assert create(env).status_code == 201
    assert create(env).status_code == 201
    assert env.issuer.jwks_calls == 1
    claims = jwt.decode(env.token, options={"verify_signature": False})
    bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, bad_key, algorithm="RS256", headers={"kid": env.issuer.kid})
    assert create(env, token=forged).status_code == 401
    hmac = jwt.encode(
        claims,
        "synthetic-secret-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": env.issuer.kid},
    )
    assert create(env, token=hmac).status_code == 401
    env.issuer.key, env.issuer.kid = bad_key, "rotated"
    assert create(env, token=env.issuer.token()).status_code == 201
    assert env.issuer.jwks_calls == 2
    expiring = env.issuer.token(exp=(env.clock.now() + timedelta(seconds=10)).timestamp())
    assert create(env, token=expiring).status_code == 201
    env.clock.advance(10)
    assert create(env, token=expiring).status_code == 401
    assert create(env, token=env.issuer.token()).status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"agent": "unknown"},
        {"operations": [""]},
        {"operations": ["unknown"]},
        {"input": []},
        {"input": {"text": "é" * 8192}},
        {"timeout_seconds": 29},
        {"timeout_seconds": 3601},
    ],
)
def test_ID_6_execution_request_validation(env, body):
    response = create(env, **body)
    assert response.status_code == 422
    assert set(response.json()["error"]) == {"code", "message", "request_id"}
    assert counts(env) == (0, 0)
    assert create(env).status_code == 201


@pytest.mark.parametrize(
    "failure",
    [
        "unreachable",
        "http_error",
        "invalid_json",
        "missing_keys",
        "invalid_keys",
        "invalid_key",
        "invalid_rsa",
        "partial_keys",
    ],
)
def test_ID_11_jwks_unavailable_fails_closed(env, monkeypatch, failure):
    original_get = env.mw.client.get

    def broken_jwks(url, **kwargs):
        assert url.endswith("/.well-known/jwks.json")
        request = httpx.Request("GET", url)
        if failure == "unreachable":
            raise httpx.ConnectError("offline", request=request)
        if failure == "http_error":
            return httpx.Response(503, request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content="not json", request=request)
        malformed = {
            "missing_keys": {},
            "invalid_keys": {"keys": None},
            "invalid_key": {"keys": [None]},
            "invalid_rsa": {"keys": [{"kid": "test-key", "kty": "RSA", "n": "AA", "e": "AA"}]},
        }
        if failure == "partial_keys":
            valid = original_get(url, **kwargs).json()
            data = {"keys": [*valid["keys"], None]}
        else:
            data = malformed[failure]
        return httpx.Response(200, json=data, request=request)

    monkeypatch.setattr(env.mw.client, "get", broken_jwks)
    for _ in range(2):
        response = create(env, key="auth-retry")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUTH_UNAVAILABLE"
        assert counts(env) == (0, 0)
        assert env.issuer.grant_calls == 0
    monkeypatch.setattr(env.mw.client, "get", original_get)
    assert create(env, key="auth-retry").status_code == 201


def test_ID_11_unknown_kid_in_valid_jwks_is_bad_token(env):
    claims = jwt.decode(env.token, options={"verify_signature": False})
    unknown = jwt.encode(claims, env.issuer.key, algorithm="RS256", headers={"kid": "unknown"})
    response = create(env, token=unknown)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert counts(env) == (0, 0)
    assert create(env).status_code == 201
