# Execution lifecycle

**Status:** decided in R0. Scoped for a learning lab: keep the mechanisms that teach something (reconciliation, desired versus observed state, source of truth, idempotency, cross-system ambiguity, races, deadlines), and cut machinery that only production completeness needs.

## Source of truth

- **Postgres holds the authoritative execution record.** An execution is control-plane state: who created it, tenant, allowed operations, deadline, result, and outcome. It must survive cluster replacement and support transactions, history, and queries.
- **Kubernetes holds disposable Pods, derived from that record,** and is authoritative only for observations ("does Pod `exec-e123` exist, and in what phase?").
- **The database outlives the cluster:** local Postgres locally, RDS in AWS. Otherwise destroying a cluster would destroy execution history.
- **The alternative considered** was a custom resource (`kind: Execution`) with an operator. It was rejected because executions need history, transactional authorization, and survival across disposable clusters.

## States

```text
PENDING ──claim──► PROVISIONING ──Pod created, uid recorded──► RUNNING
   │                    │                                         │
   │                    ├── Pod absent after ambiguous launch ──► FAILED (LAUNCH_UNKNOWN)
   │                    │                                         ├── complete(result) ──► SUCCEEDED
   └── cancel / deadline at any non-terminal state ──►            ├── cancel ────────────► CANCELLED
         CANCELLED / TIMED_OUT                                    ├── deadline ──────────► TIMED_OUT
                                                                  └── Pod failed or lost ► FAILED
```

- **PENDING:** recorded, not yet launched.
- **PROVISIONING:** a launch has been attempted, and its outcome may be unknown.
- **RUNNING:** the Pod was created *and its UID recorded*. This is the gateway's authority window: the gateway accepts requests only from the Pod whose UID matches an execution in `RUNNING`. Defining RUNNING as "observed running" would reject the agent's first calls.
- **Terminal states** (SUCCEEDED, FAILED, CANCELLED, TIMED_OUT) are immutable. Every transition is a conditional update, for example:

  ```sql
  UPDATE executions SET status = 'SUCCEEDED', result = :r, finished_at = now()
  WHERE id = :id AND status = 'RUNNING'
  RETURNING *;
  ```

  Exactly one competing terminal transition (completion, cancel, deadline, Pod loss) wins. No separate version counter is needed for this invariant.

**Execution columns:**

- `status`
- `failure_reason`
- `pod_name` (`exec-<id>`)
- `pod_uid` (nullable)
- `launch_attempted_at`
- `deadline_at`
- `result` (bounded JSON)
- `created_at` and `finished_at`

There is no persisted resource state machine: Pod existence is observed from Kubernetes, not mirrored in Postgres.

## Controller (reconciler)

**Single replica, stateless, restart-safe.** Each pass compares Postgres (desired) with Kubernetes (observed) and takes one small idempotent step:

| Observation | Action |
|---|---|
| PENDING | Conditionally claim it: `PENDING → PROVISIONING`, set `launch_attempted_at`. Then create Pod `exec-<id>` **once**. On success, record `pod_uid` and move to RUNNING. |
| PROVISIONING, Pod `exec-<id>` exists with a runtime ownership label | Adopt it: record `pod_uid`, move to RUNNING |
| PROVISIONING, Pod absent (a launch was attempted, so it might have run) | FAILED (`LAUNCH_UNKNOWN`). **Never create it again.** |
| RUNNING, Pod genuinely gone | FAILED (`POD_LOST`). Never recreate. |
| RUNNING, Pod failed (crash, out-of-memory, exit without completing) | FAILED with the reason |
| Any non-terminal state past `deadline_at` | TIMED_OUT |
| Terminal, Pod still exists | Delete the Pod (cleanup is asynchronous; the gateway's authority already ended) |
| Kubernetes unreachable | Observation unknown. Change nothing, retry later. *Unable to observe* never means *absent*. |

**At-most-once execution of agent code:**

- There is no transaction spanning Postgres and the Kubernetes API.
- Deterministic Pod names make a retried *create* safe while the Pod exists. They cannot prove whether a vanished Pod ever ran.
- v1 therefore favors at-most-once over availability: when it's ambiguous whether code ran, the execution fails rather than risking a second run.
- A crash between claiming and calling Kubernetes can cause a false failure. That is accepted. Users may start a new execution.

**Orphan collection:**

- Only Pods with the runtime's ownership labels are candidates.
- Only after a *successful* database lookup, because a database outage must never look like "zero executions, delete everything."
- No grace period: the record is always committed before the workload is created, so a labeled workload without a record cannot belong to an in-flight launch.

**Deadlines are enforced twice:**

1. The reconciler enforces `deadline_at` (authoritative).
2. The Pod carries `activeDeadlineSeconds`, computed from the *remaining* time at creation, so Kubernetes kills it even if the controller is down.

**Bare Pod, not a Job.** Jobs can be configured not to retry (`backoffLimit: 0`). But the Kubernetes docs note that a Job can still start its program twice in some failure cases, and building the lifecycle controller ourselves is the point of the lab. In production, Jobs deserve reconsideration. Pods use `restartPolicy: Never`.

## Cancellation

- Cancel is a conditional transition from any non-terminal state to CANCELLED.
- The gateway checks status on every request, so authority ends **immediately**.
- Pod deletion follows asynchronously.

Authorization state changes are immediate; resource cleanup is eventual.

## Completion

- The agent calls `complete(result)` through the gateway. **Only this** produces SUCCEEDED. A Pod exiting with code 0 without completing is FAILED.
- Status and result commit in one transaction.
- **Replay-safe:**

  | Case | Response |
  |---|---|
  | RUNNING + `complete(R)` | SUCCEEDED |
  | SUCCEEDED + same `R` | Return the existing success |
  | SUCCEEDED + different `R` | Conflict |
  | Any other state | Reject |

- **SUCCEEDED means the agent completed the runtime protocol and declared a result.** It does not mean the result is true, correct, or what the user wanted. The platform cannot judge that, and nothing security-relevant may depend on it.
- **The result is untrusted content:** JSON, UTF-8, at most 64 KiB, schema-validated, stored in Postgres, and never rendered as trusted markup.

## Idempotency at every layer

The same distributed-systems problem appears four times:

| Layer | Lost-response risk | Protection |
|---|---|---|
| `POST /executions` | Retry creates a second execution | `Idempotency-Key`, scoped to the caller and bound to a request hash (the same contract as Mock Workday) |
| Pod creation | Retry creates a duplicate Pod | Deterministic name `exec-<id>` plus the at-most-once launch rule |
| Agent operations through the gateway | Retry repeats a side effect in Mock Workday | Gateway-assigned idempotency keys per operation, honored by Mock Workday |
| `complete(result)` | Retry reports a false failure | Replay-safe terminal transition |

## Deliberately not built in R0 or R1

These are known concepts worth explaining in interviews, not built until a later stage needs them:

- custom resources or operator frameworks;
- leader election (Kubernetes Lease) and multiple controller replicas (or `SELECT … FOR UPDATE SKIP LOCKED` work claiming);
- event sourcing;
- queues for controller work;
- automatic execution retries;
- a generic workflow engine;
- a pluggable scheduler;
- large-result artifact storage;
- a persisted Pod-resource state machine;
- any "exactly-once execution" abstraction.
