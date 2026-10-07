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

**Why Fargate:**

- Each Fargate Pod runs in its own VM-level boundary, with no kernel shared with other Pods (AWS-documented).
- That addresses P2 (guest-kernel compromise) between executions without operating KVM hosts.
- The cost is control: the boundary and its implementation belong to AWS (P3 and the shared-responsibility model). S1 measures what that trade buys and what it leaves exposed.

**Reuses:**

- R2's `KubernetesBackend`, fake agent, and `test-k8s` harness, pointed at EKS instead of Colima;
- the dev network foundation (`infra/platform/envs/dev/foundation`) as the trusted VPC.

**Not in S1:**

- Mock Workday;
- the gateway (S1 uses the R1/R2 `/complete` endpoint as the only permitted destination);
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
| The Free-plan SCP allows `eks:*`, `ecs:*`, `ecr:*`, `ec2:*`, `route53resolver:*`, and IAM; it denies Spot | Published SCP for Free Tier projects |
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

**To verify during setup** (record the answer in §0; if one is false, stop and report):

1. Fargate Pods must run in private subnets.
2. The VPC endpoints EKS Fargate needs in a subnet without internet access: `ecr.api`, `ecr.dkr`, an S3 gateway, and possibly `sts`.
3. Whether EKS Fargate supports arm64. S1 uses `linux/amd64` everywhere, to avoid depending on it.
4. How security groups attach to Fargate Pods (`SecurityGroupPolicy`), and their defaults.
5. Whether the agent container shares a network namespace with Fargate's kubelet, which decides what reaching the API server means.
6. That a cluster with no CoreDNS still runs Pods that need no DNS.

## 1. Mechanisms S1 depends on

- **Two independent egress layers:**
  - **Routing:** execution subnets have no route to the internet at all.
  - **Security groups:** per Pod, deny by default, allowing only the listed destinations.

  Removing one must leave the other blocking, and the probes show which layer blocked what.
- **VPC endpoints are egress paths too.**
  - An S3 gateway endpoint lets anything in the subnet reach *any* bucket, including an attacker's publicly writable bucket, with no credentials at all. The endpoint policy is therefore restricted to the ECR image-layer bucket for this Region.
  - The ECR interface endpoint policy is restricted to the experiment's repository.
- **DNS is an egress channel that security groups cannot close.** The Pod can always send queries to the VPC resolver. DNS Firewall, associated with the VPC, applies an allowlist. Its rules are VPC-wide, so they also govern the trusted host. Query logging records each query and the firewall's action.
- **IAM to Kubernetes identity:** EKS access entries map an IAM role to Kubernetes groups. The controller's role maps to group `agent-runtime-controllers`, bound to the same namespaced Role as R2 (Pods in `agent-exec` only). It authenticates with `aws eks get-token`: a presigned STS request. The token file is refreshed by a small loop, because R2's backend re-reads it on every request.
- **The API endpoint is reachable from the VPC.** With private endpoint access, the API server has network interfaces in the VPC. Whatever Fargate requires for control-plane connectivity, the agent's real reachability is **measured**. If it can reach the API, it is authenticated (and rejected) like any caller, and that exposure is recorded, not assumed away.

## 2. Topology

```text
TRUSTED RUNTIME DOMAIN: VPC lab-dev (10.20.0.0/16), existing
│  trusted host: EC2 t3.small, amd64, public subnet (2a), Elastic IP; no inbound from the internet; operated through SSM
│    Postgres, runtime app (controller + /complete on :8000), R2 test harness
│    IAM role → EKS access entry (execution cluster) → group agent-runtime-controllers
│    reaches the execution cluster's API through its public endpoint (restricted to this host's Elastic IP and the operator's /32)
│  internal NLB :8000 → trusted host :8000
│  VPC endpoint service (PrivateLink) on that NLB, allowing only the execution account and VPC
│
│                         ▲ PrivateLink only: no peering, no routes between the VPCs
│
EXECUTION DOMAIN: new VPC lab-exec (10.30.0.0/16), created and destroyed by S1
   private subnets only (2a, 2b); NO internet gateway, NO NAT
   EKS 1.36 cluster "lab-exec-s1": no node groups, no CoreDNS; access mode API
     endpoint: private (for Fargate) + public restricted to the trusted host's Elastic IP and the operator's /32
   Fargate profile "agent-exec" (namespace agent-exec) → execution Pods
     SG (SecurityGroupPolicy): egress only to the gateway endpoint :8000, the ECR endpoint SG :443,
     the S3 prefix list :443, and the cluster SG :443 if Fargate requires it
   interface endpoint → gateway endpoint service (its IP is injected as RUNTIME_URL)
   interface endpoints ecr.api, ecr.dkr; S3 gateway endpoint (restricted policies)
   Route 53 Resolver DNS Firewall on this VPC only: allowlist the AWS names Fargate needs; block everything else; query logging
```

