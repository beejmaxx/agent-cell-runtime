# Dev cost guard

Independent, alert-only guardrails for project `729608197929`. Regional resources
are in `us-east-2`; Cost Anomaly Detection uses the billing endpoint in
`us-east-1`. The only backend is
`s3://beejmaxx-lab-tfstate-dev/dev/cost-guard.tfstate`. This stack has no dependency
on bootstrap or foundation state and does not manage the existing budgets.

## How it works

- **Billing anomalies:** a dimensional `SERVICE` monitor watches spending by AWS
  service across the project. A daily email summary includes anomalies with at
  least `$1` total impact by default (`anomaly_threshold_usd`). This is a learned
  anomaly threshold, not a hard spending cap. AWS billing data can lag by 24 hours;
  a new service needs 10 days of history before anomaly detection can work.
  [AWS detection behavior](https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html).
- **Leftovers:** EventBridge Scheduler invokes a Python Lambda every hour with
  no flexible window. The Lambda scans all resources in the selected Region,
  regardless of tags. One SNS publish consolidates every resource older than
  `max_age_hours` (default `4`), with type, name/ID, `Name`, `lab`, `experiment`,
  and age. Missing tags appear as `-`. A clean scan sends nothing. Outstanding
  findings are repeated hourly until removed; there is no persisted deduplication
  state. Scheduler, Lambda, and SNS do not provide exactly-once delivery.
- **No cleanup:** the Lambda has only explicit inventory actions plus `sns:Publish`
  on its own topic and log-stream writes in its own log group. No delete, stop,
  tag, budget, IAM, or state-bucket writes are granted. Regional discovery uses
  wildcard resources with an `aws:RequestedRegion` condition; EKS/ECS describes
  are restricted to this project's resource ARNs. Scheduler has a separate role
  that can invoke only this function, with trust restricted to this schedule
  group and project. AWS organization SCPs remain an upper bound on these grants.
- **Failure visibility:** denied/failed service scans are included in the same
  email, preserving findings from successful scans. Unhandled failures/timeouts
  use Lambda's asynchronous SNS failure destination. Automatic retries are off
  to limit duplicate alerts. Logs expire after seven days. Scheduler delivery
  failures and SNS delivery failures still require inspection of AWS service
  metrics/logs; there is no independent heartbeat alarm in this small stack.
- **No network baseline cost:** Lambda runs outside a customer VPC, so the guard
  itself needs no NAT gateway, interface endpoint, EC2 host, or database. It uses
  the Python 3.13 runtime's boto3; local boto3 is a test-only dependency.

## Inventory and age semantics

| Resource | Selection | Age evidence |
| --- | --- | --- |
| EKS clusters | All returned clusters | `createdAt` |
| EC2 | Running instances only | `LaunchTime` (launch/start age) |
| NAT gateways | Excludes deleted/failed | `CreateTime` |
| Load balancers | Classic, ALB, NLB, GWLB | `CreatedTime` |
| RDS | Instances and clusters, excludes stopped | Instance/cluster creation time |
| VPC endpoints | Interface only, excludes terminal failed/deleted states | `CreationTimestamp` |
| Elastic IPs | No association, instance, or network interface | CloudTrail `AllocateAddress` event |
| ECS | Services whose `runningCount` is greater than zero | Service `createdAt` |

Age means existence/launch age, not measured continuous billed or idle time. An
old ECS service with newly started tasks can alert immediately. Aurora instances
and their cluster can both appear; rows are inventory findings, not summed cost
estimates. Stopped RDS storage, EBS volumes/snapshots, standalone ECS tasks, and
other storage/usage charges are outside this requested inventory.

