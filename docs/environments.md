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

Layers per account, each with its own state:

```text
per account (dev, prod)
├── bootstrap      this account's state bucket (persistent; one per account, never shared)
├── platform       network (persistent); EKS cluster later (disposable)
├── mock-workday   registry (persistent); ECS, RDS and load balancer (disposable)
└── runtime        IAM roles, S3, KMS, SQS (mostly persistent); controller and gateway on EKS (disposable)
```

- **Platform stacks** live in this repository under `infra/platform/envs/<env>/`.
- **Mock Workday's stacks** live in its own repository, like another team's service.
- Stacks share values through SSM parameters (for example `/lab/dev/network/*`), not through each other's state.
- **Modules are environment-neutral:** dev-only behavior (test admin, shell access) is controlled by variables that default to off. Environments are thin compositions, never copies.
- **Promotion:** prod runs the exact image digest verified in dev. Images are built once and promoted by digest, never rebuilt.
- **Each account has its own state bucket and AWS CLI profile.** Prod state never lives in the dev account.

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

## Decided: Mock Workday placement

Mock Workday deploys into the platform's dev network as its own stacks, from its own repository (spec: `mock-workday/docs/d1-aws-dev.md`). When the runtime gateway runs in the same VPC, Mock Workday gains a private path from the gateway's security group. Its public load balancer stays restricted to the operator's IP.
