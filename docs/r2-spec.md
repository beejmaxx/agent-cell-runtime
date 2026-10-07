# R2 specification: Kubernetes execution (local k3s)

**Status:** draft for review. Not approved for implementation.

R2 replaces R1's in-memory workload backend with real Pods on the local k3s cluster, behind the same `WorkloadBackend` contract. It implements the R2 scope in [invariants.md](invariants.md):

- **Lifecycle:** LC-3, LC-5, LC-6, LC-7, LC-8 (R2 part), LC-11.
- **Isolation:** ISO-1, ISO-5, ISO-6, ISO-7.
- **Identity:** ID-8 (R2 part).
- **The mandatory happy path.**

Design rationale is in [execution-lifecycle.md](execution-lifecycle.md) and [trust-boundaries.md](trust-boundaries.md). [r1-spec.md](r1-spec.md) still applies except where this document changes it (§8).

**The evidence R2 must produce:** the R1 reconciler, essentially unchanged, keeps its lifecycle invariants when the backend is a real, slow, partially failing Kubernetes API, and an execution Pod gets no credentials beyond its own.

**Not in R2:**

- the gateway and TokenReview;
- NetworkPolicy and every network-isolation claim (ISO-2, ISO-3, ISO-4 are R3 and R6);
- deploying the controller inside the cluster;
- watches and informers;
- Jobs;
- multiple controller replicas;
- AWS and EKS;
- user namespaces and sandboxed runtimes;
- fork bombs and PID limits (R8).

## 0. Verified environment facts (2026-10-07)

| Fact | How it was verified |
|---|---|
| Colima profile `default`: macOS Virtualization.Framework, aarch64, 2 CPUs, 2 GiB memory, Docker runtime, k3s `v1.35.0+k3s1`, single node `colima` | `colima list`, `colima status`, `kubectl get nodes` |
| The cluster is shared: namespace `rook-dev` runs another project's workloads | `kubectl get pods -A` |
| The API server is at a forwarded local port (`https://127.0.0.1:62791` today; it may change when Colima restarts). The admin kubeconfig context `colima` uses a client certificate. | `kubectl config view --minify` |
| k3s uses Colima's Docker daemon, so locally built images run with `imagePullPolicy: Never`, and no registry is needed | A probe Pod ran the local busybox image |
| A Pod reaches a server bound to the Mac's `127.0.0.1` at **`192.168.5.2`**. `host.lima.internal` and `host.docker.internal` do **not** resolve inside Pods. | Probe Pod `wget` against `python -m http.server --bind 127.0.0.1` |
| Pods currently reach the Kubernetes API (`https://kubernetes.default.svc`) and get `401` without a token: there is no network policy yet | Probe Pod |
| `kubectl logs` fails (API server → kubelet `:10250` TLS handshake timeout). Tests and debugging must not depend on container logs. | `kubectl logs` |
| `python:3.12-slim` and busybox images are already present locally | `docker images` |
| kubectl client 1.32 against server 1.35 warns about version skew; basic operations work | `kubectl version` |

**Observation (unverified as policy):** anything in the VM, including `rook-dev` Pods, can likely reach every service bound to the Mac's loopback through `192.168.5.2`, including Mock Workday on 18080 and the runtime API. R2 makes no isolation claim about this. R3 adds network enforcement.

**Unverified, to check first (§9, step 1):**

- cgroup v2 CPU throttling counters are visible inside the container;
- the kubelet enforces `ephemeral-storage` limits and emptyDir `sizeLimit` under the Docker runtime (through cri-dockerd);
- a projected service-account token with a custom audience is issued.

If a mechanism is not enforced on this cluster, **stop and report.** Do not weaken the test.

## 1. Mechanisms R2 depends on

These are the Kubernetes behaviors the design relies on. Each one is either tested in §9 or explicitly assumed.

- **Create is accepted before anything runs.** `POST /pods` returning 201 means the object is persisted. Scheduling, image start, and the container process come later. RUNNING still means "created and UID recorded" (execution-lifecycle.md), not "process started."
- **A lost response is real.** If the client times out after the API server committed the object, the Pod exists, and the client does not know it. Retrying a create with the same name returns `409 AlreadyExists`. That is why the reconciler re-observes and never re-creates.
- **Consistent versus cached reads.**
  - `GET /pods/{name}` and `LIST` **without** `resourceVersion` are consistent reads.
  - `resourceVersion=0` permits a possibly stale cached answer: a stale "absent" could turn a just-created Pod into `LAUNCH_UNKNOWN` or `POD_LOST`.
  - R2 never sends `resourceVersion` on reads used for decisions.
