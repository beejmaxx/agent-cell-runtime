# R2 specification: Kubernetes execution (local k3s)

**Status:** approved for implementation (2026-10-07), after a ChatGPT review whose amendments are merged here. D6 reverses an accepted R1 limitation.

R2 replaces R1's in-memory workload backend with real Pods on the local k3s cluster, behind the same `WorkloadBackend` contract. It implements the R2 scope in [invariants.md](invariants.md):

- **Lifecycle:** LC-3, LC-5, LC-6, LC-7, LC-8 (R2 part), LC-11.
- **Isolation:** ISO-1, ISO-5, ISO-6, ISO-7.
- **Identity:** ID-8 (R2 part).
- **The mandatory happy path.**

Design rationale is in [execution-lifecycle.md](execution-lifecycle.md) and [trust-boundaries.md](trust-boundaries.md). [r1-spec.md](r1-spec.md) still applies except where this document changes it (§8).

**The evidence R2 must produce:**

- The R1 reconciler, with the few changes in §6, keeps its lifecycle invariants when the backend is a real, slow, partially failing Kubernetes API.
- An execution Pod receives no credentials the runtime did not intend to give it.

**Not in R2:**

- the gateway and TokenReview;
- NetworkPolicy and every network-isolation claim (ISO-2, ISO-3, ISO-4 are R3 and R6);
- deploying the controller inside the cluster;
- watches and informers;
- Jobs and CRDs;
- multiple controller replicas;
- a Secret per execution;
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
| The installed kubectl is 1.32, outside the supported ±1 minor-version skew for server 1.35 | `kubectl version` |

**Observation (unverified as policy):** anything in the VM, including `rook-dev` Pods, can likely reach every service bound to the Mac's loopback through `192.168.5.2`, including Mock Workday on 18080 and the runtime API. R2 makes no isolation claim about this. R3 adds network enforcement.

**Unverified, to check first (§9, preflight):**

- cgroup v2 CPU controls (`cpu.max`, `cpu.stat`) are visible inside the container;
- the kubelet enforces emptyDir `sizeLimit` and `ephemeral-storage` limits under the Docker runtime (through cri-dockerd);
- a projected service-account token with a custom audience and Pod binding is issued.

If a mechanism is not enforced on this cluster, **stop and report the evidence gap.** Do not weaken or skip the test, and do not claim §10 is met. The user then chooses an environment fix or an explicit, scoped deferral.

## 1. Mechanisms R2 depends on

These are the Kubernetes behaviors the design relies on. Each one is either tested in §9 or explicitly assumed.

- **201 confirms persistence, not readiness.** For an ordinary (not dry-run) create, storage succeeds before the response is produced. Scheduling and the container start may happen at any time after persistence, **even before the controller receives the 201.** RUNNING still means "created and UID recorded" (execution-lifecycle.md), not "process started."
- **A lost response is real.** If the client times out after the API server committed the object, the Pod exists, and the client does not know it. Retrying a create with the same name returns `409 AlreadyExists`. That is why the reconciler re-observes and never re-creates.
- **A rejection is about one request only.** A definite rejection proves *that request* stored nothing. It does not prove no Pod with the name can ever appear: an earlier ambiguous request could still become visible. That is handled by "never create twice," plus LC-3(d) cleanup.
- **Required read consistency.** Decision reads must use the "most recent" semantics:
  - `GET` and `LIST` with `resourceVersion` unset are "most recent";
  - `resourceVersion=0` permits "any," which can be stale;
  - the API server may serve a most-recent read from its watch cache and still be consistent. What R2 requires is the consistency, not a storage path.

  R2 never sends `resourceVersion` on decision reads.
- **NotFound is a snapshot, asserted by the authority.**
  - A 404 counts as absence only when the body is a Kubernetes `Status` with `reason: NotFound`, `details.kind: pods`, and `details.name` equal to the requested name. Any other 404 (wrong base URL, proxy page) is `BackendUnavailable`.
  - A valid NotFound means "absent now," not "never existed."
- **Deletion is asynchronous.**
  - A successful `DELETE` sets `metadata.deletionTimestamp`. The Pod stays visible, often still `Running`, through its grace period. Process termination is a separate step.
  - "Delete accepted" does not mean "confirmed absent." Cleanup keeps observing on later passes.
  - **Lab policy, not a Kubernetes phase:** a Pod with `deletionTimestamp` set maps to `LOST`. This makes the failure reason deterministic for an external delete. It does not establish the physical ordering of every crash-and-delete race.
