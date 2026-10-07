# Invariants and proofs

**Status:** draft, the final R0 output. Each invariant comes from an R0 decision document and has the test that will demonstrate it. Until that test passes, the invariant is a design goal, not a guarantee.

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
| R8 | Failure campaign |

## Lifecycle (`LC`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| LC-1 | Terminal states are immutable; exactly one competing terminal transition wins | Race `complete`, `cancel`, and deadline concurrently on one execution, many times. Exactly one succeeds, and the stored status never changes afterward. | R1 |
| LC-2 | Creating an execution is idempotent | Same `Idempotency-Key` and body twice returns the same execution and creates one row. Same key with a different body returns 422. | R1 |
| LC-3 | Agent code is launched at most once per execution | Simulate a lost create response with the workload absent afterward. The execution becomes `FAILED (LAUNCH_UNKNOWN)`, and the backend records exactly one launch. | R1 (fake backend), R2 |
| LC-4 | A record exists before any workload | Crash the controller between steps; never observe a runtime-labeled workload without a matching row | R1, R2 |
| LC-5 | A lost create response never duplicates a workload | Create succeeds, the response is lost, the controller retries: the retry sees the existing `exec-<id>`, adopts it, and one workload exists | R1, R2 |
| LC-6 | A workload lost while RUNNING is never recreated | Delete the Pod externally. The result is `FAILED (POD_LOST)`, and no new Pod appears. | R2 |
| LC-7 | Inability to observe is not absence | Make the workload backend unreachable. No execution changes state; once reachable, normal reconciliation resumes. | R1, R2 |
| LC-8 | Deadlines are enforced even if the controller is down | The reconciler times out an overdue execution (`TIMED_OUT`). With the controller stopped, `activeDeadlineSeconds` (computed from the remaining time) still kills the Pod. | R1, R2 |
| LC-9 | Only `complete(result)` produces SUCCEEDED | A workload exiting 0 without completing becomes `FAILED`. Replaying the same result returns the existing success; a different result conflicts; results over 64 KiB or failing the schema are rejected. | R1, R3 |
| LC-10 | The controller is restart-safe | Kill the controller at each step of a launch, cancel, and cleanup. It converges with no duplicate launches and no lost outcomes. | R1, R8 |
| LC-11 | Orphan collection never deletes on missing information | With the database unavailable, nothing is deleted. Unlabeled Pods are never touched. A labeled Pod without a record is deleted only after a successful lookup and the grace period. | R2 |
| LC-12 | Cancellation ends authority immediately | After cancel, the next gateway request from the still-running Pod is denied, before the Pod is deleted | R3 |

## Identity and authority (`ID`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| ID-1 | Identity comes only from the execution record | Agent requests carrying forged `tenant`, `user`, or `scope` fields are ignored; the downstream call uses the record's identity | R3 |
| ID-2 | The execution token carries no downstream authority | Presented to Mock Workday it is rejected (wrong issuer and audience), and network policy blocks the path anyway. Presented to the gateway from another Pod (UID mismatch), it is denied. | R3 |
| ID-3 | The gateway acts only for RUNNING executions with a matching Pod UID | Requests in PENDING or PROVISIONING, from a terminal execution, or with a mismatched UID are denied | R3 |
| ID-4 | The operation allowlist is narrower than the grant | An operation inside the grant's scopes but outside the execution's allowlist is denied at the gateway, and Mock Workday receives no call | R3, R4 |
| ID-5 | A grant can be used only by its owner | Creating an execution with someone else's grant ID is rejected (the exchanged token's subject is not the caller) | R1 (against local Mock Workday) |
| ID-6 | An execution cannot outlive its grant | Creation is rejected when the deadline is later than the grant's expiry | R1 |
| ID-7 | Revocation takes effect mid-run | Revoke the grant during an execution: the next operation is denied and the execution becomes `FAILED (GRANT_REVOKED)` | R4 |
| ID-8 | Agents hold no reusable credentials | Inspect the execution Pod's environment, files, and mounts: no Mock Workday token, client secret, AWS credential, or default service-account token | R2, R3 |

## Isolation (`ISO`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| ISO-1 | No default Kubernetes API credential | The default service-account token path is absent. The only projected token has audience `agent-cell-gateway` and no RBAC permissions. | R2 |
| ISO-2 | Network deny-by-default is enforced, not just declared | Probes from a Pod identical to an execution Pod: Mock Workday, Kubernetes API, other executions, and internet fail; the gateway succeeds. With the policies removed, the forbidden paths succeed (the test can fail). | R3 (local), R6 (EKS) |
| ISO-3 | No arbitrary DNS resolution | Resolving an external name from the execution Pod fails; only the configured gateway path works | R3 |
| ISO-4 | Node credentials are unreachable | Metadata endpoint unreachable; IMDSv2 with hop limit 1 still blocks it with the network rule removed; node role minimal | R6 |
| ISO-5 | No host access for untrusted Pods | Admission rejects execution Pods requesting `hostNetwork`, host paths, privileged mode, or added capabilities (Pod Security Admission `restricted`) | R2 |
| ISO-6 | Fresh, ephemeral filesystem per execution | Read-only root filesystem; no persistent volumes or host paths; a file written by one execution is absent in the next | R2 |
| ISO-7 | Resource exhaustion is contained | A memory hog is killed (`FAILED`, out of memory); a fork bomb hits the process limit; CPU is throttled; other executions and the node are unaffected | R2 |

## Operations (`OP`)

| ID | Invariant | Proof | Stage |
|---|---|---|---|
| OP-1 | Each agent operation has at most one effect downstream | The gateway assigns an idempotency key per operation. When the Mock Workday response is lost after commit and the operation is retried, the effect happens once (Mock Workday fault injection). | R4 |
| OP-2 | Every downstream call is correlated | Each Mock Workday call carries `X-Request-Id: <execution>/<operation>`, and runtime and Mock Workday audit records join on it | R5 |
| OP-3 | Composition gaps are visible, not silently blocked | The documented exfiltration path (read a salary, then write it somewhere readable) is reproduced, and appears in audit as two individually authorized operations. Preventing it is future policy work. | R4, R5 |

## R1 scope (derived)

R1 proves `LC-1` through `LC-5`, `LC-7` through `LC-10`, `ID-5`, and `ID-6` **without Kubernetes**. It contains:

- the execution API (`POST /executions` with idempotency, `GET`, cancel, and a temporary `complete` endpoint the fake agent calls directly; the gateway arrives in R3);
- Postgres as the authoritative store;
- the reconciler;
- a **fake workload backend** with the same create/get/delete-by-name contract Kubernetes will provide. It can simulate lost responses, unreachability, and workload loss. In R2, a Kubernetes backend replaces it behind the same contract.
- grant verification against local Mock Workday.

Local workloads are not a security sandbox. Isolation claims begin in R2 and R3.