- **Absence must be asserted by the authority.** A 404 counts as absence only when the body is a Kubernetes `Status` with `reason: NotFound`, `details.kind: pods`, and `details.name` equal to the requested name. Any other 404 (wrong base URL, proxy page) is `BackendUnavailable`. Otherwise a misconfiguration would look like "every Pod vanished."
- **Deletion is asynchronous.** `DELETE` sets `metadata.deletionTimestamp`. The Pod stays visible, often still `Running`, through its termination grace period, and then disappears.
  - An externally deleted Pod could be observed as `Failed` (exit 143) before it vanishes, making the failure reason race-dependent.
  - So a Pod with `deletionTimestamp` set maps to phase `LOST`.
- **Delete preconditions.** `DELETE` with `preconditions.uid` deletes only that exact object. A different object that later reuses the name returns `409` and is untouched. This closes the observe-then-delete race in cleanup.
- **Pod phases.**

  | Kubernetes phase | Maps to |
  |---|---|
  | `Pending` | `RUNNING` (alive, not yet started) |
  | `Running` | `RUNNING` |
  | `Succeeded` | `SUCCEEDED` |
  | `Failed` | `FAILED` |
  | `Unknown` | `RUNNING` |

  `Unknown` means the node stopped reporting. That is unable-to-observe, not absence or failure (LC-7); the deadline still applies.
  - A Pod stuck in `Pending` (for example a missing image) is not detected specially. Its deadline ends it.
- **`activeDeadlineSeconds`** counts from the Pod's start time. The kubelet then kills it, and the phase becomes `Failed` with reason `DeadlineExceeded`. It is a backstop; the reconciler's `deadline_at` stays authoritative (LC-8).
- **Labels are trusted only because creation is restricted.** Anyone who can create Pods in the execution namespace can forge `owner=agent-runtime`. The trust root is RBAC on that namespace (§3), not the label itself.
- **Projected tokens** are minted by the kubelet for the Pod's service account with a requested audience, and bound to the Pod. They work even with `automountServiceAccountToken: false`, which only removes the default API-audience token volume.
- **Pod Security Admission `restricted`** rejects privileged settings at create time with `403 Forbidden`. The Pod is never persisted.

