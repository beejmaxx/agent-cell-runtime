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

## 2026-10-08 — S1 checkpoint-4 retry with static operator credentials

**Completed with findings. Teardown and standalone leftovers checks passed, with the accepted OIDC visibility exception.**
All times below are UTC on 2026-10-07 (2026-10-08 in Shanghai). The operator is
exactly `arn:aws:iam::729608197929:user/lab-operator-cli`, using the local
`agent-runtime` profile in `us-east-2`. Credentials remain local. The earlier
15-minute `aws login` credential expiration was a rotation, not the session
end; the reviewer subsequently selected non-expiring static credentials.

Environment: Terraform 1.16.4, AWS provider 6.67.0, EKS/Kubernetes 1.36,
Ubuntu 24.04 trusted host, Python 3.12.3, AWS CLI 2.37.10, uv 0.12.5,
SSM agent 3.3.4793.0. The Docker-only Colima `s1-builder` built linux/amd64;
the local Kubernetes profile remained stopped. ECR image:
`729608197929.dkr.ecr.us-east-2.amazonaws.com/agent-runtime/fake-agent@sha256:c92e4a506cb8457473bfe654b37c4233722ffdc2d8a25eacd4c582c62f0078c9`.
Observed Fargate allocation: `0.25vCPU 0.5GB`.

### Provisioning and recovery observations

- Retry resource creation began about 20:23:40. The account-root endpoint
  permission was accepted. At 20:26:51, `AcceptVpcEndpointConnections` returned
  an `Unsuccessful` item (`Unavailable`, endpoint still provisioning) inside a
  successful API response. Provider 6.67.0 ignored that list. Retrying acceptance
  for **only** `vpce-03183c1b19676ba00` succeeded with `Unsuccessful: []`.
  The service retained `acceptance_required=true` and only the account-root
  allowed principal. Evidence: `.local/s1/retry-accept-cloudtrail.json`,
  `retry-accept-response.json`, `retry-privatelink-principals.json`,
  `retry-privatelink-connections.json`.
- DNS Firewall association priority 100 was rejected as reserved,
  `RSLVR-02017`; priority 101 succeeded (`c6fd41c`).
- **Implementer error:** a follow-up apply proceeded despite an assertion
  detecting changes beyond the intended DNS association. Its shell did not
  use `set -e`; the commentary prematurely described the plan as one addition.
  The actual plan replaced the trusted host/EIP association/NLB target
  attachment and normalized two DNS lists, in addition to the association.
  After a separate EIP is attached, EC2 reads the launch-time public-IP flag
  as true, causing replacement drift. `c44a081` ignores subsequent drift only
  in that launch flag and canonicalizes returned DNS names. The managed EIP
  remains authoritative. Subsequent dependent commands use `set -e`, and a
  later plan showed no changes. Evidence: `dns-priority-plan.json`,
  `dns-priority-plan.log`, `dns-priority-up.log`, `retry-baseline-plan.log`.
- At 20:55:46, image pulling failed because the allowed ECR layer bucket's
  CNAME `s3-r-w.us-east-2.amazonaws.com` was inspected and blocked. Query-log
  evidence showed BLOCK/NXDOMAIN. Added only that exact alias (`2051ad1`);
  the next image pull took 2.322 s. STS remained GetCallerIdentity-only, and
  no new endpoint or wildcard was introduced. Evidence:
  `retry-startup-dns.json`, `retry-dns-alias-add.json`, and
  `.local/s1/live-pods/exec-3a24a7d4-e0bc-48e0-911c-bbd8dadda6f0.json`.
- Harness setup needed kubectl's rendered exec defaults (`env: null`,
  `provideClusterInfo: false`) included in the exact guard (`89cdaf4`). Evidence
  export also needed directories owned by ubuntu rather than root (`8e4b5b6`).

### E1 lifecycle and fixture results

The preflight passed after the DNS correction. The first suite produced
**8 passed, 10 failed, 196 deselected in 678.85 s**. LC-7's runtime fail-closed
assertions passed, but the harness could not restore the RoleBinding: the
RBAC escalation check rejected granting the controller's Pod rules. The
harness's own Pod operations were otherwise authorized by its EKS access
policy; the denial must not be read as inability to create Pods. That left the
binding absent and invalidated nine subsequent failures as lifecycle evidence.
The reviewer accepted this denial as positive isolation evidence and assigned
restoration to the operator, without adding bind/escalate to the harness.