- **Delete preconditions.** `DELETE` with `preconditions.uid` deletes only that exact object. A different object that reuses the name gets `409 Conflict` and is untouched. A 409 therefore means "the observed object was not deleted," not "the name is absent."
- **Object identity.** A Pod is identified by its UID. Once an execution has recorded `workload_uid`, a Pod with the same name and a different UID is a different workload. It is never adopted or treated as the original.
- **Pod phases.** These are workload observations, not execution outcomes:

  | Kubernetes phase | Workload phase |
  |---|---|
  | `Pending` | `RUNNING` (alive, not yet started) |
  | `Running` | `RUNNING` |
  | `Succeeded` | `SUCCEEDED` |
  | `Failed` | `FAILED` |
  | `Unknown` | `RUNNING` |

  - A workload `SUCCEEDED` (exit 0) without a committed completion is an execution **`FAILED (EXITED_WITHOUT_COMPLETION)`** (LC-9).
  - `Unknown` means the node stopped reporting. That is unable-to-observe, not absence or failure (LC-7); the deadline still applies.
  - A Pod stuck in `Pending` (for example a missing image) ends at its deadline.
- **`activeDeadlineSeconds`** counts from the Pod's start time, set by the kubelet. It needs a functioning kubelet. The kubelet then kills the Pod, and the phase becomes `Failed` with reason `DeadlineExceeded`. It is a backstop, not an implementation of the absolute `deadline_at`, and it does not cover every node or controller failure (LC-8).
- **Environment variable expansion.** The kubelet expands `$(NAME)` references to earlier variables inside literal `env` values, and turns `$$` into `$`, without any shell. Untrusted JSON placed in an env value can therefore arrive altered. R2 base64-encodes the input (§4).
- **Projected tokens.**
  - The kubelet *requests* a token for the Pod's service account, with the configured audience and expiry. The API server issues it, bound to the Pod (its claims include the Pod name and UID).
  - Explicit projection works with `automountServiceAccountToken: false`, which only removes the default API token volume. That default volume normally also carries the cluster CA certificate, so R2 projects the CA separately.
- **Pod Security Admission `restricted`** rejects a *valid* Pod that violates the profile with `403 Forbidden` and a message naming PodSecurity, at create time; the Pod is not persisted. An *invalid* manifest may be rejected earlier by validation, for example `privileged: true` together with `allowPrivilegeEscalation: false`. Server-side dry-run (`dryRun=All`) runs admission without persisting anything.
- **Pod-create permission is broad.** A principal that can create Pods in a namespace can:
  - mount that namespace's Secrets and ConfigMaps;
  - run Pods as any ServiceAccount in it.

  So "no `get secrets`" does not stop a compromised controller from reaching namespace secrets. **`agent-exec` must hold no Secrets, privileged ServiceAccounts, or downstream credentials.** Pod Security Admission still bounds which Pods the controller can create.
- **Labels are trusted only because creation is restricted.** Anyone who can create Pods in the execution namespace can forge `owner=agent-runtime`. The trust root is RBAC on that namespace (§3).
- **ResourceQuota on CPU and memory** makes the API reject Pods that omit CPU or memory requests and limits. It does **not** require ephemeral-storage fields. The fixed template is what guarantees storage limits.

