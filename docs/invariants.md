# Invariants and proofs

**Status:** final R0 output, revised after review. Each invariant comes from an R0 decision document and has acceptance evidence: tests that demonstrate the specified behavior under stated configurations and faults. They do not establish unconditional guarantees, and until they pass, the invariant is a design goal.

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

**Testing rules:**

- **Positive controls:** every negative test has one, showing the target is reachable and working for a trusted caller. Without it, a "denied" result can be caused by the target being down.
- **The happy path is mandatory at every stage:** create → provision → run → complete → retrieve the authorized result → clean up. A runtime that fails everything must not pass.
- **Network tests distinguish three outcomes:** connection blocked, connection established but rejected by the application (a 401 or 403 means the network let it through), and request processed.
- **Removing a control must flip the specific test it proves,** while independent controls (routing, IAM, application auth) may correctly keep blocking.

## Lifecycle (`LC`)

| ID | Invariant | Acceptance evidence | Stage |
|---|---|---|---|
| LC-1 | Terminal states are immutable; exactly one terminal transition is committed | Assert one committed terminal transition, not one successful HTTP response (replays may also succeed). Use controlled interleavings with separate database connections, not only repetition. Include cancel or deadline during PENDING and PROVISIONING, and a stale reconciler trying to write RUNNING after cancellation (it must update zero rows). | R1 |
| LC-2 | Creating an execution is idempotent | Keys are scoped to the authenticated principal. Sequential: same key and body returns the same execution (one row); same key with a different body returns 422. Concurrent: 20 simultaneous requests with the same key and body produce exactly one execution, and every success reports it. The same key value used by another principal creates a separate execution. | R1 |
| LC-3 | The controller never automatically starts a replacement workload after a launch may have been accepted | Recovery after an ambiguous create **re-observes** the deterministic name and never re-creates. Cases: (a) crash after the claim, before create: conservative failure, zero launches; (b) create accepted, response lost, workload present: adopt; (c) create accepted, response lost, workload absent: `FAILED (LAUNCH_UNKNOWN)`; (d) the workload becomes visible **after** the execution failed: no authority, cleaned up. The backend's create count survives the simulated controller restart. | R1 (fake backend), R2 |
| LC-4 | The controller never creates a workload before the execution row is durably committed | Failpoint between claim and create; the create path is reachable only after the commit | R1 |
| LC-5 | A lost create response never duplicates a workload | The create succeeds and the response is lost; the next pass *observes* `exec-<id>` and adopts it only if its ownership and execution labels match. One workload exists. | R1, R2 |
| LC-6 | A workload lost while RUNNING is never recreated | Delete the Pod externally: the execution becomes `FAILED (POD_LOST)`, and no new Pod appears | R2 |
| LC-7 | Failure to observe workloads is never interpreted as absence, failure, or permission to relaunch or collect | With the backend unreachable: no `POD_LOST`, no relaunch, no garbage collection. Database-only transitions still apply (cancel succeeds; overdue executions become `TIMED_OUT`). | R1, R2 |
| LC-8 | Deadlines are enforced | R1: the reconciler converts overdue executions to `TIMED_OUT` using the persisted `deadline_at`. R2: with the controller stopped and the node healthy, `activeDeadlineSeconds` kills the Pod. It counts from the Pod's start time, so it is a backstop, not the authoritative clock. S1: completion checks its persisted deadline and credential expiry without reconciliation; see ID-10. R3: gateway operations. | R1, R2, S1 |
| LC-9 | Only `complete(result)` produces SUCCEEDED, and completion is replay-safe and atomic | A workload exiting 0 without completing becomes `FAILED`. Replaying the same result before deadline and credential expiry returns the existing success; a different result conflicts; results over 64 KiB or failing the schema are rejected. There is never a SUCCEEDED row without its result, and never a stored result from a completion that lost to cancellation. R1 covers the lifecycle; R3 covers gateway authentication and replay authorization. | R1, R3 |
| LC-10 | The reconciler recovers at the critical crash points | Three deterministic failpoints: (a) after claiming PENDING, before create; (b) after a successful create, before recording it; (c) after a terminal transition, before cleanup. Each converges correctly with no duplicate launch. The broad crash matrix is R8. | R1 |
| LC-11 | Orphan collection never deletes on missing information | With the database unavailable, nothing is deleted. Workloads without the runtime ownership label are never touched. A labeled workload with no record (or a terminal record) is deleted only after a successful authoritative lookup, and only if its labels identify it as the runtime's. | R1 (fake), R2 |
| LC-12 | Cancellation blocks new authorization immediately | After the cancel commits, the next gateway request from the still-running Pod is denied, before the Pod is deleted. An operation already dispatched downstream may still commit; the execution stays `CANCELLED`. There is no distributed rollback, and no database lock is held across downstream calls. | R3 |

