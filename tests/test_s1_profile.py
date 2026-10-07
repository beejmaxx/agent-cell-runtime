import base64
import json
from dataclasses import replace

import pytest
from test_kubernetes_backend import spec as spec_fixture

from agent_runtime.config import Settings
from agent_runtime.pod import pod_manifest
from scripts import k8s
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


@pytest.mark.parametrize("bad", [None, "context", "arn", "endpoint", "ca", "insecure", "proxy"])
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
    config = {"clusters": [{"name": k8s.EKS_ARN, "cluster": cluster}]}
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