## 3. Decisions (proposed)

| # | Decision | Alternative | Why |
|---|---|---|---|
| S1-D1 | **Trusted services run on one EC2 host in the trusted VPC (`lab-dev`),** in a public subnet with no inbound internet access, operated through SSM | ECS task; a second EKS cluster | The trust-domain rule with the fewest moving parts. A second cluster adds cost and nothing S1 measures. A public subnet avoids a NAT gateway (about $0.045/hour). |
| S1-D10 | **PrivateLink is the only execution-to-trusted path:** an NLB plus endpoint service in front of the gateway; one interface endpoint in the execution VPC | VPC peering or a transit gateway | Peering creates routes between whole networks. PrivateLink exposes exactly one service, and only in one direction. |
| S1-D2 | **No compute in the cluster except Fargate execution Pods:** no node groups, no CoreDNS | A system node group | Nothing trusted runs in the execution cluster. Execution Pods need no DNS: the callback address is an IP injected by the controller. |
| S1-D3 | **The execution VPC has no internet gateway at all,** with ECR reached through endpoints | NAT gateway; an internet gateway with restrictive routes | "No route out" becomes a property of the network, not a route-table setting. It is a second egress layer independent of security groups, and cheaper. |
| S1-D4 | **Endpoint policies:** ECR limited to the experiment repo; S3 limited to the Region's ECR layer bucket | Default (full-access) endpoint policies | Default policies turn endpoints into exfiltration paths. |
| S1-D5 | **DNS Firewall on the execution VPC only:** allowlist the AWS service names Fargate needs; block everything else | No DNS control | Security groups cannot close DNS tunneling (§1). Allowlisted names are not attacker-controlled, so they cannot carry tunneled data. The trusted VPC is unaffected. |
| S1-D6 | **API endpoint private (for Fargate) plus public, restricted to the trusted host's Elastic IP and the operator's /32** | Private only, with the controller reaching it over peering | The controller lives in another VPC, and peering would break S1-D10. An IAM-authenticated public endpoint with a tight CIDR list keeps the networks unconnected. The operator IP must be the real egress IP (the Mac's VPN or proxy changes it). |
| S1-D7 | **Terraform** in `infra/experiments/fargate/`, its own state key in the dev state bucket, reading the network from SSM, with tags `lab=agent-runtime`, `experiment=s1` | Console or CLI | IaC is a job requirement, and it makes teardown complete. |
| S1-D8 | **amd64 everywhere;** the fake agent image is pushed to ECR repo `agent-runtime/fake-agent` and referenced by digest | Multi-arch | Avoids an unverified arm64 dependency (§0, item 3). |
| S1-D9 | **Reuse R2's harness:** `make test-k8s` gains an `eks` target profile. The context guard requires the expected cluster ARN instead of localhost. Admin actions use the operator's kubectl. | A new harness | Directly tests whether R2's API-level evidence carries over to EKS Fargate. Differences are findings. |

## 4. Experiments

Each experiment has a pass condition, evidence, and a control showing it could fail. Results go in [experiments.md](experiments.md) with timestamps, versions, and the commands used.

| ID | Question | Procedure | Pass and evidence |
|---|---|---|---|
| E1 | Does the R2 lifecycle work on Fargate? | Run the R2 `test-k8s` suite against EKS, unchanged except for configuration | Every test passes, or each difference is explained (for example cold-start timeouts or Fargate rejecting a field). HAPPY passes end to end with the callback over the VPC. |
| E2 | Cold-start cost | 5 sequential and 3 concurrent executions | Record create → `Running` → first callback (p50 and max). No pass threshold; the numbers inform the decision. |
| E3 | Separate kernels | Two concurrent `inspect` executions report `/proc/sys/kernel/random/boot_id` and `uname -r` | The boot IDs differ (evidence of separate kernels). This is not proof of the isolation's strength. |
| E4 | Egress, by layer | From an execution Pod, probe:<br>• the gateway through the PrivateLink endpoint (positive control);<br>• the trusted host's private and public IPs directly, on :8000 and other ports;<br>• anything in `10.20.0.0/16`;<br>• the internet by IP and by name;<br>• `169.254.169.254` and `169.254.170.2`;<br>• another execution Pod's IP;<br>• the Kubernetes API private endpoint;<br>• the ECR API without credentials;<br>• S3 `PutObject` to a canary bucket outside the endpoint policy.<br>Each probe records: connection refused, timed out, or completed, and the HTTP status. | Only the positive control and the documented required paths connect. The API server, if reachable, answers 401 (recorded as exposure). The S3 put is denied by the endpoint policy. The internet is unreachable. |
| E5 | Test the tests | Remove the `SecurityGroupPolicy` (with routing still blocking), then re-run E4. Separately, add a temporary route and repeat with the security group in place. | The probes that change identify which layer enforced what. If no probe changes, a control is unproven. |
| E6 | DNS exfiltration | Query `<random>.s1-canary.example` and an allowlisted AWS name. Test the test: detach the DNS Firewall association and repeat. | With the firewall, the canary query is logged with action `BLOCK`, and the AWS name resolves. Without it, the canary query reaches the resolver and is forwarded (logged `NXDOMAIN`). |
| E7 | Identity and credentials (ID-8, ISO-1) | `inspect`: environment variable names, mounts, IMDS and credential endpoints, the projected-token claims, and whether the Pod execution role is obtainable | No AWS credentials are obtainable by any route. The token claims match R2. The environment allowlist holds, and Fargate's own differences are recorded. |
| E8 | P1 assume-breach (ISO-9, P1 only) | An execution variant with the maximum privileges Fargate admits for this namespace (records what is rejected), enumerating credentials, network, and APIs | Nothing beyond its own execution's credential and the permitted paths. Reported as **P1 evidence only.** |

## 5. Cost and teardown

**Running cost** (on-demand list prices, us-east-2; verify at approval):

| Item | Estimate |
|---|---|
| EKS control plane | $0.10/h |
| Three interface endpoints in one AZ (ECR ×2, gateway) | ~$0.03/h |
| Internal NLB for the endpoint service | ~$0.023/h plus capacity units |
| t3.small host plus public IPv4 | ~$0.026/h |
| Fargate Pods, 0.25 vCPU and 0.5 GB | ~$0.012/h each while running |
| DNS Firewall and query logging | cents |
| **Total** | **about $0.20–0.25/h while up** |

A three-hour session costs under $1.

**Teardown:**

- `make s1-up` / `make s1-down`;
- `make s1-leftovers` lists anything tagged `experiment=s1`, plus EKS clusters, VPCs, ENIs, endpoints, endpoint services, load balancers, EIPs, log groups, and Resolver configurations in us-east-2.
- Nothing is left running between sessions. A Budgets alert already exists ($1–$50).

**Safety:**

- every AWS command uses profile `agent-runtime` and Region `us-east-2`;
- Terraform refuses to apply without the expected account ID;
- `destroy` runs only on the S1 state.

## 6. Decision criteria and limits

**Adopt Fargate as the execution substrate if:**

- E1 passes, or its differences are acceptable;
- E4 through E7 pass;
- every remaining exposure (for example API-server reachability) is understood and recorded.

E2's numbers are judged by the user against the product need.

**What S1 cannot show:**

- whether Fargate's boundary survives P2 or P3: we trust AWS there, and cannot test it;
- production-grade multi-AZ operation;
- how the gateway and sidecar behave (R3).

**If Fargate fails a requirement,** the comparison candidate is Kata or Firecracker on nested-virtualization EC2 (`m7i`/`c7i` families in us-east-2, verified), where P2 can be simulated because we own the guest kernel.

## 7. Open questions

1. Is the S1-D6 public endpoint (IAM-authenticated, CIDR-restricted) acceptable, given that the private-only alternative needs peering?
2. Should E1 run the whole R2 suite, which costs time, or a named subset?
3. CloudWatch log retention for query logs: 1 day is proposed.