## Identity and authority (`ID`)

| ID | Invariant | Acceptance evidence | Stage |
|---|---|---|---|
| ID-1 | Identity comes only from the execution record | Agent requests carrying forged `tenant`, `user`, or `scope` fields are ignored; the downstream call uses the record's identity | R3 |
| ID-2 | The execution token is usable only at the gateway and grants no downstream authority | Presented to Mock Workday: rejected. To the Kubernetes API: rejected (wrong audience). To the gateway: accepted while the execution is active. After its Pod is deleted: rejected (TokenReview). For a terminal execution: denied (runtime authorization). **Limitation:** it is a bearer token. Possession is the authentication; isolation is what prevents theft. Binding to the presenting connection (for example mTLS workload identity) is out of scope. | R3 |
| ID-3 | The gateway authorizes new operations only for RUNNING executions whose token maps to the recorded Pod | Denied: requests in PENDING or PROVISIONING, from terminal executions, or from Pod B using its own token to claim Pod A's execution. **Narrow exception:** an authenticated retry of an already-committed completion returns the existing receipt. A terminal execution can never start operations or change its result. | R3 |
| ID-4 | Effective authority is the intersection of the execution's allowlist and the grant's current permissions | An operation inside the grant's scopes but outside the execution's allowlist is denied at the gateway, and Mock Workday receives no call (verified via Mock Workday audit) | R3, R4 |
| ID-5 | A grant can be used only by its delegating principal (tenant-qualified) for its intended client | Use a valid, unexpired, correctly scoped grant belonging to another user in the same tenant: creation is rejected. Positive control: the owner succeeds with the same grant. | R1 |
| ID-6 | An execution is created only within its grant | Rejected when the deadline is later than the grant's expiry, or when the requested allowlist needs scopes outside the grant | R1 |
| ID-7 | Delegated authority is rechecked during the run | (a) A **confirmed** revocation mid-run: the next operation is denied and the execution becomes `FAILED (GRANT_REVOKED)`. (b) Narrow the client's scope ceiling mid-run (Mock Workday rechecks it on every request): operations needing the removed scope are denied, while others still succeed. (c) A timeout while checking the grant denies the operation but is **not** recorded as `GRANT_REVOKED`. | R4 |
| ID-8 | The runtime supplies no downstream credentials to the agent; its only credential is a short-lived gateway authentication token | R2/R3: the execution Pod has no Mock Workday token, client secret, or default service-account token. R6: no EKS Pod Identity association; the Pod Identity agent endpoint and AWS credential chain yield nothing. | R2, R3, R6 |
| ID-9 | Authentication or authorization uncertainty never grants authority | Each of the following produces **zero** downstream calls: invalid signature, wrong issuer or audience, expired token, missing Pod binding, missing execution mapping, runtime database unavailable, grant validation unavailable, or a malformed execution record. Positive control: a valid request succeeds. | R3 |
| ID-10 | Authority ends at the deadline even if the controller is down | S1: with reconciliation stopped and status still RUNNING, completion at/after the persisted deadline is denied; credential lookup independently rejects expiry regardless of status, with zero skew. R3: an operation at `now >= deadline_at` is denied by the gateway. | S1 (completion), R3 (operations) |
| ID-11 | The runtime's control plane is tenant- and principal-scoped (reads, results, cancel, idempotent replay) | Using real, existing executions in both tenants, a caller cannot read, cancel, or replay another tenant's or another principal's execution (404). Positive control: the owner can. | R1 |

