# S1: EKS Fargate as the execution substrate, in its own trust domain (experiment)

**Status:** draft for review. **No AWS resources are created until the user explicitly approves the spend.**

**Purpose:** a decision gate. S1 answers whether EKS on Fargate can host execution Pods under [threat-model revision 1](threat-model.md), and what it costs in latency, money, and residual exposure. It is a narrow experiment, not a platform build: everything is created for a session and destroyed afterwards.

**Trust-domain requirement (2026-10-08):** compromising the execution domain must give no useful authority over the trusted platform. S1 therefore separates three trust domains, each with its own infrastructure boundary, connected only by narrow authenticated paths:

| Domain | Contents | Infrastructure in S1 |
|---|---|---|
| Workday core | Mock Workday | Not used in S1. Later: its existing ECS deployment, or the Hetzner cluster, reached only by the gateway as an external API |
| Trusted runtime | Controller, gateway (the `/complete` endpoint in S1), Postgres | The existing `lab-dev` VPC: one EC2 host |
| Execution | Customer agent Pods | A **new VPC with no internet gateway** and a dedicated EKS cluster, one Fargate VM per execution |

- **The only path from execution to trusted is AWS PrivateLink:** an interface endpoint in the execution VPC, connected to an endpoint service in front of the gateway. There is no VPC peering and no route between the VPCs.
- **Separate AWS accounts** per domain are the production-grade form and a later stage. S1 uses one account with two VPCs.
- **A separate cluster alone is not the goal:** no IAM role, database, or network path may span the domains beyond what is listed here.

**What S1 can and cannot claim (review 2026-10-08).** Both domains share one AWS account and its IAM control plane, so S1 cannot show that compromising the execution *AWS* domain leaves the trusted domain unaffected. That needs separate accounts (later).

| | In S1 |
|---|---|
| Trusted | The AWS account and IAM control plane; the EKS and Fargate services; Fargate's VM boundary; **the execution cluster's Kubernetes control plane** |
| Attacker | One execution: its container (P1) and its execution credential |
| Claim tested | **A compromised execution does not acquire another execution's credentials, trusted-runtime credentials, or network access beyond its explicitly permitted interfaces** (E4–E10) |
| Not claimed | Resistance to compromise of the execution cluster's control plane. Anyone who can read Pods in `agent-exec` reads every execution's completion credential, which R2 put in the Pod spec as temporary scaffolding (r2-spec D5); the gateway's projected-token authentication removes that. Separately, with access mode API and no OIDC provider, cluster objects cannot grant IAM roles (E9). |
| Later | AWS-account isolation between the domains |

**Why Fargate:**

- Each Fargate Pod runs in its own VM-level boundary, with no kernel shared with other Pods (AWS-documented).
- That addresses P2 (guest-kernel compromise) between executions without operating KVM hosts.
- The cost is control: the boundary and its implementation belong to AWS (P3 and the shared-responsibility model). S1 measures what that trade buys and what it leaves exposed.

**Reuses:**

- R2's `KubernetesBackend`, fake agent, and `test-k8s` harness, pointed at EKS instead of Colima;
- the dev network foundation (`infra/platform/envs/dev/foundation`) as the trusted VPC.

**Not in S1:**

- Mock Workday;
- the gateway (S1 uses the R1/R2 completion endpoint, on a completion-only listener, as the only permitted destination; S1-D11);
- the sidecar;
- Kata or Firecracker comparisons;
- simulating P2 (impossible on Fargate: we cannot control its guest kernel);
- high availability;
- autoscaling;
- prod.

## 0. Verified facts (2026-10-08)