EC2 does not expose EIP creation time. The watchdog reads CloudTrail's existing
90-day event history (no trail is created). If the allocation event is absent,
including events not yet delivered, the email says `age=UNKNOWN`; it does not
claim the EIP exceeded the threshold. Unknown timestamps trigger a conservative
manual-review warning. The configurable age is limited to 90 days. A denied
CloudTrail lookup produces an incomplete-inventory warning instead of a clean
result. [CloudTrail event history and pricing](https://aws.amazon.com/cloudtrail/pricing/).

This is intended for a small lab inventory. Scans have a 180-second timeout;
large inventories or persistent throttling may need batching later. Messages
over 250 KB are truncated with an explicit warning to stay within SNS limits.

## Private recipients and review-only commands

`alert_emails` is required and sensitive. The local ignored
`terraform.tfvars.json` was populated with the deduplicated email subscribers
from both existing budgets. No email address is committed. Terraform's saved
plan and eventual state still contain sensitive inputs; keep them private.

To rediscover recipients, list budgets and their notifications with the commands
below, then call `describe-subscribers-for-notification` for **each notification**,
retaining only `SubscriptionType == EMAIL`. Drop the response-only
`NotificationState` from the notification JSON. Deduplicate and save the resulting
addresses in `{"alert_emails": [...]}` in the ignored tfvars file (mode `0600`).

```sh
aws budgets describe-budgets --account-id 729608197929 \
  --profile agent-runtime --region us-east-1
aws budgets describe-notifications-for-budget --account-id 729608197929 \
  --budget-name "$BUDGET_NAME" --profile agent-runtime --region us-east-1
aws budgets describe-subscribers-for-notification --account-id 729608197929 \
  --budget-name "$BUDGET_NAME" --notification "$NOTIFICATION_JSON" \
  --profile agent-runtime --region us-east-1
```

From the repository root:

```sh
export AWS_PROFILE=agent-runtime
terraform -chdir=infra/platform/envs/dev/cost-guard init -input=false
terraform -chdir=infra/platform/envs/dev/cost-guard fmt -check
terraform -chdir=infra/platform/envs/dev/cost-guard validate
terraform -chdir=infra/platform/envs/dev/cost-guard plan \
  -input=false -lock=false -out=.terraform/cost-guard.tfplan
uv run pytest tests/test_cost_guard.py tests/test_aws_cost.py
make aws-cost
```

`init` prepares local provider/backend metadata; it does not deploy resources.
The review plan disables locking so it does not write an S3 lock object. Do not
use that flag for a future apply. No apply has been run. After a separately
authorized deployment, each recipient must confirm the SNS email subscription
before watchdog email delivery works. Billing anomaly emails use their own
subscription. A successful plan does not prove runtime permissions or delivery;
those require a later deployment and controlled notification check.

`make aws-cost` uses the AWS CLI with profile `agent-runtime` by default (or
`AWS_PROFILE`) and the `us-east-1` billing endpoints. It prints month-to-date
`UnblendedCost` by service, excluding credits/refunds, followed by Free-plan
remaining credits and expiration. Dates are computed in UTC, including today
using tomorrow as the exclusive end. AWS CLI pagination remains enabled. If one
API fails, the other result is still printed and the command exits nonzero.

## Verification recorded 2026-10-08 (Asia/Shanghai)

Verified facts:

- Caller project: `729608197929`; Free plan `ACTIVE`, remaining credits `$200`;
  reported expiration `2027-04-07T07:50:48.331000+00:00`.
- Budgets `free-tier-credit-monitor` and
  `free-tier-credit-monitor-early-alerts` share one unique email recipient.
  `get-anomaly-monitors` returned no existing monitors.
- `make test`: 157 passed, 22 integration/Kubernetes tests deselected; includes
  24 watchdog tests with botocore Stubber and five billing-report tests. The
  existing Starlette/httpx deprecation warning remains. Targeted Ruff checks
  passed.
- Terraform fmt/validate/plan succeeded. Plan: **13 to add, 0 to change, 0 to
  destroy**: monitor, anomaly subscription, SNS topic, email subscription, log
  group, Lambda role and inline policy, Lambda, async failure configuration,
  Scheduler group, Scheduler role and inline policy, hourly schedule.
- `make aws-cost` succeeded. Cost Explorer returned no service rows and marked
  the period estimated. That is not evidence of zero current usage.
- A read-only scan under the developer profile completed in 13.20 seconds with
  zero overdue resources, unknown ages, or scan errors. SNS was not called.
  This verifies the current profile's inventory access, not the future Lambda
  role. The optional local SDK scan needed `uv run --with 'botocore[crt]'` for
  the profile's login credential provider; the deployed role does not use it.

The saved review plan is `.terraform/cost-guard.tfplan`; human-readable output is
`.terraform/plan.txt`. Both are local and ignored. No AWS resources were created.

## Monthly cost estimate

**Assumptions:** 31 days, 744 invocations, 128 MB x86 Lambda, 30 seconds per run
(including initialization), one recipient and one alert every hour, messages
under 64 KB, 10 MB/month of logs with seven-day retention, no extra invocations.
Lambda duration in AWS has not been measured; the local scan is not a benchmark.

Before service free allocations or credits:

| Item | Approximate USD/month |
| --- | ---: |
| Lambda compute (`744 × 30 × 0.125 × $0.0000166667`) | $0.04650 |
| Lambda requests | $0.00015 |
| Scheduler invocations | $0.00074 |
| SNS publishes + 744 email deliveries | $0.01525 |
| Logs ingestion/storage allowance | $0.00510 |
| Cost Anomaly Detection | $0 |
| **Guard total, rounded** | **$0.07** |

Allow **under $0.10/month** for this small-inventory scenario before free
allocations/credits, plus negligible state storage/occasional Terraform requests
in the existing bucket. Shared free allocations can reduce this to approximately
zero; the current `$200` credit balance is separate and should not be mistaken
for a guaranteed permanent free allowance. More recipients, repeated failures,
longer runs, larger messages, or unrelated project usage change the estimate.
No monthly estimate here includes the resources the guard monitors.

`make aws-cost` adds **$0.01 per Cost Explorer API request/page**: once daily with
one page is **$0.31/month**, for about **$0.38/month** total before allowances and
credits under these assumptions. The hourly watchdog does not call Cost Explorer.

Prices checked against [Lambda](https://aws.amazon.com/lambda/pricing/),
[Scheduler](https://aws.amazon.com/eventbridge/pricing/),
[SNS](https://aws.amazon.com/sns/faqs/),
[CloudWatch](https://aws.amazon.com/cloudwatch/pricing/),
[Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/),
and [Cost Anomaly Detection](https://aws.amazon.com/aws-cost-management/aws-cost-anomaly-detection/faqs/).
