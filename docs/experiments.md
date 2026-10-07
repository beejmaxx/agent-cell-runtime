# Experiment log

Use this file to preserve observed behavior, failed hypotheses, recovery steps, and the evidence supporting design decisions. Never mark a planned experiment as completed.

## 2026-10-07 — Project initialized

**Completed:** documented scope, architectural questions, and the staged learning roadmap.

**Observed:** this repository contains documentation only. No runtime, Kubernetes cluster, AWS infrastructure, or failure experiment has been implemented here.

**Initial design questions:** whether a sidecar can provide an unavoidable enforcement path; how trusted identity reaches policy; how operation idempotency differs from execution idempotency; how cleanup preserves terminal outcome; how audit survives partial failure.

**Next:** establish the threat model, invariants, identity model, state ownership, state machine, and trust boundaries together before implementing runtime code. Include explicit control-plane/data-plane responsibilities and distinguish execution, operation, and attempt identities.

## Template for future entries

```text
Date / title:
Status: planned | running | completed | inconclusive
Question or hypothesis:
Environment and versions:
Commit:
Setup and reproduction commands:
Expected invariant:
Failure or adversarial input:
Observed behavior:
Evidence (sanitized logs, traces, commands, artifact paths):
Interpretation (separate from observations):
Recovery and teardown:
Limitations:
Decision and next experiment:
```

## 2026-10-07 — R2 Colima preflight

**Completed preflight only; R2 validation is still in progress.** Context `colima`,
node k3s 1.35, checksum-verified kubectl v1.35.0 in `.local/bin`; global kubectl
unchanged. `make k8s-up` created only marked `agent-runtime` and `agent-exec`.
`make agent-image` built `agent-runtime/fake-agent:r2` in Colima's Docker daemon.

Reproduce: `make k8s-tools k8s-up agent-image k8s-preflight` (serial Make).
The preflight receiver listens on Mac loopback and collects sanitized completion
facts; Pod status supplies failure evidence. No container logs are used.

Observed from the fixed execution template:

- `/sys/fs/cgroup/cpu.max` is `25000 100000`; `cpu.stat` is readable.
- The projected token carries audience `agent-cell-gateway`, an `exp`, subject
  `system:serviceaccount:agent-exec:agent-exec`, and the observed Pod UID.
  Issued lifetime was 600 s (recorded, not assumed to be an issuer guarantee).
- The default token directory is absent; the Kubernetes API returns 401 when
  presented with the projected token over verified TLS.
- A confirmed, fsynced 48 MiB write into the 32 MiB emptyDir was evicted:
  `Usage of EmptyDir volume "tmp" exceeds the limit "32Mi".`
- A separate bounded 80 MiB write, with only the preflight emptyDir size limit
  raised to 96 MiB to isolate container storage enforcement, was evicted:
  `Pod ephemeral local storage usage exceeds the total limit of containers 64Mi.`
- Node `Ready=True`, `DiskPressure=False`. Probe Pods were deleted with UID
  preconditions. Namespaces and local image remain for the subsequent tests.

Evidence: `.local/k8s/preflight/*-status.json`, `*-result.json`, and
`node-conditions.json` (ignored locally; contain no bearer credentials).
The first ephemeral-storage assertion expected the API field spelling with a
hyphen; the actual eviction message uses "ephemeral local storage". Correcting
that assertion and rerunning both storage probes passed; neither test was skipped.

These observations establish that the preflight mechanisms work on this cluster;
they make no network isolation or hostile-code sandbox claim.


## 2026-10-07 — R2 scope narrowed

The CPU and storage observations above are historical results of the original
preflight, not isolation evidence. After threat-model revision 1, the approved
R2 scope removed CPU, OOM, storage eviction, filesystem freshness, and containment
probes. Their fake-agent behaviors and preflight code have been removed. Current
`make k8s-preflight` checks projected-token issuance only, alongside setup guards.
The fixed resource template, controller capacity limit, and API quota remain.

## 2026-10-08 — Narrowed R2 validation completed

**Completed:** `make test` (128), `make test-integration` (4), and
`make test-k8s` (18, plus the token preflight). The final cluster suite took
288 seconds. Ruff and whitespace checks passed. This is controller and API
behavior evidence only; it establishes no execution isolation guarantee.

Observed on Colima:

