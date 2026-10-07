# Threat model (v1)

**Status:** decided in the R0 design review; revised 2026-10-07 (revision 1: compromise of an execution's guest environment, including container escape, is in scope). Guarantees listed here are design goals until a test or experiment demonstrates them.

## Core assumption

**Everything inside an agent execution is untrusted.** That includes:

- customer code and its dependencies;
- generated code;
- the model and its output;
- tool arguments;
- prompt and retrieved content.

A malicious customer, a compromised dependency, a prompt-injected model, and a jailbroken model all reduce to the same case: untrusted code proposes actions, and the platform decides what is permitted.

The runtime does not need to know whether an action came from a model, handwritten code, or a malicious script. Every action takes the same enforcement path.

## Core invariant

> An agent execution receives no authority over platform resources, tenant resources, external services, or credentials except capabilities explicitly granted to that execution.

The agent necessarily has ambient capabilities inside its sandbox: it executes instructions, allocates memory, makes syscalls, and uses its own scratch filesystem. The invariant concerns authority beyond the sandbox, and resource limits bound the ambient capabilities.

## Design principle: remove capability, don't police it

Where possible, the hostile workload has nothing to steal and nowhere to go, rather than having access that is monitored. Each item below is an **invariant the design relies on**, and each needs a test:

- **Fresh ephemeral filesystem per execution:**
  - no persistent volume claims;
  - no `hostPath`;
  - read-only root filesystem;
  - writable scratch space only where required.
- **No default Kubernetes API credential:** `automountServiceAccountToken: false`.
  - The execution may receive one explicitly projected, short-lived, Pod-bound token with audience `agent-cell-gateway`.
  - That token only proves "I am execution E" to the gateway. It carries no Kubernetes RBAC permissions and no downstream authority. What E may do comes from the gateway's execution record (see trust boundaries).
- **No AWS credentials in the workload, and no path to the node's credentials.** Four independent layers, so one mistake does not expose them:
  1. Network policy denies the agent's egress to the metadata endpoint (`169.254.169.254`).
  2. EC2 nodes require IMDSv2 with response hop limit 1, so ordinary Pods cannot obtain the node's instance-profile credentials even if the network rule is missing. The metadata endpoint itself stays enabled, because node components depend on it.
  3. Untrusted execution Pods may not use `hostNetwork` (or other host namespaces). This is enforced by admission policy (Pod Security Admission `restricted` or equivalent), not by convention. The hop-limit defense depends on it.
  4. The node IAM role is minimally privileged. For example, the VPC CNI's permissions move to its own workload identity, so stolen node credentials grant little.
- **No agent DNS resolution beyond what its granted connectivity requires.** The cluster DNS resolver forwards lookups for external names, so allowing arbitrary DNS is an exfiltration channel (DNS tunneling). Kubernetes `NetworkPolicy` cannot filter by query name. Either the agent gets no DNS at all, or DNS-aware egress enforcement allows only required internal names. On AWS, security groups cannot block queries to the VPC resolver (AmazonProvidedDNS), so the mechanism there is Route 53 Resolver DNS Firewall; the mechanism is substrate-specific and proven by probes.
- **No direct network route** to Mock Workday, other executions, or internal services. The only egress is the enforcement boundary.
- **No platform or tenant credentials** in environment variables, files, or images.

## Guest compromise (in scope since revision 1)

**Boundary statement:**

> An attacker may compromise everything inside an execution's guest environment: the agent, the sidecar, the files, and the execution credential. That must not yield another execution's identity, any downstream credential, or control of trusted runtime services. The isolation boundary around the guest, the external enforcement services, and their supporting infrastructure remain trusted.

**Attacker positions.** "Escape" is not one event. Every assume-breach test must name the position it simulates:

| Position | Meaning | In scope |
|---|---|---|
| P1: root in the container | Arbitrary code with every privilege the Pod spec allows | Yes |
| P2: control of the guest kernel | A kernel or container-runtime exploit inside the isolation boundary (a microVM's guest kernel, or the shared node kernel if there is no VM boundary) | Yes |
| P3: control of the physical host or hypervisor | Breaking the isolation boundary itself | No (see out of scope) |

**Consequences:**

- **The isolation unit is per execution.** Under P2, a shared kernel gives no separation between co-located executions. Executions therefore need a boundary that P2 does not cross: a VM-level boundary per execution (Fargate, Kata, Firecracker) or an equivalent sandbox. gVisor is a sandbox with its own kernel implementation and different trade-offs; it is not "a container with a smaller surface", and a VM is not "escape solved". The mechanism is chosen per substrate, with evidence.
- **Nothing trusted runs inside or beside the guest.** The sidecar lives inside the execution boundary, so it is untrusted under P1 and P2. Downstream credentials, authoritative policy decisions, and the execution records live in trusted services outside the execution boundary, and outside the execution cluster's nodes.
- **The execution credential is assumed stolen.** It must grant only that execution's own already-granted capabilities, at the gateway, while the execution is active.
- **Assume-breach evidence:** from each in-scope position, enumerate what is reachable (credentials, network destinations, other executions, control-plane APIs). Running a root process in a constrained container simulates P1 only; it is not evidence about P2.

## Threat categories

Five questions about untrusted code:

| # | Question | Example attempts | Primary control |
|---|---|---|---|
| 1 | **What can it reach?** | Call Mock Workday directly; reach the metadata endpoint; scan the cluster network; contact other executions; exfiltrate to the internet | Network isolation; the only route out is the enforcement boundary |
| 2 | **What can it read?** | Environment variables, mounted tokens, leftover files | Nothing sensitive is present (invariants above) |
| 3 | **Who can it impersonate?** | Claim another tenant, user, or agent in requests; replay a token it observed | Identity is established by trusted infrastructure, never taken from agent-supplied fields; the agent never holds reusable credentials |
| 4 | **What can it consume?** | CPU loops, memory exhaustion, disk filling, fork bombs, running forever | CPU, memory, disk, and process limits; execution deadlines |
| 5 | **What authority can it cause someone else to exercise?** | Ask the trusted gateway to perform `change_job` when the execution was authorized only for `request_time_off`; ask for broader data than granted; chain individually allowed actions into an exfiltration (read a salary, then email it) | **Confused-deputy controls:** the enforcement boundary acts only within the execution's granted capabilities, and the downstream service re-checks independently |

Category 5 is the distinctive problem of an agent runtime. The trusted components hold real authority, and the hostile workload's main avenue is persuading them to use it.

## Non-malicious failures

| Failure | Expected handling |
|---|---|
| Buggy agent code (crash loops, hangs) | Deadlines and restart limits; the execution fails visibly |
| Bug in a trusted component (policy evaluation errors) | Fail closed: deny when authorization cannot be established |
| Downstream failures (Mock Workday down, timeouts, expired certificates) | Fail closed for protected operations; bounded retries using idempotency keys; visible errors |
| Malicious tenant using legitimate APIs | Tenant boundary established by trusted identity; Mock Workday's own authorization and row-level security as the second layer |

## Enforcement structure

A few authoritative enforcement boundaries plus defense in depth, rather than scattered checks:

```text
Agent execution (untrusted)
   |  untrusted request
   v
Enforcement boundary (runtime-owned)
   |  constrained, delegated identity
   v
Mock Workday (independent authorization, row-level security)
   |
   v
Data
```

The runtime's job is to ensure the agent cannot bypass the first layer or acquire more authority than the execution was given. Mock Workday's checks are the second layer, not a substitute.

## Out of scope for v1

| Threat | Rationale | Production direction |
|---|---|---|
| Escaping the isolation boundary itself (P3: hypervisor, microVM monitor, or the provider's isolation, such as Fargate's) | The boundary around the guest is trusted; revision 1 moved container escape into scope (above) | Defense in depth around the hypervisor; provider responsibility under the shared-responsibility model |
| Compromised cloud or Kubernetes control plane; malicious platform operators | Lab scope; these are part of the trusted base | Separation of duties, audit, restricted administrative access |
| Hardware side channels | Lab scope | Dedicated nodes per tenant or confidential computing |
| Internet-scale denial of service | Lab scope | Edge protection and rate limiting at ingress |
