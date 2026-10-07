"""CG invariants: inventory coverage, age accuracy, and bounded alert-only behavior."""

import importlib.util
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "watchdog", ROOT / "infra/platform/envs/dev/cost-guard/lambda/watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=5)
TAGS = [{"Key": "lab", "Value": "synthetic"}, {"Key": "experiment", "Value": "cg-test"}]
TOPIC = "arn:aws:sns:us-east-2:729608197929:lab-dev-cost-guard"
RUNNING = {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}]}
INTERFACE = {"Filters": [{"Name": "vpc-endpoint-type", "Values": ["Interface"]}]}


@pytest.fixture
def aws(monkeypatch):
    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-2"
    )
    clients = {
        name: session.client(name)
        for name in ("eks", "ec2", "elbv2", "elb", "rds", "ecs", "cloudtrail", "sns")
    }
    stubs = {name: Stubber(client) for name, client in clients.items()}
    monkeypatch.setattr(watchdog.boto3, "client", lambda name, **kwargs: clients[name])
    monkeypatch.setenv("TOPIC_ARN", TOPIC)
    monkeypatch.setenv("MAX_AGE_HOURS", "4")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(watchdog, "datetime", Clock)
    with ExitStack() as stack:
        for stub in stubs.values():
            stack.enter_context(stub)
        yield clients, stubs
        for stub in stubs.values():
            stub.assert_no_pending_responses()


def empty_scan(stubs, instances=(), fail_eks=False):
    if fail_eks:
        stubs["eks"].add_client_error(
            "list_clusters", "AccessDeniedException", "synthetic denial", expected_params={}
        )
    else:
        stubs["eks"].add_response("list_clusters", {"clusters": []}, {})
    stubs["ec2"].add_response(
        "describe_instances", {"Reservations": [{"Instances": instances}]}, RUNNING
    )
    stubs["ec2"].add_response("describe_nat_gateways", {"NatGateways": []}, {})
    stubs["elbv2"].add_response("describe_load_balancers", {"LoadBalancers": []}, {})
    stubs["elb"].add_response("describe_load_balancers", {"LoadBalancerDescriptions": []}, {})
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": []}, {})
    stubs["rds"].add_response("describe_db_clusters", {"DBClusters": []}, {})
    stubs["ec2"].add_response("describe_vpc_endpoints", {"VpcEndpoints": []}, INTERFACE)
    stubs["ec2"].add_response("describe_addresses", {"Addresses": []}, {})
    stubs["ecs"].add_response("list_clusters", {"clusterArns": []}, {})


def test_cg01_empty_inventory_sends_no_email(aws):
    _, stubs = aws
    empty_scan(stubs)
    assert watchdog.handler({}, None) == {"overdue": 0, "unknown_age": 0, "scan_errors": 0}


def test_cg02_overdue_resources_produce_one_consolidated_email(aws):
    _, stubs = aws
    instances = [{"InstanceId": f"i-{i}", "LaunchTime": OLD, "Tags": TAGS} for i in range(2)]
    empty_scan(stubs, instances)
    resources = [
        watchdog.resource("EC2 running instance", r["InstanceId"], OLD, TAGS) for r in instances
    ]
    body = watchdog.message(resources, [], [], NOW, 4)
    assert "age=5.00h" in body and "lab=synthetic" in body and "experiment=cg-test" in body
    stubs["sns"].add_response(
        "publish",
        {"MessageId": "synthetic"},
        {
            "TopicArn": TOPIC,
            "Subject": "AWS cost guard: 2 overdue; 0 unknown; 0 scan errors",
            "Message": body,
        },
    )
    assert watchdog.handler({}, None)["overdue"] == 2


@pytest.mark.parametrize("hours", [0, 3, 4])
def test_cg03_age_at_or_below_threshold_is_not_overdue(aws, hours):
    _, stubs = aws
    empty_scan(stubs, [{"InstanceId": "i-new", "LaunchTime": NOW - timedelta(hours=hours)}])
    assert watchdog.handler({}, None)["overdue"] == 0


def test_cg03_threshold_is_configurable(aws, monkeypatch):
    _, stubs = aws
    monkeypatch.setenv("MAX_AGE_HOURS", "6")
    empty_scan(stubs, [{"InstanceId": "i-old", "LaunchTime": OLD}])
    assert watchdog.handler({}, None)["overdue"] == 0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "2161"])
def test_cg03_invalid_threshold_fails_before_inventory(aws, monkeypatch, value):
    monkeypatch.setenv("MAX_AGE_HOURS", value)
    with pytest.raises(ValueError):
        watchdog.handler({}, None)


def test_cg04_failure_is_reported_without_hiding_other_findings(aws):
    clients, stubs = aws
    empty_scan(stubs, [{"InstanceId": "i-old", "LaunchTime": OLD}], fail_eks=True)
    overdue, unknown, errors = watchdog.scan(clients, NOW, 4)
    assert len(overdue) == 1 and unknown == []
    assert len(errors) == 1 and "AccessDeniedException" in errors[0]
    assert "INCOMPLETE INVENTORY" in watchdog.message(overdue, unknown, errors, NOW, 4)


