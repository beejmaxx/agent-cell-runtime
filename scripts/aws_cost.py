"""Read current costs and Free-plan credit balance through the AWS CLI."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def aws(*args):
    result = subprocess.run(
        [
            "aws",
            *args,
            "--profile",
            os.environ.get("AWS_PROFILE", "agent-runtime"),
            "--region",
            "us-east-1",
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def cost_args(today):
    return (
        "ce",
        "get-cost-and-usage",
        "--time-period",
        f"Start={today.replace(day=1).isoformat()},End={(today + timedelta(days=1)).isoformat()}",
        "--granularity",
        "MONTHLY",
        "--metrics",
        "UnblendedCost",
        "--group-by",
        "Type=DIMENSION,Key=SERVICE",
        "--filter",
        json.dumps({"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}),
    )


def main():
    today = datetime.now(UTC).date()
    failed = False
    print(f"AWS costs through {today} UTC (billing data can lag up to 24 hours)")
    try:
        costs = aws(*cost_args(today))
        totals = {}
        for period in costs["ResultsByTime"]:
            for group in period["Groups"]:
                metric = group["Metrics"]["UnblendedCost"]
                key = (group["Keys"][0], metric["Unit"])
                totals[key] = totals.get(key, Decimal(0)) + Decimal(metric["Amount"])
        print("Month-to-date UnblendedCost by service, excluding credits/refunds:")
        for (service, unit), amount in sorted(totals.items()):
            print(f"  {service}: {amount:.6f} {unit}")
        if not totals:
            print("  No cost data returned; this is not proof of zero usage.")
        if any(p.get("Estimated") for p in costs["ResultsByTime"]):
            print("  AWS marks these costs as estimated.")
    except subprocess.CalledProcessError as exc:
        print(f"Cost Explorer unavailable: {exc.stderr.strip()}")
        failed = True
    try:
        plan = aws("freetier", "get-account-plan-state")
        print(
            f"Project {plan['accountId']}: {plan['accountPlanType']} / {plan['accountPlanStatus']}"
        )
        credits = plan.get("accountPlanRemainingCredits")
        if credits:
            print(f"Remaining Free-plan credits: {credits['amount']} {credits['unit']}")
        else:
            print("Remaining credits not returned by AWS (not assumed to be zero).")
        print(f"Plan expiration: {plan.get('accountPlanExpirationDate', 'not returned')}")
    except subprocess.CalledProcessError as exc:
        print(f"Free-plan state unavailable: {exc.stderr.strip()}")
        failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