| ID-12 | The gateway controls downstream destinations | An allowed operation cannot be used to choose an arbitrary URL, `Host`, authorization header, or downstream identity; the gateway builds every downstream request itself. Test: a malicious destination override is ignored or rejected, and no request reaches the attacker's destination. | R3 |
| ID-13 | Bypassing or compromising the sidecar does not bypass authorization | The agent skips the sidecar and calls the gateway directly with its own valid execution credential: an operation outside the execution's allowlist is denied, and an allowed one succeeds (positive control). The gateway never accepts a sidecar's claim that a request was already checked. | R3 |

## Isolation (`ISO`)

| ID | Invariant | Acceptance evidence | Stage |
|---|---|---|---|
| ISO-1 | No default Kubernetes API credential | The default service-account token path is absent. The only projected token has audience `agent-cell-gateway` and no RBAC permissions. | R2 |
| ISO-2 | Network deny-by-default is enforced, not just declared | For each target (Mock Workday, Kubernetes API, a listener in another execution Pod, an external canary), a trusted diagnostic Pod connects successfully and a Pod identical to an execution Pod fails. The gateway succeeds from the execution Pod. With the policies removed, the forbidden paths succeed, so the test can fail.

**Probes start at the workload's first instructions,** because VPC CNI standard mode permits traffic while policies are being configured.

**EKS gates (before hostile workloads run on EKS):**
- verify the chosen network-policy implementation enforces policies on the *actual* execution Pod ownership model. AWS documents that VPC CNI policy enforcement may be unreliable for standalone Pods without `metadata.ownerReferences`; options include owner references, strict mode, or Cilium;
- never assume a probe Pod created by a Deployment represents bare execution Pods;
- where the VPC has no internet route, "internet fails" is not evidence for NetworkPolicy. | R3 (local), R6 (EKS) |
| ISO-3 | Execution Pods cannot send DNS queries beyond the permitted boundary | The agent receives the gateway's address directly. A trusted Pod resolves through CoreDNS. DNS packets from the execution Pod to the resolver addresses (UDP and TCP 53) are blocked at the network layer, which is different from getting a negative answer. A failed `nslookup` alone is not evidence: a lookup can reach an attacker's server and still fail. Where feasible, a query recorder on a controlled domain sees nothing. The injected gateway address works. | R3 |
| ISO-4 | Node credentials are unreachable | Exercise the real IMDSv2 sequence (token `PUT`, then a token-bearing `GET`); an unauthenticated `GET` returning 401 proves nothing. Blocked with the network rule in place, and still blocked by hop limit 1 with the rule removed. Positive control: a trusted host-level check succeeds. Node-role minimality is a separate IAM policy review. | R6 |
| ISO-5 | No host access for untrusted Pods | **Enforced by Pod Security Admission `restricted`:** rejects `hostNetwork`, `hostPID`, `hostIPC`, `hostPort`, host paths, privileged mode, and capabilities beyond the Restricted allowlist (`NET_BIND_SERVICE` may be added back). **Enforced by the runtime's fixed Pod template and validation** (customers cannot override it): no added capabilities at all, no persistent volume claims (Restricted allows them), and a read-only root filesystem. | R2 |
| ISO-6 | Fresh, ephemeral filesystem per execution | Read-only root filesystem (template). No persistent volumes or host paths. Execution A first **proves** it wrote a sentinel to its scratch space, then execution B confirms the sentinel is absent. | Substrate decision (moved from R2 by threat-model revision 1) |
| ISO-7 | Basic resource limits are enforced, with bounded effect on neighbors | CPU quota throttles; exceeding the memory limit gives `OOMKilled`, and the execution becomes `FAILED`; disk and log filling are bounded by ephemeral-storage limits, and exceeding them evicts the Pod. Containment is measured: the node stays healthy, and a neighboring test workload finishes within a declared tolerance. A simple maximum-active-executions setting applies. Fork bombs and PID limits (kubelet `podPidsLimit`) and aggregate node abuse are R8. | Substrate decision; the maximum-active-executions limit is R2 |
| ISO-8 | The gateway cannot be overwhelmed by one execution | Request bodies over the maximum size are rejected, and concurrent in-flight gateway operations per execution are capped (excess requests are rejected) | R3 |
| ISO-9 | Compromise of one execution's guest yields no other execution's identity, no downstream credential, and no control of trusted services (threat-model revision 1) | Assume-breach from each in-scope position (P1 root in the container; P2 guest-kernel control, simulated as far as the substrate allows), naming the position: enumerate reachable credentials, network destinations, other executions, and control-plane APIs. Only the execution's own credential and the gateway are reachable. A P1 simulation is never reported as P2 evidence. | Substrate decision (Fargate experiment) |

