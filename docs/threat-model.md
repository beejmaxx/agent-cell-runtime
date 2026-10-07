# Threat model (v1)

**Status:** decided in the R0 design review. Guarantees listed here are design goals until a test or experiment demonstrates them.

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
- **No Kubernetes service-account credential:** `automountServiceAccountToken: false`, so there is no token to steal.
- **No path to the cloud metadata endpoint** (`169.254.169.254`) and no AWS credentials in the workload. Blocked by construction, not detected after the fact.
- **No direct network route** to Mock Workday, other executions, or internal services. The only egress is the enforcement boundary.
- **No platform or tenant credentials** in environment variables, files, or images.

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
| Kernel or container-runtime exploitation (container escape) | The Linux kernel, container runtime, and Kubernetes isolation are part of the trusted computing base in v1. | Stronger isolation such as gVisor, Kata Containers, or Firecracker microVMs, chosen per requirements |
| Compromised cloud or Kubernetes control plane; malicious platform operators | Lab scope; these are part of the trusted base | Separation of duties, audit, restricted administrative access |
| Hardware side channels | Lab scope | Dedicated nodes per tenant or confidential computing |
| Internet-scale denial of service | Lab scope | Edge protection and rate limiting at ingress |
