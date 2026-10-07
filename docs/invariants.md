# Invariants and proofs

**Status:** final R0 output, revised after review. Each invariant comes from an R0 decision document and has the test that will demonstrate it. Until that test passes, the invariant is a design goal, not a guarantee.

**Sources:**

- [threat-model.md](threat-model.md)
- [trust-boundaries.md](trust-boundaries.md)
- [identity-flow.md](identity-flow.md)
- [execution-lifecycle.md](execution-lifecycle.md)

**Stages:**

| Stage | Scope |
|---|---|
| R1 | Local lifecycle: API, Postgres, reconciler, fake workload backend |
| R2 | Kubernetes execution (local k3s) |
| R3 | Enforcement boundary: gateway, isolation |
| R4 | Consequential tools through Mock Workday |
| R5 | Observability and audit |
| R6 | AWS (EKS, IAM) |
| R7 | Queued provisioning (no invariants until designed) |
| R8 | Failure campaign |

**Testing rule:** every negative test has a positive control showing the target is reachable and working for a trusted caller. Without one, a "denied" result can be caused by the target being down.

## Lifecycle (`LC`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| LC-1 | Terminal states are immutable; exactly one competing terminal transition wins | Race `complete`, `cancel`, and deadline on one execution, many times. Exactly one succeeds, and the stored status never changes afterward. | R1 |
| LC-2 | Creating an execution is idempotent | Keys are scoped to the authenticated principal. Sequential: same key and body returns the same execution (one row); same key with a different body returns 422. Concurrent: 20 simultaneous requests with the same key and body produce exactly one execution, and every success reports it. The same key value used by another principal creates a separate execution. | R1 |
| LC-3 | The runtime never intentionally launches a second workload once launch may have occurred | Simulate a create whose result is lost with the workload absent afterward: the execution becomes `FAILED (LAUNCH_UNKNOWN)`, and the backend records exactly one create call. | R1 (fake backend), R2 |
| LC-4 | The controller never creates a workload before the execution row is durably committed | Failpoint between claim and create; the create path is reachable only after the commit | R1 |
| LC-5 | A lost create response never duplicates a workload | The create succeeds, the response is lost, the controller retries: it sees the existing `exec-<id>`, adopts it, and one workload exists | R1, R2 |
| LC-6 | A workload lost while RUNNING is never recreated | Delete the Pod externally: the execution becomes `FAILED (POD_LOST)`, and no new Pod appears | R2 |
| LC-7 | Failure to observe workloads is never interpreted as absence, failure, or permission to relaunch or collect | With the backend unreachable: no `POD_LOST`, no relaunch, no garbage collection. Database-only transitions still apply (cancel succeeds; overdue executions become `TIMED_OUT`). | R1, R2 |
| LC-8 | Deadlines are enforced | R1: the reconciler converts overdue executions to `TIMED_OUT`. R2: with the controller stopped, `activeDeadlineSeconds` (computed from the remaining time) kills the Pod. R3: see ID-10. | R1, R2 |
| LC-9 | Only `complete(result)` produces SUCCEEDED, and completion is replay-safe | A workload exiting 0 without completing becomes `FAILED`. Replaying the same result returns the existing success; a different result conflicts; results over 64 KiB or failing the schema are rejected. | R1, R3 |
| LC-10 | The reconciler recovers at the critical crash points | Three deterministic failpoints: (a) after claiming PENDING, before create; (b) after a successful create, before recording it; (c) after a terminal transition, before cleanup. Each converges correctly with no duplicate launch. The broad crash matrix is R8. | R1 |
| LC-11 | Orphan collection never deletes on missing information | With the database unavailable, nothing is deleted. Pods without the runtime ownership label are never touched. A labeled Pod with no record is deleted only after a successful authoritative lookup. | R2 |
| LC-12 | Cancellation ends authority immediately | After cancel, the next gateway request from the still-running Pod is denied, before the Pod is deleted | R3 |

## Identity and authority (`ID`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| ID-1 | Identity comes only from the execution record | Agent requests carrying forged `tenant`, `user`, or `scope` fields are ignored; the downstream call uses the record's identity | R3 |
| ID-2 | The execution token is usable only at the gateway and grants no downstream authority | Presented to Mock Workday: rejected. To the Kubernetes API: rejected (wrong audience). To the gateway: accepted while the execution is active. After its Pod is deleted: rejected (TokenReview). For a terminal execution: denied (runtime authorization). **Limitation:** it is a bearer token. Possession is the authentication; isolation is what prevents theft. Binding to the presenting connection (for example mTLS workload identity) is out of scope. | R3 |
| ID-3 | The gateway acts only for RUNNING executions whose token maps to the recorded Pod | Requests in PENDING or PROVISIONING, from terminal executions, or with a token for a different Pod UID than recorded are denied | R3 |
| ID-4 | The operation allowlist is narrower than the grant | An operation inside the grant's scopes but outside the execution's allowlist is denied at the gateway, and Mock Workday receives no call (verified via Mock Workday audit) | R3, R4 |
| ID-5 | A grant can be used only by its delegating principal | Use a valid, unexpired, correctly scoped grant belonging to another user in the same tenant: creation is rejected. Positive control: the owner succeeds with the same grant. | R1 |
| ID-6 | An execution is created only within its grant | Rejected when the deadline is later than the grant's expiry, or when the requested allowlist needs scopes outside the grant | R1 |
| ID-7 | Delegated authority is rechecked during the run | (a) Revoke the grant mid-run: the next operation is denied and the execution becomes `FAILED (GRANT_REVOKED)`. (b) Narrow the client's scope ceiling mid-run (Mock Workday rechecks it on every request): operations needing the removed scope are denied, while others still succeed. | R4 |
| ID-8 | Agents hold no reusable credentials | R2/R3: the execution Pod has no Mock Workday token, client secret, or default service-account token. R6: no EKS Pod Identity association or AWS credential chain is available to execution Pods. | R2, R3, R6 |
| ID-9 | Authorization uncertainty never grants authority | Runtime database unavailable, grant validation unavailable, or a malformed execution record: the gateway denies, and Mock Workday receives no protected call | R3 |
| ID-10 | Authority ends at the deadline even if the controller is down | With the controller stopped and the status still RUNNING, an operation at `now >= deadline_at` is denied by the gateway | R3 |
| ID-11 | The runtime's control plane is tenant- and principal-scoped | Using real, existing executions in both tenants, a caller cannot read, cancel, or replay another tenant's or another principal's execution (404). Positive control: the owner can. | R1 |

