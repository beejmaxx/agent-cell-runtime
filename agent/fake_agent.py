import base64
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from probes import CA_PATH, dns, metadata, probe, request, url_target


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
    api = probe(
        {
            "name": "projected token API",
            "host": os.environ["KUBERNETES_SERVICE_HOST"],
            "port": int(os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")),
            "server_name": "kubernetes.default.svc",
            "tls": True,
            "ca_file": CA_PATH,
            "method": "GET",
            "path": "/api",
            "projected_token": True,
        }
    )
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
        "api_status": api.get("status"),
        "api_probe": api,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "kernel_release": os.uname().release,
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
    elif behavior == "probe":
        result = {"probes": [probe(target) for target in data.get("targets", [])]}
        if data.get("metadata"):
            result["metadata"] = metadata()
        if "dns" in data:
            result["dns"] = [dns(**query) for query in data["dns"]]
    elif behavior == "listen":
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Listener(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"s1-execution-listener")

            def log_message(self, *args):
                pass

        with HTTPServer(("0.0.0.0", data.get("port", 8080)), Listener) as server:
            server.serve_forever()
        return
    elif behavior == "binding":
        target = url_target(
            "cross-execution completion",
            f"{os.environ['RUNTIME_URL']}/api/v1/executions/{data['other_execution']}/complete",
            "POST",
        )
        observation, _ = request(
            target,
            headers={
                "Authorization": f"Execution {os.environ['EXECUTION_CREDENTIAL']}",
                "Content-Type": "application/json",
            },
            body=b'{"result":{"synthetic":"cross-execution"}}',
        )
        result = {"cross_execution": observation}
    else:
        raise SystemExit(1)
    if data.get("hold_seconds"):
        time.sleep(data["hold_seconds"])
    complete(result)


if __name__ == "__main__":
    main()