## 2. Decisions (proposed)

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | The controller (the R1 app) runs **on the Mac** and talks to the API server with a **dedicated ServiceAccount token** bound to a namespaced Role, never the admin kubeconfig | Admin kubeconfig: simplest, but no least-privilege evidence. An in-cluster Deployment: needs an image, in-cluster Postgres access, and reachability; that belongs with R3's gateway deployment. | Teaches "what can the controller do if compromised" now, at almost no cost, and prepares EKS. |
| D2 | **Raw Kubernetes REST through `httpx`** (four calls), not the official client | The `kubernetes` package: standard, but heavy, and its exception and timeout surface makes the create-outcome classification less explicit | The classification in §5 is the core of R2. httpx is already a dependency. Roadmap: "create/watch/delete workloads through the API." |
| D3 | **Polling** each pass (one `LIST` plus one `GET` per active execution), no watch | Watch or informer: lower latency and load, but resourceVersion bookkeeping, `410 Gone` relists, and cache staleness | R1's loop already polls. Watch is a production optimization, explained but not built. |
| D4 | **Bare Pod** with `restartPolicy: Never` | Job with `backoffLimit: 0` | Already decided in R0 (execution-lifecycle.md). |
| D5 | **Completion keeps R1's per-execution credential,** delivered as the env var `EXECUTION_CREDENTIAL`. The agent calls `POST {RUNTIME_URL}/api/v1/executions/{id}/complete` at `http://192.168.5.2:<port>`. | Projected token plus TokenReview now: that is R3's gateway authentication. A Secret per execution: one more object and lifecycle. | Smallest change. **Limitation:** the credential is plaintext in the Pod spec (etcd, and any reader with `get pods` in the namespace; only the controller and cluster admins have that). It is removed in R3. |
| D6 | **Persist the credential hash in the claim transaction** (`PENDING → PROVISIONING`), before create, instead of after create. **Changes R1 §6 and §8.1.** | Keep R1: an adopted Pod (lost response, or crash at `after_create`) has no stored hash, so a *real* agent's completion gets 401, and the execution fails | Strictly better: the plaintext exists only in reconciler memory and in the spec the backend received. A crash before create loses it harmlessly (`LAUNCH_UNKNOWN` anyway). A lost response keeps completion possible after adoption. **Needs the user's approval: it reverses an accepted R1 limitation.** |
| D7 | **No claims without a successful observation:** each pass calls `list_owned()` first. If it raises `BackendUnavailable`, the pass claims no PENDING execution; deadlines, cancellation, and DB-only work still run. The same list feeds orphan collection. | R1 behavior: claim, create fails, stays PROVISIONING, then `LAUNCH_UNKNOWN` after recovery | An API outage or expired controller token would otherwise fail every queued execution. This narrows the loss to outages beginning mid-pass. Same at-most-once rule. |
| D8 | **Keep R1's three create outcomes.** No "definitely not sent, safe to retry" outcome yet. | A `CreateNotSent` outcome that returns the execution to PENDING (connect refused, `429`) | It would change LC-3's contract mid-stage and muddy R2's evidence. Recorded as an open question (§11). |
| D9 | **Two namespaces:** `agent-runtime` (the controller's ServiceAccount) and `agent-exec` (execution Pods, Pod Security Admission `restricted`, a ResourceQuota) | One namespace | The controller identity and the hostile workloads must not share a namespace or RBAC scope. |
| D10 | The **fake agent** is a small Python image, `agent-runtime/fake-agent:r2`, built locally. Its behavior is selected by `input.behavior`. | busybox shell scripts | It needs JSON, HTTP, and cgroup reads. `python:3.12-slim` is already local. |

## 3. Cluster setup (plain YAML in `deploy/k8s/`, applied with `kubectl --context colima`)

```text
Namespace agent-runtime
Namespace agent-exec
  labels: pod-security.kubernetes.io/enforce=restricted
          pod-security.kubernetes.io/enforce-version=v1.35
ServiceAccount agent-runtime/agent-runtime-controller
Role agent-exec/workload-controller:
  pods: create, get, list, delete            # no watch, update, patch, exec, log; no secrets
RoleBinding agent-exec/workload-controller → agent-runtime-controller
ServiceAccount agent-exec/agent-exec           # execution identity; no RoleBinding anywhere
ResourceQuota agent-exec/executions:
  pods: 4, requests.cpu: 500m, limits.cpu: "1", requests.memory: 384Mi,
  limits.memory: 640Mi, limits.ephemeral-storage: 320Mi
```

**Quota consequence:** with CPU, memory, and ephemeral-storage quota, the API rejects any Pod in `agent-exec` that omits those requests and limits. Test-injected Pods (§9) must reuse the template's resources.

**Make targets:**

- `make k8s-up` applies the manifests and writes, under the gitignored `.local/k8s/`:
  - `api-url`, read from the `colima` context;
  - `ca.crt`, from the kubeconfig;
  - `controller.token`, from `kubectl create token agent-runtime-controller -n agent-runtime --duration=24h`.
- `make k8s-token` refreshes the token.
- `make k8s-down` deletes both namespaces.
- `make agent-image` builds the fake agent.

**Safety guard:** every target, and the k8s test fixture, refuses to run unless:

- the context is `colima`;
- the API URL's host is `127.0.0.1` or `localhost`.

Nothing touches any other namespace.

## 4. The execution Pod (fixed template, not user-configurable)

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: exec-<id>
  namespace: agent-exec
  labels: {owner: agent-runtime, execution_id: <id>}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: <max(1, ceil(deadline_at - now))>
  terminationGracePeriodSeconds: 5
  serviceAccountName: agent-exec
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    seccompProfile: {type: RuntimeDefault}
  containers:
  - name: agent
    image: <AGENT_IMAGE>
    imagePullPolicy: Never
    env:   # EXECUTION_ID, EXECUTION_INPUT (JSON), EXECUTION_CREDENTIAL, RUNTIME_URL
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: [ALL]}
    resources:
      requests: {cpu: 50m, memory: 64Mi, ephemeral-storage: 16Mi}
      limits:   {cpu: 250m, memory: 128Mi, ephemeral-storage: 64Mi}
    volumeMounts:
    - {name: tmp, mountPath: /tmp}
    - {name: gateway-token, mountPath: /var/run/secrets/agent-cell, readOnly: true}
  volumes:
  - name: tmp
    emptyDir: {sizeLimit: 32Mi}
  - name: gateway-token
    projected:
      sources:
      - serviceAccountToken: {audience: agent-cell-gateway, expirationSeconds: 600, path: token}