- Happy-path input survived kubelet environment processing byte for byte.
  Lost create responses and controller restarts retained completion authority;
  no execution issued a second create. Outages preserved queued work while
  cancellation and deadlines still applied.
- The kubelet produced `DeadlineExceeded` without reconciliation. Cleanup,
  orphan checks, and same-name UID replacement behaved as specified.
- The projected token carried the intended audience, subject, expiry, and Pod
  binding; its observed lifetime was 600 seconds. The Kubernetes API rejected
  it with 401 over verified TLS. Environment names matched the exact approved
  allowlist, including runtime-provided HOME and HOSTNAME.
- All eight admission negative cases were rejected by PodSecurity, and all
  eight positive controls passed in the temporary unlabeled namespace. No
  positive control was deferred. The writable-root control wrote and read
  back its probe, while the fixed template returned EROFS.
- Global capacity and the quota backstop held. Real controller-token calls
  could read Pods in agent-exec, could not read Secrets there, and could not
  dry-run a Pod create in rook-dev. Access reviews matched the specified role.

Corrections during validation: the permissions assertion initially omitted
built-in OpenID discovery URLs. The UID-conflict parser initially assumed a
storage-layer error shape; this API returns `details.kind: Pod` and a message
that the UID in the precondition does not match the UID in the record. The
parser and unit fixture now recognize that response; the real test records
409 and verifies the replacement remains. One integration attempt received a
500 at Mock Workday login; subsequent complete runs passed using only its HTTP
API, without changing that service.

Sanitized local evidence: `.local/k8s/evidence/ISO-1-ID-8.json`,
`ISO-1-permissions.txt`, `ISO-5-admission.json`, `LC-11-uid-conflict.json`, and
per-Pod status JSON. No bearer credentials or Pod env values are saved there.
Each cluster test ended with no Pods in agent-exec; temporary control namespaces
were deleted. The two marked lab namespaces, service accounts, Role,
RoleBinding, quota, and local image remain available. No push was performed.

## 2026-10-08 — S1 checkpoint-2 local regression comparison (unresolved)

This is local test evidence, not an S1 AWS experiment. The reviewer requested
one baseline run with an approximately 20-minute investigation cap.

- **Baseline `bb4f05b`:** an unmodified detached worktree, a separate locked
  Python environment, and its original rebuilt agent image. `make test-k8s`
  passed its preflight and **18 tests** in **304.18 seconds**.
- **Checkpoint-2 `024b024`:** immediately afterwards, rebuilt its agent image
  and ran the same target. Preflight passed; **9 tests passed, 1 failed** in
  **285.20 seconds**, stopping at `test_LC_8_kubelet_deadline_without_reconciler`.
  The Pod was not observed in `Failed/DeadlineExceeded` within 60 seconds.
- **Unmodified isolated LC-8 retry:** **1 failed, 1 teardown error** in
  **170.78 seconds**. Teardown's namespace read received a server timeout.

Both checkouts used Colima default (aarch64, 2 CPUs, 2 GiB, k3s 1.35), pinned
kubectl 1.35, and local Mock Workday running alongside the existing shared
workloads. No test timeout or VM resource setting was changed. Host load was
not controlled: its one-minute average was about 11 during the baseline and
rose to about 109 later in the comparison.

For the failed full-run LC-8 Pod, saved events show start at 17:40:59 UTC,
`DeadlineExceeded` at 17:41:53, and `Killing` at 17:42:29; its last captured
phase was still Running. This establishes delayed deadline/termination
observation, not its cause. The earlier happy-path and LC-6 cleanup failures
did not recur in either full run. **The baseline passed and the current
checkout failed: a checkpoint-2 regression has not been ruled out or fixed.**
Resource pressure is a hypothesis, not a verified explanation. Investigation
stopped within the requested cap; assertions were not relaxed.

Evidence is retained locally under `.local/s1/`: `baseline-bb4f05b.log`,
`baseline-bb4f05b-summary.json`, `baseline-environment.txt`, `baseline-evidence/`,
`current-comparison.log`, `current-comparison-summary.json`,
`current-lc8-isolated.log`, and `current-lc8-events.json`. The temporary worktree
was removed after exporting its sanitized evidence. No AWS resources changed.

## 2026-10-08 — S1 LC-8 commit comparison (inconclusive bisect)

