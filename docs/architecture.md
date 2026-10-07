# Architecture and open decisions

Everything marked **proposed** describes an intended experiment, not an implemented property. External facts are linked to primary sources. Decisions should record their requirements, alternatives, tradeoffs, and validation evidence.

## Proposed execution path

```text
authenticated caller
        |
        v
execution API -> durable state -> reconciliation loop
                                      |
                                      v
                             logical Agent Cell
                             untrusted agent code
                                      |
                                      v
                             trusted enforcement
                              /       |       \
                            data     tools    models
                                      |
                                      v
                            consequential service

operational telemetry and durable audit span this path
```

These are logical responsibilities. Separate deployable services should be introduced only when an experiment needs them.

## Control plane and data plane

**Proposed control plane:** agent definitions, authenticated execution creation, policy configuration, execution records, provisioning, cancellation, and lifecycle reconciliation.

**Proposed data plane:** running agent code, mediation, per-request policy enforcement, and calls to data, tool, and model services.

Policy configuration and policy enforcement are different responsibilities. Decide how policy versions reach the request path, how updates or revocation affect active executions, and what happens when the control plane is unavailable. Do not assume that an issued execution context remains authorized indefinitely.

Proposed API:

```text
POST /executions
GET  /executions/{id}
POST /executions/{id}/cancel
GET  /executions/{id}/events
```

## Invariants to demonstrate

- An authenticated tenant boundary applies to execution creation, reads, cancellation, event access, data retrieval, and tool calls.
- Agent-provided tenant, user, agent, and execution identifiers cannot grant authority. Trusted infrastructure establishes their binding.
- An operation must pass deterministic policy evaluation before accessing protected data, tools, or models.
- The untrusted workload cannot obtain broader credentials or use a direct route around enforcement.
- Execution outcomes remain available after workload cleanup.
- Retrying a logical operation does not repeat its consequential effect within the documented idempotency contract.
- Resource creation and cleanup converge after controller restarts.
- Every claimed guarantee has a reproducible failure or adversarial experiment.

## Isolation is an unresolved design decision

**Verified Kubernetes behavior:** containers within a Pod share a network namespace and can communicate over localhost. Kubernetes NetworkPolicy selects Pods, and enforcement requires a supporting network plugin. See [Pods](https://kubernetes.io/docs/concepts/workloads/pods/) and [Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

**Design implication:** placing an untrusted maincar next to a trusted sidecar and adding ordinary Pod-level NetworkPolicy does not, by itself, establish different outbound permissions for the two containers. A diagram with arrows through the sidecar is not enforcement evidence.

Experiments should compare a same-Pod mediation design with a separate trusted gateway. Determine what actually prevents direct access, token theft, identity spoofing, and access to node credentials. Stronger runtime isolation can be evaluated when the threat model requires it.

Record the assumed attacker capabilities: arbitrary agent code, malformed requests, adversarial model output, and malicious retrieved documents. Separately state whether host compromise or kernel exploitation is in scope for a given experiment.

Before implementation, explicitly consider malicious tenants, compromised dependencies, and a buggy or compromised trusted mediator. Identify which components are trusted for each guarantee and what remains possible if that trust fails.

## Identity and policy

Proposed authorization context:

```text
authenticated tenant
authenticated agent
authenticated human, if delegated
execution binding
action and canonical resource
execution restrictions and policy version
```

For delegated execution, require the relevant human, agent, and execution permissions. For autonomous execution, use the agent's own authority with execution restrictions; do not invent an implicit human principal.

Policy inputs need trusted provenance. The policy evaluator answers whether an action is allowed; enforcement must prevent access through any unchecked path. For protected operations, the proposed default is denial when required authorization cannot be established.

The controller must validate the caller's authority to use the selected agent and delegate the requested permissions before creating the execution binding. If signed execution context is used, specify issuer, audience, expiry, replay restrictions, and how it binds to the actual workload. A signature alone does not prevent a stolen bearer token from being reused.

Data retrieval should return only explicitly authorized content. An optional execution workspace contains that authorized subset. Tenant-wide mounts, unrestricted model-provider credentials, and static AWS access keys are outside the intended design.

## State ownership and lifecycle

**Proposed:** a durable execution record owns requested intent, terminal outcome, deadlines, and resource references. Kubernetes supplies observations about actual workload state. A queue, if introduced, delivers work notifications rather than becoming the sole source of execution truth.

Keep outcome separate from resource cleanup. Candidate dimensions:

```text
requested action: RUN | CANCEL
execution phase:  PENDING | PROVISIONING | RUNNING | TERMINAL
outcome:          unset | SUCCEEDED | FAILED | CANCELLED | TIMED_OUT
resource state:   ABSENT | CREATING | PRESENT | DELETING | UNKNOWN
```

This is a discussion model, not a finalized schema. Define valid transitions, concurrency control, and precedence when cancellation races with completion before implementing it. A cleanup failure must not erase a successful or failed execution outcome.

Open questions include how to fence stale workers, correlate resources after an API timeout, prevent duplicate live workloads, and bound orphan lifetime.

## Retry contracts

Creation idempotency should be scoped to the authenticated tenant and caller, bind a key to a request payload, and define retention and conflicting-payload behavior.

A tool-operation key identifies one logical invocation within an execution. The execution ID alone is insufficient when an execution performs multiple operations. Reuse the operation key across retries, not across distinct intended actions.

Distinguish `execution_id`, `operation_id`, and `attempt_id`: a retry retains the operation identity and gets a new attempt identity for observation. Define who assigns and persists those identities so a restart does not accidentally turn a retry into a new operation.

For a fake `approve_expense` tool, explore atomically persisting the effect and its deduplication record in the service that owns the effect. When a downstream system offers no equivalent contract, explicitly represent an unknown result and investigate reconciliation; do not claim a blanket exactly-once guarantee.

## Telemetry and audit

Proposed correlations: tenant ID, execution ID, agent ID, operation ID, and trace ID.

Metrics cover latency, saturation, failures, queue depth, startup, and cleanup. Traces explain the execution graph. Audit records capture authenticated principals, action, resource, policy version, decision, reason, and result without unnecessarily recording sensitive payloads.

Open decision: how to couple consequential effects with durable audit intent and eventual outcome. Sending an event to a queue after a successful operation leaves a crash window unless the design accounts for it.

## Deferred extensions

- AWS workload identity, S3/KMS access, and cross-account role assumption.
- SQS delivery and worker recovery under duplicates and visibility expiry.
- External agents using the same governed API surface as hosted agents.
- Agent definitions, tenant document uploads, model routing, and richer policy.

These are learning targets, not current features.