def test_cg04_scan_failure_alone_sends_one_warning(aws):
    _, stubs = aws
    empty_scan(stubs, fail_eks=True)
    stubs["sns"].add_response(
        "publish",
        {"MessageId": "synthetic"},
        {
            "TopicArn": TOPIC,
            "Subject": "AWS cost guard: 0 overdue; 0 unknown; 1 scan errors",
            "Message": ANY,
        },
    )
    assert watchdog.handler({}, None)["scan_errors"] == 1


def test_cg05_ec2_pagination_keeps_later_instances(aws):
    clients, stubs = aws
    stubs["ec2"].add_response(
        "describe_instances", {"Reservations": [], "NextToken": "page2"}, RUNNING
    )
    stubs["ec2"].add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-later", "LaunchTime": OLD}]}]},
        {**RUNNING, "NextToken": "page2"},
    )
    assert [r["id"] for r in watchdog.ec2(clients)] == ["i-later"]


def test_cg06_eks_creation_age_and_tags(aws):
    clients, stubs = aws
    stubs["eks"].add_response("list_clusters", {"clusters": ["synthetic"]}, {})
    stubs["eks"].add_response(
        "describe_cluster",
        {"cluster": {"name": "synthetic", "createdAt": OLD, "tags": {"lab": "synthetic"}}},
        {"name": "synthetic"},
    )
    (r,) = watchdog.eks(clients)
    assert r["created"] == OLD and r["tags"]["lab"] == "synthetic"


def test_cg07_nat_and_interface_endpoints_exclude_terminal_states(aws):
    clients, stubs = aws
    stubs["ec2"].add_response(
        "describe_nat_gateways",
        {
            "NatGateways": [
                {"NatGatewayId": "nat-old", "CreateTime": OLD, "State": "available", "Tags": TAGS},
                {"NatGatewayId": "nat-deleted", "CreateTime": OLD, "State": "deleted"},
                {"NatGatewayId": "nat-failed", "CreateTime": OLD, "State": "failed"},
            ]
        },
        {},
    )
    stubs["ec2"].add_response(
        "describe_vpc_endpoints",
        {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": "vpce-old",
                    "CreationTimestamp": OLD,
                    "State": "available",
                    "VpcEndpointType": "Interface",
                    "Tags": TAGS,
                },
                {"VpcEndpointId": "vpce-deleted", "CreationTimestamp": OLD, "State": "deleted"},
            ]
        },
        INTERFACE,
    )
    assert [r["id"] for r in watchdog.nat(clients)] == ["nat-old"]
    (endpoint,) = watchdog.endpoints(clients)
    assert endpoint["id"] == "vpce-old" and endpoint["tags"]["experiment"] == "cg-test"


def test_cg08_classic_and_v2_load_balancers_include_tags(aws):
    clients, stubs = aws
    arn = "arn:aws:elasticloadbalancing:us-east-2:729608197929:loadbalancer/app/synthetic/123"
    stubs["elbv2"].add_response(
        "describe_load_balancers",
        {
            "LoadBalancers": [
                {"LoadBalancerName": "synthetic", "LoadBalancerArn": arn, "CreatedTime": OLD}
            ]
        },
        {},
    )
    stubs["elbv2"].add_response(
        "describe_tags",
        {"TagDescriptions": [{"ResourceArn": arn, "Tags": TAGS}]},
        {"ResourceArns": [arn]},
    )
    stubs["elb"].add_response(
        "describe_load_balancers",
        {"LoadBalancerDescriptions": [{"LoadBalancerName": "classic", "CreatedTime": OLD}]},
        {},
    )
    stubs["elb"].add_response(
        "describe_tags",
        {"TagDescriptions": [{"LoadBalancerName": "classic", "Tags": TAGS}]},
        {"LoadBalancerNames": ["classic"]},
    )
    resources = list(watchdog.load_balancers(clients))
    assert len(resources) == 2
    assert all(r["created"] == OLD and r["tags"]["lab"] == "synthetic" for r in resources)


def test_cg09_rds_instances_and_clusters_exclude_stopped(aws):
    clients, stubs = aws
    stubs["rds"].add_response(
        "describe_db_instances",
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "db-old",
                    "InstanceCreateTime": OLD,
                    "DBInstanceStatus": "available",
                    "TagList": TAGS,
                },
                {
                    "DBInstanceIdentifier": "db-stopped",
                    "InstanceCreateTime": OLD,
                    "DBInstanceStatus": "stopped",
                },
            ]
        },
        {},
    )
    stubs["rds"].add_response(
        "describe_db_clusters",
        {
            "DBClusters": [
                {
                    "DBClusterIdentifier": "cluster-old",
                    "ClusterCreateTime": OLD,
                    "Status": "available",
                    "TagList": TAGS,
                },
                {
                    "DBClusterIdentifier": "cluster-stopped",
                    "ClusterCreateTime": OLD,
                    "Status": "stopped",
                },
            ]
        },
        {},
    )
    resources = list(watchdog.rds(clients))
    assert [r["id"] for r in resources] == ["db-old", "cluster-old"]
    assert all(r["tags"]["lab"] == "synthetic" for r in resources)


