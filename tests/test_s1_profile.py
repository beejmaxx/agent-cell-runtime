import base64
import json
from dataclasses import replace

import pytest
from test_kubernetes_backend import spec as spec_fixture

from agent_runtime.config import Settings
from agent_runtime.pod import pod_manifest
from scripts import k8s
from scripts.s1_host import harness_exec
from scripts.s1_image import digest_image

spec = spec_fixture


def test_ISO_5_eks_template_changes_only_substrate_fields(spec):
    local = Settings("unused")
    eks = replace(local, k8s_profile="eks", agent_image=digest_image("sha256:" + "a" * 64))
    manifest = pod_manifest("exec-" + spec.execution_id, spec, eks)
    container = manifest["spec"]["containers"][0]
    assert container["image"] == eks.agent_image
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["resources"]["requests"]["cpu"] == container["resources"]["limits"]["cpu"]
    assert (
        container["resources"]["requests"]["memory"] == container["resources"]["limits"]["memory"]
    )
    assert json.loads(base64.b64decode(container["env"][1]["value"])) == spec.input
    expected = pod_manifest("exec-" + spec.execution_id, spec, local)
    container["image"] = local.agent_image
    container["imagePullPolicy"] = "Never"
    container["resources"]["requests"].update(cpu="50m", memory="64Mi")
    assert manifest == expected


@pytest.mark.parametrize(
    "bad",
    [None, "context", "arn", "endpoint", "ca", "insecure", "proxy", "exec_env", "cluster_info"],
)
def test_ISO_1_eks_guard_checks_cluster_identity(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(k8s, "PROFILE", "eks")
    monkeypatch.setattr(k8s, "STATE", tmp_path)
    expected = {
        "arn": k8s.EKS_ARN,
        "endpoint": "https://synthetic.eks.amazonaws.com",
        "certificateAuthority": {"data": "synthetic-ca"},
    }
    (tmp_path / "cluster.json").write_text(json.dumps(expected))
    cluster = {"server": expected["endpoint"], "certificate-authority-data": "synthetic-ca"}
    config = {
        "clusters": [{"name": k8s.EKS_ARN, "cluster": cluster}],
        "users": [{"name": "s1-harness", "user": {"exec": harness_exec()}}],
    }
    if bad == "arn":
        config["clusters"][0]["name"] = "another-cluster"
    elif bad == "endpoint":
        cluster["server"] = "https://other.eks.amazonaws.com"
    elif bad == "ca":
        cluster["certificate-authority-data"] = "other-ca"
    elif bad == "insecure":
        cluster["insecure-skip-tls-verify"] = True
    elif bad == "proxy":
        cluster["proxy-url"] = "http://proxy"
    elif bad == "exec_env":
        config["users"][0]["user"]["exec"]["env"] = [{"name": "AWS_PROFILE", "value": "other"}]
    elif bad == "cluster_info":
        config["users"][0]["user"]["exec"]["provideClusterInfo"] = True

    def kubectl(*args):
        if args == ("config", "current-context"):
            return "other" if bad == "context" else k8s.EKS_ARN
        return json.dumps(config)

    monkeypatch.setattr(k8s, "kubectl", kubectl)
    if bad:
        with pytest.raises(RuntimeError):
            k8s.guard()
    else:
        assert k8s.guard() == cluster


def test_ISO_1_colima_guard_still_rejects_remote_cluster(monkeypatch):
    monkeypatch.setattr(k8s, "PROFILE", "colima")

    def kubectl(*args):
        if args == ("config", "current-context"):
            return "colima"
        return json.dumps({"clusters": [{"cluster": {"server": "https://remote"}}]})

    monkeypatch.setattr(k8s, "kubectl", kubectl)
    with pytest.raises(RuntimeError, match="non-loopback"):
        k8s.guard()


@pytest.mark.parametrize("mismatch", [None, "account", "tags"])
def test_ISO_5_image_publish_checks_owner_tags_architecture_and_digest(
    monkeypatch, tmp_path, mismatch
):
    from scripts import s1_image

    monkeypatch.setattr(s1_image, "ROOT", tmp_path)
    commands = []
    digest = "sha256:" + "a" * 64

    def aws(*args):
        commands.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return json.dumps({"Account": "wrong" if mismatch == "account" else s1_image.ACCOUNT})
        if args[:2] == ("ecr", "describe-repositories"):
            return json.dumps(
                {
                    "repositories": [
                        {
                            "repositoryArn": "synthetic-arn",
                            "repositoryUri": f"{s1_image.REGISTRY}/{s1_image.REPOSITORY}",
                        }
                    ]
                }
            )
        if args[:2] == ("ecr", "list-tags-for-resource"):
            return json.dumps(
                {
                    "tags": []
                    if mismatch == "tags"
                    else [
                        {"Key": "lab", "Value": "agent-runtime"},
                        {"Key": "experiment", "Value": "s1"},
                    ]
                }
            )
        if args[:2] == ("ecr", "get-login-password"):
            return "synthetic-secret"
        if args[:2] == ("ecr", "describe-images"):
            return json.dumps({"imageDetails": [{"imageDigest": digest}]})
        raise AssertionError(args)

    def output(args, **kwargs):
        commands.append(args)
        if args[:3] == ["docker", "context", "inspect"]:
            return "unix:///synthetic.sock"
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": "synthetic-id",
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": {"Env": ["PATH=/bin"]},
                    }
                ]
            )
        raise AssertionError(args)

    def run(args, **kwargs):
        commands.append(args)
        assert kwargs["env"]["DOCKER_HOST"]
        if args[1] == "login":
            assert kwargs["input"] == "synthetic-secret" and "--password-stdin" in args

    monkeypatch.setattr(s1_image, "aws", aws)
    monkeypatch.setattr(s1_image.subprocess, "check_output", output)
    monkeypatch.setattr(s1_image.subprocess, "run", run)
    if mismatch:
        with pytest.raises(RuntimeError):
            s1_image.publish()
        assert not any(c[0] == "docker" for c in commands)
    else:
        s1_image.publish()
        record = json.loads((tmp_path / ".local/s1/image.json").read_text())
        assert record["image"] == digest_image(digest)
        assert record["env_names"] == ["PATH"]
        assert any(c[:4] == ["docker", "build", "--platform", "linux/amd64"] for c in commands)
        assert "synthetic-secret" not in json.dumps(record)