```

- The Pod spec is built by one function from the execution record and settings only. No request field can add volumes, capabilities, host settings, images, or resources (ISO-5).
- The projected token is unused until R3. R2 only proves its properties (ISO-1).

## 5. `KubernetesBackend`

It implements R1's contract with these changes:

- `WorkloadSpec` gains `input` and `active_deadline_seconds`. The reconciler computes the latter from its `now`.
- `delete(name, uid=None)` takes the observed UID as a precondition.
- `FakeBackend` implements both.

**Configuration:**

- `WORKLOAD_BACKEND=fake|kubernetes` (default `fake`)
- `K8S_API_URL`, `K8S_CA_FILE`, `K8S_TOKEN_FILE` (re-read on every request, so token refresh needs no restart)
- `K8S_NAMESPACE=agent-exec`
- `AGENT_IMAGE`
- `RUNTIME_URL` (default `http://192.168.5.2:8000`)
- `MAX_ACTIVE_EXECUTIONS` (default 3)
- explicit connect and read timeouts (for example 2 s and 5 s)

**Outcome classification:**

| Call | Response | Result |
|---|---|---|
| create | `201` with a valid Pod | its `metadata.uid` |
| create | `409 AlreadyExists` | `CreateOutcomeUnknown` (re-observe next pass) |
| create | `400`, `401`, `403` (RBAC, Pod Security Admission, quota), `404` (namespace), `422` | `CreateRejected`: admission or authentication refused it, and it was not persisted |
| create | timeout, connection error, `429`, `5xx`, malformed `2xx` | `CreateOutcomeUnknown` |
| get | `200` with a valid Pod | `Workload`: phase per §1; `LOST` if `deletionTimestamp` is set |
| get | `404` whose `Status` names this Pod as NotFound | `None` |
| get | anything else, including `401`, `403`, an unrecognized `404`, timeout, malformed | `BackendUnavailable` |
| list | `200`, with `labelSelector=owner=agent-runtime` and no `resourceVersion` | Pods, with labels re-checked client-side |
| list | anything else | `BackendUnavailable` |
| delete | `200`, `202`, or a recognized `404` | done |
| delete | `409` on a UID precondition mismatch | done, nothing deleted (a different object) |
| delete | anything else | `BackendUnavailable` |

## 6. Reconciler changes (the only ones)

1. **D6:** in the claim transaction, generate the credential and store its hash with `PENDING → PROVISIONING`. The create success path records only `workload_uid`.
2. **D7:** call `list_owned()` at the start of the pass.
   - If it fails, skip PENDING claims for this pass.
   - Reuse the result for orphan collection, which keeps R1's fresh per-workload database lookup.
3. **Capacity (ISO-7):** claim PENDING executions only while `count(PROVISIONING or RUNNING) < MAX_ACTIVE_EXECUTIONS` (global, all tenants). Waiting executions stay PENDING, and their deadlines still apply. The ResourceQuota is the backstop: a quota rejection is `CreateRejected`, so `LAUNCH_FAILED`.
4. **Cleanup and orphan deletes** pass the observed UID to `delete`.

Everything else in R1 §8 is unchanged, including "never create twice" and the label rules.

## 7. Fake agent (`agent/fake_agent.py`, `agent/Dockerfile`)

**Image:**

- `FROM python:3.12-slim`
- `USER 65532`
- `PYTHONDONTWRITEBYTECODE=1`
- standard library only

The agent reads its configuration from env.

**Completion protocol:** it retries `409 INVALID_STATE` once a second for up to 30 s, because an adopted Pod can start before the execution is RUNNING. Any other failure exits 1. It never prints or returns `EXECUTION_CREDENTIAL`.

