import json

import pytest

from scripts import s1


@pytest.mark.parametrize("action", [s1.up, s1.test, s1.down])
def test_ISO_1_no_mutation_before_checkpoint_4(monkeypatch, action):
    monkeypatch.delenv("S1_RUN_APPROVED", raising=False)
    monkeypatch.setattr(s1, "state_guard", lambda: pytest.fail("Touched AWS before approval gate"))
    with pytest.raises(RuntimeError, match="Checkpoint 4"):
        action()


@pytest.mark.parametrize("key", ["dev/foundation.tfstate", "bootstrap.tfstate", s1.KEY])
def test_ISO_1_teardown_state_isolation(monkeypatch, tmp_path, key):
    monkeypatch.setattr(s1, "TF", tmp_path)
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform/terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {
                    "type": "s3",
                    "config": {"bucket": s1.BUCKET, "key": key, "region": s1.REGION},
                }
            }
        )
    )
    calls = []
    monkeypatch.setattr(s1, "terraform", lambda *args, **kwargs: calls.append(args) or "default")
    monkeypatch.setattr(
        s1,
        "aws_json",
        lambda *args: {
            "Account": s1.ACCOUNT,
            "Arn": s1.OPERATOR,
        },
    )
    if key != s1.KEY:
        with pytest.raises(RuntimeError, match="isolated S1 state"):
            s1.state_guard()
        assert not calls
    else:
        s1.state_guard()
        assert calls == [("workspace", "show")]


def test_ISO_2_direct_ip_bypasses_proxy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        s1.subprocess, "check_output", lambda args, **kw: calls.append(args) or "8.8.8.8\n"
    )
    assert s1.direct_cidr() == "8.8.8.8/32"
    assert calls[0][1:3] == ["--noproxy", "*"]


def test_LC_11_teardown_keeps_network_until_pods_are_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(s1, "STATE", tmp_path)
    monkeypatch.setenv("S1_RUN_APPROVED", "1")
    monkeypatch.setattr(s1, "state_guard", lambda: None)
    monkeypatch.setattr(
        s1, "terraform", lambda *a, **kw: pytest.fail("Destroyed infrastructure while Pod remained")
    )

    def aws(*args):
        if args[:2] == ("eks", "list-clusters"):
            return {"clusters": [s1.CLUSTER]}
        return {
            "cluster": {
                "tags": {"lab": "agent-runtime", "experiment": "s1"},
                "arn": "synthetic",
                "endpoint": "synthetic",
                "certificateAuthority": {"data": "synthetic"},
            }
        }

    monkeypatch.setattr(s1, "aws_json", aws)

    def kubectl(config, *args):
        if args[:2] == ("get", "namespace"):
            return json.dumps({"metadata": {"labels": {"lab.agent-runtime/owned": "true"}}})
        return json.dumps({"items": [{"metadata": {"name": "exec-synthetic"}}]})

    monkeypatch.setattr(s1, "kubectl", kubectl)
    with pytest.raises(RuntimeError, match="Pods remain"):
        s1.down()


def test_ISO_1_operator_context_is_not_uploaded(monkeypatch, tmp_path):
    import base64
    import io
    import shlex
    import tarfile

    monkeypatch.setattr(s1, "STATE", tmp_path / ".local/s1")
    monkeypatch.setattr(s1, "ROOT", tmp_path)
    s1.STATE.mkdir(parents=True)
    (tmp_path / "source.py").write_text("# synthetic source")
    for name in ("image.json", "host.json"):
        (s1.STATE / name).write_text("{}")
    config = {
        "cluster_arn": "synthetic-arn",
        "cluster_endpoint": "https://synthetic",
        "cluster_ca": "synthetic-ca",
        "host_instance_id": "i-synthetic",
    }
    path = s1.operator_kubeconfig(config)
    assert path.stat().st_mode & 0o777 == 0o600
    (s1.STATE / "operator-secret").write_text("sentinel-secret")
    monkeypatch.setattr(s1.subprocess, "check_output", lambda *a, **kw: b"source.py\0")
    commands = []
    monkeypatch.setattr(s1, "ssm", lambda instance, parts, **kw: commands.extend(parts))
    s1.upload_source(config)
    payload = b"".join(
        base64.b64decode(shlex.split(c)[2]) for c in commands if c.startswith("printf %s")
    )
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert set(archive.getnames()) == {
            "source.py",
            ".local/s1/image.json",
            ".local/s1/host.json",
        }
    assert b"sentinel-secret" not in payload