The reviewer requested LC-8 alone at three commits, rebuilding the agent image
at each commit, with an approximately 30-minute diagnostic cap. Each detached
worktree used its own locked Python 3.12 environment and pinned kubectl. The
same Colima VM (2 CPUs, 2 GiB, shared workloads) stayed running throughout.
Neither the 30-second Pod deadline nor the 60-second assertion wait changed.

| Commit | Isolated LC-8 result | Pytest duration |
|---|---|---|
| `c21bc07` | 1 failed | 155.00 s |
| `c4dfd6f` | 1 failed | 102.25 s |
| `024b024` | 1 passed | 83.54 s |
| `bb4f05b`, additional baseline with read-only request/state tracing | 1 failed | 125.72 s |
| `c21bc07`, repeat with the same tracing | 1 passed | 75.52 s |

All three failures were at `tests/test_k8s.py:204`, waiting for phase `Failed`:
`Condition did not converge within 60 seconds`. They did not reach the
`DeadlineExceeded` reason assertion. The first `c21bc07` run saved no Pod
snapshot and had no new Pod events; its database state was not captured, so
the launch outcome is unknown. The added tracing only observes requests and
records selected database fields before teardown; it does not retry requests,
change reconciliation, or include credentials.

Verified Pod evidence (UTC):

- `c4dfd6f`, Pod `exec-081865bf-d839-404b-8872-5ea25b27be14`: start
  18:02:53, first deadline event 18:03:23, Killing 18:03:38. The last saved
  phase was `Pending`, with no top-level reason, although its container state
  was running and its readiness conditions said `PodFailed`. Deadline
  detection occurred at 30 seconds; terminal-phase observation did not
  converge before the assertion expired.
- `024b024`, Pod `exec-4dfa19e1-9dd1-4508-bc72-58e9d594b534`: start
  18:05:28, first deadline event 18:06:03, Killing 18:06:06, container exit
  18:06:11. The saved phase/reason was `Failed/DeadlineExceeded`; LC-8 passed.
- Baseline `bb4f05b`, Pod `exec-eb33cba6-32fa-4136-97ed-9306fc152f84`:
  start 18:08:35, first deadline event 18:09:42 (67 seconds later), Killing
  18:09:53. The last saved phase was `Running`, with no reason. Request tracing
  recorded a successful list (200), create (201), one create call, zero delete
  calls, and database state `RUNNING` at assertion failure. During cleanup,
  `FailedSync` reported a Docker container-status lookup for a missing container.
- The unchanged `c21bc07` repeat observed `Failed/DeadlineExceeded`, then
  transitioned the database to `TIMED_OUT` after restarting reconciliation.

The first failing commit in the requested order was `c21bc07`, but **it is not
an established first bad commit**: it also passed unchanged, the latest code
passed, and the pre-checkpoint baseline reproduced the failure. Kubelet logs
showed slow housekeeping and unrelated health-probe timeouts before testing.
At 18:08:30 UTC, the guest's one-minute load average was 11.44 and its CPU
pressure `some avg60` was 90.27%. These verify cluster delays and contention;
they do not isolate the cause of every failure or prove all checkpoint-2 code
free of regressions. No speculative runtime fix or timeout increase was made.

Local evidence: `.local/s1/bisect-summary.json`, `bisect-<commit>.log`,
`bisect-<commit>-events.json`, `bisect-evidence/`, `trace-summary.json`,
`trace-<commit>.log`, `trace-<commit>.json`, `trace-bb4f05b-events.json`,
`diagnostics/lc8_trace.py`, `bisect-kubelet-*.log`, and `bisect-pressure.txt`.

A final current-checkout image rebuild succeeded. `make test-k8s` then failed
in preflight after 45.61 seconds: `httpx.ReadTimeout` on the Pod-create POST at
`scripts/k8s_preflight.py:96`. **Zero suite tests ran; 18/18 has not been
re-established.** Its logs are `bisect-current-full.log` and
`bisect-current-full-summary.json`. Investigation stopped within the requested
cap without a root-cause fix. Checkpoint 2 remains incomplete; the approved
two-namespace harness-policy and pre-created control-namespace changes remain
pending the regression gate. There is no new spec ambiguity.

