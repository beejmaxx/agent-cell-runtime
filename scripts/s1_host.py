"""Trusted-host authentication. No operator credentials belong on this host."""

import base64
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

from scripts.s1_image import ACCOUNT, REGION, aws

CLUSTER = "lab-exec-s1"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"
CONTROLLER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/lab-s1-controller"
HARNESS_ROLE = f"arn:aws:iam::{ACCOUNT}:role/s1-harness"


def require_controller():
    identity = json.loads(aws("sts", "get-caller-identity"))
    if identity["Account"] != ACCOUNT or not identity["Arn"].startswith(
        f"arn:aws:sts::{ACCOUNT}:assumed-role/lab-s1-controller/"
    ):
        raise RuntimeError("EKS harness must run on the trusted host as lab-s1-controller")


def harness_exec():
    return {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "command": "aws",
        "args": [
            "--profile",
            "agent-runtime",
            "--region",
            REGION,
            "eks",
            "get-token",
            "--cluster-name",
            CLUSTER,
            "--role-arn",
            HARNESS_ROLE,
        ],
        "interactiveMode": "Never",
    }


def atomic_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
    temporary.chmod(0o600)
    temporary.replace(path)


def refresh_token(state):
    require_controller()
    # No --role-arn: runtime requests retain the controller's narrow RBAC identity.
    result = json.loads(aws("eks", "get-token", "--cluster-name", CLUSTER))
    atomic_private(state / "controller.token", result["status"]["token"] + "\n")


@contextmanager
def controller_tokens(state):
    refresh_token(state)
    stop = Event()

    def renew():
        while not stop.wait(60):
            try:
                refresh_token(state)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                # The existing token expires; failures never substitute a broader identity.
                logging.getLogger(__name__).error("Controller token refresh failed")

    thread = Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(30)


def setup(state):
    require_controller()
    cluster = json.loads(aws("eks", "describe-cluster", "--name", CLUSTER))["cluster"]
    if cluster["arn"] != CLUSTER_ARN:
        raise RuntimeError("Unexpected cluster ARN")
    atomic_private(state / "cluster.json", json.dumps(cluster))
    atomic_private(
        state / "ca.crt", base64.b64decode(cluster["certificateAuthority"]["data"]).decode()
    )
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": CLUSTER_ARN,
        "clusters": [
            {
                "name": CLUSTER_ARN,
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["certificateAuthority"]["data"],
                },
            }
        ],
        "contexts": [
            {"name": CLUSTER_ARN, "context": {"cluster": CLUSTER_ARN, "user": "s1-harness"}}
        ],
        "users": [{"name": "s1-harness", "user": {"exec": harness_exec()}}],
    }
    atomic_private(state / "kubeconfig.json", json.dumps(config))
    refresh_token(state)


if __name__ == "__main__":
    setup(Path(__file__).resolve().parents[1] / ".local/s1")
