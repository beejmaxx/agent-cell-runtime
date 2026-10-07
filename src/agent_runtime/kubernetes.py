import ssl
from pathlib import Path

import httpx

from agent_runtime.backend import BackendUnavailable, CreateOutcomeUnknown, CreateRejected, Workload
from agent_runtime.pod import pod_manifest


class KubernetesBackend:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.k8s_api_url,
            trust_env=False,
            verify=ssl.create_default_context(cafile=settings.k8s_ca_file),
            timeout=httpx.Timeout(5, connect=2),
        )
        self.path = f"/api/v1/namespaces/{settings.k8s_namespace}/pods"

    def request(self, method, path, **kwargs):
        token = Path(self.settings.k8s_token_file).read_text().strip()
        return self.client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )

    def workload(self, data, name=None):
        metadata = data["metadata"]
        actual_name = metadata["name"]
        uid, labels = metadata["uid"], metadata["labels"]
        if (
            metadata["namespace"] != self.settings.k8s_namespace
            or not isinstance(actual_name, str)
            or not actual_name.startswith("exec-")
            or not actual_name[5:]
            or (name is not None and name != actual_name)
            or not isinstance(uid, str)
            or not uid
            or labels.get("owner") != "agent-runtime"
            or labels.get("execution_id") != actual_name[5:]
        ):
            raise ValueError("Invalid Pod identity")
        phase = {
            "Pending": "RUNNING",
            "Running": "RUNNING",
            "Unknown": "RUNNING",
            "Succeeded": "SUCCEEDED",
            "Failed": "FAILED",
        }[data.get("status", {}).get("phase", "Pending")]
        if metadata.get("deletionTimestamp"):
            phase = "LOST"
        return Workload(actual_name, uid, phase, labels)

    @staticmethod
    def not_found(response, name):
        try:
            data = response.json()
            return (
                response.status_code == 404
                and data["kind"] == "Status"
                and data["reason"] == "NotFound"
                and data["details"]["kind"] == "pods"
                and data["details"]["name"] == name
            )
        except (ValueError, KeyError, TypeError):
            return False

    def create(self, name, spec):
        try:
            response = self.request("POST", self.path, json=pod_manifest(name, spec, self.settings))
        except (httpx.HTTPError, OSError) as exc:
            raise CreateOutcomeUnknown() from exc
        if response.status_code in {400, 401, 403, 404, 422}:
            raise CreateRejected(response.text)
        try:
            if response.status_code != 201:
                raise ValueError("Unconfirmed create")
            return self.workload(response.json(), name).uid
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CreateOutcomeUnknown() from exc

    def get(self, name):
        try:
            response = self.request("GET", f"{self.path}/{name}")
            if self.not_found(response, name):
                return None
            if response.status_code != 200:
                raise ValueError("Cannot observe Pod")
            return self.workload(response.json(), name)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BackendUnavailable() from exc

    def list_owned(self):
        try:
            response = self.request(
                "GET", self.path, params={"labelSelector": "owner=agent-runtime"}
            )
            if response.status_code != 200:
                raise ValueError("Cannot list Pods")
            items = response.json()["items"]
            if not isinstance(items, list):
                raise TypeError("Malformed Pod list")
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            raise BackendUnavailable() from exc
        workloads = []
        for item in items:
            try:
                workloads.append(self.workload(item))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        return workloads

    def delete(self, name, uid=None):
        body = {"apiVersion": "v1", "kind": "DeleteOptions"}
        if uid is not None:
            body["preconditions"] = {"uid": uid}
        try:
            response = self.request("DELETE", f"{self.path}/{name}", json=body)
            if response.status_code in {200, 202} or self.not_found(response, name):
                return
            if response.status_code == 409 and uid is not None:
                data = response.json()
                if (
                    data.get("kind") == "Status"
                    and data.get("reason") == "Conflict"
                    and data.get("details", {}).get("kind") == "pods"
                    and data.get("details", {}).get("name") == name
                    and "Precondition failed: UID in precondition:" in data.get("message", "")
                ):
                    return
            raise ValueError("Cannot delete Pod")
        except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise BackendUnavailable() from exc
