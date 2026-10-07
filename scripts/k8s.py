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
STATE = ROOT / ".local/k8s"
VERSION = "v1.35.0"
NAMESPACES = ("agent-runtime", "agent-exec")
MARKER = "lab.agent-runtime/owned"
IMAGE = "agent-runtime/fake-agent:r2"


def kubectl(*args):
    binary = str(KUBECTL) if KUBECTL.exists() else shutil.which("kubectl")
    if not binary:
        raise RuntimeError("kubectl is needed to verify the existing Colima configuration")
    return subprocess.check_output([binary, "--context", "colima", *args], text=True)


def guard():
    if kubectl("config", "current-context").strip() != "colima":
        raise RuntimeError("Refusing a current context other than colima")
    config = json.loads(kubectl("config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
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
        subprocess.run(
            ["docker", "--context", "colima", "build", "-t", IMAGE, str(ROOT / "agent")], check=True
        )
    else:
        raise ValueError(command)


if __name__ == "__main__":
    main()
