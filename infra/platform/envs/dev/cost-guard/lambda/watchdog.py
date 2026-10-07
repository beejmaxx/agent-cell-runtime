"""Read-only resource inventory; the only writes are SNS alerts and runtime logs."""

import json
import os
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = "us-east-2"
CONFIG = Config(
    retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=10
)


def items(client, operation, key, **kwargs):
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page.get(key, [])


def resource(kind, identifier, created, tags=None, name=None):
    if isinstance(tags, list):
        tags = {t.get("Key", t.get("key")): t.get("Value", t.get("value")) for t in tags}
    return {
        "type": kind,
        "id": identifier,
        "name": name or identifier,
        "created": created,
        "tags": tags or {},
    }


def eks(c):
    for name in items(c["eks"], "list_clusters", "clusters"):
        r = c["eks"].describe_cluster(name=name)["cluster"]
        yield resource("EKS cluster", name, r.get("createdAt"), r.get("tags"))


def ec2(c):
    for reservation in items(
        c["ec2"],
        "describe_instances",
        "Reservations",
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
    ):
        for r in reservation["Instances"]:
            yield resource(
                "EC2 running instance", r["InstanceId"], r.get("LaunchTime"), r.get("Tags")
            )


def nat(c):
    for r in items(c["ec2"], "describe_nat_gateways", "NatGateways"):
        if r["State"] not in {"deleted", "failed"}:
            yield resource("NAT gateway", r["NatGatewayId"], r.get("CreateTime"), r.get("Tags"))


def load_balancers(c):
    for service, key, id_key in [
        ("elbv2", "LoadBalancers", "LoadBalancerArn"),
        ("elb", "LoadBalancerDescriptions", "LoadBalancerName"),
    ]:
        for r in items(c[service], "describe_load_balancers", key):
            identifier = r[id_key]
            params = {"ResourceArns" if service == "elbv2" else "LoadBalancerNames": [identifier]}
            tags = c[service].describe_tags(**params)["TagDescriptions"][0].get("Tags", [])
            yield resource(
                "Load balancer", identifier, r.get("CreatedTime"), tags, r["LoadBalancerName"]
            )


def rds(c):
    for operation, key, id_key, time_key in [
        ("describe_db_instances", "DBInstances", "DBInstanceIdentifier", "InstanceCreateTime"),
        ("describe_db_clusters", "DBClusters", "DBClusterIdentifier", "ClusterCreateTime"),
    ]:
        for r in items(c["rds"], operation, key):
            # Stopped databases still incur storage charges, but no instance-hour charge.
            status = r.get("DBInstanceStatus", r.get("Status"))
            if status != "stopped":
                yield resource(
                    "RDS instance" if key == "DBInstances" else "RDS cluster",
                    r[id_key],
                    r.get(time_key),
                    r.get("TagList"),
                )


def endpoints(c):
    for r in items(
        c["ec2"],
        "describe_vpc_endpoints",
        "VpcEndpoints",
        Filters=[{"Name": "vpc-endpoint-type", "Values": ["Interface"]}],
    ):
        if r["State"] not in {"deleted", "failed", "rejected", "expired"}:
            yield resource(
                "Interface VPC endpoint",
                r["VpcEndpointId"],
                r.get("CreationTimestamp"),
                r.get("Tags"),
            )


def elastic_ips(c):
    addresses = [
        r
        for r in c["ec2"].describe_addresses()["Addresses"]
        if not r.get("AssociationId")
        and not r.get("NetworkInterfaceId")
        and not r.get("InstanceId")
    ]
    if not addresses:
        return
    allocations = {}
    # DescribeAddresses has no creation time. Missing history is unknown, not proof of age.
    for event in items(
        c["cloudtrail"],
        "lookup_events",
        "Events",
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "AllocateAddress"}],
    ):
        detail = json.loads(event["CloudTrailEvent"])
        allocation = (detail.get("responseElements") or {}).get("allocationId")
        if allocation:
            allocations[allocation] = event["EventTime"]
    for r in addresses:
        yield resource(
            "Unattached Elastic IP",
            r["AllocationId"],
            allocations.get(r["AllocationId"]),
            r.get("Tags"),
            r.get("PublicIp"),
        )