def test_ISO_1_operator_kubectl_ignores_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(s1, "STATE", tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy")
    seen = []
    from types import SimpleNamespace

    monkeypatch.setattr(
        s1.subprocess, "run", lambda *a, **kw: seen.append(kw) or SimpleNamespace(stdout="{}")
    )
    s1.kubectl(
        {"cluster_arn": "test", "cluster_endpoint": "https://test", "cluster_ca": "test"},
        "get",
        "pods",
    )
    assert "HTTPS_PROXY" not in seen[0]["env"]


@pytest.mark.parametrize("accepted", [False, True])
def test_ISO_1_inventory_denial_cannot_report_clean(monkeypatch, tmp_path, accepted):
    import subprocess

    monkeypatch.setattr(s1, "STATE", tmp_path)
    (tmp_path / "connection.json").write_text(
        json.dumps({"cluster_oidc_issuer": "https://oidc.eks.us-east-2.amazonaws.com/id/SYNTHETIC"})
    )
    check = {"outcome": "unverified"}
    if accepted:
        check["stderr"] = (
            "(AccessDenied) explicit deny in a service control policy: "
            "arn:aws:organizations::482314592941:policy/o-aq8i87xn93/"
            "service_control_policy/p-5fs30qru"
        )
    monkeypatch.setattr(s1, "oidc_evidence", lambda issuer: check)
    calls = []
    keys = {
        "get-resources": "ResourceTagMappingList",
        "describe-vpcs": "Vpcs",
        "describe-subnets": "Subnets",
        "describe-route-tables": "RouteTables",
        "describe-security-groups": "SecurityGroups",
        "describe-network-interfaces": "NetworkInterfaces",
        "describe-vpc-endpoints": "VpcEndpoints",
        "describe-addresses": "Addresses",
        "describe-volumes": "Volumes",
        "describe-snapshots": "Snapshots",
        "describe-dhcp-options": "DhcpOptions",
        "describe-vpc-endpoint-service-configurations": "ServiceConfigurations",
        "describe-internet-gateways": "InternetGateways",
        "describe-nat-gateways": "NatGateways",
        "describe-instances": "Reservations",
        "list-clusters": "clusters",
        "describe-load-balancers": "LoadBalancers",
        "describe-target-groups": "TargetGroups",
        "list-firewall-rule-groups": "FirewallRuleGroups",
        "list-firewall-domain-lists": "FirewallDomainLists",
        "list-firewall-rule-group-associations": "FirewallRuleGroupAssociations",
        "list-resolver-query-log-configs": "ResolverQueryLogConfigs",
        "list-resolver-query-log-config-associations": "ResolverQueryLogConfigAssociations",
        "describe-log-groups": "logGroups",
        "describe-resource-policies": "resourcePolicies",
        "list-roles": "Roles",
        "list-instance-profiles": "InstanceProfiles",
        "list-buckets": "Buckets",
        "describe-repositories": "repositories",
    }

    def aws(*args):
        calls.append(args)
        if args[1] == "get-caller-identity":
            return {"Account": s1.ACCOUNT}
        if args[1] == "list-open-id-connect-providers":
            raise subprocess.CalledProcessError(254, "aws")
        return {keys[args[1]]: []}

    monkeypatch.setattr(s1, "aws_json", aws)
    if accepted:
        s1.leftovers()
        assert json.loads((tmp_path / "oidc-evidence.json").read_text()) == check
    else:
        with pytest.raises(RuntimeError, match="incomplete checks"):
            s1.leftovers()
    evidence = json.loads((tmp_path / "leftovers.json").read_text())
    assert evidence == ([] if accepted else [{"kind": "inventory-error", "value": check}])
    assert calls[-1][:2] == ("ecr", "describe-repositories")


@pytest.mark.parametrize(
    "code,error,outcome",
    [
        (254, "An error occurred (NoSuchEntity) when calling GetOpenIDConnectProvider", "absent"),
        (
            254,
            "An error occurred (AccessDenied) when calling GetOpenIDConnectProvider",
            "unverified",
        ),
        (0, "", "present"),
    ],
)
def test_ISO_1_exact_issuer_oidc_evidence(monkeypatch, code, error, outcome):
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=code, stdout="{}", stderr=error)

    monkeypatch.setattr(s1.subprocess, "run", run)
    result = s1.oidc_evidence("https://oidc.eks.us-east-2.amazonaws.com/id/SYNTHETIC")
    assert result["outcome"] == outcome
    assert (
        result["arn"]
        == f"arn:aws:iam::{s1.ACCOUNT}:oidc-provider/oidc.eks.us-east-2.amazonaws.com/id/SYNTHETIC"
    )
    assert "get-open-id-connect-provider" in calls[0]
    assert "list-open-id-connect-providers" not in calls[0]


