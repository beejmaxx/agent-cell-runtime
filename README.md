# Agent Cell Runtime

A hands-on exploration of infrastructure for securely executing AI agents: Python control services, Kubernetes workloads, deterministic authorization, AWS identity, and distributed failure recovery.

**Status:** R1 local execution lifecycle implemented: synchronous FastAPI, PostgreSQL, a reconciler, and a deterministic fake workload backend. The invariant tests exercise lifecycle and caller scoping; this is not a sandbox and establishes no workload isolation guarantees.

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

Integration tests use only Mock Workday's HTTP API: synthetic Alice/Bob/Dave logins, JWKS, and delegation grants. Requests bypass HTTP proxies and send the tenant's `Host` header. Grants created by the tests are revoked afterward; the tests do not reset the service. Because Mock Workday test-admin time is frozen, the integration runtime clock is set to the issued token's time. Both tenants' execution ownership and replay cases are covered by the self-contained suite; live tests use Acme's seeded `hr-assistant` client.

To run the API against an existing local runtime database:

```sh
export DATABASE_URL=postgresql+psycopg://localhost/agent_runtime
export MW_BASE_URL=http://127.0.0.1:18080
make init-db
make run
```

`make init-db` installs the schema and seeds Acme/Globex agent definitions. The app also initializes these on startup. `make run` serves on port 8000 and runs a reconciliation pass every second. Its clock follows wall time; use Mock Workday tokens valid at that time. Tests inject their own frozen clock and disable the background loop. OpenAPI is available at `/openapi.json`.

A human token and `Idempotency-Key` create an execution through `POST /api/v1/executions`. The owner can GET or cancel it. R1 workloads are in-memory records: they do not run code. The temporary `/complete` endpoint accepts only the credential passed to that execution's fake backend spec; it is a test-only stand-in for R3's gateway. The database stores only its hash.

The reconciler attempts launch at most once. An uncertain launch can fail conservatively, and an adopted workload whose credential was never persisted must time out. Deadlines and cancellation change database authority even when the backend is unavailable; label-checked cleanup follows. Results are untrusted JSON. These are lab policies, not claims about Workday behavior. No Kubernetes, gateway, or AWS implementation is included in R1.

This is an independent educational project. It does not describe or reproduce any company's proprietary architecture. Use synthetic data for experiments.

## License

[MIT](LICENSE)
