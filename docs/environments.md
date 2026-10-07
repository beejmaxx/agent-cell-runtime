# Environments

**Status:** decided. Nothing in AWS is provisioned yet.

## Topology

| Environment | Where | Purpose | Lifetime |
|---|---|---|---|
| **local** | Mac with Colima (Docker plus a local Kubernetes cluster) | Fast development, unit and integration tests | Always available, no cloud cost |
| **dev** | AWS project/account `729608197929`, Region `us-east-2` | Mock Workday and the runtime in their real environment; IAM, STS, EKS, S3, and KMS tests; deliberate breakage | Account stays; resources are created and destroyed freely |
| **prod** | Separate AWS project/account, created when dev deploys reproducibly | Showcase deployment with tighter permissions and no debug or admin exposure | Account stays; resources are **down by default** and brought up for demonstrations |

**Account separation:** dev and prod are separate AWS accounts (projects), not separate VPCs in one account. Account boundaries are AWS's recommended isolation between production and non-production, and they limit the blast radius of policy mistakes or a stray `destroy`. Cross-account role assumption between them is a deliberate learning target. Whether the account plan's managed guardrails allow it will be verified before anything depends on it.

## What "disposable" means

Terraform destroys **resources**, not accounts.

- **Persistent and cheap (foundation):**
  - Terraform state storage
  - IAM roles and policies (no cost)
  - budgets and alerts
  - possibly container registries
- **Disposable (destroyed between sessions):**
  - EKS clusters and nodes
  - EC2 instances
  - load balancers
  - NAT gateways
  - databases
  - anything billed by the hour

A leftover check after every destroy confirms nothing billable remains.

## Infrastructure layout

One set of modules; environments are thin compositions, never copies:

```text
infra/
  modules/
    network/
    mock-workday/
    runtime/
    eks/
    iam/
  envs/
    dev/
    prod/
```

Each environment has its own state and its own AWS CLI profile.

## IAM learning happens in the runtime itself

There is no separate toy IAM lab. Identity is exercised by the runtime's real architecture, with denial tests as acceptance criteria. For example:

```text
Agent Pod        → no AWS credentials at all
Runtime          → EKS workload identity → runtime role
Runtime role     → sts:AssumeRole with session tag tenant=acme → tenant-scoped role
Tenant role      → S3 acme/* only; KMS only with matching encryption context
```

Planned denial tests:

- a Globex execution reading `acme/…`;
- an agent calling STS;
- the runtime assuming a tenant role without the tenant tag;
- Acme decrypting with Globex's encryption context.

## Sequence

```text
R0   threat model, trust boundaries, identity design
 ↓
AWS dev baseline (state storage, tagging, leftover check)
 ↓
Mock Workday running in dev
 ↓
Runtime R1–R3
 ↓
Real IAM/STS/EKS tests as they arise
 ↓
Prod account once dev deployment is reproducible
```

## Open decision

Where Mock Workday's AWS deployment lives and how it connects to the runtime's network is to be decided before the dev baseline.