Cleanup verified an empty `agent-exec` namespace. The four temporary
worktrees were removed after exporting their sanitized evidence; the current
agent image was restored. No AWS resources were created or changed.

## 2026-10-08 — S1 checkpoint 2 code complete; EKS validation deferred to E1

The reviewer accepted the local Kubernetes failures as environmental and
instructed that Colima remain stopped. LC-8 and the other Kubernetes tests will
run on EKS in E1; a failure there remains a finding, not an assertion to relax.

Implemented the test-only `s1-harness` trust and EKS policy association, scoped
exactly to `agent-exec` and `agent-exec-psa-control`. The EKS harness uses that
role only for admin kubectl calls and separately refreshes controller tokens.
Operator setup pre-creates the unlabeled PSA control namespace and its service
account. ISO-5 never creates/deletes that namespace on EKS; LC-7 restores only
its namespaced RoleBinding. Added credential-path probes that retain no secret
values, profile guards, and unit coverage of the approved boundaries. The spec
and implementation record the same test-only rationale.

Validation: `make test` **169 passed, 22 deselected** (18 Kubernetes and four
HTTP integration tests), one dependency deprecation warning. Ruff check,
formatting and `git diff --check` passed. Mock Workday was unavailable, so its
four integration tests retain the earlier passing evidence, not a fresh run.
Colima was not started. Live EKS role assumption, namespace authorization and
Fargate lifecycle behavior remain unverified until E1. No AWS resources changed.

## 2026-10-08 — S1 checkpoint 3 plan prepared; no apply

Checkpoint 2's approved harness/control-namespace implementation is committed
as `eac2101`. Colima stayed stopped throughout this continuation. The final
`make test` run passed **180 tests, 22 deselected**, in 62.62 seconds: the
169 checkpoint-2 unit tests plus 11 operator-workflow cases. The deselected
set is 18 Kubernetes tests and four HTTP integration tests. Mock Workday is
unavailable while its local container environment is stopped; the four HTTP
tests retain the earlier passing evidence and were **not rerun**. This is not
a fresh full non-Kubernetes integration result. Ruff check/format, shell syntax,
`git diff --check`, `terraform fmt -check` and `terraform validate` passed.

Terraform 1.16.4 with the signed, locked AWS provider 6.67.0 produced two saved
plans, each **76 additions, zero changes, zero deletions**. A JSON comparison
verified identical planned resource values except for the STS endpoint policy.
The spec keeps STS but does not specify that policy; the review alternatives are
`sts:GetCallerIdentity` only and the default full endpoint policy. Neither is
selected for apply. The DNS block response is proposed as `BLOCK/NXDOMAIN`.

The isolated backend is `beejmaxx-lab-tfstate-dev`, key
`dev/experiments/s1-fargate.tfstate`. Planning only read the foundation's SSM
network outputs; it did not modify foundation/bootstrap state, the state
bucket configuration, or `mock-workday` ECR. The direct operator egress was
verified without proxies as `120.229.48.82/32`. The provider pins account
`729608197929` and Region `us-east-2`. All planned taggable resources carry
`lab=agent-runtime`, `experiment=s1`.

The plan includes the execution VPC/two private subnets, one EKS cluster and
Fargate profile, required endpoints and restricted policies, DNS Firewall and
query logs, the canary bucket and fake-agent repository, trusted EC2/EIP and
PrivateLink/NLB, and the approved IAM/access entries. It has no IGW, NAT,
peering, node group, IAM OIDC provider or Pod Identity association. The two
required EKS/Fargate service-linked roles were verified absent, so Terraform
owns their lifecycle; the existing shared ELB service-linked role is untouched.

The Makefile/operator tooling guards the S1 state and saved plan, keeps operator
credentials off the host, exports sanitized evidence, and orders teardown so
Pods are gone before removing their API/network paths. Unit tests cover those
boundaries, including an inventory authorization failure producing an incomplete
result rather than a false clean inventory. Setup and teardown themselves have
not run against AWS and remain checkpoint-4 validation, as do minimum Fargate
SG/DNS dependencies, namespace-scoped harness authorization and E1-E10.

