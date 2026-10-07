import json
import subprocess

import pytest

from scripts import s1


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail(f"Unexpected external call: {args}")

    monkeypatch.setattr(s1, "STATE", tmp_path)
    monkeypatch.setattr(s1, "aws", unexpected)
    monkeypatch.setattr(s1, "aws_json", unexpected)
    monkeypatch.setattr(s1, "terraform", unexpected)
    monkeypatch.setattr(s1.time, "sleep", lambda seconds: None)


@pytest.fixture
def config():
    return {
        "cluster_name": s1.CLUSTER,
        "cluster_security_group_id": "sg-recorded",
        "execution_vpc_id": "vpc-recorded",
        "gateway_endpoint_id": "vpce-recorded",
        "endpoint_service_id": "vpce-svc-recorded",
    }


def endpoint_api(monkeypatch, states, *, unsuccessful=False, change=None):
    calls = []
    states = iter(states)

    def api(*args):
        calls.append(args)
        if args[1] == "describe-vpc-endpoint-service-configurations":
            assert args[2:] == ("--service-ids", "vpce-svc-recorded")
            return {
                "ServiceConfigurations": [
                    {"ServiceId": "vpce-svc-recorded", "ServiceName": "service"}
                ]
            }
        if args[1] == "describe-vpc-endpoints":
            assert args[2:] == ("--vpc-endpoint-ids", "vpce-recorded")
            endpoint = {
                "VpcEndpointId": "vpce-recorded",
                "VpcId": "vpc-recorded",
                "ServiceName": "service",
                "State": next(states),
            }
            return {"VpcEndpoints": [{**endpoint, **(change or {})}]}
        assert args == (
            "ec2",
            "accept-vpc-endpoint-connections",
            "--service-id",
            "vpce-svc-recorded",
            "--vpc-endpoint-ids",
            "vpce-recorded",
        )
        return {
            "Unsuccessful": (
                [
                    {
                        "ResourceId": "vpce-recorded",
                        "Error": {"Code": "Unavailable", "Message": "provisioning"},
                    }
                ]
                if unsuccessful
                else []
            )
        }

    monkeypatch.setattr(s1, "aws_json", api)
    return calls


@pytest.mark.parametrize("unsuccessful", [False, True])
def test_ISO_2_endpoint_retries_only_recorded_id_until_available(monkeypatch, config, unsuccessful):
    calls = endpoint_api(
        monkeypatch,
        ["pendingAcceptance", "pendingAcceptance", "pending", "available"],
        unsuccessful=unsuccessful,
    )
    sleeps = []
    monkeypatch.setattr(s1.time, "sleep", sleeps.append)
    s1.ensure_endpoint_available(config)
    assert sum(c[1] == "accept-vpc-endpoint-connections" for c in calls) == 2
    assert calls[-1][1] == "describe-vpc-endpoints"
    assert sleeps == [0, 1, 2, 4]


def test_ISO_2_available_endpoint_needs_no_acceptance(monkeypatch, config):
    calls = endpoint_api(monkeypatch, ["available"])
    s1.ensure_endpoint_available(config)
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["pendingAcceptance", "pending"])
def test_ISO_2_endpoint_retry_budget_fails_loudly(monkeypatch, config, state):
    calls = endpoint_api(monkeypatch, [state] * len(s1.RECOVERY_DELAYS), unsuccessful=True)
    with pytest.raises(RuntimeError, match="did not become Available") as exc:
        s1.ensure_endpoint_available(config)
    if state == "pendingAcceptance":
        assert "Unavailable" in str(exc.value)
    assert sum(c[1] == "describe-vpc-endpoints" for c in calls) == len(s1.RECOVERY_DELAYS)
    assert sum(c[1] == "accept-vpc-endpoint-connections" for c in calls) == (
        len(s1.RECOVERY_DELAYS) - 1 if state == "pendingAcceptance" else 0
    )


