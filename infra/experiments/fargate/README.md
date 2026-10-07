# S1 Fargate experiment

This stack owns only S1 resources. It reads the existing `lab-dev` VPC/public
subnet IDs from SSM and uses the separate backend key
`dev/experiments/s1-fargate.tfstate` in `beejmaxx-lab-tfstate-dev`.
The backend and AWS provider use profile `agent-runtime`, Region `us-east-2`;
the provider rejects accounts other than `729608197929`. Never run Terraform
from the foundation/bootstrap directories as part of S1.

## Review before apply

The first checkpoint-4 apply was blocked by PrivateLink rejecting the operator
ARN's `/managed/` IAM path. The reviewer approved this account's root ARN as the
service principal, with acceptance required for only the Terraform-created
endpoint ID. The approved operator is now IAM user `lab-operator-cli`; the former role path no longer blocks
`s1-up`. Regenerate the saved plan before retrying; the original plan contains
the rejected principal. See `docs/experiments.md` for the attempt and outstanding
cleanup evidence. The operator IP comes from
`curl --noproxy '*' https://checkip.amazonaws.com`, following Mock Workday's
existing direct-egress detection. A changed IP requires a new plan.

The reviewer approved the **76-addition, zero-change, zero-deletion** plan
with `sts:GetCallerIdentity` only and DNS `BLOCK/NXDOMAIN`. The selected saved
plan is `.local/s1/plan-sts-identity.tfplan`; the full-policy alternative is not
approved. If startup fails, use CloudTrail/query-log evidence to add only a
minimum observed STS action or exact blocked DNS chain target. Record every
change as an experiment finding; never silently broaden to full access or
wildcard DNS names.

`make s1-plan` uses `S1_STS_GET_CALLER_IDENTITY_ONLY=true`. Recheck any regenerated
plan against the approved resource changes and direct operator IP before apply.
The checkpoint-4 authorization covers the approved plan and evidence-driven
adjustments described in the spec.

## Resources and boundaries

- Execution VPC `10.30.0.0/16`: two private subnets, DNS support and explicit
  AmazonProvidedDNS DHCP options; no IGW, NAT or default route.
- EKS 1.36, API access entries only, no bootstrap creator grant, self-managed
  add-ons, CoreDNS, node groups, IAM OIDC provider or Pod Identity association.
  One Fargate profile selects `agent-exec` in 2a only.
- Four caller identities remain distinct: existing operator IAM user, controller/instance
  role, Fargate Pod execution role and test-only `s1-harness`. The latter has
  AmazonEKSAdminPolicy only in `agent-exec` and `agent-exec-psa-control`.
  EKS's service role and two service-linked roles are separate infrastructure
  identities. The service-linked roles were verified absent before planning;
  Terraform owns their creation and deletion. The existing shared ELB
  service-linked role is not adopted or destroyed.
- PrivateLink: internal NLB TCP 8001 in the trusted VPC, an endpoint service
  permitting only account `729608197929` (`arn:aws:iam::729608197929:root`, never
  `*`) with acceptance required. The accepter names only the Terraform-created
  interface endpoint in execution subnet 2a. Specific endpoint acceptance is the
  gate in this single-account lab; separate accounts would name the consumer
  account and retain explicit acceptance. The NLB skips ingress-SG
  evaluation for accepted PrivateLink traffic; it has no direct ingress rule.
  Its egress and the host's ingress allow only TCP 8001 between their groups.
- Execution SG: callback 8001, AWS endpoint 443, S3 prefix-list 443, API 443;
  kubelet ingress 10250 from the cluster SG. The cluster SG is not selected
  for normal execution Pods. E5 removes the SecurityGroupPolicy deliberately;
  its fallback can still pull the image, while a blocked callback is collected
  through the harness's Pod logs. Probe stdout contains sanitized results.