After operator restoration, a run excluding only LC-7's RBAC case produced
**16 passed, 1 failed, 197 deselected in 727.28 s**. The remaining failure was
LC-8. The reviewed operator fixtures then passed **2 tests, 16 deselected in
83.17 s**: LC-7 RBAC restoration and ISO-1/ISO-5/ID-8. Thus **17 of 18 distinct
E1 tests passed across these runs; E1's mandatory pass condition is not met**.
ISO-7 remains a substrate observation. No assertion or timeout was relaxed.
The latest ISO credential/template runs passed with the original 60-second
wait, so no timing adjustment was justified or made.

LC-7 restoration (`4224e53`) uses a fixed ConfigMap request and restores only
the source-defined controller binding from the local operator. The approved
ISO-1 read-only impersonation query (`464ce5c`) similarly runs on the operator;
only its text report reaches the host. Existing permission assertions remain
unchanged. Missing/malformed reports fail closed. No operator credential,
cluster-scoped harness grant, bind, or escalate permission was introduced.

**LC-8 observed timing:** execution `1fed8c88-e3d9-408f-a76f-bdec9a2f6d03`
was created at 21:17:39.971654 with runtime deadline 21:18:09.971654; its Pod
was created at 21:17:40, scheduled at 21:18:15, kubelet startTime 21:18:16,
and container started at 21:18:19. It was still Running when the 60-second
assertion wait ended. The reconciler was intentionally stopped. Kubernetes
[defines activeDeadlineSeconds relative to Pod startTime](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/),
so the 30-second kubelet timer was not due until approximately 21:18:46.
This explains why the test's budget was insufficient for that startup, but
**does not establish eventual DeadlineExceeded or a wall-clock bound from
execution creation**. LC-8 remains failed; no local-cluster debugging resumed.
Two captured scheduling-to-container-start samples were 4 s each; total
Pod-creation-to-start was 39 s and 46 s. These are observations, not a latency
or isolation guarantee.

Reproduction commands: `S1_RUN_APPROVED=1 make s1-up`; trusted-host
`K8S_PROFILE=eks uv run python -m scripts.k8s_preflight`, then
`K8S_PROFILE=eks uv run pytest -m k8s -p scripts.s1_evidence --tb=short`;
rerun selection `-k 'not (LC_7 and rbac)'`; reviewed selection
`-k '(LC_7 and rbac) or ISO_1_ISO_5_ID_8'` with the local operator polling
`restore_fixture_binding`. Exact drivers are retained in `.local/s1/`.

Evidence: `.local/s1/e1-first-suite.tgz`, `e1-remainder.tgz`,
`retry-reviewed-fixtures.log`, `retry-lc7-binding-denial.log`,
`retry-lc8-evidence.log`, `retry-observed-startup-times.json`, and sanitized
`live-pods/`. Local validation after the fixture changes:
**198 passed, 22 deselected in 58.45 s** (unit suite, excluding Kubernetes and
integration), in `retry-final-unit.log`; focused fixture/profile tests:
**49 passed** in `retry-permissions-units.log`.

### E9 configuration evidence collected before mutations

`.local/s1/retry-e9.json` records API access mode, the exact endpoint-service
principal/acceptance gate, controller access-policy associations empty, and
harness `AmazonEKSAdminPolicy` scoped exactly to `agent-exec` and
`agent-exec-psa-control`. The harness trust names only `lab-s1-controller`;
the controller can assume only that harness role. The Fargate execution trust
names only `eks-fargate-pods.amazonaws.com`, constrained by the account and
`arn:aws:eks:us-east-2:729608197929:fargateprofile/lab-exec-s1/agent-exec/*`.
Its policy permits GetAuthorizationToken on `*` (the API's required resource
scope) and the three image-pull actions only on `agent-runtime/fake-agent`.
These are four distinct authority identities: operator IAM user, controller
instance role, Fargate execution role, and test-only harness role. No operator
role was copied onto the host.

The execution VPC has no attached internet gateway, no NAT gateway, and no
default route; its routes are local plus the S3 gateway endpoint. The control
namespace has no PSA labels (`retry-namespace-labels.json`). ENI evidence in
`pod-enis/` maps each captured Pod's UID and IP to the EC2 interface by exact
VPC/IP filters. The observed Fargate annotation supplies the security group,
not an ENI ID; the mapping is not inferred from a nonexistent ENI annotation.
Baseline samples had exactly `sg-0c66b5cc14c01d62d`, without the cluster group.

The exact-issuer IAM lookup was explicitly SCP-denied again. **OIDC absence
remains unverified**, as accepted by the reviewer. The plan contains no IAM
OIDC provider, EKS uses API access mode, and the E7 application-visible checks
supply mitigating evidence; none proves provider absence.

### E2 startup observations

