# Design scenarios: customer agents, certificates, and audit

**Status:** draft for discussion (2026-10-08). Speculative by design: it derives requirements from a concrete scenario **before** choosing technology. Nothing here is decided until it moves into a spec.

**Labels used below:**

- **Workday fact:** published by Workday, with a citation.
- **Reported:** from the user's interview; not public.
- **Hypothesis:** our assumption for the lab. Never present it as Workday behavior.

## Context

- **Workday fact:** in June 2025 Workday announced an Agent System of Record, which manages AI agents, including third-party ones, alongside people, and an Agent Gateway. The Gateway registers third-party agents and supports agent-to-agent interaction over A2A and MCP ([press release](https://newsroom.workday.com/2025-06-03-Workday-Announces-New-AI-Agent-Partner-Network-and-Agent-Gateway-to-Power-the-Next-Generation-of-Human-and-Digital-Workforces?asPDF=1), [Agent System of Record](https://www.workday.com/en-ae/artificial-intelligence/agent-system-of-record.html)). That concerns agents running elsewhere.
- **Reported:** the agent runtime is new and not yet implemented. It lets Workday customers run **their own agent code inside Workday**. The hiring manager named handling per-agent certificates as an open problem ("TBD"). The job description includes audit logging.
- **Hypothesis:** the lab's model applies: customer code is untrusted, it runs in an isolated execution, and only the trusted gateway holds authority ([threat-model.md](threat-model.md), [trust-boundaries.md](trust-boundaries.md)).

Terms from [identity-flow.md](identity-flow.md): an **agent** is a registered definition (code plus configuration); an **execution** is one run of it.

## Scenario A: Acme's onboarding agent

Acme, a Workday customer, writes its own onboarding agent and deploys it into Workday's runtime.

| Actor | Trust | Notes |
|---|---|---|
| Acme's agent code | Untrusted | Customer code, inside an execution |
| Workday runtime: controller, gateway, edge | Trusted | Holds credentials and makes authorization decisions |
| Workday core (Mock Workday) | Trusted, separate domain | Reached only by the gateway |
| Acme IT provisioning API | Acme's system, outside Workday | Requires mTLS with Acme's own certificate authority |
| Acme chat bot | Acme's system, outside Workday | Sends messages to the agent |
| Alice (Acme HR) | Human, Acme tenant | Starts onboarding; her grant bounds what the agent may do |
| Acme admins | Human, Acme tenant | Configure the agent; read its audit trail |

**Story:**

1. Alice hires Bob and starts the onboarding agent for him (delegate mode: the execution acts for Alice within her grant).
2. The agent reads Bob's start date, manager, and location from Workday, through the gateway.
3. It asks Acme IT's provisioning API to create Bob's accounts and order a laptop. Acme's security team requires mTLS with a client certificate from Acme's CA.
4. Two days later, Acme's chat bot asks the agent: "Is Bob's laptop shipped?" No execution may be running at that moment.
5. Three months later, Acme's auditors ask: what did the agent do for Bob, on whose authority, and what data did it send outside Workday?

```text
Alice ──start──► runtime API ──► controller ──► execution (Acme's code, untrusted)
                                                   │ only outbound, to the gateway
                                                   ▼
Acme chat bot ──► edge (trusted) ──────────────► gateway (trusted)
                                                   ├──► Workday core (delegated token)
                                                   └──► Acme IT API (mTLS: whose certificate?)
```

## Part 1: Identity and certificates

**Mechanism reminder:** a certificate proves possession of a private key. A server certificate proves to a client that it reached the right server. A client certificate (mTLS) proves the client's identity to the server. Whoever holds the key *is* that identity. So the core question in every case below is **who holds the key**.

**Fixed by the threat model:** no private key or downstream credential may live inside the execution. Under guest compromise (P1 and P2 in scope), anything in the guest is stolen.

### C1. Outbound to the customer's system (step 3)

Acme IT accepts only clients that present a certificate it trusts.

| Option | How | Gives | Costs |
|---|---|---|---|
| C1a. Customer-provided key, gateway-held | Acme uploads a client certificate and key for "their agent"; Workday stores them (Secrets Manager or KMS); the gateway presents them when it calls Acme IT on the agent's behalf | Fits Acme's existing PKI unchanged | Acme hands Workday a private key. Workday stores customer keys. Rotation and revocation are manual and shared between two parties. |
| C1b. Workday-issued identity | Workday's CA issues a certificate per agent (for example `acme/onboarding-agent`), and Acme configures its API to trust that CA for that identity | No customer private keys leave Acme. Workday rotates automatically. | Acme must trust a Workday CA. Workday must run a CA safely and tie identities to tenants. |
| C1c. Non-exportable key | Like C1a or C1b, but the key lives in KMS or an HSM, and the gateway's TLS stack signs through it | Even the trusted gateway cannot leak the key | Harder TLS integration. Per-handshake latency and cost. |

**Questions to decide:**

- **Who does Acme IT believe it is talking to:** "Acme's onboarding agent", or "Workday, on behalf of Acme"? A certificate carries one identity. **On whose behalf** (Alice, for Bob's onboarding) needs a second, per-request mechanism, for example a short-lived token signed by the gateway that carries the delegation chain.
- **Which destinations may the agent reach at all?** Acme IT must be on the agent definition's egress allowlist. The gateway builds the request (ID-12), and the agent cannot choose an arbitrary host.
- **Revocation:** Acme disables the agent, or rotates its CA mid-run. The next call must fail, just as grant revocation does today (ID-7).

### C2. Inbound to the agent (step 4)

Acme's chat bot needs an address and a server identity for "Acme's onboarding agent". The design forbids inbound connections to executions.

- **Hypothesis:** a trusted **edge** owns the inbound side. It terminates TLS with the endpoint's server certificate, authenticates the caller (Acme's bot), and turns the message into work: either delivered to a running execution, which pulls it over its existing outbound connection to the gateway, or used to start a new execution (ambient mode, as the agent's own integration identity).
- **Server certificate options:** a Workday domain per tenant or agent (Workday-managed certificate), or a customer custom domain (customer-provided certificate, or one issued automatically after Acme proves it owns the domain). Either way it terminates at the edge, never in the execution.
- **Caller authentication options:** OAuth client credentials, mTLS with Acme's client certificate (the mirror image of C1), or signed webhooks.

**Questions to decide:**

- When no execution is running, does a message start one, queue for the next one, or fail?
- Whose authority does an execution started by a message carry: Alice's grant (possibly expired), or the agent's own integration identity?
- How does the edge stop one tenant's caller from reaching another tenant's agent?

### C3. The agent's own identity, across executions

Today the only workload identity is per **execution**: a short-lived token bound to one Pod. C1 and C2 need a stable identity per **agent**: "Acme's onboarding agent" exists across thousands of runs and versions.

**Questions to decide:**

- What does a per-agent certificate bind to: the agent, a version, or the tenant?
- Does a new version get a new identity, so Acme IT can tell v3 from v4?
- How do the two identities relate? A plausible shape: the execution proves "I am run E of agent X" to the gateway (existing token); the gateway then acts outward as agent X (C1) and records both (Part 2).
- How would this map to Workday's Agent System of Record and A2A registration? Unknown. Leave it out until there is a source.

**Hypothesis for the lab:** keys live only at the gateway and the edge (C1b, with C1a as a supported exception); the per-execution token stays the only credential inside the guest.

## Part 2: Audit

Audit is defined by the questions it must answer before any storage choice.

### Questions from Scenario A

1. **Timeline:** everything Acme's onboarding agent did for Bob, across executions and days, in order.
2. **Authority:** for each action, who acted (the agent and version, and the execution), on whose behalf (Alice), under which grant and allowlist, and whether it was allowed or denied.
3. **Data leaving Workday:** which fields went to Acme IT, under which certificate identity, when.
4. **Inbound:** which caller sent which message to the agent, and what it caused.
5. **Configuration changes:** who uploaded, rotated, or revoked the agent's certificates, and who changed its egress allowlist.
6. **Correlation:** join the runtime's records with Workday core's audit (`X-Request-Id`, OP-2).

### Required properties

| Property | Question to settle | Initial position (hypothesis) |
|---|---|---|
| Who writes | Can the agent write audit records? | Never. The gateway, edge, and controller record what they observed and decided. The agent's own claims may be logged, but only labeled as untrusted. |
| Completeness | Must the record be durable **before** a protected action is dispatched? If audit is unavailable, is the action refused? | Yes for protected actions (fail closed); deferred for low-risk telemetry. This is the main design trade-off: availability versus completeness. |
| Tamper evidence | Could an operator or a compromised trusted service quietly change history? | Append-only, with tamper evidence (for example hash-chained batches or write-once storage). Detection, not prevention. |
| Tenant isolation | Who may read which records? | Acme reads only Acme's; Workday operators get scoped, themselves audited, access. |
| Retention and minimization | Years of retention versus salaries and personal data in arguments | Record field names, hashes, and decisions by default; store values only where the question requires them, with their own retention and deletion rules. |
| Volume | Every model call, tool call, and decision is an event | Estimate before choosing (below). |

**Volume, illustrative only (invented numbers):** tenants × executions per tenant per day × events per execution. For example 1,000 × 50 × 40 = 2 million events per day. The real inputs are unknown; replacing them with defensible ranges is part of the design work.

### What the answers imply for technology (not a choice)

The properties split into two kinds of job:

- **A system of record:** append-only, written before protected actions, tamper-evident, tenant-scoped, long retention. Optimized for correctness and durability, not query speed.
- **An investigation and analytics index:** fast timelines, search, and aggregates over large volumes. ClickHouse is a candidate here.

Whether one system can do both jobs, or the index is built from the record, follows from the completeness and volume answers above. Choose after those.

## Part 3: What the current design already answers

| Need | Current design | Gap |
|---|---|---|
| No credentials in the guest | Execution holds only its per-execution token (ID-8) | None for C1–C3, provided keys stay at the gateway and edge |
| Gateway builds downstream requests | ID-12 | No per-agent egress allowlist for customer destinations yet |
| Per-execution identity | Projected, Pod-bound token | No per-agent identity (C3) |
| Inbound path to an agent | None, deliberately | Edge component and message delivery (C2) |
| Delegation chain to external systems | Workday core only (delegated token) | How Alice's authority is conveyed to Acme IT (C1) |
| Audit | Correlation IDs planned (OP-2); audit is a later stage | Everything in Part 2 |

## Open questions (for the user)

1. Do we model C1 (outbound to a customer system with mTLS) in the lab? It needs a small mock "Acme IT" service with its own CA, separate from Mock Workday, which must stay free of agent concepts.
2. Which audit completeness rule do we adopt: fail closed for protected actions only, as proposed above?
3. Is C2 (inbound) in scope, or recorded as a known gap until later?
