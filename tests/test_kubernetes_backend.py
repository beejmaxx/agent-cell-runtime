import base64
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_runtime.backend import (
    BackendUnavailable,
    CreateOutcomeUnknown,
    CreateRejected,
    WorkloadSpec,
)
from agent_runtime.config import Settings
from agent_runtime.kubernetes import KubernetesBackend
from agent_runtime.pod import pod_manifest


@pytest.fixture
def spec():
    return WorkloadSpec(
        str(uuid4()),
        "synthetic-credential",
        datetime(2030, 1, 1, tzinfo=UTC),
        {},
        {"behavior": "complete", "payload": '$(EXECUTION_ID) $$ é\n"'},
        37,
    )


@pytest.fixture
def settings(tmp_path):
    token = tmp_path / "token"
    token.write_text("first-token")
    return Settings("unused", k8s_api_url="https://kubernetes.test", k8s_token_file=str(token))


def valid_pod(spec, settings):
    pod = pod_manifest(f"exec-{spec.execution_id}", spec, settings)
    pod["metadata"]["uid"] = "pod-uid"
    pod["status"] = {"phase": "Running"}
    return pod


def backend(settings, handler):
    return KubernetesBackend(
        settings,
        httpx.Client(
            base_url=settings.k8s_api_url, transport=httpx.MockTransport(handler), trust_env=False
        ),
    )


@pytest.mark.parametrize(
    "status", [201, 400, 401, 403, 404, 422, 409, 429, 500, 502, 200, 202, 302, 418]
)
def test_LC_3_create_outcome_matrix(settings, spec, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=valid_pod(spec, settings))

    b = backend(settings, respond)
    name = f"exec-{spec.execution_id}"
    if status == 201:
        assert b.create(name, spec) == "pod-uid"
    else:
        error = CreateRejected if status in {400, 401, 403, 404, 422} else CreateOutcomeUnknown
        with pytest.raises(error):
            b.create(name, spec)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert "resourceVersion" not in requests[0].url.params


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_LC_3_LC_7_transport_failure(settings, spec, failure):
    calls = []

    def fail(request):
        calls.append(request)
        raise failure("synthetic outage", request=request)

    b = backend(settings, fail)
    name = f"exec-{spec.execution_id}"
    for call, error in (
        (lambda: b.create(name, spec), CreateOutcomeUnknown),
        (lambda: b.get(name), BackendUnavailable),
        (b.list_owned, BackendUnavailable),
        (lambda: b.delete(name, "uid"), BackendUnavailable),
    ):
        before = len(calls)
        with pytest.raises(error):
            call()
        assert len(calls) == before + 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "other"),
        ("name", "exec-other"),
        ("uid", ""),
        ("uid", None),
        ("labels", {}),
        ("labels", {"owner": "other"}),
        ("labels", None),
    ],
)
def test_LC_3_LC_7_malformed_pod_is_not_success(settings, spec, field, value):
    pod = valid_pod(spec, settings)
    pod["metadata"][field] = value
    b = backend(
        settings, lambda request: httpx.Response(201 if request.method == "POST" else 200, json=pod)
    )
    with pytest.raises(CreateOutcomeUnknown):
        b.create(f"exec-{spec.execution_id}", spec)
    with pytest.raises(BackendUnavailable):
        b.get(f"exec-{spec.execution_id}")


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("Pending", "RUNNING"),
        ("Running", "RUNNING"),
        ("Unknown", "RUNNING"),
        ("Succeeded", "SUCCEEDED"),
        ("Failed", "FAILED"),
    ],
)
def test_LC_6_LC_7_phase_and_deletion_observations(settings, spec, phase, expected):
    pod = valid_pod(spec, settings)
    pod["status"]["phase"] = phase
    b = backend(settings, lambda r: httpx.Response(200, json=pod))
    assert b.get(f"exec-{spec.execution_id}").phase == expected
    pod["metadata"]["deletionTimestamp"] = "2030-01-01T00:00:00Z"
    assert b.get(f"exec-{spec.execution_id}").phase == "LOST"


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500, 201])
def test_LC_7_unexpected_read_status(settings, spec, status):
    b = backend(settings, lambda r: httpx.Response(status, json=valid_pod(spec, settings)))
    with pytest.raises(BackendUnavailable):
        b.get(f"exec-{spec.execution_id}")
    with pytest.raises(BackendUnavailable):
        b.list_owned()


def test_LC_7_only_authoritative_not_found_is_absence(settings, spec):
    name = f"exec-{spec.execution_id}"
    document = {"kind": "Status", "reason": "NotFound", "details": {"kind": "pods", "name": name}}
    b = backend(settings, lambda r: httpx.Response(404, json=document))
    assert b.get(name) is None
    b.delete(name, "uid")
    for bad in (
        {},
        {**document, "kind": "Other"},
        {**document, "reason": "Forbidden"},
        {**document, "details": {"kind": "namespaces", "name": name}},
        {**document, "details": {"kind": "pods", "name": "other"}},
    ):
        b = backend(settings, lambda r, bad=bad: httpx.Response(404, json=bad))
        with pytest.raises(BackendUnavailable):
            b.get(name)
        with pytest.raises(BackendUnavailable):
            b.delete(name, "uid")