| Fact | Source |
|---|---|
| Project 729608197929, Free plan, $200 credits until 2027-04-07 | `aws freetier get-account-plan-state` |
| Published Free-plan guidance permits the listed service families, but is not evidence that every IAM action is allowed. Checkpoint 3 observed an explicit SCP deny for `iam:ListOpenIDConnectProviders` (`p-5fs30qru`); E9's OIDC-absence check is currently unverified. | Published SCP guidance; live read-only IAM request, 2026-10-08 |
| VPC `lab-dev` `vpc-08ffe624b1fdb4d4c` `10.20.0.0/16`:<br>• public `10.20.0.0/24` (2a), `10.20.1.0/24` (2b);<br>• private `10.20.10.0/24` (2a), `10.20.11.0/24` (2b).<br>Published in SSM under `/lab/dev/network/*`. | `aws ec2 describe-subnets`, SSM |
| No NAT gateway and no VPC endpoints exist; private subnets have no internet route | `describe-nat-gateways`, `describe-vpc-endpoints`, foundation Terraform |
| EKS versions in standard support: 1.34, 1.35, 1.36 (default), 1.37 | `aws eks describe-cluster-versions` |
| ECR holds only `mock-workday` | `aws ecr describe-repositories` |

**AWS facts from documentation** (cited in the ChatGPT review, 2026-10-07):

- each Fargate Pod gets its own VM and kernel;
- VPC CNI NetworkPolicy does not apply to Fargate Pods;
- security groups for Pods support Fargate;
- EKS Pod Identity does not support Fargate;
- the Fargate Pod execution role serves infrastructure and is not available to application containers;
- security groups cannot block queries to the VPC resolver (AmazonProvidedDNS), while Route 53 Resolver DNS Firewall can filter them;
- Fargate needs connectivity to the Kubernetes control plane.

**Verified from AWS documentation (2026-10-08):**

