# Agent Cell Runtime

A hands-on exploration of infrastructure for securely executing AI agents: Python control services, Kubernetes workloads, deterministic authorization, AWS identity, and distributed failure recovery.

**Status:** R2 adds a Colima Kubernetes backend to the R1 execution lifecycle: synchronous FastAPI, PostgreSQL, a reconciler, a deterministic fake workload backend, and real Kubernetes Pods. The invariant tests exercise lifecycle and caller scoping; this is not a sandbox and establishes no workload isolation guarantees.

The goal is to build and understand this path:

```text
submit execution
      |
      v
execution controller -> durable execution state
      |
      v
isolated workload -> trusted mediation -> data / tools / models
      |                     |
      +------ telemetry and audit ------+
      |
      v
reconcile completion and clean up resources
```

An **Agent Cell** is our logical execution boundary. Its implementation is an open design question. A Pod with a maincar and sidecar is one experiment, not proof of isolation.

## What we want to learn

- Write clear Python and reason about async work, cancellation, bounded concurrency, and shared state.
- Understand Kubernetes by creating, observing, and deleting workloads through its API, and by building a small kubeadm cluster.
- Make authorization unavoidable, bind requests to authenticated execution identity, and isolate tenant data.
- Recover from partial failure without blindly repeating consequential operations.
- Use AWS workload identity, temporary credentials, cross-account access, and controlled networking.
- Reconstruct execution behavior through metrics, traces, and durable audit records.

The model proposes actions. Trusted software decides which actions may occur, and the execution environment must prevent bypassing that software.

## Start here

1. [Architecture and open decisions](docs/architecture.md)
2. [Learning roadmap](docs/roadmap.md)
3. [Experiment log](docs/experiments.md)
4. [Contributing and learning approach](CONTRIBUTING.md)

## Run R1 locally

Requirements: Python 3.12, `uv`, and PostgreSQL with `initdb` and `pg_ctl` on `PATH`. On macOS with Homebrew PostgreSQL, add its `bin` directory to `PATH`.

```sh
uv sync --python 3.12 --locked
make test
```

`make test` starts and stops its own throwaway PostgreSQL cluster. It requires no external service or Docker. Tests freeze the runtime clock, use an HTTP JWKS/grant fake, and drive reconciliation directly. Test names refer to [R1 invariant IDs](docs/r1-spec.md), including the `HAPPY` end-to-end path. Restart tests replace the reconciler while retaining the database and the same fake backend.

For integration tests, start Mock Workday with its test-admin mode enabled, then run from this repository:

```sh
MW_PUBLIC_PORT=18080 MW_TEST_ADMIN=1 make -C ../mock-workday up
make test-integration
# Override the service address if necessary:
MW_BASE_URL=http://127.0.0.1:18080 make test-integration
```

Integration tests use only Mock Workday's HTTP API: synthetic Alice/Bob/Dave logins, JWKS, and delegation grants. Requests bypass HTTP proxies and send the tenant's `Host` header. Before each test's logins, the fixture advances the frozen test-admin clock by one hour through `http://127.0.0.1:8081/admin/clock`, so repeated runs do not exhaust the login rate limit. This local admin endpoint is required even when `MW_BASE_URL` is overridden. Grants created by the tests are revoked afterward; the tests do not reset the service. The integration runtime clock is set to the issued token's time. Both tenants' execution ownership and replay cases are covered by the self-contained suite; live tests use Acme's seeded `hr-assistant` client.

To run the API against an existing local runtime database:

```sh
export DATABASE_URL=postgresql+psycopg://localhost/agent_runtime
export MW_BASE_URL=http://127.0.0.1:18080
make init-db
make run
```

`make init-db` installs the schema and seeds Acme/Globex agent definitions. The app also initializes these on startup. `make run` serves on port 8000 and runs a reconciliation pass every second. Its clock follows wall time; use Mock Workday tokens valid at that time. Tests inject their own frozen clock and disable the background loop. OpenAPI is available at `/openapi.json`.

A human token and `Idempotency-Key` create an execution through `POST /api/v1/executions`. The owner can GET or cancel it. R1 workloads are in-memory records: they do not run code. The temporary `/complete` endpoint accepts only the credential passed to that execution's fake backend spec; it is a test-only stand-in for R3's gateway. The database stores only its hash.

