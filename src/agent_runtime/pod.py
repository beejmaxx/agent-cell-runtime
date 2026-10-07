import base64

from agent_runtime.db import canonical


def pod_manifest(name, spec, settings):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": settings.k8s_namespace,
            "labels": {"owner": "agent-runtime", "execution_id": spec.execution_id},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "terminationGracePeriodSeconds": 5,
            "serviceAccountName": "agent-exec",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "agent",
                    "image": settings.agent_image,
                    "imagePullPolicy": "Never",
                    "env": [
                        {"name": name, "value": value}
                        for name, value in {
                            "EXECUTION_ID": spec.execution_id,
                            "EXECUTION_INPUT_B64": base64.b64encode(canonical(spec.input)).decode(),
                            "EXECUTION_CREDENTIAL": spec.credential,
                            "RUNTIME_URL": settings.runtime_url,
                        }.items()
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi", "ephemeral-storage": "16Mi"},
                        "limits": {"cpu": "250m", "memory": "128Mi", "ephemeral-storage": "64Mi"},
                    },
                    "volumeMounts": [
                        {"name": "tmp", "mountPath": "/tmp"},
                        {
                            "name": "agent-cell",
                            "mountPath": "/var/run/secrets/agent-cell",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            "volumes": [
                {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}},
                {
                    "name": "agent-cell",
                    "projected": {
                        "sources": [
                            {
                                "serviceAccountToken": {
                                    "audience": "agent-cell-gateway",
                                    "expirationSeconds": 600,
                                    "path": "token",
                                }
                            },
                            {
                                "configMap": {
                                    "name": "kube-root-ca.crt",
                                    "items": [{"key": "ca.crt", "path": "ca.crt"}],
                                }
                            },
                        ]
                    },
                },
            ],
        },
    }
