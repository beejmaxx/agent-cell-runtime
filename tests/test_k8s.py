import json
from uuid import uuid4

import pytest
from conftest import cancel, create, get, reconcile, row
from k8s_support import admin_create, admin_pod, delete_pod, evidence, poll, restart, start, stop
from sqlalchemy.exc import OperationalError

from agent_runtime.backend import WorkloadSpec
from agent_runtime.failpoints import SimulatedCrash
from agent_runtime.pod import pod_manifest
from scripts.k8s import ROOT, kubectl

pytestmark = pytest.mark.k8s


@pytest.fixture
def live(db):
    env = start(db)
    try:
        yield env
    finally:
        stop(env)


def submit(live, behavior="sleep", **data):
    response = create(live, input={"behavior": behavior, **data})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def launched(live, behavior="sleep", **data):
    execution_id = submit(live, behavior, **data)
    reconcile(live)
    assert row(live, execution_id)["status"] == "RUNNING"
    return execution_id


def success(live, execution_id):
    def check():
        record = row(live, execution_id)
        assert record["status"] not in {"FAILED", "TIMED_OUT", "CANCELLED"}, dict(record)
        return record if record["status"] == "SUCCEEDED" else None

    return poll(check)


def absent(execution_id):
    poll(lambda: admin_pod(f"exec-{execution_id}") is None)


def active(name):
    pod = admin_pod(name)
    return (
        pod
        if pod
        and pod.get("status", {}).get("containerStatuses", [{}])[0].get("state", {}).get("running")
        else None
    )


def injected(live, execution_id=None, behavior="sleep", **data):
    execution_id = execution_id or str(uuid4())
    spec = WorkloadSpec(
        execution_id, "synthetic-probe", live.clock.now(), {}, {"behavior": behavior, **data}, 120
    )
    return pod_manifest(f"exec-{execution_id}", spec, live.settings)


def test_HAPPY_k8s_round_trip(live):
    payload = '$(EXECUTION_ID) $$ "quotes" é\nnext line'
    execution_id = launched(live, "complete", payload=payload)
    result = success(live, execution_id)
    assert result["result"] == {"echo": payload}
    absent(execution_id)
    reconcile(live)
    assert get(live, execution_id).json()["result"] == {"echo": payload}


@pytest.mark.parametrize("fault", ["lost_response", "after_create"])
def test_LC_5_LC_3_adoption_keeps_completion_credential(live, fault):
    execution_id = submit(live, "complete", payload="adopted")
    if fault == "lost_response":
        live.transport.lose_create = True
        reconcile(live)
    else:
        live.failpoints.arm("after_create")
        with pytest.raises(SimulatedCrash):
            reconcile(live)
    pod = admin_pod(f"exec-{execution_id}")
    assert pod is not None
    assert row(live, execution_id)["status"] == "PROVISIONING"
    assert len(live.backend.list_owned()) == 1
    restart(live)
    reconcile(live)
    record = success(live, execution_id)
    assert record["workload_uid"] == pod["metadata"]["uid"]
    assert record["result"] == {"echo": "adopted"}
    assert live.transport.create_calls == 1
    absent(execution_id)


