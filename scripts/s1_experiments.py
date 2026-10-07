"""Explicit S1 observations on the trusted host; never collected by make test.

Run with K8S_PROFILE=eks uv run pytest scripts/s1_experiments.py -s.
Operator mutations are performed separately and recorded alongside these results.
"""

import json
import os
import statistics
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

from scripts.k8s import STATE

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
    key = launched(live, "probe", **config["input"])
    results = observe(live, [key], config["label"])
    assert results[key]["status"] == "SUCCEEDED"
