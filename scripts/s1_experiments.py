"""Explicit S1 observations on the trusted host; never collected by make test.

Run with K8S_PROFILE=eks uv run pytest scripts/s1_experiments.py -s.
Operator mutations are performed separately and recorded alongside these results.
"""

import json
import os
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import cancel, database_url, db, reconcile, row  # noqa: F401
from k8s_support import admin_pod, evidence, poll
from test_k8s import launched
from test_k8s import live as live_fixture

from scripts.k8s import STATE, kubectl

live = live_fixture

pytestmark = pytest.mark.skipif(
    os.getenv("K8S_PROFILE") != "eks", reason="S1 experiments require the trusted EKS host"
)


def save(name, value):
    directory = STATE / "evidence/experiments"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps({"recorded_at": datetime.now(UTC).isoformat(), **value}, indent=2, default=str)
    )


def observe(live, ids, label):
    until = monotonic() + 600
    records = {key: {"execution_id": key} for key in ids}
    try:
        while monotonic() < until:
            for key in ids:
                pod = evidence(f"exec-{key}")
                item = records[key]
                if pod:
                    item["pod_uid"] = pod["metadata"]["uid"]
                    item["phase"] = pod.get("status", {}).get("phase")
                    if item["phase"] in {"Failed", "Succeeded"} and "stdout" not in item:
                        try:
                            item["stdout"] = kubectl("logs", f"exec-{key}", "-n", "agent-exec")
                        except subprocess.SubprocessError as exc:
                            item["stdout_error"] = type(exc).__name__
                    item["capacity"] = (
                        pod["metadata"].get("annotations", {}).get("CapacityProvisioned")
                    )
                    statuses = pod.get("status", {}).get("containerStatuses", [])
                    for container in statuses:
                        started = container.get("state", {}).get("running", {}).get("startedAt")
                        started = started or container.get("state", {}).get("terminated", {}).get(
                            "startedAt"
                        )
                        if started:
                            item["container_started_at"] = started
                current = row(live, key)
                item.update(
                    status=current["status"],
                    created_at=current["created_at"],
                    first_callback_at=current["finished_at"],
                    result=current["result"],
                    failure_reason=current["failure_reason"],
                )
            if all(
                v["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}
                for v in records.values()
            ):
                return records
            reconcile(live)
            sleep(1)
        raise AssertionError(f"{label}: observations did not complete within execution deadline")
    finally:
        save(label, {"executions": list(records.values())})


def test_S1_E2_cold_start(live):
    all_rows = []
    for index in range(5):
        key = launched(live, "complete", payload="s1-sequential")
        data = observe(live, [key], f"E2-sequential-{index + 1}")
        assert data[key]["status"] == "SUCCEEDED"
        all_rows.extend(data.values())
        poll(lambda key=key: admin_pod(f"exec-{key}") is None, 120)
    keys = [launched(live, "complete", payload="s1-concurrent") for _ in range(3)]
    concurrent = observe(live, keys, "E2-concurrent")
    assert all(v["status"] == "SUCCEEDED" for v in concurrent.values())
    all_rows.extend(concurrent.values())
    timings = [(v["first_callback_at"] - v["created_at"]).total_seconds() for v in all_rows]
    save(
        "E2-summary",
        {
            "executions": all_rows,
            "callback_p50_seconds": statistics.median(timings),
            "callback_max_seconds": max(timings),
        },
    )


def test_S1_E3_E7_kernel_and_identity(live):
    keys = [launched(live, "inspect", hold_seconds=10) for _ in range(2)]
    results = observe(live, keys, "E3-E7-inspect")
    assert all(v["status"] == "SUCCEEDED" for v in results.values())
    assert len({v["result"]["boot_id"] for v in results.values()}) == 2
    for v in results.values():
        assert v["result"]["aud"] == ["agent-cell-gateway"]
        assert not v["result"]["default_token_directory"]


def test_S1_E10_completion_binding(live):
    other = launched(live, "sleep")
    key = launched(live, "binding", other_execution=other)
    results = observe(live, [key], "E10-binding")
    assert results[key]["status"] == "SUCCEEDED"
    assert results[key]["result"]["cross_execution"]["status"] in {401, 403}
    assert row(live, other)["status"] == "RUNNING"
    save("E10-other", {"execution_id": other, "status": "RUNNING"})
    cancel(live, other)


def test_S1_E4_E5_E6_E7_probes(live):
    config = json.loads((STATE / "probe-targets.json").read_text())
    if config.get("peer_control"):
        peer = launched(live, "listen")
        pod = poll(lambda: _running_peer(peer), 300)
        positive = kubectl(
            "exec",
            f"exec-{peer}",
            "-n",
            "agent-exec",
            "--",
            "python",
            "-c",
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080').read().decode())",
        )
        save(
            config["label"] + "-peer-control",
            {"pod_ip": pod["status"]["podIP"], "local_listener_response": positive},
        )
        config["input"].setdefault("targets", []).append(
            {
                "name": "peer execution listener",
                "host": pod["status"]["podIP"],
                "port": 8080,
                "method": "GET",
            }
        )
    key = launched(live, "probe", **config["input"])
    results = observe(live, [key], config["label"])
    if results[key]["status"] != "SUCCEEDED":
        try:
            output = kubectl("logs", f"exec-{key}", "-n", "agent-exec")
            save(config["label"] + "-stdout", {"stdout": output})
        except subprocess.SubprocessError as exc:
            save(config["label"] + "-stdout", {"error_type": type(exc).__name__})
    assert results[key]["status"] == "SUCCEEDED"


def _running_peer(key):
    pod = admin_pod(f"exec-{key}")
    return pod if pod and pod.get("status", {}).get("phase") == "Running" else None


def test_S1_E8_admitted_privileges(live, monkeypatch):
    from k8s_support import admin_create
    from test_k8s import injected

    from agent_runtime import kubernetes

    trials = {
        "root": {"runAsUser": 0, "runAsNonRoot": False},
        "privileged": {"privileged": True},
        "escalation": {"allowPrivilegeEscalation": True},
        "net_raw": {"capabilities": {"drop": ["ALL"], "add": ["NET_RAW"]}},
        "unconfined": {"seccompProfile": {"type": "Unconfined"}},
        "writable_root_and_bind_service": {
            "readOnlyRootFilesystem": False,
            "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
        },
    }
    observations = {}
    for name, changes in trials.items():
        manifest = injected(live, behavior="inspect")
        manifest["spec"]["containers"][0]["securityContext"].update(changes)
        response = admin_create(manifest, dry_run=True)
        observations[name] = {"returncode": response.returncode, "stderr": response.stderr}
    save("E8-admission", {"trials": observations})
    assert observations["writable_root_and_bind_service"]["returncode"] == 0
    assert all(
        observations[k]["returncode"] != 0 for k in trials if k != "writable_root_and_bind_service"
    )
    original = kubernetes.pod_manifest

    def variant(*args):
        manifest = original(*args)
        manifest["spec"]["containers"][0]["securityContext"].update(
            trials["writable_root_and_bind_service"]
        )
        return manifest

    monkeypatch.setattr(kubernetes, "pod_manifest", variant)
    key = launched(live, "inspect")
    result = observe(live, [key], "E8-inspect")
    assert result[key]["status"] == "SUCCEEDED"
    config = json.loads((STATE / "probe-targets.json").read_text())
    key = launched(live, "probe", **config["input"])
    result = observe(live, [key], "E8-probes")
    assert result[key]["status"] == "SUCCEEDED"