def test_LC_3_after_claim_restart_never_creates(live):
    execution_id = submit(live)
    live.failpoints.arm("after_claim")
    with pytest.raises(SimulatedCrash):
        reconcile(live)
    restart(live)
    reconcile(live)
    assert row(live, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    assert live.transport.create_calls == 0
    assert admin_pod(f"exec-{execution_id}") is None


def test_LC_3_lost_removed_and_late_visible_pod(live):
    execution_id = submit(live)
    name = f"exec-{execution_id}"
    live.transport.lose_create = True
    reconcile(live)
    delete_pod(name)
    restart(live)
    reconcile(live)
    assert row(live, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    for _ in range(3):
        reconcile(live)
    assert live.transport.create_calls == 1 and admin_pod(name) is None
    response = admin_create(injected(live, execution_id))
    assert response.returncode == 0, response.stderr
    reconcile(live)
    absent(execution_id)
    assert row(live, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"


@pytest.mark.parametrize("replacement", [False, True])
def test_LC_6_external_delete_and_uid_identity(live, replacement):
    execution_id = launched(live)
    name = f"exec-{execution_id}"
    poll(lambda: active(name))
    original_uid = row(live, execution_id)["workload_uid"]
    if replacement:
        delete_pod(name)
        response = admin_create(injected(live, execution_id))
        assert response.returncode == 0, response.stderr
        assert json.loads(response.stdout)["metadata"]["uid"] != original_uid
    else:
        kubectl("delete", "pod", name, "-n", "agent-exec", "--wait=false")
        pod = admin_pod(name)
        if pod:
            assert pod["metadata"].get("deletionTimestamp")
            assert live.backend.get(name).phase == "LOST"
    reconcile(live)
    assert row(live, execution_id)["failure_reason"] == "POD_LOST"
    assert row(live, execution_id)["workload_uid"] == original_uid
    absent(execution_id)
    reconcile(live)
    assert live.transport.create_calls == 1


@pytest.mark.parametrize("outage", ["transport", "rbac"])
def test_LC_7_outage_keeps_pending_and_allows_database_transitions(live, outage):
    cancelled = launched(live)
    timed = launched(live)
    waiting = submit(live)
    before = (live.transport.create_calls, live.transport.delete_calls)
    if outage == "transport":
        live.transport.offline = True
    else:
        kubectl("delete", "rolebinding", "workload-controller", "-n", "agent-exec")
    try:
        reconcile(live)
        assert row(live, timed)["status"] == "RUNNING"
        assert row(live, waiting)["status"] == "PENDING"
        assert cancel(live, cancelled).status_code == 200
        # Advance only this execution's persisted deadline without moving the wall clock.
        from sqlalchemy import update

        with live.db.engine.begin() as conn:
            e = live.db.executions
            record = row(live, timed)
            conn.execute(
                update(e)
                .where(e.c.tenant_id == record["tenant_id"], e.c.id == timed)
                .values(deadline_at=live.clock.now())
            )
        reconcile(live)
        assert row(live, timed)["status"] == "TIMED_OUT"
        assert row(live, cancelled)["status"] == "CANCELLED"
        assert (live.transport.create_calls, live.transport.delete_calls) == before
    finally:
        live.transport.offline = False
        if outage == "rbac":
            kubectl("apply", "-f", str(ROOT / "deploy/k8s/resources.yaml"))
    reconcile(live)
    assert row(live, waiting)["status"] == "RUNNING"
    absent(cancelled)
    absent(timed)


def test_LC_8_kubelet_deadline_without_reconciler(live):
    response = create(live, timeout_seconds=30, input={"behavior": "sleep"})
    execution_id = response.json()["id"]
    reconcile(live)
    name = f"exec-{execution_id}"
    pod = poll(
        lambda: (
            p if (p := evidence(name)) and p.get("status", {}).get("phase") == "Failed" else None
        ),
        60,
    )
    assert pod["status"]["reason"] == "DeadlineExceeded"
    assert row(live, execution_id)["status"] == "RUNNING"
    restart(live)
    reconcile(live)
    assert row(live, execution_id)["status"] == "TIMED_OUT"
    absent(execution_id)


def test_LC_9_exit_zero_without_completion(live):
    execution_id = launched(live, "exit", code=0)
    name = f"exec-{execution_id}"
    poll(
        lambda: (
            p if (p := evidence(name)) and p.get("status", {}).get("phase") == "Succeeded" else None
        )
    )
    reconcile(live)
    assert row(live, execution_id)["failure_reason"] == "EXITED_WITHOUT_COMPLETION"
    absent(execution_id)


def test_LC_9_lost_completion_response_replays_committed_result(live):
    live.failpoints.arm("before_cleanup")
    execution_id = launched(live, "complete", payload="retry-same-result")
    committed = dict(success(live, execution_id))
    # With reconciliation stopped, only the agent's successful retry can request cleanup.
    absent(execution_id)
    assert dict(row(live, execution_id)) == committed
    assert committed["result"] == {"echo": "retry-same-result"}
    reconcile(live)
    assert dict(row(live, execution_id)) == committed


def test_LC_11_labels_database_failure_and_uid_precondition(live, monkeypatch):
    manifests = [injected(live) for _ in range(3)]
    manifests[0]["metadata"]["labels"] = {}
    manifests[1]["metadata"]["labels"]["owner"] = "other"
    for manifest in manifests:
        response = admin_create(manifest)
        assert response.returncode == 0, response.stderr
    names = [m["metadata"]["name"] for m in manifests]
    original_connect = live.db.engine.connect

    def unavailable():
        raise OperationalError("connection", {}, Exception("offline"))

    monkeypatch.setattr(live.db.engine, "connect", unavailable)
    with pytest.raises(OperationalError):
        reconcile(live)
    assert live.transport.delete_calls == 0
    assert all(admin_pod(n) is not None for n in names)
    monkeypatch.setattr(live.db.engine, "connect", original_connect)
    reconcile(live)
    poll(lambda: admin_pod(names[2]) is None)
    assert all(admin_pod(n) is not None for n in names[:2])
    response = admin_create(manifests[2])
    assert response.returncode == 0
    old_uid = json.loads(response.stdout)["metadata"]["uid"]
    delete_pod(names[2])
    response = admin_create(manifests[2])
    assert response.returncode == 0
    new_uid = json.loads(response.stdout)["metadata"]["uid"]
    assert old_uid != new_uid
    original_request = live.backend.request
    conflicts = []

    def observe_conflict(method, path, **kwargs):
        response = original_request(method, path, **kwargs)
        if method == "DELETE" and response.status_code == 409:
            conflicts.append(response.json())
            directory = ROOT / ".local/k8s/evidence"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "LC-11-uid-conflict.json").write_text(
                json.dumps(response.json(), indent=2)
            )
        return response

    monkeypatch.setattr(live.backend, "request", observe_conflict)
    live.backend.delete(names[2], old_uid)
    assert conflicts
    assert admin_pod(names[2])["metadata"]["uid"] == new_uid
    live.backend.delete(names[2], new_uid)
    poll(lambda: admin_pod(names[2]) is None)


def test_ISO_1_ISO_5_ID_8_intended_credentials_and_template(live):
    import errno
    import subprocess

    execution_id = submit(live, "inspect")
    live.failpoints.arm("before_cleanup")
    reconcile(live)
    name = f"exec-{execution_id}"
    pod = admin_pod(name)
    spec = pod["spec"]
    container = spec["containers"][0]
    intended = {"EXECUTION_ID", "EXECUTION_INPUT_B64", "EXECUTION_CREDENTIAL", "RUNTIME_URL"}
    assert {e["name"] for e in container["env"]} == intended
    assert not container.get("envFrom")
    assert {v["name"] for v in spec["volumes"]} == {"tmp", "agent-cell"}

    def no_secret_references(value):
        if isinstance(value, dict):
            assert not {"secret", "secretName", "secretKeyRef", "secretRef"}.intersection(value)
            for item in value.values():
                no_secret_references(item)
        elif isinstance(value, list):
            for item in value:
                no_secret_references(item)

    no_secret_references(spec)
    projection = next(v for v in spec["volumes"] if v["name"] == "agent-cell")
    assert projection["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"] == 600
    result = success(live, execution_id)["result"]
    assert result["default_token_directory"] is False
    assert result["aud"] == ["agent-cell-gateway"]
    assert isinstance(result["exp"], (int, float))
    assert result["sub"] == "system:serviceaccount:agent-exec:agent-exec"
    assert result["pod_uid"] == pod["metadata"]["uid"]
    assert result["pod_name"] == name
    assert result["api_status"] == 401
    assert result["uid"] == result["gid"] == 65532
    assert int(result["cap_eff"], 16) == 0
    assert result["rootfs_errno"] == errno.EROFS
    assert "/rootfs-probe" not in result["mount_points"]
    assert "/var/run/secrets/kubernetes.io/serviceaccount" not in result["mount_points"]
    assert "/run/secrets/kubernetes.io/serviceaccount" not in result["mount_points"]
    image = json.loads(
        subprocess.check_output(
            ["docker", "--context", "colima", "image", "inspect", live.settings.agent_image],
            text=True,
        )
    )[0]
    baseline = {e.split("=", 1)[0] for e in image["Config"]["Env"]}
    runtime = {"HOME", "HOSTNAME"}
    kubelet = {
        "KUBERNETES_SERVICE_HOST",
        "KUBERNETES_SERVICE_PORT",
        "KUBERNETES_SERVICE_PORT_HTTPS",
        "KUBERNETES_PORT",
        "KUBERNETES_PORT_443_TCP",
        "KUBERNETES_PORT_443_TCP_ADDR",
        "KUBERNETES_PORT_443_TCP_PORT",
        "KUBERNETES_PORT_443_TCP_PROTO",
    }
    assert set(result["env_names"]) == intended | baseline | runtime | kubelet
    directory = ROOT / ".local/k8s/evidence"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ISO-1-ID-8.json").write_text(json.dumps(result, indent=2))
    permissions = kubectl(
        "auth",
        "can-i",
        "--list",
        "--as=system:serviceaccount:agent-exec:agent-exec",
        "-n",
        "agent-exec",
    )
    (directory / "ISO-1-permissions.txt").write_text(permissions)
    for line in permissions.splitlines()[1:]:
        fields = line.split()
        if not fields:
            continue
        if fields[0] != "":
            assert fields[0] in {
                "selfsubjectreviews.authentication.k8s.io",
                "selfsubjectaccessreviews.authorization.k8s.io",
                "selfsubjectrulesreviews.authorization.k8s.io",
                "[/.well-known/openid-configuration]",
                "[/.well-known/openid-configuration/]",
                "[/openid/v1/jwks]",
                "[/openid/v1/jwks/]",
                "[/api]",
                "[/api/*]",
                "[/apis]",
                "[/apis/*]",
                "[/healthz]",
                "[/livez]",
                "[/openapi]",
                "[/openapi/*]",
                "[/readyz]",
                "[/version]",
                "[/version/]",
            }, line
            assert fields[-1] == ("[get]" if fields[0].startswith("[/") else "[create]"), line
    absent(execution_id)


def test_ISO_5_read_only_root_positive_control(live):
    execution_id = submit(live, "inspect")
    # Claim and create through the runtime, but change only the root-filesystem control
    # in the real outbound manifest as the trusted admin test harness.
    original = live.backend.create

    def writable(name, spec):
        manifest = pod_manifest(name, spec, live.settings)
        manifest["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
        response = admin_create(manifest)
        assert response.returncode == 0, response.stderr
        return json.loads(response.stdout)["metadata"]["uid"]

    live.backend.create = writable
    try:
        reconcile(live)
    finally:
        live.backend.create = original
    result = success(live, execution_id)["result"]
    assert result["rootfs_errno"] == 0
    assert result["rootfs_readback"] == "probe"
    absent(execution_id)


def test_ISO_5_admission_and_positive_controls(live):
    from copy import deepcopy

    base = injected(live)
    response = live.backend.request("POST", live.backend.path, params={"dryRun": "All"}, json=base)
    assert response.status_code == 201
    namespace = f"agent-psa-control-{uuid4().hex[:10]}"
    kubectl("create", "namespace", namespace)
    try:
        kubectl("create", "serviceaccount", "agent-exec", "-n", namespace)
        cases = []
        for change in (
            "privileged",
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "hostPath",
            "hostPort",
            "NET_ADMIN",
            "runAsUser",
        ):
            manifest = deepcopy(base)
            spec = manifest["spec"]
            container = spec["containers"][0]
            if change == "privileged":
                container["securityContext"].update(privileged=True, allowPrivilegeEscalation=True)
            elif change in {"hostNetwork", "hostPID", "hostIPC"}:
                spec[change] = True
            elif change == "hostPath":
                spec["volumes"].append({"name": "host", "hostPath": {"path": "/tmp"}})
            elif change == "hostPort":
                container["ports"] = [{"containerPort": 8080, "hostPort": 8080}]
            elif change == "NET_ADMIN":
                container["securityContext"]["capabilities"]["add"] = ["NET_ADMIN"]
            else:
                spec["securityContext"]["runAsUser"] = 0
            response = live.backend.request(
                "POST", live.backend.path, params={"dryRun": "All"}, json=manifest
            )
            assert response.status_code == 403, (change, response.status_code)
            message = response.json()["message"]
            assert "PodSecurity" in message and change.lower() in message.lower(), message
            manifest["metadata"]["namespace"] = namespace
            control = admin_create(manifest, dry_run=True)
            if control.returncode:
                assert "Forbidden" in control.stderr, control.stderr
                cases.append(
                    {
                        "case": change,
                        "positive_control": "unavailable: blocked by another control",
                        "message": control.stderr,
                    }
                )
            else:
                cases.append({"case": change, "positive_control": "passed"})
        directory = ROOT / ".local/k8s/evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ISO-5-admission.json").write_text(json.dumps(cases, indent=2))
        assert not json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json"))["items"]
        assert not live.backend.list_owned()
    finally:
        kubectl("delete", "namespace", namespace, "--wait=true", "--timeout=60s")


def test_ISO_7_capacity_and_quota_backstop(live):
    from agent_runtime.backend import CreateRejected

    live.app.state.reconciler.max_active_executions = 2
    first, second = launched(live), launched(live)
    third = submit(live)
    reconcile(live)
    assert row(live, third)["status"] == "PENDING"
    assert live.transport.create_calls == 2
    assert cancel(live, first).status_code == 200
    absent(first)
    reconcile(live)
    assert row(live, second)["status"] == row(live, third)["status"] == "RUNNING"
    for _ in range(2):
        response = admin_create(injected(live))
        assert response.returncode == 0, response.stderr
    assert len(live.backend.list_owned()) == 4
    execution_id = str(uuid4())
    spec = WorkloadSpec(
        execution_id, "synthetic-probe", live.clock.now(), {}, {"behavior": "sleep"}, 120
    )
    with pytest.raises(CreateRejected, match="quota"):
        live.backend.create(f"exec-{execution_id}", spec)
    assert len(live.backend.list_owned()) == 4


def test_ISO_1_controller_RBAC_real_calls_and_reviews(live):
    response = live.backend.request("GET", live.backend.path + "/missing-probe")
    assert response.status_code in {200, 404}
    response = live.backend.request("GET", "/api/v1/namespaces/agent-exec/secrets/missing-probe")
    assert response.status_code == 403
    manifest = injected(live)
    manifest["metadata"]["namespace"] = "rook-dev"
    response = live.backend.request(
        "POST", "/api/v1/namespaces/rook-dev/pods", params={"dryRun": "All"}, json=manifest
    )
    assert response.status_code == 403
    assert 'cannot create resource "pods"' in response.json()["message"]
    cases = [(verb, "pods", "agent-exec", True) for verb in ("create", "get", "list", "delete")]
    cases += [
        (verb, resource, namespace, False)
        for verb, resource, namespace in (
            ("get", "secrets", "agent-exec"),
            ("create", "pods/exec", "agent-exec"),
            ("get", "pods/log", "agent-exec"),
            ("update", "pods", "agent-exec"),
            ("patch", "pods", "agent-exec"),
            ("get", "pods", "agent-runtime"),
            ("create", "pods", "default"),
            ("create", "pods", "rook-dev"),
            ("get", "nodes", ""),
            ("list", "namespaces", ""),
        )
    ]
    for verb, resource, namespace, allowed in cases:
        resource, _, subresource = resource.partition("/")
        attributes = {"verb": verb, "resource": resource, "namespace": namespace}
        if subresource:
            attributes["subresource"] = subresource
        response = live.backend.request(
            "POST",
            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            json={
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": attributes},
            },
        )
        assert response.status_code == 201
        assert response.json()["status"]["allowed"] is allowed, attributes
