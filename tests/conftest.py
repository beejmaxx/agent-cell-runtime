import json
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from agent_runtime.app import create_app
from agent_runtime.backend import FakeBackend
from agent_runtime.clock import Clock
from agent_runtime.config import Settings
from agent_runtime.db import Database
from agent_runtime.failpoints import Failpoints
from agent_runtime.mockworkday import MockWorkday
from agent_runtime.reconciler import Reconciler
from agent_runtime.seed import seed


@pytest.fixture(scope="session")
def database_url(tmp_path_factory):
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.fail("PostgreSQL initdb and pg_ctl must be on PATH")
    root = tmp_path_factory.mktemp("postgres")
    data = root / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [initdb, "-D", str(data), "-A", "trust", "-U", "runtime", "--no-locale", "--encoding=UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -k ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql+psycopg://runtime@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
        )


@pytest.fixture
def db(database_url):
    database = Database(database_url)
    database.initialize()
    with database.engine.begin() as conn:
        conn.execute(text("TRUNCATE idempotency_records, executions, agents, tenants CASCADE"))
    seed(database, "http://mockworkday.test")
    yield database
    database.engine.dispose()


class Issuer:
    def __init__(self, clock):
        self.clock = clock
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        self.grant_lists = {}
        self.jwks_calls = 0
        self.grant_calls = 0
        self.grant_delay = 0
        self.unavailable = False
        self.malformed = None

    def token(self, name="alice", tenant="acme", **changes):
        claims = {
            "iss": f"https://{tenant}.mockworkday.local",
            "sub": name,
            "aud": f"https://{tenant}.mockworkday.local/api",
            "typ": "human",
            "iat": self.clock.now().timestamp(),
            "exp": (self.clock.now() + timedelta(hours=48)).timestamp(),
            **changes,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def grant(self, owner="alice", tenant="acme", **changes):
        grant = {
            "id": str(uuid4()),
            "client_id": "hr-assistant",
            "scopes": ["staffing"],
            "expires_at": (self.clock.now() + timedelta(hours=2)).isoformat(),
            "revoked_at": None,
            **changes,
        }
        self.grant_lists.setdefault((tenant, owner), []).append(grant)
        return grant

    def handle(self, request):
        if request.url.path == "/.well-known/jwks.json":
            self.jwks_calls += 1
            key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**key, "kid": self.kid, "alg": "RS256"}]})
        assert request.url.path == "/api/v1/delegation-grants"
        self.grant_calls += 1
        sleep(self.grant_delay)
        if self.unavailable:
            raise httpx.ConnectError("offline", request=request)
        if self.malformed is not None:
            return httpx.Response(200, json=self.malformed)
        claims = jwt.decode(
            request.headers["Authorization"][7:], options={"verify_signature": False}
        )
        tenant = request.headers["Host"].split(".")[0]
        return httpx.Response(200, json=self.grant_lists.get((tenant, claims["sub"]), []))


@pytest.fixture
def env(db):
    clock = Clock(datetime(2030, 1, 1, tzinfo=UTC))
    issuer = Issuer(clock)
    mw = MockWorkday(httpx.Client(transport=httpx.MockTransport(issuer.handle), trust_env=False))
    backend, failpoints = FakeBackend(), Failpoints()
    app = create_app(
        db=db,
        mw=mw,
        clock=clock,
        backend=backend,
        failpoints=failpoints,
        reconcile=False,
        settings=Settings(str(db.engine.url), max_active_executions=1000),
    )
    with TestClient(app) as client:
        env = SimpleNamespace(
            db=db,
            clock=clock,
            issuer=issuer,
            mw=mw,
            backend=backend,
            failpoints=failpoints,
            app=app,
            client=client,
        )
        env.token = issuer.token()
        env.grant = issuer.grant()
        yield env
    mw.client.close()


def create(env, *, token=None, key=None, grant=None, **changes):
    body = {
        "agent": "hr-assistant",
        "grant_id": (grant or env.grant)["id"],
        "operations": ["read_worker"],
        "input": {"task": "synthetic"},
        "timeout_seconds": 600,
        **changes,
    }
    return env.client.post(
        "/api/v1/executions",
        json=body,
        headers={
            "Authorization": f"Bearer {token or env.token}",
            "Idempotency-Key": key or str(uuid4()),
        },
    )


def row(env, execution_id):
    with env.db.engine.connect() as conn:
        return (
            conn.execute(select(env.db.executions).where(env.db.executions.c.id == execution_id))
            .mappings()
            .one()
        )


def reconcile(env, restart=False):
    if restart:
        env.app.state.reconciler = Reconciler(
            env.db, env.backend, Failpoints(), env.app.state.reconciler.max_active_executions
        )
    env.app.state.reconciler.reconcile_once(env.clock.now())


def running(env):
    response = create(env)
    assert response.status_code == 201, response.text
    execution_id = response.json()["id"]
    reconcile(env)
    assert row(env, execution_id)["status"] == "RUNNING"
    return execution_id


def complete(env, execution_id, result=None, credential=None):
    if credential is None:
        credential = env.backend.specs[f"exec-{execution_id}"].credential
    return env.client.post(
        f"/api/v1/executions/{execution_id}/complete",
        headers={"Authorization": f"Execution {credential}"},
        json={"result": result or {"ok": True}},
    )


def cancel(env, execution_id, token=None):
    return env.client.post(
        f"/api/v1/executions/{execution_id}/cancel",
        headers={"Authorization": f"Bearer {token or env.token}"},
    )


def get(env, execution_id, token=None):
    return env.client.get(
        f"/api/v1/executions/{execution_id}",
        headers={"Authorization": f"Bearer {token or env.token}"},
    )
