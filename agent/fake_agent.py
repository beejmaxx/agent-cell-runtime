import base64
import json
import os
import ssl
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def complete(result):
    payload = json.dumps({"result": result}, ensure_ascii=False).encode()
    request = Request(
        f"{os.environ['RUNTIME_URL']}/api/v1/executions/{os.environ['EXECUTION_ID']}/complete",
        data=payload,
        headers={
            "Authorization": f"Execution {os.environ['EXECUTION_CREDENTIAL']}",
            "Content-Type": "application/json",
        },
    )
    until = time.monotonic() + 30
    while time.monotonic() < until:
        try:
            with urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
                raise SystemExit(1)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read())["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            if not (exc.code >= 500 or (exc.code == 409 and code == "INVALID_STATE")):
                raise SystemExit(1) from None
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(min(1, max(0, until - time.monotonic())))
    raise SystemExit(1)


def inspect():
    directory = Path("/var/run/secrets/agent-cell")
    encoded = (directory / "token").read_text().split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    pod = claims["kubernetes.io"]["pod"]
    request = Request(
        "https://kubernetes.default.svc/api",
        headers={"Authorization": "Bearer " + (directory / "token").read_text()},
    )
    context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
    try:
        with urlopen(request, context=context, timeout=5) as response:
            api_status = response.status
    except HTTPError as exc:
        api_status = exc.code
    rootfs_readback = None
    try:
        Path("/rootfs-probe/x").write_text("probe")
        rootfs_readback = Path("/rootfs-probe/x").read_text()
        rootfs_errno = 0
    except OSError as exc:
        rootfs_errno = exc.errno
    return {
        "default_token_directory": Path("/var/run/secrets/kubernetes.io/serviceaccount").exists(),
        "aud": claims["aud"],
        "exp": claims["exp"],
        "token_lifetime": claims["exp"] - claims["iat"],
        "sub": claims["sub"],
        "pod_name": pod["name"],
        "pod_uid": pod["uid"],
        "env_names": sorted(os.environ),
        "api_status": api_status,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "rootfs_errno": rootfs_errno,
        "rootfs_readback": rootfs_readback,
        "cap_eff": next(
            line.split()[1]
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("CapEff:")
        ),
        "mount_points": [
            line.split()[1] for line in Path("/proc/self/mounts").read_text().splitlines()
        ],
    }


def main():
    data = json.loads(base64.b64decode(os.environ["EXECUTION_INPUT_B64"]))
    behavior = data["behavior"]
    if behavior == "complete":
        result = {"echo": data.get("payload")}
    elif behavior == "exit":
        raise SystemExit(data["code"])
    elif behavior == "sleep":
        if "seconds" not in data:
            while True:
                time.sleep(1)
        time.sleep(data["seconds"])
        result = {"slept": data["seconds"]}
    elif behavior == "inspect":
        result = inspect()
    else:
        raise SystemExit(1)
    complete(result)


if __name__ == "__main__":
    main()