Eight completions (five sequential, three concurrent) succeeded. All eight
have database create/callback timestamps. Six have exact container-status
`startedAt`; two concurrent Pods were deleted before that field was captured,
so their Kubernetes `Started` event timestamps are reported as proxies, not
silently treated as exact container-status observations. Scheduled timestamps
come from Kubernetes events. Combining those explicitly labeled sources:

| Group | Create → container start, p50 / max | Scheduled → start, p50 / max | Create → callback, p50 / max |
|---|---|---|---|
| Five sequential | 40.34 / 43.07 s | 3.23 / 3.80 s | 41.55 / 44.29 s |
| Three concurrent | 38.26 / 39.31 s | 3.57 / 3.74 s | 39.33 / 40.74 s |
| All eight | 39.04 / 43.07 s | 3.24 / 3.80 s | 40.20 / 44.29 s |

These are one-session observations, not a benchmark. The runtime's RUNNING
state was not substituted for Kubernetes/container start. Evidence:
`.local/s1/retry-E2-summary.json`, `retry-E2-events.json`,
`retry-E2-timing-analysis.json`; `summarize_timing.py` records the calculation
and the source selected for every row. Sample recording window:
21:29:56–21:34:22 UTC. All captured allocations were 0.25 vCPU / 0.5 GB.

### E3, E4, E7, E8 and E10 baseline observations

The explicit baseline driver
`K8S_PROFILE=eks uv run pytest scripts/s1_experiments.py -s -p scripts.s1_evidence --tb=short`
passed **5 tests in 775.98 s**. These test counts confirm the driver completed;
the security conclusions below come from inspecting its observations.
Evidence archive: `.local/s1/baseline-experiments.tgz`, extracted at
`baseline-experiments/.local/s1/evidence/experiments/`; runner log:
`retry-experiments.log`.

- **E3:** the concurrent inspect executions reported kernel `6.1.186` and
  distinct boot IDs `5c0743c0-d75a-40fb-b73a-0b443e37a644` and
  `0c32bf51-ecd6-421d-8b9b-92b8000e0d49`. This supports separate kernels,
  not a claim about resistance to kernel or hypervisor compromise.
- **E4:** completion through PrivateLink succeeded; the five tested non-completion
  routes returned 404. Port 8000, direct trusted-host private/public listeners,
  the private closed-port control, peer execution listener, and public IP
  listener timed out. The peer listener was confirmed live by a loopback HTTP
  request in its own Pod. The trusted listener was serving successful callbacks.
  The host reached `https://1.1.1.1` (301) and `https://example.com` (200), proving
  the external controls live; the Mac's direct 1.1.1.1 request failed, so it was
  not used as the positive control. The public-name probe in the execution
  returned `gaierror` after 20 s: **DNS failure, not proof of a TCP network deny**.
  Its default resolver points to absent CoreDNS; the direct VPC-resolver tests
  are reported separately in E6. Evidence: `retry-host-controls.log`,
  `retry-E4-baseline.json`, and the archive’s `E4-baseline-peer-control.json`.
- **Required-path exposure:** the private Kubernetes API accepted TCP/TLS;
  anonymous `/api` and the projected token both returned 401, while `/healthz`
  returned 200. ECR API, ECR DKR and STS endpoint IPs accepted TCP on 443
  without credentials. Those were TCP-connect probes, not authenticated AWS
  API successes. The canary anonymous PutObject connected over verified TLS
  and received 403. Its enforcing layer is tested by E5 below.
- **E7:** UID/GID 65532, effective capabilities zero, read-only root, intended
  token audience/subject/Pod binding, no default service-account token mount,
  no AWS credential environment names, and no nonempty default credentials
  file. IMDSv2 token, IMDS role listing, and container metadata probes timed
  out; no credentials were obtained through the enumerated paths. The private
  image started, providing the infrastructure-role positive control. This is
  application-visible evidence; the node's infrastructure identity is trusted.
- **E8:** root, escalation, NET_RAW and unconfined-seccomp trials were rejected
  by restricted PodSecurity. The privileged trial was rejected by Kubernetes
  validation because `privileged=true` conflicted with
  `allowPrivilegeEscalation=false`; **that trial alone does not prove PSA or
  Fargate rejected a valid privileged request**. Writable root plus requested
  NET_BIND_SERVICE was admitted. Actual UID/GID stayed 65532, effective
  capabilities stayed zero, and root write/read-back succeeded (`probe`).
  All 18 repeated network targets and credential probes matched baseline;
  this variant did not repeat the separate peer-listener target. These are
  bounded P1 observations, not exhaustive maximum-privilege or P2 evidence.