Read-only inventory found no S1 resources in the network, compute, EKS, load
balancer, DNS/logging, role/profile, canary or ECR checks. The **OIDC inventory
was denied by project SCP `p-5fs30qru`**. E9 and a fully clean leftovers claim
therefore need visibility restored or reviewer-agreed alternative evidence;
absence from the Terraform plan alone does not prove absence in AWS. The spec's
former broad statement that the Free-plan SCP "allows IAM" was too broad and
now records the observed exception. The Free plan is active with $200 credits;
a successful plan does not prove create permissions or quotas.

Local evidence: `.local/s1/checkpoint3-final-unit.log`,
`terraform-plan-identity.log`, `terraform-plan-default.log`,
`plan-sts-{identity,default}.tfplan`, `plan-sts-{identity,default}.json`,
`inputs-sts-{identity,default}.tfvars.json`, `plan-comparison.json`,
`plan-review-sts-{identity,default}.json`, and `inventory-readonly.json`.
See `infra/experiments/fargate/README.md` for the review and run workflow.
No AWS resources were created or changed; no apply/destroy or local Kubernetes
start occurred. Stop here for plan review before checkpoint 4.

## 2026-10-08 — S1 checkpoint 4 first attempt: setup blocked; teardown

The reviewer approved the narrow STS baseline (`sts:GetCallerIdentity` only),
DNS `BLOCK/NXDOMAIN` with redirection inspection, and an exact-issuer IAM lookup
instead of the SCP-denied provider listing. Those decisions and the lookup's
error handling are committed together in `4683504`. No STS action or DNS name
was broadened in this attempt.

The approved plan was selected as `.local/s1/plan.tfplan`, with the same direct
operator egress `120.229.48.82/32`. `S1_RUN_APPROVED=1
S1_STS_GET_CALLER_IDENTITY_ONLY=true make s1-up` began at
**2026-10-07 19:17:23 UTC** (2026-10-08 03:17:23 Asia/Shanghai). Resources were
created only in the S1 state/Region. No foundation/bootstrap configuration or
Mock Workday resource was changed.

**Verified setup blocker:** CloudTrail records `ModifyVpcEndpointServicePermissions`
at **19:20:36 UTC**, request ID `d96c476a-2489-451f-a2f4-3012b4d622fc`, with
`Client.InvalidPrincipal` for
`arn:aws:iam::729608197929:role/managed/AccountFullAccessRole`.
The endpoint service was created, but its allowed-principal list remained empty
and no callback interface endpoint was created. AWS explicitly does not support
principal ARNs containing IAM path components in this API
([API restriction](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeVpcEndpointServicePermissions.html)).
The plan therefore passed static validation but could not establish PrivateLink.
No account-root or wildcard principal was substituted. A pathless operator
identity needs a reviewed design before retry.

**Verified E9 visibility failure:** querying the exact ARN
`arn:aws:iam::729608197929:oidc-provider/oidc.eks.us-east-2.amazonaws.com/id/D73CABB693770EDF0B9025002907EA16`
also returned `AccessDenied` with an explicit deny in SCP `p-5fs30qru`.
It did **not** return `NoSuchEntity`. No OIDC absence claim follows from this
response, and no SCP/permission bypass was attempted.

The EKS control plane became active; the Fargate profile reached `ACTIVE` with
no health issues. Neither proves that execution Pods start. Live IAM reads
confirmed the controller and Fargate role trust/permissions, harness trust only
to the controller, and `AmazonEKSAdminPolicy` scoped exactly to `agent-exec` and
`agent-exec-psa-control`. The controller had zero EKS access-policy associations.
The operator role was distinct from these three roles. Namespace setup had not
run, so the control-namespace and per-Pod ENI portions of E9 remain unverified.

| Experiment | Result in this attempt |
|---|---|
| E1 | Blocked before harness setup; **0 Kubernetes tests ran**. No lifecycle assertion or timeout was weakened. |
| E2 | Not run; no cold-start measurements. |
| E3 | Not run; no boot-ID/kernel comparison. |
| E4 | Not run; no execution egress or callback-path evidence. |
| E5 | Not run; neither mutation control was exercised. |
| E6 | Not run; no DNS probe or redirection-chain finding. |
| E7 | Not run; publishing an image does not prove a Fargate image pull or container credential isolation. |
| E8 | Not run; prepared privilege/admission probes were not executed. |
| E9 | Partial static/live role evidence above; failed PrivateLink configuration and unverified OIDC absence block a pass. |
| E10 | Not run; no cross-execution completion test. |