## Isolation (`ISO`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| ISO-1 | No default Kubernetes API credential | The default service-account token path is absent. The only projected token has audience `agent-cell-gateway` and no RBAC permissions. | R2 |
| ISO-2 | Network deny-by-default is enforced, not just declared | For each target (Mock Workday, Kubernetes API, a listener in another execution Pod, an external canary), a trusted diagnostic Pod connects successfully and a Pod identical to an execution Pod fails. The gateway succeeds from the execution Pod. With the policies removed, the forbidden paths succeed, so the test can fail. **AWS caveat:** where the VPC has no internet route, "internet fails" is not evidence for NetworkPolicy. | R3 (local), R6 (EKS) |
| ISO-3 | Execution Pods cannot use DNS | The agent receives the gateway's address directly. A trusted Pod resolves through CoreDNS; the execution Pod's UDP/TCP 53 to CoreDNS fails; the injected gateway address works. | R3 |
| ISO-4 | Node credentials are unreachable | Metadata endpoint unreachable (positive control: a trusted host-level check reaches it). IMDSv2 with hop limit 1 still blocks it with the network rule removed. The node role is minimal. | R6 |
| ISO-5 | No host access for untrusted Pods | Admission (Pod Security Admission `restricted`) rejects execution Pods requesting `hostNetwork`, `hostPID`, `hostIPC`, `hostPort`, host paths, privileged mode, or capabilities beyond the Restricted allowlist (only `NET_BIND_SERVICE` may be added back after dropping `ALL`) | R2 |
| ISO-6 | Fresh, ephemeral filesystem per execution | Read-only root filesystem, enforced by the Pod template because Restricted does not require it. No persistent volumes or host paths. A file written by one execution is absent in the next. | R2 |
| ISO-7 | Basic resource limits are enforced | CPU quota throttles; exceeding the memory limit gives `OOMKilled`, and the execution becomes `FAILED`; scratch and ephemeral storage is bounded, and exceeding it evicts the Pod. Fork bombs and PID limits (kubelet `podPidsLimit`) and node-level abuse are R8. | R2 |
| ISO-8 | The gateway cannot be overwhelmed by one execution | Request bodies over the maximum size are rejected, and concurrent in-flight gateway operations per execution are capped (excess requests are rejected) | R3 |

## Operations (`OP`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| OP-1 | Agent operations are end-to-end retry-safe | The agent supplies an `operation_id` per logical operation. `(execution, operation_id)` plus a request hash identifies it: the same request replays the stored outcome, and a different request conflicts. The downstream Mock Workday idempotency key is derived from and stored for that pair. Proof: (a) the Mock Workday response is lost after commit, and the gateway retries; (b) the gateway's response to the agent is lost, and the agent retries. In both cases the effect occurs once. | R4 |
| OP-2 | Every downstream call is correlated | Each Mock Workday call carries `X-Request-Id: <execution>/<operation_id>`, and runtime and Mock Workday audit records join on it | R5 |

## Known limitations demonstrated (not invariants)

- **Composition gaps:** reading a salary and then writing it where others can read it is allowed when each step is individually authorized. R4 and R5 reproduce it and show it in audit as two authorized operations. Preventing it is future policy work.
- **Prompt injection is contained, not prevented:** the agent reads Mock Workday's seeded malicious content, and the model tries an operation outside its allowlist. The gateway denies it (ID-4) and audit records the attempt. The model being manipulated is expected; the runtime's job is that manipulation gains no authority. (R4)

## R1 scope (derived)

R1 proves LC-1, LC-2, LC-3, LC-4, LC-5, LC-7, LC-8 (R1 part), LC-9 (R1 part), LC-10, ID-5, ID-6, and ID-11. It runs without Kubernetes and contains:

- **Execution API:**
  - `POST /executions` with principal-scoped idempotency, safe under concurrency;
  - `GET`;
  - cancel;
  - a temporary `complete` endpoint, authenticated by a per-execution credential handed to the fake workload (a stand-in for the projected token until R3's gateway exists);
  - every route tenant- and principal-scoped.
- **Postgres as the authoritative store,** with conditional transitions.
- **The reconciler,** with three deterministic failpoints.
- **The smallest fake workload backend** offering create, get, and delete by name, with deterministic fault injection: lost create response, unreachable, workload lost. In R2 a Kubernetes backend replaces it behind the same small interface. No generic plugin system.
- **Grant checks at creation against local Mock Workday:** owner, expiry, and the requested allowlist within the grant's scopes. Fails closed when Mock Workday is unreachable.

Local workloads are not a security sandbox. Isolation claims begin in R2 and R3.

Not in R1: Kubernetes, the gateway, leader election, event sourcing, generic fault frameworks.