- **E10:** execution A's credential received 401 when completing B; B remained
  RUNNING. A's own completion succeeded through PrivateLink. The destination
  therefore enforced per-execution authority in this attempt.

E8 admission correction: the trial now sets both `privileged=true` and
`allowPrivilegeEscalation=true`, avoiding the inconsistent-manifest rejection.
A separate operator `kubectl create --dry-run=server` of that valid trial was
rejected explicitly by `restricted:v1.36` for privileged execution and
escalation. No Pod was created. This closes the privileged-admission evidence
gap above; it does not attribute the rejection to Fargate itself. Exact driver
and output: `.local/s1/valid_privileged_control.py` and
`retry-E8-valid-privileged.json`.

### E5 security-group mutation

The operator deleted only `agent-exec/execution` SecurityGroupPolicy, then
recreated both the peer listener and probe Pod. Both ENIs had exactly the
cluster group `sg-07e005ac3dc0cbcf4`, verified in `retry-mutation-enis.log` and
`pod-enis/`. The probe's stdout was collected instead of relying on a callback.
The mutation test passed **1 test in 163.69 s**; results were recorded at
21:46:58.306101. The original policy was restored in the runner's `finally`.

Compared with E4, peer HTTP changed **timeout → 200**, while all five tested
PrivateLink routes changed **404 → timeout**. The former demonstrates a
measurable peer-isolation effect of the Pod group; the latter demonstrates
the callback endpoint's ingress requirement for that group. The other probes
had the same outcomes, including canary 403 and no metadata credentials.
Those unchanged outcomes do not independently prove a security-group effect.
Routing remains structurally checked with no IGW, NAT, or default route; no
internet-capable routing mutation was introduced.

Exact operator/host drivers: `.local/s1/run_mutations.py E5-no-SGP` and
`mutation_sg.py`; evidence: `retry-E5-no-SGP.log`, `retry-E5-result.json`,
`E5-original-SGP.json`, `E5-no-SGP.tgz`. The host driver confirms the peer's
loopback listener before probing across Pods, and cancels both executions
after collecting stdout. No Pod credential is included in the result logs.

### E5 S3 endpoint-policy mutation

Changing only the S3 gateway endpoint policy to full access changed the
anonymous canary PutObject from **403 to 200** over verified TLS. The test
passed **1 test in 56.68 s**, with result recorded at 21:49:44.854125. The
bucket's SourceVpce restriction remained intact. The original image-layer-only
endpoint policy was restored immediately in `finally`. This positive control
identifies the endpoint policy as the baseline write-denial layer.
Evidence: `.local/s1/retry-E5-s3-result.json`, `retry-E5-s3-full.log`,
`E5-original-s3-policy.json`, `E5-s3-full.tgz`; driver:
`PYTHONPATH=. uv run python .local/s1/run_mutations.py E5-s3-full`.

### E6 DNS Firewall mutation

Baseline direct UDP queries to `10.30.0.2` resolved the allowlisted ECR name.
Fresh `s1-e891f01bcf0d4ca9a3b46fa4c33898f2.example.com` returned NXDOMAIN;
the 21:40:01 query log explicitly records `firewall_rule_action: BLOCK`.
After detaching the association, a newly generated name,
`s1-bef607bcec3d406785b9ab6d3b5e1781.example.com`, returned NOERROR with zero
A answers and an authority SOA. The 21:53:26 query log records NOERROR with
no firewall-action fields. The allowlisted ECR name still resolved. This
supports the stated resolver-filtering boundary and its bypass under removal;
there is no attacker-controlled authoritative-server observation, so no claim
of authoritative-side non-receipt is made.

The detached test passed **1 test in 58.54 s**. The runner restored the same
rule group at priority 101 with mutation protection disabled and S1 tags,
waited for COMPLETE, removed only the obsolete S1 association state address,
and imported replacement `rslvr-frgassoc-fb7b46580a34466d` into that address.
No other state was modified. Evidence: `.local/s1/retry-E6-baseline-query.json`,
`retry-E6-detached-query.json`, `retry-E6-result.json`, `retry-E6-detached.log`,
`E6-original-association.json`, `E6-restored-association.json`. Exact driver:
`PYTHONPATH=. uv run python .local/s1/run_dns_mutation.py`.

### Restoration verification and decision