The image was built as `linux/amd64`, pushed to the tagged S1 ECR repository,
pulled by digest and compared to the built image:
`sha256:c92e4a506cb8457473bfe654b37c4233722ffdc2d8a25eacd4c582c62f0078c9`.
Command: `DOCKER_HOST=unix:///Users/bijan/.colima/s1-builder/docker.sock
uv run python -m scripts.s1_image`. The temporary registry credentials were not
persisted. A separate Docker-only Colima builder was used and stopped; the
existing local Kubernetes profile remained stopped throughout.

To stop further provisioning after the known blocker, Terraform received one
SIGINT for graceful shutdown. Terraform 1.16.4 instead panicked while serializing
an in-progress resource: `Instance aws_eks_fargate_profile.execution has status
ObjectStatus(0), which cannot be saved in state`. The saved state was backed up
and reconciled with AWS before cleanup. It retained the failed endpoint service
as tainted, but had no saved instance for the Fargate profile or the DNS allowlist.
The teardown's explicit profile deletion covers the former. The unattached
allowlist `rslvr-fdl-62e8036b33f64041` was verified S1-tagged and deleted explicitly
at **19:31:23 UTC**; its only sibling firewall rule referenced the blocklist.

Validation before apply: **183 unit tests passed, 22 deselected** in 105.39 s;
14 focused operator/OIDC tests passed. The deselected set remains 18 Kubernetes
and four unavailable HTTP integration tests. The five explicit experiment
functions collected successfully but were not executed. Ruff check/format and
`git diff --check` passed. Tool versions: Terraform 1.16.4, AWS provider 6.67.0,
Python 3.12, uv 0.12.5 on the host, EKS 1.36/platform `eks.14`, SSM agent
3.3.4793.0, Docker client 20.10.11/server 29.5.2. Host Ubuntu 24.04 bootstrap
finished successfully at **19:21:27 UTC**.

Raw local evidence (ignored by Git): `.local/s1/checkpoint4-up.log`,
`checkpoint4-session.json`, `checkpoint4-image-push.log`, `image.json`,
`privatelink-invalid-principal.json` (CloudTrail fields with identity-session
metadata omitted), `e9-roles.json`, `e9-partial-config.json`,
`e9-partial-live.json` (exact lookup denial and tool exception),
`partial-state-backup.json`, `partial-state-list.txt`,
`cleanup-untracked-domain-list.json`, `checkpoint4-down.log`,
`host-evidence.tgz`, `dns-query-events.json`, `checkpoint4-unit.log`,
`checkpoint4-oidc-tests.log`, `experiment-collection.log`, and builder logs.

A post-finding pre-apply guard now rejects the current path-bearing operator ARN
before contacting AWS. Its focused suite passed **15 tests**; this changes no
principal permissions and does not implement an unreviewed replacement role.

**Cleanup result at 19:46 UTC: incomplete, authentication blocked.**
`S1_RUN_APPROVED=1 make s1-down` deleted the Fargate profile and cluster; an
independent `ListClusters` returned an empty list. Its subsequent Terraform
destroy was blocked by the crashed apply's stale S1 lock. After verifying that
the original process had exited and that the lock referenced only
`dev/experiments/s1-fargate.tfstate`,
`terraform -chdir=infra/experiments/fargate force-unlock -force
f23b34f7-7b4d-41f5-ce1d-c3319d4a0e6a` released it. Locking remained enabled.

The second `make s1-down` planned **64 destructions** and confirmed **63**,
including the trusted host, EIP, endpoints, endpoint service/NLB, canary,
fake-agent ECR repository/images, S1 roles and both new service-linked roles.
The last VPC deletion returned HTTP 400 with provider error `unexpected EOF`
(request ID `71564542-27ef-4716-9eec-70de3b848636`). A subsequent state list
contained only `aws_vpc.execution`. This is a saved-state observation, **not**
proof of whether the VPC still exists or which service-created dependency may
remain. The possible outstanding VPC is `vpc-09ffaf540e50e3e9a`; its recorded
cluster security group is `sg-097473168467cf37f`.

