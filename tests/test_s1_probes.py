import errno
import json
import socket
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agent import probes


@pytest.fixture
def endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            seen.append((self.command, self.path, dict(self.headers)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic-imds-token")

        def do_GET(self):
            seen.append((self.command, self.path, dict(self.headers)))
            self.send_response(401 if self.path == "/denied" else 200)
            self.end_headers()
            if self.path.endswith("security-credentials/"):
                self.wfile.write(b"synthetic-role")
            elif self.path.endswith("synthetic-role"):
                self.wfile.write(
                    b'{"AccessKeyId":"sentinel-key","SecretAccessKey":"sentinel-secret"}'
                )
            else:
                self.wfile.write(b"sentinel-body")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ISO_2_probe_distinguishes_transport_and_application(endpoint):
    port, _ = endpoint
    target = {"name": "control", "host": "127.0.0.1", "port": port}
    assert probes.probe(target)["outcome"] == "TCP accepted"
    allowed = probes.probe({**target, "method": "GET"})
    assert allowed["outcome"] == "request processed" and allowed["status"] == 200
    denied = probes.probe({**target, "method": "GET", "path": "/denied"})
    assert denied["outcome"] == "HTTP 401" and denied["tcp_connected"]
    assert "sentinel-body" not in json.dumps(allowed)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        refused = probes.probe({**target, "port": sock.getsockname()[1]})
    assert refused["outcome"] in {"connection refused", "timed out"}
    assert not refused["tcp_connected"]


@pytest.mark.parametrize(
    "exc,outcome",
    [
        (TimeoutError(), "timed out"),
        (OSError(errno.ENETUNREACH, "network"), "no route"),
        (ConnectionRefusedError(), "connection refused"),
        (socket.gaierror(), "probe error"),
    ],
)
def test_ISO_2_probe_errors_never_claim_denial(monkeypatch, exc, outcome):
    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(socket, "create_connection", fail)
    assert probes.probe({"name": "negative", "host": "synthetic", "port": 80})["outcome"] == outcome


def test_ISO_4_ID_8_metadata_positive_control_never_exports_credentials(endpoint):
    port, seen = endpoint
    result = probes.metadata("127.0.0.1", port, "127.0.0.1")
    assert result["credentials_obtained"]
    assert seen[0][0:2] == ("PUT", "/latest/api/token")
    assert seen[1][2]["X-aws-ec2-metadata-token"] == "synthetic-imds-token"
    for secret in ("sentinel-key", "sentinel-secret", "synthetic-imds-token", "synthetic-role"):
        assert secret not in json.dumps(result)


def test_ISO_3_dns_fresh_names_and_direct_resolver():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(5)
        seen = []

        def respond():
            for _ in range(2):
                wire, peer = server.recvfrom(4096)
                seen.append(wire)
                header = struct.pack("!6H", struct.unpack("!H", wire[:2])[0], 0x8183, 1, 0, 0, 0)
                server.sendto(header + wire[12:], peer)

        thread = Thread(target=respond)
        thread.start()
        first = probes.dns("127.0.0.1", "example.com", fresh=True, port=server.getsockname()[1])
        second = probes.dns("127.0.0.1", "example.com", fresh=True, port=server.getsockname()[1])
        thread.join(5)
    assert not thread.is_alive() and len(seen) == 2
    assert first["name"] != second["name"]
    assert first["rcode"] == second["rcode"] == 3
    assert first["outcome"] == "DNS response"  # NXDOMAIN does not prove filtering.