The compact post-mutation baseline passed **1 test in 55.94 s**: canary writes
returned 403 again, the fresh non-allowlisted direct DNS query returned
NXDOMAIN, and the allowlisted ECR name resolved. The functional check and
original probe configuration were exported before teardown. The final
`S1_STS_GET_CALLER_IDENTITY_ONLY=true make s1-plan` reported **no changes**;
fmt and validate passed. `retry-restored-configuration.json` records the
restored Pod policy, DNS association COMPLETE/priority 101, fail-open disabled,
and ALLOW/BLOCK rules. E9's repeated inventory retained the namespace-only
harness policy, exact PrivateLink acceptance gate, and unchanged STS restriction.
No observed startup requirement justified widening STS. This is not evidence
that the endpoint was unused.

Evidence: `.local/s1/retry-restored-baseline.log`,
`retry-restored-baseline.json`, `retry-restored-query.json`,
`retry-final-plan.log`, `retry-restored-configuration.json`,
`retry-e9-baseline.json`, and `retry-e9.json` (post-mutation).

**Decision for review:** E1 did not meet its mandatory condition because LC-8
failed. E2 is observational, with two explicitly labeled event-time proxies.
E3 and E10 passed their checks. E4–E8 produced the bounded network/identity,
mutation, and admission evidence described above. E9 verified the accessible
configuration checks but cannot prove OIDC-provider absence under the SCP.
S1 therefore does not receive an unconditional adoption recommendation from
this run. No lifecycle assertion was loosened to obtain a pass, and no claim
extends to P2, P3, or control-plane compromise.


The restored fresh query `s1-1ee9c8e7f16a49c4b113f53c5fc83f12.example.com`
was explicitly logged BLOCK after reattachment (`retry-restored-query.json`).
The final read-only plan and repeated inventory found no mutation left active.
Provisioning commands and outputs are retained in `.local/s1/retry-plan.log`,
`retry-plan.json`, `retry-up.log`, and `dns-priority-up.log`. Image publication
used the dedicated S1 ECR repository; `image.json` records the digest and
architecture. The Mock Workday repository and all foundation/bootstrap states
were outside this run's mutation scope.

Result timestamps in the final archive (UTC): E3/E7 inspect 21:35:27;
E10 21:37:27; E4 21:40:02; E8 admission/inspect/probes
21:40:33 / 21:41:14 / 21:42:38; restored baseline 21:57:56.
`.local/s1/retry-evidence-sha256.json` hashes 13 retained evidence artifacts.
`retry-canary-policy.json` independently verifies that the SourceVpce bucket
condition remained in place for the S3 mutation.

### Teardown

`S1_RUN_APPROVED=1 make s1-down` started at **22:00:00 UTC**. Evidence export
completed before deletion. The Fargate profile was deleted before the cluster;
Terraform then destroyed **69 remaining resources**, including the execution
VPC and dedicated S1 ECR repository. The destroy completed by 22:11:26.

The EKS-created cluster security group survived cluster deletion again.
At 22:08:31.989184, the operator verified group `sg-07e005ac3dc0cbcf4` belonged
to `lab-exec-s1` in `vpc-0a7cf985b8f044cf6` and had **zero attached ENIs**.
After Terraform removed its cross-group rules, explicit deletion succeeded.
This manual, ownership-checked cleanup remains a teardown finding; the group
was not silently ignored. Raw evidence: `.local/s1/retry-orphan-cluster-group.json`,
`retry-orphan-delete.log`, `delete_orphan_cluster_group.py`, and
`retry-final-down.log`.

The Docker-only `s1-builder` stopped at 22:01:00; local Kubernetes was never
started for this checkpoint (`retry-builder-stop.log`). Retry resource lifetime
was approximately **1 hour 48 minutes**, from first creation around 20:23:40 to
completed destruction around 22:11, below the three-hour target. Actual billing
was not measured; the spec's hourly figures remain estimates.

`s1-down` exited **0** at 22:14 with inventory **`[]`**. The S1 state list was
empty (`retry-final-state-list.txt`). The inventory explicitly reported the
reviewer-accepted SCP/OIDC exception; this is not an OIDC absence proof.
Prior partial-apply cleanup had likewise removed the old orphan cluster group
`sg-097473168467cf37f` and VPC `vpc-09ffaf540e50e3e9a` before the retry;
those resources were not carried into this run.

The standalone **`make s1-leftovers` exited 0 with `[]` at 22:17:46 UTC**,
again retaining only the explicit OIDC visibility exception. It checked the
recorded cluster security group and ENIs as well as tagged resources. Evidence:
`.local/s1/retry-final-leftovers.log`, `leftovers.json`,
`retired-tagged-resources.json`, and `oidc-evidence.json`. No S1 resource
leftovers were reported. The state bucket and foundation/bootstrap resources
were not destroyed. Checkpoint 4 stops here for review; no push was performed.