@pytest.mark.parametrize(
    "change",
    [
        {"VpcEndpointId": "vpce-other"},
        {"VpcId": "vpc-other"},
        {"ServiceName": "other-service"},
        {"State": "rejected"},
        {"State": "failed"},
    ],
)
def test_ISO_2_endpoint_refuses_wrong_identity_or_failed_state(monkeypatch, config, change):
    calls = endpoint_api(monkeypatch, ["pendingAcceptance"], change=change)
    with pytest.raises(RuntimeError, match="unexpected"):
        s1.ensure_endpoint_available(config)
    assert len(calls) == 2


def test_ISO_2_acceptance_never_follows_unsolicited_endpoint_id(monkeypatch, config):
    calls = endpoint_api(monkeypatch, ["pendingAcceptance"])
    original = s1.aws_json

    def api(*args):
        value = original(*args)
        if args[1] == "accept-vpc-endpoint-connections":
            return {"Unsuccessful": [{"ResourceId": "vpce-other"}]}
        return value

    monkeypatch.setattr(s1, "aws_json", api)
    with pytest.raises(RuntimeError, match="unexpected endpoint ID"):
        s1.ensure_endpoint_available(config)
    assert len(calls) == 3


def group_api(monkeypatch, *, change=None, enis=0, failures=0, exists=True, cluster=False):
    calls = []

    def api(*args):
        nonlocal enis, failures, exists
        calls.append(args)
        if args[1] == "list-clusters":
            return {"clusters": [s1.CLUSTER] if cluster else []}
        if args[1] == "describe-security-groups":
            assert args[2:] == ("--filters", "Name=group-id,Values=sg-recorded")
            group = {
                "GroupId": "sg-recorded",
                "VpcId": "vpc-recorded",
                "OwnerId": s1.ACCOUNT,
                "Tags": [{"Key": "aws:eks:cluster-name", "Value": s1.CLUSTER}],
            }
            return {"SecurityGroups": [{**group, **(change or {})}] if exists else []}
        if args[1] == "describe-network-interfaces":
            assert args[2:] == ("--filters", "Name=group-id,Values=sg-recorded")
            if enis:
                enis -= 1
                return {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-busy"}]}
            return {"NetworkInterfaces": []}
        assert args == ("ec2", "delete-security-group", "--group-id", "sg-recorded")
        if failures:
            failures -= 1
            raise subprocess.CalledProcessError(254, "synthetic DependencyViolation")
        exists = False
        return {"Return": True}

    monkeypatch.setattr(s1, "aws_json", api)
    return calls


@pytest.mark.parametrize("enis,failures", [(0, 0), (2, 0), (0, 2)])
def test_LC_11_orphan_cleanup_retries_and_verifies_absence(monkeypatch, config, enis, failures):
    calls = group_api(monkeypatch, enis=enis, failures=failures)
    assert s1.cleanup_cluster_security_group(config)
    assert sum(c[1] == "delete-security-group" for c in calls) == failures + 1
    assert calls[-1][1] == "describe-security-groups"
    # Every delete follows a new ownership and attachment check.
    for index, call in enumerate(calls):
        if call[1] == "delete-security-group":
            assert calls[index - 1][1] == "describe-network-interfaces"
            assert calls[index - 2][1] == "describe-security-groups"


@pytest.mark.parametrize(
    "change",
    [
        {"GroupId": "sg-other"},
        {"VpcId": "vpc-other"},
        {"OwnerId": "000000000000"},
        {"Tags": []},
        {"Tags": [{"Key": "aws:eks:cluster-name", "Value": "other"}]},
    ],
)
def test_LC_11_orphan_cleanup_refuses_wrong_ownership(monkeypatch, config, change):
    calls = group_api(monkeypatch, change=change)
    with pytest.raises(RuntimeError, match="ownership/VPC"):
        s1.cleanup_cluster_security_group(config)
    assert not any(c[1] == "delete-security-group" for c in calls)


@pytest.mark.parametrize("field", ["cluster_name", "cluster_security_group_id", "execution_vpc_id"])
def test_LC_11_orphan_cleanup_requires_recorded_identity(config, field):
    config.pop(field)
    with pytest.raises(RuntimeError, match="Missing recorded"):
        s1.cleanup_cluster_security_group(config)


def test_LC_11_orphan_cleanup_refuses_live_cluster(monkeypatch, config):
    calls = group_api(monkeypatch, cluster=True)
    with pytest.raises(RuntimeError, match="cluster exists"):
        s1.cleanup_cluster_security_group(config)
    assert len(calls) == 1


@pytest.mark.parametrize("blocked", ["enis", "delete"])
def test_LC_11_orphan_cleanup_exhaustion_is_not_clean(monkeypatch, config, blocked):
    count = len(s1.RECOVERY_DELAYS)
    calls = group_api(
        monkeypatch,
        enis=count if blocked == "enis" else 0,
        failures=count if blocked == "delete" else 0,
    )
    with pytest.raises(RuntimeError, match="exhausted retries.*s1-leftovers"):
        s1.cleanup_cluster_security_group(config)
    assert sum(c[1] == "delete-security-group" for c in calls) == (
        0 if blocked == "enis" else count
    )


def test_LC_11_absent_orphan_is_idempotent(monkeypatch, config):
    calls = group_api(monkeypatch, exists=False)
    assert not s1.cleanup_cluster_security_group(config)
    assert not any(c[1] == "delete-security-group" for c in calls)


@pytest.mark.parametrize("failure", [False, True])
def test_LC_11_down_recovers_orphan_then_checks_leftovers(monkeypatch, config, failure):
    (s1.STATE / "connection.json").write_text(json.dumps(config))
    monkeypatch.setattr(s1, "run_approved", lambda: None)
    monkeypatch.setattr(s1, "state_guard", lambda: None)
    calls = group_api(monkeypatch)
    destroys = []

    def terraform(*args):
        destroys.append(args)
        if failure and len(destroys) == 1:
            raise subprocess.CalledProcessError(1, "terraform destroy")
        if len(destroys) == 2:
            assert any(c[1] == "delete-security-group" for c in calls)

    monkeypatch.setattr(s1, "terraform", terraform)
    checked = []
    monkeypatch.setattr(s1, "leftovers", lambda: checked.append(True))
    s1.down()
    assert len(destroys) == (2 if failure else 1)
    assert all(d == destroys[0] for d in destroys)
    assert destroys[0][0] == "destroy"
    assert checked == [True]


def test_LC_11_destroy_failure_without_orphan_is_not_hidden(monkeypatch, config):
    group_api(monkeypatch, exists=False)
    calls = []

    def terraform(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, "terraform destroy")

    monkeypatch.setattr(s1, "terraform", terraform)
    with pytest.raises(subprocess.CalledProcessError):
        s1.destroy_infrastructure(config)
    assert len(calls) == 1


def test_ISO_2_endpoint_api_capitalization(monkeypatch, config):
    calls = endpoint_api(monkeypatch, ["PendingAcceptance", "Available"])
    s1.ensure_endpoint_available(config)
    assert sum(c[1] == "accept-vpc-endpoint-connections" for c in calls) == 1


def test_ISO_2_up_requires_available_endpoint_before_setup(monkeypatch, config):
    import hashlib

    (s1.STATE / "plan.tfplan").write_bytes(b"synthetic")
    (s1.STATE / "plan-review.json").write_text(
        json.dumps(
            {
                "source_digest": "source",
                "plan_digest": hashlib.sha256(b"synthetic").hexdigest(),
            }
        )
    )
    (s1.STATE / "inputs.tfvars.json").write_text(json.dumps({"operator_cidr": "8.8.8.8/32"}))
    monkeypatch.setattr(s1, "run_approved", lambda: None)
    monkeypatch.setattr(s1, "state_guard", lambda: None)
    monkeypatch.setattr(s1, "source_digest", lambda: "source")
    monkeypatch.setattr(s1, "direct_cidr", lambda: "8.8.8.8/32")
    monkeypatch.setattr(s1.subprocess, "run", lambda *a, **kw: None)
    calls = []

    def terraform(*args, **kwargs):
        calls.append(args)
        return json.dumps(config) if args[0] == "output" else "{}"

    monkeypatch.setattr(s1, "terraform", terraform)
    endpoint_api(monkeypatch, ["failed"])
    with pytest.raises(RuntimeError, match="unexpected state failed"):
        s1.up()
    assert [call[0] for call in calls] == ["apply", "output", "show"]
    assert s1.connection() == config
    assert not (s1.STATE / "host.json").exists()


def test_LC_11_cleanup_refusal_stops_down_before_clean_report(monkeypatch, config):
    (s1.STATE / "connection.json").write_text(json.dumps(config))
    monkeypatch.setattr(s1, "run_approved", lambda: None)
    monkeypatch.setattr(s1, "state_guard", lambda: None)
    monkeypatch.setattr(s1, "terraform", lambda *a: None)
    group_api(monkeypatch, change={"Tags": []})
    monkeypatch.setattr(s1, "leftovers", lambda: pytest.fail("Reported clean despite refusal"))
    with pytest.raises(RuntimeError, match="ownership/VPC"):
        s1.down()
    assert s1.connection() == config


def test_LC_11_cleanup_rechecks_ownership_after_delete_failure(monkeypatch, config):
    calls = group_api(monkeypatch, failures=1)
    original = s1.aws_json
    descriptions = 0

    def api(*args):
        nonlocal descriptions
        result = original(*args)
        if args[1] == "describe-security-groups":
            descriptions += 1
            if descriptions > 1:
                result["SecurityGroups"][0]["Tags"] = []
        return result

    monkeypatch.setattr(s1, "aws_json", api)
    with pytest.raises(RuntimeError, match="ownership/VPC"):
        s1.cleanup_cluster_security_group(config)
    assert sum(c[1] == "delete-security-group" for c in calls) == 1


def test_LC_11_partial_apply_records_group_before_cluster_deletion(monkeypatch, config):
    monkeypatch.setattr(s1, "run_approved", lambda: None)
    monkeypatch.setattr(s1, "state_guard", lambda: None)
    monkeypatch.setattr(s1, "kubectl", lambda *a: "")
    deleted = False

    def api(*args):
        if args[1] == "list-clusters":
            return {"clusters": [s1.CLUSTER]}
        if args[1] == "describe-cluster":
            return {
                "cluster": {
                    "tags": {"lab": "agent-runtime", "experiment": "s1"},
                    "arn": "synthetic",
                    "endpoint": "synthetic",
                    "certificateAuthority": {"data": "synthetic"},
                    "resourcesVpcConfig": {
                        "clusterSecurityGroupId": "sg-recorded",
                        "vpcId": "vpc-recorded",
                    },
                }
            }
        assert args[1] == "list-fargate-profiles"
        return {"fargateProfileNames": []}

    def aws(*args):
        nonlocal deleted
        assert s1.connection()["cluster_security_group_id"] == "sg-recorded"
        assert s1.connection()["execution_vpc_id"] == "vpc-recorded"
        if args[1] == "delete-cluster":
            deleted = True
        else:
            assert args[1:3] == ("wait", "cluster-deleted")

    def destroy(recorded):
        assert deleted
        assert recorded == s1.connection()

    monkeypatch.setattr(s1, "aws_json", api)
    monkeypatch.setattr(s1, "aws", aws)
    monkeypatch.setattr(s1, "destroy_infrastructure", destroy)
    monkeypatch.setattr(s1, "leftovers", lambda: None)
    s1.down()