- ECR API/DKR and STS interface endpoints, S3 gateway endpoint; ECR policy
  permits only the Fargate role and one repository, S3 permits only image-layer
  reads. The canary's anonymous PutObject grant requires this S3 endpoint and
  retains all Block Public Access switches. E5 changes the endpoint policy,
  not the bucket's boundary.
- DNS allowlist contains explicit AWS hostnames plus the actual cluster
  endpoint, followed by block-all; fail-open disabled, redirection inspection
  enabled. Standard DNS Firewall only. Query logging uses CloudWatch's default
  encryption at rest and a one-day retention fallback; Terraform deletes the
  log group at teardown. No extra KMS key or DNS Firewall Advanced resource.
- Ubuntu 24.04 amd64 `t3.small`, encrypted 16-GiB root volume, EIP, IMDSv2/hop
  limit 1, no internet ingress. HTTPS and package-install HTTP egress belong
  only to the trusted host. The runtime full API binds loopback 8000; the
  completion app binds 8001. SSM transfers source/configuration without
  copying operator credentials. The host profile uses instance credentials.

## Checkpoint 4, only after reviewer approval

`make s1-up`, `s1-test` and `s1-down` require `S1_RUN_APPROVED=1` after review.
The gate is local workflow protection, not an AWS security boundary. The saved
plan and source hashes must match; `s1-up` also rechecks the direct IP and
requires a working local Docker daemon before applying. It does not start
Colima. The current checkpoint keeps local Kubernetes stopped.

`make s1-up` applies the reviewed S1 plan, records IDs, pre-creates namespaces,
service accounts, RBAC, quota and SecurityGroupPolicy as the operator, publishes
and verifies the amd64 image digest, then transfers tracked source plus only
`image.json` and `host.json` via SSM. The trusted host creates its own guarded
harness kubeconfig and controller-token file. The control namespace has no PSA
labels and no Fargate selector; ISO-5 uses dry-run control Pods there.

`make s1-test` runs E1 on the trusted host with the existing test assertions.
It records all tests rather than stopping after the first failure, exports
sanitized Pod/result and per-test database rows before fixture teardown, and
returns failure if E1 fails. It does not claim to execute E2-E10. Those remain
explicit experiments using the agent's `inspect`, `probe`, `listen`, and
`binding` modes and the mutations in the spec.

At the end of every run, including a partial setup failure, run `make s1-down`.
It stops the recorded harness process group, exports available evidence and DNS
logs, deletes and verifies execution Pods while API access remains, then deletes
and waits for the Fargate profile and cluster before destroying the remaining
S1 state. It refuses unmarked namespaces/clusters or unexpected profiles.
`make s1-leftovers` checks tags, known names and recorded IDs, including ENIs,
service roles and endpoint connections. Stale EC2 tagging-index entries are
checked by exact resource ID; confirmed deleted or terminated entries are saved
in `retired-tagged-resources.json`. Exported local evidence is intentional.

## Review limitations

- Live Fargate startup, minimum SG/DNS dependencies, namespace-scoped admin
  behavior and all lifecycle tests remain E1 findings until actually run.
  EC2's optional endpoint is intentionally absent per the spec.
- The project SCP currently explicitly denies `iam:ListOpenIDConnectProviders`.
  The approved alternative queries `GetOpenIDConnectProvider` for the exact ARN
  derived from the recorded cluster issuer. Only `NoSuchEntity` proves absence;
  authorization failures remain incomplete. The exact lookup was also explicitly
  SCP-denied during the first attempt, so E9 remains unverified. The reviewer
  accepts this specific SCP denial as an explicit inventory exception; all
  other incomplete checks still fail. Mitigation evidence is the plan without
  an OIDC provider, API access mode, and E7 showing no web-identity token in
  Pods. These do not prove provider absence. Evidence is retained locally.
- The current Free plan is active with $200 remaining credits. Planning does
  not verify that every create action/quota will succeed. The spec's running
  estimate is roughly $0.20–0.25/hour plus usage; it is not a spending cap.
