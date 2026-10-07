"""S1 probes report observations, never infer which network control enforced them."""

import errno
import http.client
import json
import secrets
import socket
import ssl
import struct
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

TOKEN_PATH = "/var/run/secrets/agent-cell/token"
CA_PATH = "/var/run/secrets/agent-cell/ca.crt"


def failure(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if getattr(exc, "errno", None) in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return "no route"
    # DNS and TLS errors must not be mistaken for a network deny.
    return "probe error"


def request(target, *, headers=None, body=b"", read_body=False):
    start = monotonic()
    result = {"name": target["name"], "tcp_connected": False}
    sock = None
    try:
        sock = socket.create_connection((target["host"], target["port"]), timeout=3)
        result.update(tcp_connected=True, outcome="TCP accepted")
        if target.get("tls"):
            context = ssl.create_default_context(cafile=target.get("ca_file"))
            sock = context.wrap_socket(
                sock, server_hostname=target.get("server_name", target["host"])
            )
            result["tls_verified"] = True
        if "method" not in target:
            return result, b""
        path = target.get("path", "/")
        method = target["method"]
        fields = {
            "Host": target.get("server_name", target["host"]),
            "Connection": "close",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        if any("\r" in str(v) or "\n" in str(v) for v in [path, method, *fields, *fields.values()]):
            raise ValueError("Invalid HTTP probe fields")
        wire = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
        sock.sendall(wire.encode() + b"\r\n" + body)
        response = http.client.HTTPResponse(sock)
        response.begin()
        result.update(
            status=response.status,
            outcome=f"HTTP {response.status}"
            if response.status in {401, 403}
            else "request processed",
        )
        # Bodies can contain credentials. Only the metadata detector reads them; evidence never does.
        payload = response.read(16384) if read_body else b""
        response.close()
        return result, payload
    except (OSError, ValueError, http.client.HTTPException) as exc:
        result["outcome"] = "TCP accepted" if result["tcp_connected"] else failure(exc)
        result["error_type"] = type(exc).__name__
        result["errno"] = getattr(exc, "errno", None)
        return result, b""
    finally:
        if sock is not None:
            sock.close()
        result["elapsed_seconds"] = monotonic() - start


def probe(target):
    headers = {}
    if target.get("projected_token"):
        from pathlib import Path

        headers["Authorization"] = "Bearer " + Path(TOKEN_PATH).read_text().strip()
    result, _ = request(target, headers=headers, body=target.get("body", "").encode())
    return result


def metadata(host="169.254.169.254", port=80, container_host="169.254.170.2"):
    base = {"host": host, "port": port}
    token_result, token = request(
        {**base, "name": "IMDSv2 token", "method": "PUT", "path": "/latest/api/token"},
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        read_body=True,
    )
    results = [token_result]
    headers = (
        {"X-aws-ec2-metadata-token": token.decode()} if token_result.get("status") == 200 else {}
    )
    # Also probe IMDSv1 if IMDSv2 is unavailable; an IMDSv2-only failure is insufficient evidence.
    listing, names = request(
        {
            **base,
            "name": "IMDS roles",
            "method": "GET",
            "path": "/latest/meta-data/iam/security-credentials/",
        },
        headers=headers,
        read_body=True,
    )
    results.append(listing)
    obtained = False
    if listing.get("status") == 200:
        from urllib.parse import quote

        for name in names.decode(errors="replace").splitlines()[:10]:
            result, payload = request(
                {
                    **base,
                    "name": "IMDS credentials",
                    "method": "GET",
                    "path": "/latest/meta-data/iam/security-credentials/" + quote(name, safe=""),
                },
                headers=headers,
                read_body=True,
            )
            obtained |= has_credentials(payload)
            results.append(result)
    container, payload = request(
        {
            "name": "container metadata",
            "host": container_host,
            "port": port,
            "method": "GET",
            "path": "/",
        },
        read_body=True,
    )
    results.append(container)
    obtained |= has_credentials(payload)
    return {"credentials_obtained": obtained, "probes": results}


def has_credentials(payload):
    try:
        value = json.loads(payload)
        return isinstance(value, dict) and bool(
            value.get("AccessKeyId") and value.get("SecretAccessKey")
        )
    except ValueError:
        return False


def dns(resolver, domain, *, fresh=False, tcp=False, port=53):
    name = f"s1-{uuid4().hex}.{domain}" if fresh else domain
    labels = name.rstrip(".").split(".")
    encoded = [label.encode("idna") for label in labels]
    if any(not label or len(label) > 63 for label in encoded):
        raise ValueError("Invalid DNS name")
    question = b"".join(bytes([len(label)]) + label for label in encoded) + b"\0\0\1\0\1"
    transaction = secrets.randbelow(65536)
    wire = struct.pack("!6H", transaction, 0x100, 1, 0, 0, 0) + question
    result = {"name": name, "resolver": resolver, "transport": "TCP" if tcp else "UDP"}
    try:
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
        ) as sock:
            sock.settimeout(3)
            sock.connect((resolver, port))
            if tcp:
                sock.sendall(struct.pack("!H", len(wire)) + wire)

                def receive(size):
                    data = b""
                    while len(data) < size:
                        part = sock.recv(size - len(data))
                        if not part:
                            raise ValueError("Incomplete DNS response")
                        data += part
                    return data

                response = receive(struct.unpack("!H", receive(2))[0])
            else:
                sock.send(wire)
                response = sock.recv(4096)
        if len(response) < 12:
            raise ValueError("Short DNS response")
        ident, flags, questions, answers, _, _ = struct.unpack("!6H", response[:12])
        if ident != transaction or not flags & 0x8000 or questions != 1:
            raise ValueError("Mismatched DNS response")
        result.update(
            outcome="DNS response",
            rcode=flags & 15,
            answers=answers,
            truncated=bool(flags & 0x200),
            response_hex=response.hex(),
        )
    except (OSError, ValueError) as exc:
        result.update(outcome=failure(exc), error_type=type(exc).__name__)
    return result


def url_target(name, url, method="GET", **extra):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("Expected an HTTP(S) probe URL without credentials")
    return {
        "name": name,
        "host": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "tls": parsed.scheme == "https",
        "method": method,
        "path": parsed.path or "/",
        **extra,
    }


def credential_paths(environment=None):
    """Inspect application-visible credential sources without returning their contents."""
    import os
    from pathlib import Path

    environment = os.environ if environment is None else environment
    obtained = bool(
        environment.get("AWS_ACCESS_KEY_ID") and environment.get("AWS_SECRET_ACCESS_KEY")
    )
    files = {}
    for key in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    ):
        path = environment.get(key)
        if path:
            try:
                files[key] = bool(Path(path).read_bytes())
            except OSError:
                files[key] = False
    default = Path(environment.get("HOME", "/nonexistent")) / ".aws/credentials"
    try:
        files["shared_credentials_default"] = bool(default.read_bytes())
    except OSError:
        files["shared_credentials_default"] = False
    results = []
    uri = environment.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if not uri and environment.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
        uri = "http://169.254.170.2" + environment["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"]
    if uri:
        headers = {}
        token = environment.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        token_file = environment.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
        if token_file:
            try:
                token = Path(token_file).read_text().strip()
            except OSError:
                token = None
        if token:
            headers["Authorization"] = token
        try:
            target = url_target("injected container credential URI", uri)
            result, payload = request(target, headers=headers, read_body=True)
            obtained |= has_credentials(payload)
            results.append(result)
        except ValueError:
            results.append({"name": "injected container credential URI", "outcome": "probe error"})
    return {
        "credentials_obtained": obtained,
        "credential_files_nonempty": files,
        "aws_env_names": sorted(k for k in environment if k.startswith("AWS_")),
        "probes": results,
    }
