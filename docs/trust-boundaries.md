# Trust boundaries

**Status:** decided in R0. Builds on [threat-model.md](threat-model.md). Each "proof" item becomes a test.

## Decision: sidecar as interface, gateway as authority (revised 2026-10-07)

**Kubernetes facts:**

- Containers in one Pod share the network namespace, so they have the same IP address and the same `localhost`.
- NetworkPolicy selects Pods, not containers.
- EKS Pod Identity credentials are delivered to the Pod: any container in it can obtain them.
- In-Pod traffic capture (iptables redirection to a sidecar, as service meshes do) can be strengthened with a separate sidecar UID and no `NET_ADMIN` for the agent, but Istio's own guidance says sidecar capture is not an unconditional security boundary and recommends external egress enforcement.

**Revision 1 (threat model: guest compromise in scope):** the original decision rejected a sidecar as the hard boundary. That remains true. It does not reject a sidecar as an interface component. Anything inside the execution boundary, including a platform-written sidecar, is assumed compromised (P1, P2).

| Component | Where | Responsibilities | Trusted |
|---|---|---|---|
| **Maincar** | Inside the execution boundary | Agent code | No |
| **Sidecar** | Inside the execution boundary, beside the maincar | The agent's versioned interface for tool, model, and data requests; request shaping; cancellation propagation; cheap local checks; telemetry | No: an optimization and an interface, never the authority |
| **Gateway** | Outside the execution boundary and off the execution nodes | Authenticates the execution, looks up the authoritative execution record, checks current authorization, quotas and budgets, holds downstream credentials, dispatches protected calls | Yes |

**Requirement:** bypassing or compromising the sidecar must not bypass authorization. The gateway never trusts a sidecar's assertion that a request was already checked.

**Acceptance test:** the agent skips the sidecar and calls the gateway directly with its own valid execution credential. An operation outside that execution's allowlist is still denied.

**Model mediation** is part of the same path: model calls go through the sidecar to the gateway, which holds the provider credentials and enforces the approved models and bounded requests.

**Placement:** trusted services (controller, gateway, execution records) run outside the execution cluster's nodes, preferably outside the execution cluster entirely. A second cluster for trusted services is an operations choice, not a prerequisite.

```text
Execution boundary (untrusted: P1/P2 in scope)        Trusted services (outside)
  maincar: agent code                                   gateway: authz, downstream credentials
  sidecar: interface, local checks, telemetry           controller, execution records
  projected token, aud=agent-cell-gateway
        │  only permitted egress                             │
        └───────────────────────────────────────────►        ├──► Mock Workday
                                                             ├──► model providers
                                                             └──► AWS (S3, KMS, ...)
```

## Authentication, authorization, credentials

| Concern | Answered by | The agent has it? |
|---|---|---|
| **Authentication:** which execution is calling? | A projected, short-lived, Pod-bound Kubernetes token with audience `agent-cell-gateway` and no RBAC permissions | Yes. It proves only "I am execution E." |
| **Authorization:** what may E do? | The gateway's authoritative execution record, keyed by Pod UID, written by the controller | No |
| **Credentials:** authority to act downstream | Held and used only by the gateway | No |

The execution's token identifies the caller but grants no downstream authority. Stealing or replaying it yields only the same execution's already-granted capabilities, only at the gateway, only until expiry, and only while the execution is still active.

## Gateway rules

- **Validating the token:** prefer `TokenReview`, which also rejects tokens whose Pod no longer exists. Local signature verification alone keeps a token valid until expiry. The execution-state check below covers that gap either way.
- **Pod-to-execution mapping:** the controller writes it when it creates the Pod. Until the mapping exists, requests are denied (fail closed).
- **No trusted claims from the agent:** tenant, user, scopes, and allowed operations come only from the execution record. Request fields claiming identity are ignored.
- **Checks on every request:** valid token → execution still `RUNNING` → operation in the execution's allowlist → grant still active → perform. Any failure denies.

## Network rules for execution Pods

| From an execution Pod to | Allowed |
|---|---|
| Gateway | Yes |
| Mock Workday | No |
| AWS endpoints, including `169.254.169.254` | No |
| Kubernetes API | No, where the substrate allows it. On EKS Fargate, the Pod's infrastructure needs control-plane connectivity, so the agent's actual reachability must be measured; if it cannot be removed, the path relies on authentication and RBAC, and that exposure is recorded. |
| Other execution Pods | No |
| Internet | No |
| DNS | No general resolution. Either no DNS, with the gateway address injected, or DNS-aware enforcement that allows only required internal names. Vanilla NetworkPolicy cannot filter by query name, so open port 53 is a DNS-tunneling exfiltration channel. |

Metadata-endpoint protection is layered: network deny, IMDSv2 with hop limit 1, no `hostNetwork` for untrusted Pods (enforced by admission policy), and a minimal node IAM role. See [threat-model.md](threat-model.md).

## Proving enforcement

A NetworkPolicy object proves nothing: enforcement depends on the network plugin (CNI), and an unsupported or disabled plugin accepts policies silently. Enforcement is demonstrated by behavior:

1. **Negative probes:** a Pod identical to an execution Pod (same namespace, labels, and service account) attempts every forbidden path: Mock Workday, metadata endpoint, Kubernetes API, another execution, internet, external DNS. Each must fail.
2. **Positive control:** the same probe reaches the gateway. Otherwise a broken network would make the negative probes pass for the wrong reason.
3. **Test the test:** without the policies, the forbidden paths succeed. A test that cannot fail proves nothing.
4. **When:** on every new cluster, after every deploy, and periodically to catch drift.
5. **Visibility:** policy-decision logging (VPC CNI network-policy logs or Cilium Hubble) shows denials directly.

## Enforcer per environment

| Environment | Network policy enforced by |
|---|---|
| local | k3s's embedded network-policy controller |
| dev and prod | EKS VPC CNI with network policy enabled, or Cilium if DNS-aware enforcement is required. Chosen when R3 starts, and proven by the probes above. |
| EKS Fargate (under evaluation) | VPC CNI NetworkPolicy does **not** apply to Fargate Pods. Candidates: security groups for Pods (supported on Fargate) plus Route 53 Resolver DNS Firewall, because security groups cannot block the VPC resolver. Proven by the same probes. |