def test_LC_11_list_filters_invalid_pods_and_uses_most_recent_reads(settings, spec):
    good = valid_pod(spec, settings)
    bad = copy.deepcopy(good)
    bad["metadata"]["namespace"] = "other"

    def respond(request):
        assert dict(request.url.params) == {"labelSelector": "owner=agent-runtime"}
        return httpx.Response(200, json={"items": [None, {}, bad, good]})

    assert backend(settings, respond).list_owned()[0].uid == "pod-uid"
    for data in ({}, {"items": None}):
        with pytest.raises(BackendUnavailable):
            backend(settings, lambda r, data=data: httpx.Response(200, json=data)).list_owned()


@pytest.mark.parametrize("status", [200, 202, 409, 400, 401, 403, 422, 429, 500])
def test_LC_11_delete_uid_precondition(settings, spec, status):
    def respond(request):
        assert json.loads(request.content)["preconditions"] == {"uid": "observed"}
        assert "resourceVersion" not in request.url.params
        return httpx.Response(
            status,
            json={
                "kind": "Status",
                "reason": "Conflict",
                "details": {"kind": "pods", "name": f"exec-{spec.execution_id}"},
                "message": "Precondition failed: UID in precondition: observed, UID in object meta: replacement",
            },
        )

    b = backend(settings, respond)
    if status in {200, 202, 409}:
        b.delete(f"exec-{spec.execution_id}", "observed")
    else:
        with pytest.raises(BackendUnavailable):
            b.delete(f"exec-{spec.execution_id}", "observed")


def test_LC_7_malformed_response_bodies(settings, spec):
    def respond(request):
        return httpx.Response(201 if request.method == "POST" else 200, text="not-json")

    b = backend(settings, respond)
    with pytest.raises(CreateOutcomeUnknown):
        b.create(f"exec-{spec.execution_id}", spec)
    with pytest.raises(BackendUnavailable):
        b.get(f"exec-{spec.execution_id}")
    with pytest.raises(BackendUnavailable):
        b.list_owned()


def test_LC_7_token_refresh_without_restart(settings, spec):
    tokens = []

    def respond(request):
        tokens.append(request.headers["Authorization"])
        return httpx.Response(200, json=valid_pod(spec, settings))

    b = backend(settings, respond)
    b.get(f"exec-{spec.execution_id}")
    Path(settings.k8s_token_file).write_text("refreshed")
    b.get(f"exec-{spec.execution_id}")
    assert tokens == ["Bearer first-token", "Bearer refreshed"]


def test_ISO_1_ISO_5_ISO_7_ID_8_fixed_pod_template(settings, spec):
    pod = pod_manifest(f"exec-{spec.execution_id}", spec, settings)
    assert pod["metadata"] == {
        "name": f"exec-{spec.execution_id}",
        "namespace": "agent-exec",
        "labels": {"owner": "agent-runtime", "execution_id": spec.execution_id},
    }
    expected = json.loads((Path(__file__).parent / "pod-template.json").read_text())
    expected["metadata"] = pod["metadata"]
    expected["spec"]["containers"][0]["env"] = pod["spec"]["containers"][0]["env"]
    assert pod == expected
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert set(env) == {
        "EXECUTION_ID",
        "EXECUTION_INPUT_B64",
        "EXECUTION_CREDENTIAL",
        "RUNTIME_URL",
    }
    assert (
        env["EXECUTION_ID"] == spec.execution_id and env["EXECUTION_CREDENTIAL"] == spec.credential
    )
    assert env["RUNTIME_URL"] == settings.runtime_url
    assert json.loads(base64.b64decode(env["EXECUTION_INPUT_B64"])) == spec.input
    changed_spec = copy.deepcopy(spec)
    changed_spec.input.update(
        {
            "image": "evil",
            "envFrom": [{"secretRef": {"name": "evil"}}],
            "hostNetwork": True,
            "volumes": [{"hostPath": {"path": "/"}}],
        }
    )
    changed = pod_manifest(f"exec-{spec.execution_id}", changed_spec, settings)
    changed["spec"]["containers"][0]["env"] = pod["spec"]["containers"][0]["env"]
    assert changed == pod


def test_LC_11_unrecognized_conflict_is_unavailable(settings, spec):
    b = backend(
        settings, lambda r: httpx.Response(409, json={"kind": "Status", "reason": "Conflict"})
    )
    with pytest.raises(BackendUnavailable):
        b.delete(f"exec-{spec.execution_id}", "observed")
