# Identity flow

**Status:** decided in R0. Environment-independent: local, dev, and prod use the same design with different configuration (table at the end).

## Four identities, kept separate

| Identity | Example | Established by |
|---|---|---|
| Human | Alice | Mock Workday login; the runtime trusts Mock Workday's signed tokens |
| Execution | "I am execution e-123" | Projected, Pod-bound Kubernetes token (see [trust-boundaries.md](trust-boundaries.md)) |
| Runtime workload (cloud) | "I am the trusted gateway" | EKS Pod Identity → runtime IAM role. A separate trust domain from Mock Workday identity. |
| Delegated application | "Acting for Alice within these scopes" | Mock Workday delegation grant, exchanged for short-lived tokens |

**Terminology:**

- An **agent** is a registered definition: code plus configuration.
- An **execution** is one run of an agent, for one principal, with one task, a deadline, and its own audit trail.

## Delegate mode (acting for a human)

```text
1. Consent (once, by Alice in Mock Workday)
   Alice → POST /delegation-grants {client: runtime client, scopes, ttl} → grant G-42

2. Create the execution
   Alice → POST /executions {agent, grant: G-42, operations, input} → controller
   The controller:
     a. authenticates Alice (verifies Mock Workday's signature via JWKS);
     b. lists Alice's own grants from Mock Workday using her token, and requires
        G-42 to be among them, active, and issued to the agent's client
        (stops anyone from using someone else's grant ID);
     c. checks the requested operations are within the grant's scopes, and
        that the execution deadline is no later than the grant's expiry;
     d. records the execution: tenant, user, agent, grant, operation allowlist,
        deadline, state;
     e. creates the execution Pod and records the Pod UID → execution mapping.

3. Each agent request
   agent → gateway: "perform operation X" + Pod token
   gateway: token → Pod UID → execution → RUNNING? X in allowlist? grant active?
   gateway → Mock Workday: delegated token (exchanged via grant, cached briefly)
                           + X-Request-Id: <execution>/<operation>
   Mock Workday independently re-checks Alice's current access, the client
   ceiling, and the grant's scopes.
   gateway → agent: result only.

4. Revocation
   Alice revokes G-42 → the next exchange or call fails → the gateway denies →
   the controller marks the execution FAILED (reason: grant revoked).
```

## Permissions narrow at each layer

```text
client scope ceiling  ⊇  grant scopes  ⊇  execution operation allowlist
    (Mock Workday enforces the first two; the gateway enforces the third)
```

Scopes are coarse: `absence` covers both requesting and approving time off. The execution's **operation allowlist** (for example only `request_time_off`) keeps an agent from using everything the grant technically allows. This is the main confused-deputy control.

## Ambient mode (no human)

Scheduled or event-driven executions use the agent's own integration identity. The execution's principal is an integration user, and the gateway uses client-credentials tokens. The pipeline is the same, with the same allowlist and the same checks.

## Mapping to standard OAuth

Real deployments use OAuth 2.0's authorization-code flow:

1. a consent screen;
2. a one-time code;
3. a refresh token plus short-lived access tokens.

Mock Workday simplifies this. Creating a grant is the consent step, and the grant plays the role of the refresh token. Users signing into the runtime with Mock Workday tokens stands in for single sign-on (OIDC). A real system would issue a token whose audience is the runtime, not reuse a Mock Workday API token. That simplification is accepted for the lab.

## Per-environment configuration

| | local | dev (AWS) | prod (AWS, later) |
|---|---|---|---|
| Kubernetes | k3s in Colima | EKS, created and destroyed freely | EKS, up for demonstrations |
| Gateway client secret | local env file | Secrets Manager, dev account | Secrets Manager, prod account; never shared with dev |
| Gateway AWS identity | none needed | Pod Identity → dev runtime role | separate prod role in the prod account |
| Mock Workday | Compose | dev deployment, test admin on | prod deployment, test admin off, no shell access |
| Debug access | full | shell into Pods, verbose logs | none by default |