Before that question could be resolved, both the MCP connection and local CLI
lost valid AWS credentials. CLI `GetCallerIdentity` returned `ExpiredToken`.
The local CLI is 2.37.10 and supports `aws login`; the installed Signing In to
AWS skill requires explicit confirmation before invoking that command. A
confirmation request was issued for `aws login --profile agent-runtime
--region us-east-2`; no answer had arrived when this record was committed.
Another `make s1-down` stopped at its caller guard, and `make s1-leftovers`
could not complete authentication. **No clean inventory claim is made.**
Even after reauthentication, the exact OIDC lookup's SCP denial must remain an
incomplete check unless visibility is restored or new evidence is agreed.

Known billable compute, public IPv4 and interface endpoints were deleted within
about 27 minutes of apply start; exact final resource absence is not verified.
The session did not leave the local builder or Kubernetes running. Next action
after approved sign-in: inspect the recorded execution VPC's ENIs/security
groups, remove only verified S1 leftovers, rerun `make s1-down` and
`make s1-leftovers`, then update this record with actual results.

Additional cleanup evidence: `.local/s1/checkpoint4-down-retry.log`,
`checkpoint4-down-final.log`, `checkpoint4-unlock.log`, `cleanup-progress.json`,
`cleanup-vpc-dependencies.json` (MCP credential failure),
`cleanup-auth-error.log`, `checkpoint4-leftovers.log`,
`checkpoint4-final-focused.log`, and `checkpoint4-handoff.txt`.

### S1 PrivateLink reviewer correction — 2026-10-08

The reviewer approved `arn:aws:iam::729608197929:root` as the sole endpoint
service allowed principal, with `acceptance_required=true`. The existing
accepter still accepts only the Terraform-created endpoint ID. In this
single-account lab, account IAM controls who can request a connection and
explicit acceptance of that endpoint gates the connection. The account-root
ARN identifies account principals; it does not require root-user credentials.
A deployment across separate accounts would name the consumer account instead.
No wildcard principal or additional operator role was introduced.

The obsolete operator-role-path pre-apply guard and its test were removed;
the spec and operator instructions now reflect the approved design. Local
validation: **14 focused tests passed**, Terraform fmt/validate, Ruff
check/format, and `git diff --check` passed. Evidence:
`.local/s1/privatelink-correction-tests.log` and
`.local/s1/privatelink-correction-validate.log`.

This correction has not been applied to AWS. The old saved plan must be
regenerated before a retry. Expired credentials still block final cleanup
verification, and the exact-issuer OIDC lookup denial remains unresolved;
neither a clean inventory nor a successful PrivateLink connection is claimed.

### S1 partial-apply cleanup completed — 2026-10-07 20:19 UTC

After the reviewer refreshed `agent-runtime`, `S1_RUN_APPROVED=1 make s1-down`
found the execution VPC still present. Direct EC2 inspection found no ENIs,
but EKS-created security group `sg-097473168467cf37f` remained, with ownership
tags for `lab-exec-s1`. Deleting that unused group at **20:12:16 UTC** allowed
Terraform to finish deleting `vpc-09ffaf540e50e3e9a` (one remaining resource).
The isolated S1 Terraform state is empty. Foundation/bootstrap were untouched.

The tagging index retained deleted resources. The inventory now checks supported
EC2 entries by exact resource ID and records confirmed deleted/terminated entries
separately; unknown resource types and failed lookups remain failures.
`make s1-leftovers` finished successfully at **20:19 UTC**, with `[]` and the
explicit reviewer-accepted OIDC exception. Exact-issuer lookup remains denied
by SCP `p-5fs30qru`; provider absence is **unverified**, not a pass. The approved
mitigations are the plan with no provider, API access mode, and E7's live Pod
web-identity checks (the latter still awaits the retry).

Validation: **185 non-Kubernetes tests passed, 22 deselected** in 37.04 s;
**16 focused operation tests passed**. Ruff passed. The regenerated plan has
**76 creates, zero changes/deletions**, the account-root allowed principal with
acceptance required, STS `GetCallerIdentity` only, API access mode, and no IAM
OIDC provider. Raw evidence: `.local/s1/retry-cleanup-down.log`, `retry-vpc.json`,
`retry-security-groups.json`, `retry-enis.json`, `retry-delete-cluster-sg.log`,
`retry-leftovers-verified.log`, `first-attempt-final/`, `retry-operations-tests.log`,
`retry-unit.log`, `retry-plan.log`, and `retry-plan.json`.
