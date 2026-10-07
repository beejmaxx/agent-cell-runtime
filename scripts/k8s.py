import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = ROOT / ".local/bin/kubectl"
PROFILE = os.getenv("K8S_PROFILE", "colima")
if PROFILE not in {"colima", "eks"}:
    raise ValueError("K8S_PROFILE must be colima or eks")
STATE = ROOT / (".local/s1" if PROFILE == "eks" else ".local/k8s")
EKS_ARN = "arn:aws:eks:us-east-2:729608197929:cluster/lab-exec-s1"
VERSION = "v1.35.0"
NAMESPACES = ("agent-runtime", "agent-exec")
MARKER = "lab.agent-runtime/owned"
IMAGE = "agent-runtime/fake-agent:r2"


def context():
    return EKS_ARN if PROFILE == "eks" else "colima"


def image_metadata():
    value = json.loads((STATE / "image.json").read_text())
    from agent_runtime.config import Settings

    Settings("unused", k8s_profile="eks", agent_image=value["image"])
    if value["architecture"] != "amd64" or value["os"] != "linux":
        raise RuntimeError("S1 requires a verified linux/amd64 image")
    return value


def image():
    return image_metadata()["image"] if PROFILE == "eks" else IMAGE


def kubectl_args(*args):
    binary = str(KUBECTL) if KUBECTL.exists() else shutil.which("kubectl")
    if not binary:
        raise RuntimeError("kubectl is needed to verify the existing Colima configuration")
    return [binary, "--context", context(), *args]


def kubectl(*args):
    return subprocess.check_output(kubectl_args(*args), text=True)


def guard():
    if kubectl("config", "current-context").strip() != context():
        raise RuntimeError(f"Refusing a current context other than {context()}")
    config = json.loads(kubectl("config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
    if PROFILE == "eks":
        expected = json.loads((STATE / "cluster.json").read_text())
        if (
            expected["arn"] != EKS_ARN
            or config["clusters"][0]["name"] != EKS_ARN
            or cluster["server"] != expected["endpoint"]
            or cluster.get("certificate-authority-data") != expected["certificateAuthority"]["data"]
            or cluster.get("insecure-skip-tls-verify")
            or cluster.get("proxy-url")
        ):
            raise RuntimeError(
                "EKS context does not match the recorded S1 cluster ARN, endpoint and CA"
            )
        return cluster
    if urlparse(cluster["server"]).hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Refusing a non-loopback Kubernetes API URL")
    return cluster


def require_owned(allow_missing=False):
    for name in NAMESPACES:
        raw = kubectl("get", "namespace", name, "--ignore-not-found", "-o", "json")
        if not raw.strip() and allow_missing:
            continue
        if not raw.strip() or json.loads(raw)["metadata"].get("labels", {}).get(MARKER) != "true":
            raise RuntimeError(f"Refusing namespace without ownership marker: {name}")


def tools():
    system = platform.system().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
    url = f"https://dl.k8s.io/release/{VERSION}/bin/{system}/{arch}/kubectl"
    with urlopen(url + ".sha256", timeout=30) as response:
        expected = response.read().decode().strip().split()[0]
    with urlopen(url, timeout=30) as response:
        binary = response.read()
    if hashlib.sha256(binary).hexdigest() != expected:
        raise RuntimeError("kubectl checksum mismatch")
    KUBECTL.parent.mkdir(parents=True, exist_ok=True)
    temporary = KUBECTL.with_suffix(".download")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    temporary.replace(KUBECTL)
    print(f"Installed checksum-verified {VERSION} at {KUBECTL}")


def token(cluster):
    if PROFILE == "eks":
        raise RuntimeError("EKS controller tokens must be issued by the trusted-host role")
    require_owned()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    (STATE / "api-url").write_text(cluster["server"] + "\n")
    (STATE / "ca.crt").write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
    value = kubectl(
        "create", "token", "agent-runtime-controller", "-n", "agent-runtime", "--duration=24h"
    )
    path = STATE / "controller.token"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
    path.chmod(0o600)
    print("Refreshed controller connection files under .local/k8s/")


def main():
    command = sys.argv[1]
    if PROFILE == "eks" and command in {"up", "down", "token"}:
        raise RuntimeError("EKS harness administration awaits the S1 operator-path decision")
    cluster = guard()
    if command == "tools":
        tools()
        return
    if not KUBECTL.exists():
        raise RuntimeError("Run make k8s-tools first")
    if command == "up":
        require_owned(allow_missing=True)
        print(kubectl("apply", "-f", str(ROOT / "deploy/k8s/namespaces.yaml")), end="")
        require_owned()
        print(kubectl("apply", "-f", str(ROOT / "deploy/k8s/resources.yaml")), end="")
        token(cluster)
    elif command == "token":
        token(cluster)
    elif command == "down":
        require_owned(allow_missing=True)
        for name in NAMESPACES:
            print(kubectl("delete", "namespace", name, "--ignore-not-found"), end="")
    elif command == "image":
        if PROFILE == "eks":
            raise RuntimeError("Use scripts/s1_image.py at checkpoint 4 to publish the S1 image")
        subprocess.run(
            ["docker", "--context", "colima", "build", "-t", IMAGE, str(ROOT / "agent")], check=True
        )
    else:
        raise ValueError(command)


if __name__ == "__main__":
    main()
