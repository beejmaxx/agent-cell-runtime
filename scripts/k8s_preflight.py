import hmac
import json
import secrets
import ssl
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
from k8s import IMAGE, ROOT, guard, kubectl, require_owned

from agent_runtime.pod import pod_manifest


def run():
    cluster = guard()
    require_owned()
    version = json.loads(kubectl("version", "--client", "-o", "json"))["clientVersion"][
        "gitVersion"
    ]
    assert version.startswith("v1.35."), version
    for resource, namespace, name in (
        ("serviceaccount", "agent-runtime", "agent-runtime-controller"),
        ("serviceaccount", "agent-exec", "agent-exec"),
        ("role", "agent-exec", "workload-controller"),
        ("rolebinding", "agent-exec", "workload-controller"),
        ("resourcequota", "agent-exec", "executions"),
    ):
        kubectl("get", resource, name, "-n", namespace, "-o", "name")
    subprocess.run(
        ["docker", "--context", "colima", "image", "inspect", IMAGE],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    evidence = ROOT / ".local/k8s/preflight"
    evidence.mkdir(parents=True, exist_ok=True)
    results, credentials = {}, {}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            execution_id = self.path.split("/")[-2]
            expected = credentials.get(execution_id)
            if expected is None or not hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Execution {expected}"
            ):
                self.send_error(401)
                return
            results[execution_id] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = ROOT / ".local/k8s"
    client = httpx.Client(
        base_url=cluster["server"],
        trust_env=False,
        verify=ssl.create_default_context(cafile=str(state / "ca.crt")),
        headers={"Authorization": "Bearer " + (state / "controller.token").read_text().strip()},
        timeout=httpx.Timeout(5, connect=2),
    )
    settings = SimpleNamespace(
        k8s_namespace="agent-exec",
        agent_image=IMAGE,
        runtime_url=f"http://192.168.5.2:{server.server_port}",
    )
    pods = []
    path = "/api/v1/namespaces/agent-exec/pods"

    def save(name, pod):
        sanitized = {
            "metadata": {k: pod["metadata"].get(k) for k in ("name", "uid", "creationTimestamp")},
            "status": pod.get("status", {}),
        }
        (evidence / f"{name}-status.json").write_text(json.dumps(sanitized, indent=2))

    def launch(behavior, disk_mib=None, volume_limit=None):
        execution_id = str(uuid4())
        credential = credentials[execution_id] = secrets.token_urlsafe(32)
        spec = SimpleNamespace(
            execution_id=execution_id,
            credential=credential,
            active_deadline_seconds=240,
            input={"behavior": behavior},
        )
        name = f"exec-{execution_id}"
        manifest = pod_manifest(name, spec, settings)
        if volume_limit:
            manifest["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] = volume_limit
        if disk_mib:
            # Report a bounded, fsynced write to the probe receiver, then await kubelet eviction.
            manifest["spec"]["containers"][0]["command"] = [
                "python",
                "-c",
                f"""
import os, time
from fake_agent import complete
with open('/tmp/fill', 'wb') as output:
    for _ in range({disk_mib}):
        output.write(b'x' * (1024 * 1024))
    output.flush()
    os.fsync(output.fileno())
complete({{'written_bytes': os.stat('/tmp/fill').st_size}})
while True:
    time.sleep(1)
""",
            ]
        response = client.post(path, json=manifest)
        assert response.status_code == 201, f"Probe create failed: {response.status_code}"
        pod = response.json()
        pods.append((name, pod["metadata"]["uid"]))
        save(name, pod)
        return execution_id, name, pod["metadata"]["uid"]

    def observe(name):
        response = client.get(f"{path}/{name}")
        response.raise_for_status()
        pod = response.json()
        save(name, pod)
        return pod

    def await_result(execution_id, name):
        until = time.monotonic() + 60
        while time.monotonic() < until:
            if execution_id in results:
                result = results[execution_id]["result"]
                (evidence / f"{name}-result.json").write_text(json.dumps(result, indent=2))
                return result
            pod = observe(name)
            assert pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}, pod.get(
                "status"
            )
            time.sleep(0.5)
        raise AssertionError(f"No callback from {name}; Pod status saved under {evidence}")

    def delete(name, uid):
        response = client.request(
            "DELETE",
            f"{path}/{name}",
            json={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}},
        )
        assert response.status_code in {200, 202, 404}, response.status_code

    try:
        execution_id, name, uid = launch("inspect")
        result = await_result(execution_id, name)
        assert result["cpu_max"] == "25000 100000", result["cpu_max"]
        assert "nr_throttled" in result["cpu_stat"]
        assert result["aud"] == ["agent-cell-gateway"]
        assert result["pod_uid"] == uid
        assert result["pod_name"] == name
        assert isinstance(result["exp"], (int, float))
        assert result["sub"] == "system:serviceaccount:agent-exec:agent-exec"
        print(
            f"PASS: cpu.max/cpu.stat readable; 250m quota; projected audience, exp, and Pod UID; issued lifetime {result['token_lifetime']} s",
            flush=True,
        )
        delete(name, uid)
        for mib, volume_limit, mechanism in (
            (48, None, "emptydir"),
            (80, "96Mi", "ephemeral local storage"),
        ):
            execution_id, name, uid = launch("fill_disk", disk_mib=mib, volume_limit=volume_limit)
            result = await_result(execution_id, name)
            assert result["written_bytes"] == mib * 1024 * 1024
            print(f"Storage probe: fsynced {mib} MiB; waiting for {mechanism} eviction", flush=True)
            until = time.monotonic() + 180
            while time.monotonic() < until:
                pod = observe(name)
                status = pod.get("status", {})
                if status.get("phase") == "Failed":
                    assert status.get("reason") == "Evicted", status
                    assert mechanism in status.get("message", "").lower(), status
                    print("PASS: " + status["message"], flush=True)
                    break
                time.sleep(1)
            else:
                raise AssertionError(
                    f"{mechanism} limit not enforced within 180 s after the confirmed write"
                )
            delete(name, uid)
        node = json.loads(kubectl("get", "node", "colima", "-o", "json"))
        conditions = node["status"]["conditions"]
        (evidence / "node-conditions.json").write_text(json.dumps(conditions, indent=2))
        assert next(c["status"] for c in conditions if c["type"] == "DiskPressure") == "False"
    finally:
        for name, uid in pods:
            delete(name, uid)
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    run()
