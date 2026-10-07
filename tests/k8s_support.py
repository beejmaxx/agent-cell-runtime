import json
import socket
import ssl
import subprocess
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import uvicorn
from conftest import Issuer

from agent_runtime.app import create_app, create_completion_app
from agent_runtime.clock import Clock
from agent_runtime.config import Settings
from agent_runtime.failpoints import Failpoints
from agent_runtime.kubernetes import KubernetesBackend
from agent_runtime.mockworkday import MockWorkday
from agent_runtime.reconciler import Reconciler
from scripts.k8s import IMAGE, KUBECTL, ROOT, STATE, guard, kubectl, require_owned


def poll(check, seconds=60):
    until = monotonic() + seconds
    while monotonic() < until:
        value = check()
        if value:
            return value
        sleep(0.2)
    raise AssertionError(f"Condition did not converge within {seconds} seconds")


def admin_create(manifest, dry_run=False):
    args = [str(KUBECTL), "--context", "colima", "create", "-f", "-", "-o", "json"]
    if dry_run:
        args.append("--dry-run=server")
    return subprocess.run(
        args, input=json.dumps(manifest), capture_output=True, text=True, check=False
    )


def admin_pod(name):
    raw = kubectl("get", "pod", name, "-n", "agent-exec", "--ignore-not-found", "-o", "json")
    return json.loads(raw) if raw.strip() else None


def delete_pod(name):
    kubectl(
        "delete",
        "pod",
        name,
        "-n",
        "agent-exec",
        "--ignore-not-found",
        "--wait=true",
        "--timeout=30s",
    )


def clear_pods():
    guard()
    require_owned()
    kubectl("delete", "pods", "--all", "-n", "agent-exec", "--wait=true", "--timeout=60s")
    assert not json.loads(kubectl("get", "pods", "-n", "agent-exec", "-o", "json"))["items"]


class FaultTransport(httpx.BaseTransport):
    def __init__(self, ca):
        self.inner = httpx.HTTPTransport(verify=ssl.create_default_context(cafile=ca), retries=0)
        self.offline = False
        self.lose_create = False
        self.create_calls = 0
        self.delete_calls = 0

    def handle_request(self, request):
        if self.offline:
            raise httpx.ConnectError("injected outage", request=request)
        create = request.method == "POST" and request.url.path.endswith("/pods")
        if create:
            self.create_calls += 1
        if request.method == "DELETE":
            self.delete_calls += 1
        response = self.inner.handle_request(request)
        if create and self.lose_create:
            self.lose_create = False
            response.read()
            response.close()
            raise httpx.ReadTimeout("injected lost create response", request=request)
        return response

    def close(self):
        self.inner.close()


def start(db):
    cluster = guard()
    require_owned()
    clear_pods()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    completion_sock = socket.socket()
    completion_sock.bind(("127.0.0.1", 0))
    completion_port = completion_sock.getsockname()[1]
    settings = Settings(
        str(db.engine.url),
        workload_backend="kubernetes",
        k8s_api_url=cluster["server"],
        k8s_ca_file=str(STATE / "ca.crt"),
        k8s_token_file=str(STATE / "controller.token"),
        agent_image=IMAGE,
        runtime_url=f"http://192.168.5.2:{completion_port}",
        max_active_executions=3,
    )
    transport = FaultTransport(settings.k8s_ca_file)
    http = httpx.Client(
        base_url=settings.k8s_api_url,
        transport=transport,
        trust_env=False,
        timeout=httpx.Timeout(5, connect=2),
    )
    backend = KubernetesBackend(settings, http)
    clock, failpoints = Clock(), Failpoints()
    issuer = Issuer(clock)
    mw = MockWorkday(httpx.Client(transport=httpx.MockTransport(issuer.handle), trust_env=False))
    app = create_app(
        db=db,
        mw=mw,
        clock=clock,
        backend=backend,
        failpoints=failpoints,
        settings=settings,
        reconcile=False,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    poll(lambda: server.started, 10)
    completion_app = create_completion_app(app)
    completion_server = uvicorn.Server(
        uvicorn.Config(completion_app, log_level="critical", access_log=False)
    )
    completion_thread = Thread(
        target=completion_server.run, kwargs={"sockets": [completion_sock]}, daemon=True
    )
    completion_thread.start()
    poll(lambda: completion_server.started, 10)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10)
    return SimpleNamespace(
        db=db,
        app=app,
        client=client,
        clock=clock,
        backend=backend,
        transport=transport,
        settings=settings,
        issuer=issuer,
        token=issuer.token(),
        grant=issuer.grant(),
        failpoints=failpoints,
        server=server,
        thread=thread,
        sock=sock,
        completion_server=completion_server,
        completion_thread=completion_thread,
        completion_sock=completion_sock,
        mw=mw,
        http=http,
    )


def stop(env):
    env.transport.offline = False
    env.completion_server.should_exit = True
    env.completion_thread.join(10)
    env.completion_sock.close()
    env.server.should_exit = True
    env.thread.join(10)
    env.client.close()
    env.http.close()
    env.mw.client.close()
    env.sock.close()
    clear_pods()


def restart(env):
    env.backend = KubernetesBackend(env.settings, env.http)
    env.app.state.backend = env.backend
    env.app.state.reconciler = Reconciler(
        env.db, env.backend, Failpoints(), env.app.state.reconciler.max_active_executions
    )


def evidence(name):
    pod = admin_pod(name)
    if pod:
        directory = ROOT / ".local/k8s/evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(
                {
                    "metadata": {
                        k: pod["metadata"].get(k) for k in ("name", "uid", "creationTimestamp")
                    },
                    "status": pod.get("status", {}),
                },
                indent=2,
            )
        )
    return pod