The reconciler attempts launch at most once. An uncertain launch can fail conservatively, and an adopted workload retains the credential hash persisted at claim time so it can complete. Deadlines and cancellation change database authority even when the backend is unavailable; label-checked cleanup follows. Results are untrusted JSON. These are lab policies, not claims about Workday behavior. No Kubernetes, gateway, or AWS implementation is included in R1.

This is an independent educational project. It does not describe or reproduce any company's proprietary architecture. Use synthetic data for experiments.

## Run R2 against Colima

R2 is evidence about the controller against a real Kubernetes API server. It makes
no isolation claims; the execution substrate for the revised threat model remains
undecided. The projected token is unused until R3. The temporary completion
credential is visible to Pod readers and travels over plain HTTP.

Use the existing Colima context and run these targets serially:

```sh
make k8s-tools agent-image k8s-up
make test-k8s
```

The checksum-verified kubectl 1.35 binary is local to `.local/bin`; the global
kubectl is unchanged. Targets refuse other contexts and non-loopback API URLs.
Only `agent-runtime` and `agent-exec` carrying `lab.agent-runtime/owned=true` are
managed. The admission test briefly creates and deletes an unlabeled control
namespace; the RBAC test only sends a dry-run request to `rook-dev` and expects 403.
No NetworkPolicy, gateway, AWS, or EKS code is included.

`make test-k8s` first checks the tool, marked namespaces, RBAC, local image, and
projected token claims inside a Pod. Tests use a throwaway PostgreSQL database,
a real local runtime server, an HTTP Mock Workday fake, Pod status, and completion
results. They do not depend on `kubectl logs`. Sanitized evidence is saved under
`.local/k8s/`. CPU, OOM, storage eviction, and containment tests are outside R2.

`make k8s-up` writes the API URL, CA, and a 24-hour controller token under ignored
`.local/k8s/`. Refresh it with `make k8s-token`; the backend rereads it each request.
To run the application with your local runtime database:

```sh
export WORKLOAD_BACKEND=kubernetes
export K8S_API_URL="$(cat .local/k8s/api-url)"
export K8S_CA_FILE="$PWD/.local/k8s/ca.crt"
export K8S_TOKEN_FILE="$PWD/.local/k8s/controller.token"
export RUNTIME_URL=http://192.168.5.2:8000
make run
```

Keep the database and Mock Workday configuration from the local setup above.
`MAX_ACTIVE_EXECUTIONS` defaults to 3; the namespace quota is the admission backstop.
`make k8s-down` tears down only the two marked namespaces. The local image and
ignored connection/evidence files remain.

## S1 code preparation

`create_completion_app(runtime)` exposes only
`POST /api/v1/executions/{id}/complete`. It shares the runtime's live state and
completion handler, including execution-credential authentication, the 64 KiB
result limit, replay handling, and cleanup. It has no OpenAPI, documentation,
health, or control-plane routes. The local k8s harness now serves this app on a
separate listener and directs agent callbacks there.

The EKS Pod template requires `K8S_PROFILE=eks` and a sha256 digest in the S1 ECR
repository. Its CPU/memory requests match the existing limits (250m/128Mi), as
Fargate requires. The Colima template remains unchanged. At checkpoint 4,
`uv run python scripts/s1_image.py` builds for `linux/amd64`, publishes to the
already-provisioned, S1-tagged repository, pulls the digest to verify image
identity, and writes `.local/s1/image.json` with the digest and environment-name
baseline. It does not create a repository. Registry credentials are temporary.

The fake agent also supports `probe` (explicit TCP/HTTP targets, metadata and
direct-resolver DNS probes), `listen` (a peer HTTP positive control), and `binding`
(try its own completion credential against another execution, then complete
itself). `inspect` includes boot ID and kernel release and reaches the Kubernetes
API by its injected service IP with verified TLS, without requiring CoreDNS.
Probe results distinguish transport failures from HTTP rejection; response bodies
and credential values are never included in evidence. A DNS response, including
NXDOMAIN, is not evidence of blocking: E6 still requires query logs and the
firewall-removal control.

Checkpoint 2 is still in progress. The EKS harness execution location and its
operator-only administrative path require a spec clarification before they can
be wired together. No S1 infrastructure has been provisioned or experiments run.

## License

[MIT](LICENSE)
