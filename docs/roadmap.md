# Learning roadmap

All implementation milestones are pending. Advance when the behavior can be explained and demonstrated, rather than when every possible feature exists.

| Stage | Build or explore | Evidence required |
| --- | --- | --- |
| 0. Design review | Threat model, invariants, identity provenance, state ownership, state machine, trust boundaries, and control/data plane responsibilities | Walk through normal execution and adversarial/failure timelines; resolve the minimum contracts together before starting implementation. |
| 1. Local lifecycle | Python API, durable execution state, fake agent, cancellation, deadlines, reconciliation | Duplicate create returns the same logical execution; conflicting payload is rejected; restart preserves outcome and recovers unfinished work. Local agent execution is explicitly not a hostile-code sandbox. |
| 2. Kubernetes execution | Create/watch/delete workloads through the API; compare Pods and Jobs; inspect scheduling, kubelet behavior, probes, and resources | Recover from a lost creation response and controller restart; explain workload ownership, bounded retries, and cleanup. |
| 3. Enforcement boundary | Compare maincar/sidecar and separate-gateway options; establish trusted identity; implement a small deterministic policy | Attempts to bypass mediation, spoof a tenant, retrieve credentials, or access unauthorized data are denied by a demonstrated mechanism. |
| 4. Consequential tools | Synthetic employee lookup and expense approval; per-operation idempotency | Lose the response after committing approval, retry, and verify the effect occurs once under the stated contract. |
| 5. Observability | Structured events from the first slice, then correlated traces, metrics, and durable audit semantics | Reconstruct a failed execution; explain what happens if audit persistence fails before or after a tool effect. |
| 6. AWS foundations | Terraform, networking, workload identity, S3/KMS, and cross-account roles; EKS when useful | Explain packet and authorization paths, demonstrate intended allows and denies, and document teardown. |
| 7. Queued provisioning | Introduce SQS only after defining worker ownership and retry contracts | Crash after performing work but before acknowledging it; inject duplicates and visibility expiry; reconcile without duplicate effects. |
| 8. Failure campaign | Kill components, interrupt dependencies, expire credentials, overload workers | Record authoritative state, retries, cleanup, user-visible outcome, and remaining limitations for each experiment. |

## Kubernetes mechanics track

Build a small kubeadm cluster with one control-plane node and two workers, containerd, and a network plugin. Use VMs so the underlying control plane is visible.

Inspect static Pod manifests, kubelet logs, runtime state, API objects, events, and network paths. Break a workload and a worker, observe reconciliation, and explain which component takes each action. Record versions and commands when this experiment is actually run.

## Python practice throughout

Use ordinary implementation and review to cover object identity, mutability, hashability, scopes, iterators, generators, context managers, exception handling, and common collections.

For concurrency, explain task ownership, cancellation propagation, blocking I/O, bounded worker pools, synchronization, and execution-context propagation. Choose threads, processes, or async based on the workload and interpreter behavior being tested.

## Questions for every milestone

1. What invariant must hold, and who owns the authoritative state?
2. What happens if an RPC succeeds but its response is lost?
3. What can safely be retried, and with which identity or key?
4. Can an untrusted workload bypass the intended enforcement path?
5. What happens during overload, cancellation, or a crash halfway through?
6. What evidence establishes the result, and what remains unproven?

## First working session

Establish the threat model and minimum design contracts together. Work through concurrent identical requests, a controller crash during provisioning, cancellation racing with completion, forged tenant identity, and direct access around mediation. Keep technology choices provisional until those requirements justify them. Runtime implementation begins only after this design review.