| Fact | Source |
|---|---|
| Fargate Pods run only in private subnets (no direct route to an internet gateway); the VPC must have DNS resolution and DNS hostnames enabled | [EKS: Fargate considerations](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html) |
| EKS Fargate cannot run workloads that require Arm processors, so `linux/amd64` is required, not just conservative | same page, comparison table |
| IMDS is not available to Fargate Pods | same page |
| Privileged containers, `hostPort`, and `hostNetwork` are not supported on Fargate | same page |
| Endpoint-service permissions name AWS principals (an account root, a role, a user, or `*`), not VPCs; connection acceptance can be required | [PrivateLink: configure an endpoint service](https://docs.aws.amazon.com/vpc/latest/privatelink/configure-endpoint-service.html) |
| Traffic arriving through PrivateLink has the NLB's private IP as its source; client IP preservation does not apply (proxy protocol v2 can carry the consumer IP and endpoint ID) | [NLB target group attributes](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/edit-target-group-attributes.html) |
| A bucket policy granting `Principal: *` is non-public when conditioned on a fixed `aws:SourceVpce`, so Block Public Access can stay on | [S3 Block Public Access: the meaning of "public"](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html) |

**To verify during setup** (record the answer here; if one is false, stop and report):

1. Endpoints: `ecr.api`, `ecr.dkr`, and the S3 gateway (for private ECR pulls), and `sts`, which AWS's private-cluster guidance lists "to support Fargate" ([eksctl private clusters](https://docs.aws.amazon.com/eks/latest/eksctl/eks-private-cluster.html)). The same guidance lists `ec2` for the cloud-provider integration and does not exempt Fargate. S1 starts without `ec2`, because every endpoint is another egress path; if Pods fail to start or register, add it and record that it was needed. `logs` only if something sends to CloudWatch Logs (nothing does in S1).
2. The exact security-group rules: see §1. Which groups actually attached is checked per Pod (annotation → ENI ID → `describe-network-interfaces`, asserting the exact set).
3. A cluster with no CoreDNS still runs Pods that need no DNS.

Whether the kubelet shares the agent's network namespace is not a design dependency: E4 measures whether the agent can connect to the API server.

**Also verified (Astra fact check, 2026-10-08):**

| Fact | Source |
|---|---|
| Without a `SecurityGroupPolicy`, Fargate Pods get the cluster security group. With one, the listed groups are used, and they must allow the Pod to reach the control plane (AWS suggests including the cluster SG, or adding the minimum rules yourself) | [Security groups per Pod](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html), [SecurityGroupPolicy](https://docs.aws.amazon.com/eks/latest/userguide/sg-pods-example-deployment.html) |
| A `SecurityGroupPolicy` affects newly scheduled Pods only; several selected groups combine their allow rules | [SecurityGroupPolicy](https://docs.aws.amazon.com/eks/latest/userguide/sg-pods-example-deployment.html) |
| Fargate reserves 256 MB per Pod for `kubelet`, `kube-proxy`, and `containerd`, so they run with the Pod (that they share its VM is a strong inference, not stated) | [Fargate Pod configuration](https://docs.aws.amazon.com/eks/latest/userguide/fargate-pod-configuration.html) |
| The Pod execution role lets Fargate's infrastructure pull images, route logs, and register the kubelet as a node; application containers cannot use it. The managed policy holds only four ECR actions | [Pod execution role](https://docs.aws.amazon.com/eks/latest/userguide/pod-execution-role.html) |
| Its trust policy should restrict `aws:SourceArn` to the Fargate-profile ARN pattern (`fargateprofile/<cluster>/*`), not the cluster ARN | same page |
| From EKS 1.32, anonymous requests are allowed only to `/healthz`, `/livez`, `/readyz`; anything else gets 401 | [EKS 1.32 release notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html) |
| One subnet per Fargate profile is recommended; the cluster itself needs subnets in two AZs | [Fargate profiles](https://docs.aws.amazon.com/eks/latest/userguide/fargate-profile.html), [network requirements](https://docs.aws.amazon.com/eks/latest/userguide/network-reqs.html) |

## 1. Mechanisms S1 depends on

- **Two egress layers for internet and cross-VPC destinations:**
  - **Routing:** execution subnets have no route to the internet at all.
  - **Security groups:** per Pod, deny by default, allowing only the listed destinations.

  For those destinations, removing one must leave the other blocking, and the probes show which layer blocked what. The layers are not independent everywhere: S3 and PrivateLink are intentionally routed, and there the controls are endpoint policies and application authentication.
- **VPC endpoints are egress paths too.**
  - An S3 gateway endpoint lets anything in the subnet reach *any* bucket, including an attacker's publicly writable bucket, with no credentials at all. The endpoint policy is therefore restricted to the ECR image-layer bucket for this Region.
  - The ECR interface endpoint policy is restricted to the experiment's repository.
  - **An unauthenticated probe proves nothing about an endpoint policy unless the target accepts anonymous requests.** An agent without AWS credentials is refused by a normal bucket anyway. E4 therefore uses a canary bucket whose policy allows anonymous `PutObject` only through this VPC's S3 endpoint (`aws:SourceVpce`, which keeps the bucket non-public). The restricted endpoint policy must be what refuses it, and E5 shows that a full-access endpoint policy lets the same request through. ECR has no anonymous access, so its endpoint policy is checked statically.
- **PrivateLink hides the caller's address.** The gateway sees the NLB's private IP as the source of every request, so network origin cannot identify an execution. Identity comes from the per-execution credential: in S1, R2's completion credential (r2-spec D5); with the gateway, the projected token.
- **Endpoint-service permissions are principals, not networks.** The endpoint service allows only the S1 Terraform role and requires acceptance. Which workloads can use it is decided by where the interface endpoint exists (only the execution VPC) and its security group (only the execution Pod security group).
- **DNS is an egress channel that security groups cannot close.** The Pod can always send queries to the VPC resolver. DNS Firewall, associated with the execution VPC only, applies an allowlist. Query logging records queries the resolver processes and the firewall's action, but not queries answered from its cache, so every probe uses a fresh name.
- **Execution Pod security group rules** (the cluster SG is not attached): egress TCP 443 to the cluster SG, with the cluster SG admitting TCP 443 from the Pod SG; the cluster SG may reach the Pod on TCP 10250 (kubelet: logs, exec). No port 53 rules: there is no CoreDNS. AWS publishes no validated "no CoreDNS" minimum, so a Pod that fails to start with these rules is a finding, not a reason to attach the whole cluster SG.
- **Four distinct authority roles (the fourth is test scaffolding):**
  - the **controller role** (trusted host): its EKS access entry maps it to the namespaced Role only; no IAM, network, or EKS access-entry administration permissions. For S1 tests only, it may `sts:AssumeRole` the `s1-harness` role;
  - the **Fargate Pod execution role**: ECR pull of one repository; trust limited to this cluster's Fargate profile (§0);
  - the **operator/Terraform role**: everything else, never present on the trusted host or in the execution domain;
  - **`s1-harness` (test scaffolding, absent from production):** its trust policy allows only the trusted-host/controller instance role. Its EKS access entry has **`AmazonEKSAdminPolicy` scoped to exactly `agent-exec` and `agent-exec-psa-control`**, never cluster scope. LC-7 deletes/restores a RoleBinding; `AmazonEKSEditPolicy` lacks the necessary RBAC permissions. ISO-5 dry-runs control Pods in the operator-pre-created `agent-exec-psa-control` namespace, which has **no Pod Security Admission labels** and is not created/deleted by the EKS harness. The Colima profile may retain its temporary namespace. The operator also pre-creates the control service account. Runtime/controller calls always retain the controller identity; only admin test calls use `s1-harness`.

  **Rationale (reviewer-approved):** S1's attacker is an execution, with no path to the trusted host. These admin actions are test-only and namespace-scoped on that host. This exception is not a production authority model. EKS's separate service role for managing the control plane and its required EKS/Fargate service-linked roles are infrastructure, not these four caller roles. Terraform owns newly required service roles so they are removed during S1 teardown; pre-existing shared service roles are not adopted or destroyed.
- **IAM to Kubernetes identity:** EKS access entries map an IAM role to Kubernetes groups. The controller's role maps to group `agent-runtime-controllers`, bound to the same namespaced Role as R2 (Pods in `agent-exec` only). It authenticates with `aws eks get-token`: a presigned STS request. The token file is refreshed by a small loop, because R2's backend re-reads it on every request.
- **The API endpoint is reachable from the VPC.** With private endpoint access, the API server has network interfaces in the VPC. Whatever Fargate requires for control-plane connectivity, the agent's real reachability is **measured**. If it can reach the API, it is authenticated (and rejected) like any caller, and that exposure is recorded, not assumed away.

## 2. Topology

```text
TRUSTED RUNTIME DOMAIN: VPC lab-dev (10.20.0.0/16), existing
│  trusted host: EC2 t3.small, amd64, public subnet (2a), Elastic IP; no inbound from the internet; operated through SSM
│    Postgres, runtime app (controller and API on :8000, local only), completion-only listener on :8001 (S1-D11), R2 test harness
│    IAM role → EKS access entry (execution cluster) → group agent-runtime-controllers
│    reaches the execution cluster's API through its public endpoint (restricted to this host's Elastic IP and the operator's /32)
│  internal NLB :8001 → trusted host :8001 (the host's security group admits the NLB on :8001 only)
│  VPC endpoint service (PrivateLink) on that NLB: allowed principal = the S1 Terraform role; acceptance required
│
│                         ▲ PrivateLink only: no peering, no routes between the VPCs
│
EXECUTION DOMAIN: new VPC lab-exec (10.30.0.0/16), created and destroyed by S1
   private subnets only (2a, 2b); NO internet gateway, NO NAT
   EKS 1.36 cluster "lab-exec-s1": cluster subnets 2a + 2b (EKS requires two AZs); no node groups, no CoreDNS; access mode API
     endpoint: private (for Fargate) + public restricted to the trusted host's Elastic IP and the operator's /32
     no IAM OIDC provider for the cluster, so no IAM role can be assumed through a service-account token (no IRSA)
   Fargate profile "agent-exec" (namespace agent-exec, subnet 2a only) → execution Pods
     SG (SecurityGroupPolicy): egress only to the gateway endpoint :8001, the AWS endpoint SG :443,
     the S3 prefix list :443, and the minimum control-plane rule Fargate requires (not the whole cluster SG)
   all endpoints in 2a only (not an availability experiment):
     interface endpoint → gateway endpoint service (its IP is injected as RUNTIME_URL)
     interface endpoints ecr.api, ecr.dkr, sts; S3 gateway endpoint (restricted policies)
   Route 53 Resolver DNS Firewall (standard, not Advanced) on this VPC only: allowlist the AWS names Fargate needs; block everything else; query logging
   canary S3 bucket: anonymous PutObject allowed only via this VPC's S3 endpoint (for E4/E5)
```

## 3. Decisions (proposed)

| # | Decision | Alternative | Why |
|---|---|---|---|
| S1-D1 | **Trusted services run on one EC2 host in the trusted VPC (`lab-dev`),** in a public subnet with no inbound internet access, operated through SSM | ECS task; a second EKS cluster | The trust-domain rule with the fewest moving parts. A second cluster adds cost and nothing S1 measures. A public subnet avoids a NAT gateway (about $0.045/hour). Once the gateway exists, its load grows with the number of executions; the trusted side then needs replicas and autoscaling, likely its own EKS cluster. |
| S1-D10 | **PrivateLink is the only execution-to-trusted path:** an NLB plus endpoint service in front of the gateway; one interface endpoint in the execution VPC | VPC peering or a transit gateway | Peering creates routes between whole networks. PrivateLink exposes exactly one service, and only in one direction. |
| S1-D2 | **No compute in the cluster except Fargate execution Pods:** no node groups, no CoreDNS | A system node group | No authority-bearing runtime service runs in the execution cluster. (Fargate's own infrastructure, the kubelet using the Pod execution role, still runs with each Pod; that is a dependency on AWS's isolation, see §6.) Execution Pods need no DNS: the callback address is an IP injected by the controller. |
| S1-D3 | **The execution VPC has no internet gateway at all,** with ECR reached through endpoints | NAT gateway; an internet gateway with restrictive routes | "No route out" becomes a property of the network, not a route-table setting. It is a second egress layer independent of security groups, and cheaper. |
| S1-D4 | **Endpoint policies with concrete actions:** ECR: `ecr:GetAuthorizationToken` on `*` (it has no resource), and `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability` on the one repository ARN, for the Pod execution role only. S3: `s3:GetObject` only, on `arn:aws:s3:::prod-us-east-2-starport-layer-bucket/*` | Default (full-access) endpoint policies | Default policies turn endpoints into exfiltration paths. An endpoint policy limits what passes through the endpoint; it does not replace IAM or bucket authorization. |
| S1-D5 | **DNS Firewall on the execution VPC only:** an explicit inventory of the AWS hostnames Fargate needs (not `*.amazonaws.com`), then a final block-all rule; fail-open disabled; redirection-chain inspection left on | No DNS control | Security groups cannot close DNS tunneling (§1). A narrow allowlist of names under AWS's authority keeps attacker-chosen names out; a broad wildcard would not. The trusted VPC is unaffected. |
| S1-D6 | **API endpoint private (for Fargate) plus public, restricted to the trusted host's Elastic IP and the operator's /32** | Private only, reached through a custom PrivateLink service: an NLB targeting the control-plane ENIs, with automation tracking ENI changes and a private DNS override to keep TLS hostnames valid ([AWS blog](https://aws.amazon.com/blogs/containers/enable-private-access-to-the-amazon-eks-kubernetes-api-with-aws-privatelink/)). Peering is ruled out by S1-D10. | The public endpoint is IAM-authenticated and CIDR-restricted, and needs no extra infrastructure. The custom PrivateLink path is recorded as the private alternative (and is how a trusted cluster in private subnets could reach this API later); it is not needed for S1. Public-access CIDRs do not govern the private endpoint, whose security groups are a separate control. The operator IP must be the real egress IP (the Mac's VPN or proxy changes it). The `com.amazonaws.<region>.eks` interface endpoint serves the EKS management API, not Kubernetes API calls. |
| S1-D7 | **Terraform** in `infra/experiments/fargate/`, its own state key in the dev state bucket, reading the network from SSM, with tags `lab=agent-runtime`, `experiment=s1` | Console or CLI | IaC is a job requirement, and it makes teardown complete. |
| S1-D8 | **amd64 everywhere;** the fake agent image is pushed to ECR repo `agent-runtime/fake-agent` and referenced by digest | Multi-arch | Avoids an unverified arm64 dependency (§0, item 3). |
| S1-D11 | **A completion-only listener:** the runtime serves a second, minimal app on `:8001` with only `POST /api/v1/executions/{id}/complete` (same handler, same per-execution authentication, size limit, and replay rules). The NLB forwards only to `:8001`; the full API on `:8000` is not reachable from the execution domain. | NLB to `:8000` | An NLB forwards connections; it has no notion of HTTP paths. Pointing it at `:8000` would expose execution creation, cancellation, and every other route to agents. Plain HTTP stays in S1 (the credential is temporary scaffolding, r2-spec D5); verified TLS arrives with the gateway. |
| S1-D9 | **Reuse R2's harness:** `make test-k8s` gains an `eks` target profile. The context guard requires the expected cluster ARN instead of localhost. The harness runs in-process on the trusted EC2 host; admin kubectl assumes `s1-harness`, while runtime/controller requests keep the controller role. The operator creates namespaces, RBAC, quota, and SecurityGroupPolicy during setup and never transfers its credentials to the host. | A new harness | Directly tests whether R2's API-level evidence carries over to EKS Fargate. Differences are findings. |

## 4. Experiments

Each experiment has a pass condition, evidence, and a control showing it could fail. Results go in [experiments.md](experiments.md) with timestamps, versions, and the commands used.

| ID | Question | Procedure | Pass and evidence |
|---|---|---|---|
| E1 | Does the R2 lifecycle work on Fargate? | A **named subset** of the R2 `test-k8s` suite, with configuration changes only where Fargate requires them (the image comes from ECR by digest, so `imagePullPolicy: Never` changes; Fargate sizes Pods from requests plus 256 MB and rounds up):<br>• **mandatory:** HAPPY (including the input round trip), LC-5/LC-3 adoption after a lost response, LC-3 restart and late-visible cases, LC-6, LC-7, LC-8, both LC-9 tests, LC-11;<br>• **run and record differences:** ISO-1/ISO-5/ID-8 (credentials and template), ISO-1 RBAC, ISO-5 admission and read-only root;<br>• **substrate comparison, not pass/fail:** ISO-7 capacity and resource behavior. | Every mandatory test passes; lifecycle invariants are not weakened. Other differences are explained. Each Pod's `CapacityProvisioned` annotation is recorded. |
| E2 | Cold-start cost | 5 sequential and 3 concurrent executions | Record create → Pod phase `Running` (container started, per Kubernetes, not the runtime's RUNNING state) → first callback (p50 and max). Observations, not a benchmark; no pass threshold. |
| E3 | Separate kernels | Two concurrent `inspect` executions report `/proc/sys/kernel/random/boot_id` and `uname -r` | The boot IDs differ (evidence of separate kernels). This is not proof of the isolation's strength. |
| E4 | Egress, by layer | From an execution Pod, probe **known-live targets only** (a timeout to an address with nothing behind it proves nothing):<br>• the completion listener through the PrivateLink endpoint, :8001 (positive control);<br>• the trusted host directly, bypassing PrivateLink: its private IP :8001 (known listener) and a closed port, and its Elastic IP :8001;<br>• a second execution Pod running a listener;<br>• the Kubernetes API private endpoint :443, anonymous and with the projected token;<br>• the ECR and STS endpoint IPs :443, without credentials;<br>• `169.254.169.254` and `169.254.170.2`;<br>• a public IP and a public name;<br>• anonymous `PutObject` to the canary bucket (§1);<br>• through the PrivateLink endpoint, every route other than completion (create, list, get, cancel, health) and port `:8000`.<br>Each probe records one of: no route, timed out, connection refused, TCP accepted, HTTP 401/403, or request processed. | Only the positive control and the documented required paths connect. The API server, if reachable, answers 401 to an anonymous request outside the health paths and to the projected token (wrong audience); a 200 from `/healthz` is expected and is not a failure. The reachability is recorded as exposure. The canary put is refused by the endpoint policy. The internet is unreachable. Through PrivateLink, only the completion route exists (other routes 404; `:8000` does not connect). |
| E5 | Test the tests | **Security groups, by mutation:** remove the `SecurityGroupPolicy` (the Pod falls back to the cluster security group), **recreate the probe Pods** (policies apply only at scheduling), and re-run E4. **S3 endpoint policy, by mutation:** switch it to full access and repeat the canary put, which must then succeed. **Routing, structurally:** a NAT positive control is disproportionate (an internet gateway route alone gives private-only Pods no egress), so `describe-*` output shows no internet gateway, no NAT, and no default route, and E4's internet probes fail. | The probes that change identify which layer enforced what. A mutation that changes no probe means that control is unproven. |
| E6 | DNS filtering | Query the VPC resolver **directly** (VPC base +2), not through `/etc/resolv.conf`, which points at a CoreDNS that doesn't exist. Query a fresh random name (new for every attempt, so the resolver cache cannot hide it) under a real public domain outside the allowlist, and an allowlisted AWS name. Test the test: detach the DNS Firewall association and repeat. | With the firewall: the query log shows the random name with action `BLOCK`, and the AWS name resolves. Without it: the resolver processes the random name and forwards it. **Claim scope:** DNS Firewall blocks non-allowlisted public names at the VPC resolver. Proving that an attacker's authoritative server sees nothing needs a domain we control and observe; that is optional and not in S1. |
| E7 | Identity and credentials (ID-8, ISO-1) | `inspect`: environment variable names (`AWS_*`, `AWS_CONTAINER_CREDENTIALS_*`, `AWS_WEB_IDENTITY_TOKEN_FILE`), mounts and projected volumes, `169.254.169.254` and `169.254.170.2`, and the projected-token claims | No unintended credentials are injected, and none are obtained through the enumerated application-accessible paths (paired with the admitted Pod spec, service-account configuration, role policies, and effective network configuration); a reachable endpoint that yields no credentials is recorded as such, not as a leak. The token claims match R2. **Positive control:** the private image starts, so Fargate's infrastructure used the Pod execution role to pull it, while the application container could not obtain that role. |
| E8 | P1 assume-breach (ISO-9, P1 only) | An execution variant with the maximum privileges Fargate admits for this namespace (records what is rejected), enumerating credentials, network, and APIs | Nothing beyond its own execution's credential and the permitted paths. Records exactly which privileges the variant gained over the ordinary template (under the same admission policy, possibly almost none). Root in the container is not guest-kernel control: reported as **P1 evidence only.** |
| E9 | Static IAM and configuration review | From the Terraform plan and `describe`/`get` calls after apply | No IAM OIDC provider exists for the cluster's issuer (no IRSA). The Pod execution role trusts only `eks-fargate-pods.amazonaws.com` with `ArnLike aws:SourceArn = arn:aws:eks:us-east-2:<account>:fargateprofile/lab-exec-s1/agent-exec/*`, and its permissions are ECR pull on the one repository. No trusted-domain role (controller, trusted host) is assumable by any execution-side principal. Endpoint policies and endpoint-service permissions match §1. The execution Pod ENI has exactly the expected security groups. The four authority roles are distinct and match §1. `s1-harness` trusts only the controller instance role; its only EKS policy is `AmazonEKSAdminPolicy` with namespace scope exactly `[agent-exec, agent-exec-psa-control]`, never cluster scope. Check the control namespace has no PSA labels and that the runtime identity has no harness access policy. |
| E10 | Authority at the destination | Live, through PrivateLink: execution A's credential completes A (positive control); A's credential against execution B, while B is RUNNING, is rejected and B is unchanged | A network path alone is not the boundary: the destination must also enforce per-execution authority. |

## 5. Cost and teardown

**Running cost** (on-demand list prices, us-east-2; verify at approval):

| Item | Estimate |
|---|---|
| EKS control plane | $0.10/h |
| Four interface endpoints in one AZ (ECR ×2, STS, gateway) | ~$0.04/h |
| Internal NLB for the endpoint service | ~$0.023/h plus capacity units |
| t3.small host plus public IPv4 | ~$0.026/h |
| Fargate Pods, 0.25 vCPU and 0.5 GB | ~$0.012/h each while running |
| DNS Firewall (standard, $0.60 per million queries) and query logging | cents. Never enable DNS Firewall Advanced (about $0.16/h). |
| **Total** | **about $0.20–0.25/h while up** |

A three-hour session is **estimated** under $1; this is not a spending ceiling. Count the whole resource lifetime, including provisioning and teardown, plus EBS, ECR storage, logs, the canary bucket, and Fargate's billed size (`CapacityProvisioned`, which the 256 MB overhead can push up a size). An Elastic IP keeps billing after its instance is gone.

**Teardown:**

- `make s1-up` / `make s1-down`. `s1-down` runs in order, keeping network access and credentials until the steps that need them finish:
  1. stop new launches;
  2. export the evidence (results, query logs, Postgres rows) off the trusted host;
  3. delete execution Pods and confirm they are gone (the runtime created them; they are not in Terraform state);
  4. delete the Fargate profile, then the cluster;
  5. destroy the remaining S1 infrastructure;
  6. run `s1-leftovers`.
- `make s1-leftovers` lists anything tagged `experiment=s1`, and in us-east-2 explicitly checks:
  - EKS clusters, Fargate profiles, access entries;
  - VPCs, subnets, route tables, security groups, ENIs;
  - interface and gateway endpoints, endpoint services and their connections;
  - NLBs, target groups, listeners;
  - Resolver firewall rule groups, domain lists, VPC associations, query-log configurations, CloudWatch log groups;
  - EC2 instances, EBS volumes and snapshots, Elastic IPs, instance profiles, S1 IAM roles, the IAM OIDC provider (must not exist);
  - the canary bucket, and the ECR repository `agent-runtime/fake-agent` with its images (easy to miss).
- Terraform owns the query-log group, so `destroy` deletes it; 1-day retention is only a fallback.
- Fargate profile deletion is slow and serialized (a profile can't be created or deleted while another is deleting); `s1-down` waits for it.
- Leftovers are found by tag **and** by recorded resource IDs, since service-created resources may not inherit tags. Intentionally exported evidence is listed separately from unexpected leftovers.
- Nothing is left running between sessions. A Budgets alert already exists ($1–$50).

**Safety:**

- every AWS command uses profile `agent-runtime` and Region `us-east-2`;
- Terraform refuses to apply without the expected account ID;
- `destroy` runs only on the S1 state.

## 6. Decision criteria and limits

**Adopt Fargate as the execution substrate if:**

- E1 passes, or its differences are acceptable;
- E4 through E7, E9, and E10 pass;
- every remaining exposure (for example API-server reachability) is understood and recorded.

E2's numbers are judged by the user against the product need.

**What S1 cannot show:**

- whether Fargate's boundary survives P2 or P3: we trust AWS there, and cannot test it;
- **residual exposure under P2 (recorded, not tested):** the kubelet and container runtime run with the Pod, very likely inside its VM. An attacker controlling that guest kernel should be assumed to hold that Fargate node's Kubernetes identity and whatever the Pod execution role grants there. Mitigations S1 already relies on: the execution role allows only ECR pull of one repository; `agent-exec` holds no Secrets or credentials (R2); a node identity is limited to objects bound to its own node (Kubernetes Node authorizer; confirm that EKS enables it);
- production-grade multi-AZ operation;
- how the gateway and sidecar behave (R3).

**If Fargate fails a requirement,** the comparison candidate is Kata or Firecracker on nested-virtualization EC2 (`m7i`/`c7i` families in us-east-2, verified), where P2 can be simulated because we own the guest kernel.

## 7. Resolved in review (2026-10-08)

- The S1-D6 public endpoint is kept; custom PrivateLink to the Kubernetes API is the recorded private alternative.
- E1 runs a named subset (§4).
- Query-log retention is 1 day, as a fallback only.
- **STS endpoint:** AWS's eksctl private-cluster guidance lists STS "to support Fargate"; the EKS user guide lists it for IRSA. S1 keeps it (documented baseline) and records whether Fargate used it.

- **Local validation decision (2026-10-08):** the reviewer accepted the LC-8 comparison as environmental after the pre-checkpoint baseline also failed and `024b024` passed, with slow kubelet housekeeping and API timeouts under host load. Do not restart local Kubernetes for checkpoint 2. Run the non-Kubernetes unit suite; LC-8 and the rest of the Kubernetes suite run on EKS in E1, where failures are findings. This deferral does not weaken E1's mandatory pass conditions.