## Operations (`OP`)

| ID | Invariant | Acceptance evidence | Stage |
|---|---|---|---|
| OP-1 | Retries of the same accepted operation commit at most one downstream mutation, within the downstream service's idempotency contract | The agent supplies an `operation_id` per logical operation, scoped to the execution. Its fingerprint (the request hash) and the derived Mock Workday idempotency key persist across lost responses and gateway restarts. Reusing an `operation_id` with different arguments conflicts. Cases: (a) Mock Workday's response is lost after commit, and the gateway retries; (b) the gateway's response to the agent is lost, and the agent retries. The effect happens once in each. Depends on Mock Workday's 24-hour key retention. Does not stop malicious code from submitting the same business action under different operation IDs. | R4 |
| OP-2 | Every downstream call is correlated | Each Mock Workday call carries `X-Request-Id: <execution>/<operation_id>`, and runtime and Mock Workday audit records join on it. This is correlation, not an audit-durability guarantee; R5 defines whether audit must persist before protected dispatch. | R5 |
| OP-3 | Secrets never leak into logs, audit, or agent-visible errors | Sentinel token and client-secret values are absent from logs, audit records, and every error returned to the agent | R5 |

## Cloud identity (`IAM`)

| ID | Invariant | Acceptance evidence | Stage |
|---|---|---|---|
| IAM-1 | Cloud resource access is tenant-scoped | Through the runtime role and tenant-tagged role assumption: the permitted tenant's resource succeeds, another tenant's resource is denied, and missing or forged tenant context is denied | R6 |

## Known limitations demonstrated (not invariants)

- **Composition gaps:** Carol (HR Partner) reads Bob's salary (source), writes it into a `DOC_ORG` document on Engineering (writable sink), and Alice, a manager without compensation access (unauthorized reader), reads it. Each step is individually authorized. R4 demonstrates that Alice actually retrieves the value, and R5 shows the chain in audit. Uses only Mock Workday's existing API (its test T-D-06). Preventing it is future policy work.
- **Prompt injection is contained, not prevented:** the agent reads Mock Workday's seeded malicious content, and the model tries an operation outside its allowlist. The gateway denies it (ID-4) and audit records the attempt. The model being manipulated is expected; the runtime's job is that manipulation gains no authority. (R4)

## R1 scope (derived)

R1 demonstrates LC-1, LC-2, LC-3, LC-4, LC-5, LC-7, LC-10, LC-11 (fake backend), the lifecycle parts of LC-8 and LC-9, ID-5, ID-6, and ID-11, plus the mandatory happy path. It runs without Kubernetes and contains:

- **Execution API:**
  - `POST /executions` with principal-scoped idempotency, safe under concurrency;
  - `GET`;
  - cancel;
  - a temporary, **test-only** `complete` endpoint, authenticated by a per-execution credential handed to the fake workload (a stand-in for the projected token). It is removed when R3's gateway arrives;
  - every route tenant- and principal-scoped.
- **Postgres as the authoritative store,** with conditional transitions.
- **The reconciler,** with three deterministic failpoints.
- **The smallest fake workload backend** offering create, get, and delete by name, with deterministic fault injection: lost create response, unreachable, workload lost. In R2 a Kubernetes backend replaces it behind the same small interface. No generic plugin system.
- **Grant checks at creation against local Mock Workday:** owner, expiry, and the requested allowlist within the grant's scopes. Fails closed when Mock Workday is unreachable.

Local workloads are not a security sandbox. Isolation claims begin in R2 and R3.

Not in R1: Kubernetes, the gateway, leader election, event sourcing, generic fault frameworks.