def ecs(c):
    for cluster in items(c["ecs"], "list_clusters", "clusterArns"):
        arns = list(items(c["ecs"], "list_services", "serviceArns", cluster=cluster))
        for offset in range(0, len(arns), 10):
            response = c["ecs"].describe_services(
                cluster=cluster, services=arns[offset : offset + 10], include=["TAGS"]
            )
            if response.get("failures"):
                raise RuntimeError(f"ECS describe_services failures: {response['failures']}")
            for r in response["services"]:
                if r["runningCount"] > 0:
                    yield resource(
                        "ECS service with running tasks",
                        r["serviceArn"],
                        r.get("createdAt"),
                        r.get("tags"),
                        r["serviceName"],
                    )


SCANNERS = (eks, ec2, nat, load_balancers, rds, endpoints, elastic_ips, ecs)


def scan(clients, now, max_age_hours):
    overdue, unknown, errors = [], [], []
    for scanner in SCANNERS:
        try:
            for r in scanner(clients):
                created = r["created"]
                if created is None:
                    unknown.append(r)
                elif now - created > timedelta(hours=max_age_hours):
                    overdue.append(r)
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            # Preserve findings from other services; an incomplete scan must never look clean.
            errors.append(f"{scanner.__name__}: {exc}")
    return overdue, unknown, errors


def one_line(value):
    return str(value).replace("\n", " ").replace("\r", " ")


def message(overdue, unknown, errors, now, max_age_hours):
    lines = [
        f"AWS cost guard: {REGION} at {now.isoformat()}",
        f"Resources older than {max_age_hours:g} hours: {len(overdue)}.",
        "Alert only: no resources were modified or deleted.",
        "Age is creation/launch age, not measured continuous billable or idle time.",
    ]
    for r in overdue + unknown:
        age = (
            f"{(now - r['created']).total_seconds() / 3600:.2f}h"
            if r["created"]
            else "UNKNOWN (creation timestamp unavailable; review manually)"
        )
        tags = r["tags"]
        lines.append(
            f"{r['type']} | {r['name']} | {r['id']} | "
            f"Name={one_line(tags.get('Name', '-'))} | "
            f"lab={one_line(tags.get('lab', '-'))} | "
            f"experiment={one_line(tags.get('experiment', '-'))} | age={age}"
        )
    if errors:
        lines += ["INCOMPLETE INVENTORY (these checks failed):", *errors]
    body = "\n".join(lines)
    # One SNS publish per scan; reserve room below the 256 KiB UTF-8 message limit.
    if len(body.encode("utf-8")) > 250_000:
        body = body.encode("utf-8")[:249_000].decode("utf-8", errors="ignore")
        body += "\nTRUNCATED: inventory exceeds one email; inspect resources in AWS."
    return body


def handler(event, context):
    max_age_hours = float(os.environ.get("MAX_AGE_HOURS", "4"))
    if not 0 < max_age_hours <= 2160:
        raise ValueError("MAX_AGE_HOURS must be greater than 0 and at most 2160 (90 days)")
    clients = {
        name: boto3.client(name, region_name=REGION, config=CONFIG)
        for name in ("eks", "ec2", "elbv2", "elb", "rds", "cloudtrail", "ecs", "sns")
    }
    now = datetime.now(UTC)
    overdue, unknown, errors = scan(clients, now, max_age_hours)
    if overdue or unknown or errors:
        clients["sns"].publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Subject=f"AWS cost guard: {len(overdue)} overdue; "
            f"{len(unknown)} unknown; {len(errors)} scan errors",
            Message=message(overdue, unknown, errors, now, max_age_hours),
        )
    result = {"overdue": len(overdue), "unknown_age": len(unknown), "scan_errors": len(errors)}
    print(json.dumps(result))
    return result
