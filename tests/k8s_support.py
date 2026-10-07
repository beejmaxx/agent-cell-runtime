import json
import socket
import ssl
import subprocess
from contextlib import contextmanager, nullcontext
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

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
from scripts.k8s import (
    PROFILE,
    ROOT,
    STATE,
    guard,
    image,
    image_metadata,
    kubectl,
    kubectl_args,
    require_owned,
)


def poll(check, seconds=60):
    until = monotonic() + seconds
    while monotonic() < until:
        value = check()
        if value:
            return value
        sleep(0.2)
    raise AssertionError(f"Condition did not converge within {seconds} seconds")


def admin_create(manifest, dry_run=False):
    args = kubectl_args("create", "-f", "-", "-o", "json")
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
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 8000 if PROFILE == "eks" else 0))
    port = sock.getsockname()[1]
    completion_sock = socket.socket()
    completion_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    completion_sock.bind(("0.0.0.0", 8001) if PROFILE == "eks" else ("127.0.0.1", 0))
    completion_port = completion_sock.getsockname()[1]
    settings = Settings(
        str(db.engine.url),
        workload_backend="kubernetes",
        k8s_api_url=cluster["server"],
        k8s_ca_file=str(STATE / "ca.crt"),
        k8s_token_file=str(STATE / "controller.token"),
        agent_image=image(),
        k8s_profile=PROFILE,
        runtime_url=completion_url(completion_port),
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
        directory = STATE / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(
                {
                    "metadata": {
                        k: pod["metadata"].get(k)
                        for k in ("name", "uid", "creationTimestamp", "annotations")
                    },
                    "status": pod.get("status", {}),
                },
                indent=2,
            )
        )
    return pod


def completion_url(port):
    if PROFILE != "eks":
        return f"http://192.168.5.2:{port}"
    from ipaddress import ip_address, ip_network
    from urllib.parse import urlsplit

    url = json.loads((STATE / "host.json").read_text())["completion_url"]
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.port != 8001
        or parsed.username
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or ip_address(parsed.hostname) not in ip_network("10.30.0.0/16")
    ):
        raise ValueError("Expected the S1 PrivateLink IP on port 8001")
    return url.rstrip("/")


def token_lifetime():
    if PROFILE == "eks":
        from scripts.s1_host import controller_tokens

        return controller_tokens(STATE)
    return nullcontext()


def image_env_names(settings):
    if PROFILE == "eks":
        return set(image_metadata()["env_names"])
    data = json.loads(
        subprocess.check_output(
            ["docker", "--context", "colima", "image", "inspect", settings.agent_image], text=True
        )
    )[0]
    return {e.split("=", 1)[0] for e in data["Config"]["Env"]}


def restore_controller_binding():
    if PROFILE == "eks":
        from scripts.s1_kubernetes import controller_binding

        kubectl(
            "create",
            "configmap",
            "s1-restore-controller-binding",
            "-n",
            "agent-exec",
            "--from-literal=action=restore-workload-controller",
        )
        expected = controller_binding()

        def restored():
            raw = kubectl(
                "get",
                "rolebinding",
                "workload-controller",
                "-n",
                "agent-exec",
                "--ignore-not-found",
                "-o",
                "json",
            )
            if not raw.strip():
                return False
            actual = json.loads(raw)
            return all(actual.get(key) == expected[key] for key in ("roleRef", "subjects"))

        # The local operator handles the request; no operator credentials enter this host.
        poll(restored, 120)
    else:
        kubectl("apply", "-f", str(ROOT / "deploy/k8s/resources.yaml"))


@contextmanager
def psa_control_namespace():
    if PROFILE == "eks":
        from scripts.s1_kubernetes import CONTROL_NAMESPACE, MARKER

        value = json.loads(kubectl("get", "namespace", CONTROL_NAMESPACE, "-o", "json"))
        labels = value["metadata"].get("labels", {})
        if labels.get(MARKER) != "true" or any(
            k.startswith("pod-security.kubernetes.io/") for k in labels
        ):
            raise RuntimeError("Expected the operator-owned, unlabeled PSA control namespace")
        kubectl("get", "serviceaccount", "agent-exec", "-n", CONTROL_NAMESPACE)
        yield CONTROL_NAMESPACE
        return
    namespace = f"agent-psa-control-{uuid4().hex[:10]}"
    kubectl("create", "namespace", namespace)
    try:
        kubectl("create", "serviceaccount", "agent-exec", "-n", namespace)
        yield namespace
    finally:
        kubectl("delete", "namespace", namespace, "--wait=true", "--timeout=60s")


def agent_permissions():
    if PROFILE == "eks":
        # Impersonation stays with the operator; the host receives only this report.
        report = (STATE / "agent-permissions.txt").read_text()
        if not report.startswith("Resources") or len(report.splitlines()) < 2:
            raise RuntimeError("Operator permissions report is empty or malformed")
        return report
    return kubectl(
        "auth",
        "can-i",
        "--list",
        "--as=system:serviceaccount:agent-exec:agent-exec",
        "-n",
        "agent-exec",
    )
