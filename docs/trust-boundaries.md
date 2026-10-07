# Trust boundaries

**Status:** decided in R0. Builds on [threat-model.md](threat-model.md). Each "proof" item becomes a test.

## Decision: a separate gateway, not a sidecar

**Kubernetes facts:**

- Containers in one Pod share the network namespace, so they have the same IP address and the same `localhost`.
- NetworkPolicy selects Pods, not containers.
- EKS Pod Identity credentials are delivered to the Pod: any container in it can obtain them.

So a sidecar beside hostile code cannot hold authority the agent can't reach, and cannot be a hard boundary. The enforcer is a **separate, trusted gateway Pod**:

```text
Execution Pod (untrusted)                 Gateway Pod (trusted)
  agent code                                EKS Pod Identity → runtime role
  no AWS identity                           Mock Workday client secret
  no Mock Workday token                     execution records (read)
  no default Kubernetes API credential      policy enforcement
  projected token, aud=agent-cell-gateway
        │  only permitted egress                 │
        └──────────────────────────────────►     ├──► Mock Workday
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
| Kubernetes API | No |
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