def test_ISO_1_tag_index_requires_authoritative_absence(monkeypatch, tmp_path):
    monkeypatch.setattr(s1, "STATE", tmp_path)
    prefix = f"arn:aws:ec2:{s1.REGION}:{s1.ACCOUNT}:"
    entries = [
        {"ResourceARN": prefix + suffix}
        for suffix in (
            "security-group/sg-gone",
            "security-group/sg-live",
            "instance/i-terminated",
            "unknown/keep",
        )
    ]

    def describe(*args):
        if args[1] == "describe-instances":
            return {"Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]}
        return {"SecurityGroups": [{}] if args[-1].endswith("sg-live") else []}

    monkeypatch.setattr(s1, "aws_json", describe)
    assert s1.live_tagged_resources(entries) == [entries[1], entries[3]]
    assert json.loads((tmp_path / "retired-tagged-resources.json").read_text()) == [
        entries[0],
        entries[2],
    ]
    monkeypatch.setattr(
        s1, "aws_json", lambda *a: (_ for _ in ()).throw(RuntimeError("ExpiredToken"))
    )
    with pytest.raises(RuntimeError, match="ExpiredToken"):
        s1.live_tagged_resources(entries)


@pytest.mark.parametrize(
    "arn",
    [
        s1.OPERATOR,
        f"arn:aws:iam::{s1.ACCOUNT}:user/lab-operator-cli-extra",
        f"arn:aws:iam::{s1.ACCOUNT}:root",
        f"arn:aws:sts::{s1.ACCOUNT}:assumed-role/AccountFullAccessRole/synthetic",
        f"arn:aws:sts::{s1.ACCOUNT}:assumed-role/lab-s1-controller/synthetic",
        f"arn:aws:sts::{s1.ACCOUNT}:assumed-role/s1-harness/synthetic",
        "arn:aws:iam::000000000000:user/lab-operator-cli",
    ],
)
def test_ISO_1_operator_identity_is_exact(monkeypatch, tmp_path, arn):
    monkeypatch.setattr(s1, "TF", tmp_path)
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform/terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {
                    "type": "s3",
                    "config": {"bucket": s1.BUCKET, "key": s1.KEY, "region": s1.REGION},
                }
            }
        )
    )
    monkeypatch.setattr(s1, "terraform", lambda *a, **kw: "default")
    monkeypatch.setattr(s1, "aws_json", lambda *a: {"Account": s1.ACCOUNT, "Arn": arn})
    if arn == s1.OPERATOR:
        s1.state_guard()
    else:
        with pytest.raises(RuntimeError, match="Expected the S1 operator"):
            s1.state_guard()


@pytest.mark.parametrize(
    "fixture_request", [None, {"action": "restore-workload-controller"}, {"action": "grant-admin"}]
)
def test_ISO_1_operator_restores_only_fixed_fixture(monkeypatch, fixture_request):
    from scripts.s1_kubernetes import controller_binding

    calls = []

    def kubectl(config, *args, **kwargs):
        calls.append((args, kwargs))
        return "" if fixture_request is None else json.dumps({"data": fixture_request})

    monkeypatch.setattr(s1, "kubectl", kubectl)
    if fixture_request and fixture_request["action"] != "restore-workload-controller":
        with pytest.raises(RuntimeError, match="Unexpected harness fixture request"):
            s1.restore_fixture_binding({})
        assert len(calls) == 1
    else:
        s1.restore_fixture_binding({})
        assert len(calls) == (1 if fixture_request is None else 3)
        if fixture_request:
            assert calls[1][1] == {"manifest": controller_binding()}
            assert calls[2][0][:3] == ("delete", "configmap", "s1-restore-controller-binding")
