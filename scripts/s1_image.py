"""Publish the amd64 probe image to the existing S1 repository (checkpoint 4)."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ACCOUNT = "729608197929"
REGION = "us-east-2"
REPOSITORY = "agent-runtime/fake-agent"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
ROOT = Path(__file__).resolve().parents[1]


def aws(*args):
    return subprocess.check_output(
        ["aws", "--profile", "agent-runtime", "--region", REGION, "--no-cli-pager", *args],
        text=True,
    )


def digest_image(digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("ECR did not return a sha256 image digest")
    return f"{REGISTRY}/{REPOSITORY}@{digest}"


def publish():
    if json.loads(aws("sts", "get-caller-identity"))["Account"] != ACCOUNT:
        raise RuntimeError("Wrong AWS account")
    repo = json.loads(aws("ecr", "describe-repositories", "--repository-names", REPOSITORY))[
        "repositories"
    ][0]
    tags = json.loads(
        aws("ecr", "list-tags-for-resource", "--resource-arn", repo["repositoryArn"])
    )["tags"]
    if not {("lab", "agent-runtime"), ("experiment", "s1")} <= {
        (t["Key"], t["Value"]) for t in tags
    }:
        raise RuntimeError("Refusing an ECR repository without the S1 tags")
    if repo["repositoryUri"] != f"{REGISTRY}/{REPOSITORY}":
        raise RuntimeError("Unexpected ECR repository")
    from uuid import uuid4

    tag = f"{REGISTRY}/{REPOSITORY}:s1-{uuid4().hex}"
    # Keep registry credentials out of the user's persistent Docker configuration.
    with tempfile.TemporaryDirectory(prefix="s1-docker-") as directory:
        endpoint = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True
        ).strip()
        environment = {
            **os.environ,
            "DOCKER_CONFIG": directory,
            "DOCKER_HOST": os.getenv("DOCKER_HOST", endpoint),
        }
        environment.pop("DOCKER_CONTEXT", None)
        docker = ["docker"]
        subprocess.run(
            [*docker, "login", "--username", "AWS", "--password-stdin", REGISTRY],
            input=aws("ecr", "get-login-password"),
            text=True,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [*docker, "build", "--platform", "linux/amd64", "-t", tag, str(ROOT / "agent")],
            env=environment,
            check=True,
        )
        built = json.loads(
            subprocess.check_output([*docker, "image", "inspect", tag], env=environment, text=True)
        )[0]
        if (built["Os"], built["Architecture"]) != ("linux", "amd64"):
            raise RuntimeError("S1 image must be linux/amd64")
        subprocess.run([*docker, "push", tag], env=environment, check=True)
        details = json.loads(
            aws(
                "ecr",
                "describe-images",
                "--repository-name",
                REPOSITORY,
                "--image-ids",
                f"imageTag={tag.rsplit(':', 1)[1]}",
            )
        )["imageDetails"][0]
        image = digest_image(details["imageDigest"])
        # Inspect the pulled digest, not a mutable tag or an unrelated local baseline.
        subprocess.run(
            [*docker, "pull", "--platform", "linux/amd64", image], env=environment, check=True
        )
        pulled = json.loads(
            subprocess.check_output(
                [*docker, "image", "inspect", image], env=environment, text=True
            )
        )[0]
        if pulled["Id"] != built["Id"]:
            raise RuntimeError("Published digest does not match the built image")
    state = ROOT / ".local/s1"
    state.mkdir(parents=True, exist_ok=True)
    (state / "image.json").write_text(
        json.dumps(
            {
                "image": image,
                "architecture": "amd64",
                "os": "linux",
                "env_names": sorted(e.split("=", 1)[0] for e in pulled["Config"]["Env"]),
            },
            indent=2,
        )
        + "\n"
    )
    print(image)


if __name__ == "__main__":
    publish()