def test_ISO_5_digest_required_for_eks():
    image = digest_image("sha256:" + "a" * 64)
    assert Settings("unused", k8s_profile="eks", agent_image=image).agent_image == image
    for bad in (
        image.replace("@sha256:", ":"),
        image.replace("us-east-2", "us-east-1"),
        image.replace("fake-agent", "mock-workday"),
        image[:-1],
    ):
        with pytest.raises(ValueError):
            Settings("unused", k8s_profile="eks", agent_image=bad)
    with pytest.raises(ValueError):
        digest_image("latest")


def test_ISO_1_harness_trust_and_namespace_scope():
    from pathlib import Path

    data = json.loads(Path("infra/experiments/fargate/harness.tf.json").read_text())["resource"]
    role = data["aws_iam_role"]["harness"]
    trust = json.loads(role["assume_role_policy"])["Statement"]
    assert trust == [
        {
            "Effect": "Allow",
            "Principal": {"AWS": "${aws_iam_role.controller.arn}"},
            "Action": "sts:AssumeRole",
        }
    ]
    policy = data["aws_eks_access_policy_association"]["harness"]
    assert policy["policy_arn"].endswith("/AmazonEKSAdminPolicy")
    assert policy["access_scope"] == {
        "type": "namespace",
        "namespaces": ["agent-exec", "agent-exec-psa-control"],
    }
    assume = json.loads(data["aws_iam_role_policy"]["assume_harness"]["policy"])["Statement"]
    assert assume == [
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": "${aws_iam_role.harness.arn}"}
    ]


@pytest.mark.parametrize("psa_label", [False, True])
def test_ISO_5_eks_control_never_mutates_namespace(monkeypatch, psa_label):
    import k8s_support as support

    from scripts.s1_kubernetes import CONTROL_NAMESPACE, MARKER, manifests

    monkeypatch.setattr(support, "PROFILE", "eks")
    calls = []
    labels = {MARKER: "true"}
    if psa_label:
        labels["pod-security.kubernetes.io/enforce"] = "restricted"

    def kubectl(*args):
        calls.append(args)
        return json.dumps({"metadata": {"labels": labels}})

    monkeypatch.setattr(support, "kubectl", kubectl)
    if psa_label:
        with pytest.raises(RuntimeError, match="unlabeled"), support.psa_control_namespace():
            pytest.fail("Labeled namespace was accepted")
    else:
        with (
            pytest.raises(ValueError, match="test failure"),
            support.psa_control_namespace() as namespace,
        ):
            assert namespace == CONTROL_NAMESPACE
            raise ValueError("test failure")
    assert all(call[0] == "get" for call in calls)
    setup = manifests("sg-synthetic")["items"]
    control = next(
        m for m in setup if m["kind"] == "Namespace" and m["metadata"]["name"] == CONTROL_NAMESPACE
    )
    assert not any(k.startswith("pod-security.") for k in control["metadata"]["labels"])
    assert any(
        m["kind"] == "ServiceAccount" and m["metadata"].get("namespace") == CONTROL_NAMESPACE
        for m in setup
    )


def test_LC_7_harness_requests_operator_restore(monkeypatch):
    import k8s_support as support

    from scripts.s1_kubernetes import controller_binding

    monkeypatch.setattr(support, "PROFILE", "eks")
    calls = []

    def kubectl(*args):
        calls.append(args)
        return json.dumps(controller_binding()) if args[0] == "get" else "created"

    monkeypatch.setattr(support, "kubectl", kubectl)
    support.restore_controller_binding()
    assert calls[0][:3] == ("create", "configmap", "s1-restore-controller-binding")
    assert calls[1][:3] == ("get", "rolebinding", "workload-controller")
    assert all(call[0] != "apply" for call in calls)


@pytest.mark.parametrize("role", ["lab-s1-controller", "s1-harness", "AccountFullAccessRole"])
def test_ISO_1_controller_tokens_never_assume_harness(monkeypatch, tmp_path, role):
    from scripts import s1_host

    calls = []

    def aws(*args):
        calls.append(args)
        if args[0] == "sts":
            return json.dumps(
                {
                    "Account": "729608197929",
                    "Arn": f"arn:aws:sts::729608197929:assumed-role/{role}/synthetic",
                }
            )
        return json.dumps({"status": {"token": "synthetic-token"}})

    monkeypatch.setattr(s1_host, "aws", aws)
    if role != "lab-s1-controller":
        with pytest.raises(RuntimeError, match="trusted host"):
            s1_host.refresh_token(tmp_path)
        assert not (tmp_path / "controller.token").exists()
    else:
        s1_host.refresh_token(tmp_path)
        assert (tmp_path / "controller.token").read_text() == "synthetic-token\n"
        assert (tmp_path / "controller.token").stat().st_mode & 0o777 == 0o600
        assert "--role-arn" not in calls[-1]
    assert s1_host.HARNESS_ROLE in s1_host.harness_exec()["args"]
