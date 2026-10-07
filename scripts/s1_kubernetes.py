"""Operator-owned EKS setup; the harness never creates cluster-scoped objects."""

EXEC_NAMESPACE = "agent-exec"
CONTROL_NAMESPACE = "agent-exec-psa-control"
MARKER = "lab.agent-runtime/owned"


def controller_binding():
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "workload-controller", "namespace": EXEC_NAMESPACE},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "workload-controller",
        },
        "subjects": [
            {
                "kind": "Group",
                "apiGroup": "rbac.authorization.k8s.io",
                "name": "agent-runtime-controllers",
            }
        ],
    }


def manifests(pod_security_group):
    items = []
    for namespace in (EXEC_NAMESPACE, CONTROL_NAMESPACE):
        labels = {MARKER: "true"}
        if namespace == EXEC_NAMESPACE:
            labels.update(
                {
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/enforce-version": "v1.36",
                }
            )
        items += [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": labels},
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "agent-exec", "namespace": namespace},
                "automountServiceAccountToken": False,
            },
        ]
    items += [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "workload-controller", "namespace": EXEC_NAMESPACE},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["create", "get", "list", "delete"],
                }
            ],
        },
        controller_binding(),
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "executions", "namespace": EXEC_NAMESPACE},
            "spec": {
                "hard": {
                    "pods": "4",
                    "requests.cpu": "1",
                    "limits.cpu": "1",
                    "requests.memory": "512Mi",
                    "limits.memory": "640Mi",
                    "limits.ephemeral-storage": "320Mi",
                }
            },
        },
        {
            "apiVersion": "vpcresources.k8s.aws/v1beta1",
            "kind": "SecurityGroupPolicy",
            "metadata": {"name": "execution", "namespace": EXEC_NAMESPACE},
            "spec": {"podSelector": {}, "securityGroups": {"groupIds": [pod_security_group]}},
        },
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}