| `input.behavior` | Does |
|---|---|
| `complete` | Completes with `{"echo": input.payload}` |
| `exit` | Exits with `input.code` without completing |
| `sleep` | Sleeps `input.seconds`, then completes. With no value, it sleeps forever. |
| `inspect` | Completes with facts:<br>• the default token path `/var/run/secrets/kubernetes.io/serviceaccount` exists or not;<br>• the projected token's `aud`, `exp - iat`, and `sub`, decoded without verification;<br>• env var **names**;<br>• the HTTP status of `GET https://kubernetes.default.svc/api` with the projected token;<br>• its uid and gid;<br>• whether writing `/probe` (root filesystem) fails;<br>• its effective capabilities (`CapEff` from `/proc/self/status`). |
| `write_sentinel` / `check_sentinel` | Writes and reads back `/tmp/sentinel` / reports whether it exists |
| `cpu_burn` | Burns CPU for `input.seconds`, then completes with `/sys/fs/cgroup/cpu.stat` (`nr_throttled`, `throttled_usec`) |
| `oom` | Allocates until killed |
| `fill_disk` | Writes to `/tmp` past its size limit |
| `work` | Fixed CPU work, then completes with the elapsed seconds (used to measure a neighbor's slowdown) |

## 8. Changes to R1 documents (in the same commit as the code)

- **r1-spec §6:** the launch credential's hash is persisted when the launch is claimed (D6).
- **r1-spec §7:** adds `WorkloadSpec.input`, `WorkloadSpec.active_deadline_seconds`, and `delete(name, uid=None)`.
- **r1-spec §8:**
  - step 1 stores the hash at claim (D6);
  - claims require a successful `list_owned()` in the pass (D7);
  - the capacity limit;
  - remove the sentence accepting that an adopted execution without a persisted credential will time out.

## 9. Tests (named by invariant ID)

1. **Preflight** (`make test-k8s` runs it first; it fails loudly):
   - the guard (§3);
   - namespaces and RBAC exist;
   - the agent image exists;
   - CPU throttling counters are readable;
   - a projected token with the gateway audience is issued.
2. **`make test`** (no cluster) adds `KubernetesBackend` tests against `httpx.MockTransport`:
   - every row of the §5 table, including the unrecognized-404 case and the UID precondition;
   - Pod-template golden tests: every field in §4; no request input changes anything but `EXECUTION_INPUT`.

   R1's tests still pass, updated for D6 (adopted executions now store a hash and can complete) and D7. The R1 test fixture sets a high `MAX_ACTIVE_EXECUTIONS` except in capacity tests.
3. **`make test-k8s`** (marked `k8s`), against Colima.
   - **Harness:**
     - the runtime runs as a real uvicorn server on `127.0.0.1:<free port>`, with `RUNTIME_URL=http://192.168.5.2:<port>` so agents can call back;
     - the wall clock;
     - R1's in-process Mock Workday fake.
   - **Admin actions** use `kubectl --context colima` as the trusted harness: external deletes, injected Pods, RBAC changes, and status reads.
   - **Lost responses and outages** are injected with an httpx transport wrapper around the real API (send, then raise `ReadTimeout`; or raise `ConnectError` without sending). The wrapper counts create POSTs, and it outlives a simulated restart.
   - **Isolation between tests:** each test starts and ends with no Pods in `agent-exec`.

| ID | What to test against the real cluster |
|---|---|
| HAPPY | Create through the API → the reconciler launches a Pod → the agent calls back and completes → GET returns the result → the Pod is deleted. The execution outcome remains after cleanup. |
| LC-5 | Lost create response (the Pod was really created) → the next pass adopts it (labels match) → exactly one Pod and one create POST → the agent completes successfully (proves D6) |
| LC-3 | (a) Failpoint `after_claim`, then a restart with a **new `KubernetesBackend` instance**: `LAUNCH_UNKNOWN`, zero create POSTs.<br>(b) Failpoint `after_create`, then a restart: adopted, completes.<br>(c) Lost response, then an admin deletes the Pod: `LAUNCH_UNKNOWN`, one create POST across the restart, and no `exec-<id>` Pod after three more passes.<br>(d) After `LAUNCH_UNKNOWN`, an admin creates a correctly labeled `exec-<id>` Pod: it is deleted, and the execution stays `FAILED (LAUNCH_UNKNOWN)`. |
| LC-6 | An admin deletes the Pod of a sleeping RUNNING execution: `FAILED (POD_LOST)`, deterministically. This includes observation during termination (`deletionTimestamp`). No new Pod, and no new create POST. |
| LC-7 | (a) Transport `ConnectError` on every call.<br>(b) The admin deletes the controller's RoleBinding, so reads return `403`.<br>In both: no `POD_LOST`, no create, no delete, and no PENDING execution claimed (D7); cancel and deadline still apply. After restoring access, the system converges: cleanup happens, and the waiting execution launches. |
| LC-8 | `timeout_seconds=30`, an agent that sleeps forever, and the reconciler **stopped** after launch: the Pod reaches `Failed` with reason `DeadlineExceeded` without the controller. A restarted reconciler marks it `TIMED_OUT` and deletes the Pod. |
| LC-11 | A Pod-Security-compliant unlabeled Pod and a Pod labeled `owner=other` in `agent-exec` stay untouched. An owned-labeled `exec-<uuid>` with no record is deleted only after a successful DB lookup. With the DB unavailable: no deletes. Positive control: the controller *can* delete a Pod it owns. |
| ISO-1 | `inspect`: the default token path is absent; the projected token has `aud == ["agent-cell-gateway"]`, a lifetime of at most 600 s, and `sub == system:serviceaccount:agent-exec:agent-exec`; the Kubernetes API answers it with **401** (the network allowed the call; authentication rejected the audience). Admin check: `kubectl auth can-i --list --as=system:serviceaccount:agent-exec:agent-exec -n agent-exec` shows no resource permissions beyond the built-in self-review and discovery rules (`selfsubjectreviews`, `selfsubjectaccessreviews`, `selfsubjectrulesreviews`, non-resource URLs). |
| ISO-5 | **Admission:** with the controller's own token, creating Pods that use each of `privileged`, `hostNetwork`, `hostPID`, `hostIPC`, `hostPath`, `hostPort`, added capability `NET_ADMIN`, and `runAsUser: 0` returns `403`, so `CreateRejected`.<br>**Template:** `inspect` shows uid 65532, a failing root-filesystem write, and an empty `CapEff`.<br>**Positive control:** the real template is admitted. |
| ISO-6 | Execution A runs `write_sentinel` and proves it read the sentinel back. Execution B runs `check_sentinel` and reports it absent. (The cluster has one node, so both ran on it.) |
| ISO-7 | (a) `cpu_burn` reports `nr_throttled > 0`.<br>(b) `oom` → Pod reason `OOMKilled` → `FAILED (EXITED_WITHOUT_COMPLETION)`.<br>(c) `fill_disk` → Pod evicted → `FAILED`.<br>(d) **Containment:** `work` alone gives a baseline; then `work` runs beside `cpu_burn` and `oom`. The contended time must be at most 2 × baseline + 2 s, and the node stays `Ready`. Measured numbers are recorded in [experiments.md](experiments.md).<br>(e) With `MAX_ACTIVE_EXECUTIONS=2`, a third execution stays PENDING until one finishes.<br>(f) The quota backstop: a direct create beyond the quota → `CreateRejected`. |
| ID-8 | `inspect`: env var names are exactly `EXECUTION_ID`, `EXECUTION_INPUT`, `EXECUTION_CREDENTIAL`, `RUNTIME_URL`, plus the image's standard ones (`PATH`, `HOME`, `LANG`, `PYTHON*`, `GPG_KEY`, `HOSTNAME`) and the API server's `KUBERNETES_SERVICE_*` and `KUBERNETES_PORT*` variables. The kubelet injects the latter even with `enableServiceLinks: false`; they are an address, not a credential (to verify). There are no other services' variables, no Mock Workday or AWS variable, and no default token. |

**Timing:** the k8s suite may take a few minutes (LC-8 waits about 35 s). Tests must poll with timeouts, never fixed sleeps longer than needed.

## 10. Done when

- `make test` (no cluster), `make test-integration` (Mock Workday), and `make test-k8s` (Colima) all pass.
- The README documents setup (`make agent-image k8s-up`), the token refresh, and teardown.
- There is no NetworkPolicy, gateway, AWS, or EKS code.
- ISO-7 containment measurements are recorded in [experiments.md](experiments.md).

## 11. Open questions (not blocking R2)

1. **`CreateNotSent` (D8):**
   - Connection refused or `429` on create definitely did not persist anything. Returning the execution to PENDING would recover availability without violating at-most-once.
   - A read timeout, `5xx`, or `409` must stay ambiguous.
   - Candidate for R8.
2. **Watch-based observation (D3):** when polling cost matters on EKS.
3. **In-cluster controller and RBAC on EKS:** with R3, alongside the gateway Deployment.
4. **User namespaces (`hostUsers: false`) and gVisor or Kata:** require runtime support that the Docker runtime here likely lacks. Revisit for hostile-code hardening (R3 or R8).