## 2. Decisions

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | The controller (the R1 app) runs **on the Mac** and talks to the API server with a **dedicated ServiceAccount token** bound to a namespaced Role, never the admin kubeconfig | Admin kubeconfig: simplest, but no least-privilege evidence. An in-cluster Deployment: needs an image, in-cluster Postgres access, and reachability; that belongs with R3's gateway deployment. | Teaches "what can the controller do if compromised" now, at almost no cost, and prepares EKS. |
| D2 | **Raw Kubernetes REST through `httpx`** (four calls), not the official client. No automatic transport retries on create. | The `kubernetes` package: standard, but heavy, and its exception and timeout surface makes the create-outcome classification less explicit | The classification in §5 is the core of R2. httpx is already a dependency. |
| D3 | **Polling** each pass (one `LIST` plus one `GET` per active execution), no watch | Watch or informer: lower latency and load, but resourceVersion bookkeeping, `410 Gone` relists, and cache staleness | R1's loop already polls. Watch is a production optimization, explained but not built. |
| D4 | **Bare Pod** with `restartPolicy: Never` | Job with `backoffLimit: 0` | Already decided in R0 (execution-lifecycle.md). |
| D5 | **Completion keeps R1's per-execution credential,** delivered as the env var `EXECUTION_CREDENTIAL`. The agent calls `POST {RUNTIME_URL}/api/v1/executions/{id}/complete` at `http://192.168.5.2:<port>`. | Projected token plus TokenReview now: that is R3's gateway authentication. A Secret per execution: one more object and lifecycle. | Smallest change. **Temporary scaffolding, not confidential delivery:** the credential is plaintext in the Pod spec (etcd, and any reader with `get pods` in the namespace; only the controller and cluster admins have that) and sent over plain HTTP. It is removed in R3. |
| D6 | **Persist the credential hash in the claim transaction** (`PENDING → PROVISIONING`), before create. Adoption never replaces the credential. **Changes R1 §6 and §8.1.** | Keep R1: an adopted Pod (lost response, or crash at `after_create`) has no stored hash, so a real agent's completion gets 401, and the execution fails | The plaintext is needed only to build the one create request. A crash before create loses it harmlessly (`LAUNCH_UNKNOWN` anyway). A lost response keeps completion possible after adoption. |
| D7 | **No claims without a successful observation:** each pass calls `list_owned()` first. If it raises `BackendUnavailable`, the pass claims no PENDING execution; deadlines, cancellation, and DB-only work still run. The same list feeds orphan collection. | R1 behavior: claim, create fails, stays PROVISIONING, then `LAUNCH_UNKNOWN` after recovery | Avoids failing every queued execution during a known outage or with an expired controller token. A successful LIST does not guarantee the next create succeeds; it does not need to. |
| D8 | **Keep R1's three create outcomes.** No "definitely not sent, safe to retry" outcome yet. | A `CreateNotSent` outcome that returns the execution to PENDING (connect refused, `429`) | It would change LC-3's contract mid-stage. Open question (§11). |
| D9 | **Two namespaces:** `agent-runtime` (the controller's ServiceAccount) and `agent-exec` (execution Pods, Pod Security Admission `restricted`, a ResourceQuota, no Secrets) | One namespace | Pod-create permission implies access to namespace Secrets and ServiceAccounts (§1), so the controller identity and downstream credentials must live outside `agent-exec`. |
| D10 | The **fake agent** is a small Python image, `agent-runtime/fake-agent:r2`, built locally. Its behavior is selected by `input.behavior`. | busybox shell scripts | It needs JSON, HTTP, base64, and cgroup reads. `python:3.12-slim` is already local. |

## 3. Cluster setup (plain YAML in `deploy/k8s/`)

```text
Namespace agent-runtime    label lab.agent-runtime/owned=true
Namespace agent-exec       label lab.agent-runtime/owned=true
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

`agent-exec` contains no Secrets. The only ConfigMap is the automatically published, public `kube-root-ca.crt`.

**Tooling:** the harness and Make targets use a pinned kubectl 1.35.x at `.local/bin/kubectl`, downloaded and checksum-verified from `dl.k8s.io` by `make k8s-tools`. The global kubectl is not modified.

**Make targets:**

- `make k8s-up` applies the manifests and writes, under the gitignored `.local/k8s/`:
  - `api-url`, read from the `colima` context;
  - `ca.crt`, from the kubeconfig;
  - `controller.token`, from `kubectl create token agent-runtime-controller -n agent-runtime --duration=24h`.

  It refuses to adopt an existing namespace that lacks the `lab.agent-runtime/owned=true` marker.
- `make k8s-token` refreshes the token.
- `make k8s-down` deletes only namespaces carrying the marker.
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
    env:   # EXECUTION_ID, EXECUTION_INPUT_B64 (base64 of canonical JSON), EXECUTION_CREDENTIAL, RUNTIME_URL
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: [ALL]}
    resources:
      requests: {cpu: 50m, memory: 64Mi, ephemeral-storage: 16Mi}
      limits:   {cpu: 250m, memory: 128Mi, ephemeral-storage: 64Mi}
    volumeMounts:
    - {name: tmp, mountPath: /tmp}
    - {name: agent-cell, mountPath: /var/run/secrets/agent-cell, readOnly: true}
  volumes:
  - name: tmp
    emptyDir: {sizeLimit: 32Mi}
  - name: agent-cell
    projected:
      sources:
      - serviceAccountToken: {audience: agent-cell-gateway, expirationSeconds: 600, path: token}
      - configMap: {name: kube-root-ca.crt, items: [{key: ca.crt, path: ca.crt}]}
```

- The Pod spec is built by one function from the execution record and settings only. No request field can add volumes, capabilities, host settings, images, resources, `envFrom`, or Secret references (ISO-5).
- **Input is base64-encoded,** so no `$(...)` or `$$` expansion can alter it. The other env values never contain `$`: the ID is a UUID, the credential comes from `token_urlsafe`, and the URL is from configuration.
- The projected token is unused until R3. R2 only proves its properties (ISO-1). `ca.crt` is the public cluster CA, not a credential.
- The image contains `/rootfs-probe/`, owned by UID 65532 and outside every mount. It exists so the read-only-root test can isolate that control.

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

**A Pod is valid only if** it has the configured namespace, the requested name, a non-empty `metadata.uid`, and labels `owner=agent-runtime` and `execution_id` matching the name. Anything else from a 2xx response counts as malformed.

**Outcome classification:**

| Call | Response | Result |
|---|---|---|
| create | `201` with a valid Pod | its `metadata.uid` |
| create | `409 AlreadyExists` | `CreateOutcomeUnknown` (re-observe next pass) |
| create | `400`, `401`, `403` (RBAC, Pod Security Admission, quota), `404` (namespace), `422` | `CreateRejected`: this request was refused before storage |
| create | **anything else:** timeout, connection error, `429`, `5xx`, an unlisted status, a malformed body | `CreateOutcomeUnknown`. An unexpected response is never success and never a definite rejection. |
| get | `200` with a valid Pod | `Workload`: phase per §1; `LOST` if `deletionTimestamp` is set |
| get | `404` whose `Status` names this Pod as NotFound | `None` |
| get | anything else, including `401`, `403`, an unrecognized `404`, timeout, malformed | `BackendUnavailable` |
| list | `200`, with `labelSelector=owner=agent-runtime` and no `resourceVersion` | valid Pods only; anything else in the response is skipped and never deleted |
| list | anything else | `BackendUnavailable` |
| delete | `200`, `202`, or a recognized `404` | request accepted. Not proof of absence: keep observing. |
| delete | `409` on a UID precondition mismatch | nothing deleted (a different object) |
| delete | anything else | `BackendUnavailable` |

## 6. Reconciler changes (the only ones)

1. **D6:** in the claim transaction, generate the credential and store its hash with `PENDING → PROVISIONING`. The create success path records only `workload_uid`. Adoption never changes the credential.
2. **D7:** call `list_owned()` at the start of the pass.
   - If it fails, skip PENDING claims for this pass.
   - Reuse the result for orphan collection, which keeps R1's fresh per-workload database lookup.
3. **UID identity:** a RUNNING execution whose observed Pod has a UID different from `workload_uid` → `FAILED (POD_LOST)`, because the recorded workload is gone. The other Pod is cleaned up by label rules, with its own UID as the delete precondition.
4. **Capacity (ISO-7):** claim PENDING executions only while `count(PROVISIONING or RUNNING) < MAX_ACTIVE_EXECUTIONS` (global, all tenants). Waiting executions stay PENDING, and their deadlines still apply.
   - This counts executions holding launch authority, not live processes: a terminal execution's Pod may still be shutting down when its slot is reused.
   - The ResourceQuota is the separate admission backstop. A quota rejection is `CreateRejected`, so `LAUNCH_FAILED`.
5. **Cleanup and orphan deletes** pass the observed UID to `delete`.
6. **Deadline precedence:** unchanged from R1 and now explicit. The persisted-deadline check runs before workload observation, so an overdue execution whose Pod shows `DeadlineExceeded` becomes `TIMED_OUT`, not `FAILED`.

Everything else in R1 §8 is unchanged, including "never create twice" and the label rules.

## 7. Fake agent (`agent/fake_agent.py`, `agent/Dockerfile`)

**Image:**

- `FROM python:3.12-slim`
- `/rootfs-probe/` owned by 65532
- `USER 65532`
- `PYTHONDONTWRITEBYTECODE=1`
- standard library only

It decodes `EXECUTION_INPUT_B64`.

**Completion protocol:** it always resends the identical payload, once a second, within a 30 s lab liveness bound.

- **Retry on:** connection errors and timeouts, `5xx`, and `409` with code `INVALID_STATE` (an adopted Pod can start before the execution is RUNNING).
- **Stop, exit 1:** `401`, `422`, and `409 COMPLETION_CONFLICT`.
- **Success:** `200` (including a replay), exit 0.

The 30 s bound means an adoption delayed longer than that can make a recoverable agent give up. That is accepted and documented. The agent never prints or returns `EXECUTION_CREDENTIAL` or the projected token.

| `input.behavior` | Does |
|---|---|
| `complete` | Completes with `{"echo": input.payload}` |
| `exit` | Exits with `input.code` without completing |
| `sleep` | Sleeps `input.seconds`, then completes. With no value, it sleeps forever. |
| `inspect` | Completes with facts:<br>• the default token directory `/var/run/secrets/kubernetes.io/serviceaccount` exists or not;<br>• the projected token's claims, decoded without verification: `aud`, `exp - iat`, `sub`, and the bound Pod name and UID;<br>• env var **names**;<br>• the HTTP status of `GET https://kubernetes.default.svc/api` with the projected token, verifying TLS with the projected `ca.crt`;<br>• its uid and gid;<br>• the errno of writing `/rootfs-probe/x`;<br>• its effective capabilities (`CapEff` from `/proc/self/status`);<br>• its mount points (from `/proc/self/mounts`). |
| `write_sentinel` / `check_sentinel` | Writes and reads back `/tmp/sentinel` / reports whether it exists |
| `cpu_burn` | Reads `cpu.max` and `cpu.stat`, burns CPU for `input.seconds`, then completes with `cpu.max` and the **change** in `nr_throttled` and `throttled_usec` |
| `oom` | Allocates past the memory limit |
| `fill_disk` | Writes a **fixed** `input.mib` to `/tmp`, then sleeps until killed or until its deadline. It never writes without bound. |
| `work` | Fixed CPU work, then completes with its start time, end time, and elapsed seconds |

## 8. Changes to R1 documents (in the same commit as the code)

- **r1-spec §6:** the launch credential's hash is persisted when the launch is claimed (D6).
- **r1-spec §7:**
  - adds `WorkloadSpec.input`, `WorkloadSpec.active_deadline_seconds`, and `delete(name, uid=None)`;
  - workload phases are observations, not execution outcomes.
- **r1-spec §8:**
  - step 1 stores the hash at claim (D6);
  - claims require a successful `list_owned()` in the pass (D7);
  - UID identity (§6.3);
  - the capacity limit;
  - remove the sentence accepting that an adopted execution without a persisted credential will time out.

## 9. Tests (named by invariant ID)

1. **Preflight** (`make test-k8s` runs it first; it fails loudly):
   - the guard (§3);
   - the pinned kubectl is 1.35.x;
   - namespaces carry the marker, and RBAC exists;
   - the agent image exists;
   - inside a Pod: `cpu.max` and `cpu.stat` are readable, and a projected token with the gateway audience and Pod-UID claim is issued.
2. **`make test`** (no cluster) adds `KubernetesBackend` tests against `httpx.MockTransport`:
   - every row of the §5 table, including the unrecognized-404 case, the create catch-all, invalid Pods in `2xx` responses, and the UID precondition request;
   - Pod-template golden tests: every field in §4; no request input changes anything but `EXECUTION_INPUT_B64`; no `envFrom` and no Secret reference.

   R1's tests still pass, updated:
   - D6: adopted executions now store a hash and can complete;
   - D7;
   - UID identity;
   - a unit test that credential A cannot complete execution B while B is RUNNING, with A completing as the positive control (extends R1's existing binding test).

   The R1 test fixture sets a high `MAX_ACTIVE_EXECUTIONS`, except in capacity tests.
3. **`make test-k8s`** (marked `k8s`), against Colima.
   - **Harness:**
     - the runtime runs as a real uvicorn server on `127.0.0.1:<free port>`, with `RUNTIME_URL=http://192.168.5.2:<port>` so agents can call back;
     - the wall clock;
     - R1's in-process Mock Workday fake.
   - **Admin actions** use the pinned kubectl with context `colima`, as the trusted harness: external deletes, injected Pods, RBAC changes, dry-runs, and status reads.
   - **Lost responses and outages** are injected with an httpx transport wrapper around the real API (send, then raise `ReadTimeout`; or raise `ConnectError` without sending). The wrapper counts create POSTs, and it outlives a simulated restart.
   - **Evidence before cleanup:** failure evidence (Pod status, container termination reasons, eviction messages) is captured before cleanup can remove the Pod. A test may hold the reconciler or arm `before_cleanup` for this.
   - **Isolation between tests:** each test starts and ends with no Pods in `agent-exec`.

| ID | What to test against the real cluster |
|---|---|
| HAPPY | Create through the API → the reconciler launches a Pod → the agent calls back and completes → GET returns the result → the Pod is deleted. The outcome remains after cleanup.<br>**Input round trip:** the result echoes a payload containing `$(EXECUTION_ID)`, `$$`, quotes, Unicode, and newlines, byte for byte. |
| LC-5 | Lost create response (the Pod was really created) → the next pass adopts it (labels and UID valid) → exactly one Pod and one create POST → the agent completes successfully (proves D6) |
| LC-3 | (a) Failpoint `after_claim`, then a restart with a **new `KubernetesBackend` instance**: `LAUNCH_UNKNOWN`, zero create POSTs.<br>(b) Failpoint `after_create`, then a restart: adopted, completes.<br>(c) Lost response, then an admin deletes the Pod: `LAUNCH_UNKNOWN`, one create POST across the restart, and no `exec-<id>` Pod after three more passes.<br>(d) After `LAUNCH_UNKNOWN`, an admin creates a correctly labeled `exec-<id>` Pod: it is deleted, and the execution stays `FAILED (LAUNCH_UNKNOWN)`. |
| LC-6 | An admin deletes the Pod of a sleeping RUNNING execution: `FAILED (POD_LOST)`, deterministically, including when observed during termination. No new Pod, and no new create POST.<br>**UID replacement:** an admin deletes the Pod and creates a same-name, correctly labeled replacement: `POD_LOST`, never adoption; the replacement is deleted using its own UID. |
| LC-7 | (a) Transport `ConnectError` on every call.<br>(b) The admin deletes the controller's RoleBinding, so reads return `403`.<br>In both: no `POD_LOST`, no create, no delete, and no PENDING execution claimed (D7); cancel and deadline still apply. After restoring access, the system converges: cleanup happens, and the waiting execution launches. |
| LC-8 | `timeout_seconds=30`, an agent that sleeps forever, and the reconciler **stopped** after launch: the Pod reaches `Failed` with reason `DeadlineExceeded` without the controller. A restarted reconciler marks it `TIMED_OUT` (deadline precedence, not `FAILED`) and deletes the Pod. |
| LC-9 (R2 check) | `exit` with code 0 → Pod `Succeeded` → execution `FAILED (EXITED_WITHOUT_COMPLETION)`.<br>A committed completion stays `SUCCEEDED` through later cleanup observations.<br>**Lost completion response:** arm `before_cleanup` so the complete route crashes after commit and the agent sees a 5xx → the agent resends the identical payload → `200` replay with the same result → one terminal transition. |
| LC-11 | A Pod-Security-compliant unlabeled Pod and a Pod labeled `owner=other` in `agent-exec` stay untouched. An owned-labeled `exec-<uuid>` with no record is deleted only after a successful DB lookup. With the DB unavailable: no deletes. Positive control: the controller *can* delete a Pod it owns.<br>**UID precondition, real mechanism:** observe Pod N with UID A; the admin replaces it with UID B; the backend deletes N with precondition A → `409`, and B remains. |
| ISO-1 | `inspect`: the default token directory is absent; the projected token has `aud == ["agent-cell-gateway"]`, a lifetime of at most 600 s, `sub == system:serviceaccount:agent-exec:agent-exec`, and a Pod UID claim equal to the UID the harness observed; over verified TLS, the Kubernetes API rejects it (`401`).<br>**Claimed evidence:** "the token carries the intended claims and Pod binding, and the Kubernetes API rejects it." Proving rejection *because of* audience is R3 (TokenReview).<br>**Admin checks:** `kubectl auth can-i --list --as=system:serviceaccount:agent-exec:agent-exec -n agent-exec` shows only the built-in self-review and discovery permissions. |
| ISO-5 | **Admission:** each negative manifest is the real template with exactly one change, otherwise API-valid, created with the controller's token as a **server-side dry-run** (nothing persists even if admission were broken). Changes: `privileged: true` with `allowPrivilegeEscalation: true`, `hostNetwork`, `hostPID`, `hostIPC`, a `hostPath` volume, `hostPort`, added capability `NET_ADMIN`, `runAsUser: 0`. Each returns `403` with a message naming PodSecurity and the specific violation, not RBAC or quota.<br>**Test the test:** the same manifests pass admin dry-run in a temporary namespace without Pod Security labels, which proves they are otherwise valid. The real template passes dry-run with the controller's token.<br>**Read-only root:** `inspect` shows writing `/rootfs-probe/x` fails with `EROFS`. Control: an admin-created Pod from the same image with only `readOnlyRootFilesystem: false` writes and reads it back.<br>**Template:** uid 65532 and an empty `CapEff`. |
| ISO-6 | Execution A runs `write_sentinel` and proves it read the sentinel back. Execution B runs `check_sentinel` and reports it absent. (The cluster has one node, so both ran on it.) |
| ISO-7 | (a) `cpu_burn` reports `cpu.max` equal to the 250m limit (`25000 100000`), and an **increase** in `nr_throttled` during the burn.<br>(b) `oom` → the agent container's `state.terminated.reason == OOMKilled` → `FAILED (EXITED_WITHOUT_COMPLETION)`.<br>(c) `fill_disk` with a fixed 48 MiB (above the 32 MiB emptyDir limit, below the 64 Mi container limit) → evicted, with an eviction message citing the emptyDir limit, and the node shows no `DiskPressure` → `FAILED`.<br>(d) **Containment:** `work` alone gives a baseline. Then a `cpu_burn` runs for longer than the whole measurement, with its running state verified before `work` starts and after it ends, and `work` runs again. The contended time must be at most 2 × baseline + 2 s, and the node stays `Ready`. Measured numbers are recorded in [experiments.md](experiments.md). (OOM is tested in (b), not here: a Pod that dies first exercises no contention.)<br>(e) With `MAX_ACTIVE_EXECUTIONS=2`, a third execution stays PENDING until one finishes.<br>(f) The quota backstop: a direct create beyond the quota → `CreateRejected`, with a message naming quota. |
| ID-8 | The claim is "no unintended credentials were injected," not "no secret exists anywhere."<br>**Admitted Pod spec:**<br>• only the `tmp` and `agent-cell` volumes;<br>• no Secret references;<br>• no `envFrom`;<br>• env names exactly `EXECUTION_ID`, `EXECUTION_INPUT_B64`, `EXECUTION_CREDENTIAL`, `RUNTIME_URL`.<br>**In the Pod (`inspect`):**<br>• env names are those four, plus the image's baseline names taken from `docker image inspect`, plus the kubelet's `KUBERNETES_SERVICE_*` and `KUBERNETES_PORT*` (an address, injected despite `enableServiceLinks: false`);<br>• mounts include no default service-account token. |
| Controller RBAC | With the controller's token, `SelfSubjectAccessReview`: allowed `create`, `get`, `list`, `delete` on Pods in `agent-exec`. Denied:<br>• Secrets, `pods/exec`, `pods/log`, `update` and `patch` on Pods in `agent-exec`;<br>• Pods in `agent-runtime`, `default`, and `rook-dev`;<br>• cluster-scoped resources. |

**Timing:** the k8s suite may take a few minutes (LC-8 waits about 35 s). Tests poll with timeouts, never fixed sleeps longer than needed.

## 10. Done when

- `make test` (no cluster), `make test-integration` (Mock Workday), and `make test-k8s` (Colima, pinned kubectl 1.35) all pass.
  - Alternatively, a preflight evidence gap is reported, and the user explicitly approves a scoped deferral.
- The README documents setup (`make k8s-tools agent-image k8s-up`), the token refresh, and teardown.
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
5. **TokenReview-based proof of audience rejection:** R3.