def test_cg10_elastic_ip_age_uses_allocation_evidence_and_marks_unknown(aws):
    clients, stubs = aws
    stubs["ec2"].add_response(
        "describe_addresses",
        {
            "Addresses": [
                {"AllocationId": "eipalloc-old", "PublicIp": "192.0.2.1", "Tags": TAGS},
                {"AllocationId": "eipalloc-unknown", "PublicIp": "192.0.2.2"},
                {"AllocationId": "eipalloc-attached", "AssociationId": "eipassoc-test"},
                {"AllocationId": "eipalloc-eni", "NetworkInterfaceId": "eni-test"},
            ]
        },
        {},
    )
    stubs["cloudtrail"].add_response(
        "lookup_events",
        {
            "Events": [
                {
                    "EventTime": OLD,
                    "CloudTrailEvent": json.dumps(
                        {"responseElements": {"allocationId": "eipalloc-old"}}
                    ),
                }
            ]
        },
        {"LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": "AllocateAddress"}]},
    )
    old, unknown = watchdog.elastic_ips(clients)
    assert old["created"] == OLD and unknown["created"] is None
    assert old["tags"]["lab"] == "synthetic"
    assert "age=UNKNOWN" in watchdog.message([old], [unknown], [], NOW, 4)


def test_cg10_unknown_timestamp_is_alerted_not_assumed_old(aws):
    _, stubs = aws
    empty_scan(stubs, [{"InstanceId": "i-unknown"}])
    stubs["sns"].add_response(
        "publish",
        {"MessageId": "synthetic"},
        {
            "TopicArn": TOPIC,
            "Subject": "AWS cost guard: 0 overdue; 1 unknown; 0 scan errors",
            "Message": ANY,
        },
    )
    assert watchdog.handler({}, None) == {"overdue": 0, "unknown_age": 1, "scan_errors": 0}


def test_cg11_ecs_paginates_batches_and_requires_running_tasks(aws):
    clients, stubs = aws
    cluster = "arn:aws:ecs:us-east-2:729608197929:cluster/synthetic"
    services = [f"arn:aws:ecs:us-east-2:729608197929:service/synthetic/s{i}" for i in range(11)]
    stubs["ecs"].add_response("list_clusters", {"clusterArns": [cluster]}, {})
    stubs["ecs"].add_response(
        "list_services", {"serviceArns": services[:10], "nextToken": "next"}, {"cluster": cluster}
    )
    stubs["ecs"].add_response(
        "list_services", {"serviceArns": services[10:]}, {"cluster": cluster, "nextToken": "next"}
    )
    for offset in (0, 10):
        batch = services[offset : offset + 10]
        stubs["ecs"].add_response(
            "describe_services",
            {
                "services": [
                    {
                        "serviceArn": arn,
                        "serviceName": arn.rsplit("/", 1)[1],
                        "createdAt": OLD,
                        "runningCount": int(arn == services[-1]),
                        "tags": [{"key": "lab", "value": "synthetic"}],
                    }
                    for arn in batch
                ]
            },
            {"cluster": cluster, "services": batch, "include": ["TAGS"]},
        )
    (r,) = watchdog.ecs(clients)
    assert r["id"] == services[-1] and r["tags"]["lab"] == "synthetic"


def test_cg11_ecs_api_failure_is_not_silently_empty(aws):
    clients, stubs = aws
    stubs["ecs"].add_response("list_clusters", {"clusterArns": ["synthetic"]}, {})
    stubs["ecs"].add_response(
        "list_services", {"serviceArns": ["service"]}, {"cluster": "synthetic"}
    )
    stubs["ecs"].add_response(
        "describe_services",
        {"services": [], "failures": [{"arn": "service", "reason": "MISSING"}]},
        {"cluster": "synthetic", "services": ["service"], "include": ["TAGS"]},
    )
    with pytest.raises(RuntimeError, match="MISSING"):
        list(watchdog.ecs(clients))


def test_cg12_email_is_valid_utf8_and_bounded():
    resources = [watchdog.resource("EKS cluster", "x" * 1000, OLD, {"lab": "测" * 500})] * 200
    body = watchdog.message(resources, [], [], NOW, 4)
    assert len(body.encode("utf-8")) < 256 * 1024
    assert body.endswith("TRUNCATED: inventory exceeds one email; inspect resources in AWS.")


def test_cg13_publish_failure_is_not_reported_as_success(aws):
    _, stubs = aws
    empty_scan(stubs, [{"InstanceId": "i-old", "LaunchTime": OLD}])
    stubs["sns"].add_client_error("publish", "AuthorizationError", "synthetic denied")
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        watchdog.handler({}, None)
