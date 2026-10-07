"""S1 operator workflow. Never reads or mutates another Terraform state."""

import base64
import hashlib
import io
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from scripts.s1_image import ACCOUNT, REGION, aws, publish
from scripts.s1_kubernetes import manifests

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra/experiments/fargate"
STATE = ROOT / ".local/s1"
BUCKET = "beejmaxx-lab-tfstate-dev"
KEY = "dev/experiments/s1-fargate.tfstate"
CLUSTER = "lab-exec-s1"
OPERATOR = f"arn:aws:iam::{ACCOUNT}:role/managed/AccountFullAccessRole"


def aws_json(*args):
    return json.loads(aws(*args, "--output", "json"))


def terraform(*args, capture=False):
    command = ["terraform", f"-chdir={TF}", *args]
    if capture:
        return subprocess.check_output(command, text=True)
    subprocess.run(command, check=True)


def run_approved():
    if os.getenv("S1_RUN_APPROVED") != "1":
        raise RuntimeError(
            "Checkpoint 4 requires reviewer approval; set S1_RUN_APPROVED=1 only after approval"
        )


def state_guard():
    backend = json.loads((TF / ".terraform/terraform.tfstate").read_text())["backend"]
    config = backend["config"]
    if backend["type"] != "s3" or (config["bucket"], config["key"], config["region"]) != (
        BUCKET,
        KEY,
        REGION,
    ):
        raise RuntimeError("Refusing a backend other than the isolated S1 state")
    if terraform("workspace", "show", capture=True).strip() != "default":
        raise RuntimeError("S1 uses only the default workspace")
    caller = aws_json("sts", "get-caller-identity")
    if caller["Account"] != ACCOUNT or not caller["Arn"].startswith(
        f"arn:aws:sts::{ACCOUNT}:assumed-role/AccountFullAccessRole/"
    ):
        raise RuntimeError("Expected the S1 operator, never the host or execution role")


def direct_cidr():
    value = subprocess.check_output(
        ["curl", "--noproxy", "*", "-fsS", "--max-time", "10", "https://checkip.amazonaws.com"],
        text=True,
    ).strip()
    address = ipaddress.IPv4Address(value)
    if not address.is_global:
        raise ValueError("Expected a public direct-egress IPv4 address")
    return f"{address}/32"


def source_digest():
    paths = [
        ROOT / "Makefile",
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        TF / ".terraform.lock.hcl",
    ]
    for directory, pattern in (
        (TF, "*.tf*"),
        (TF, "*.sh"),
        (ROOT / "scripts", "*.py"),
        (ROOT / "agent", "*"),
        (ROOT / "src", "*"),
        (ROOT / "tests", "*.py"),
    ):
        paths += [
            p
            for p in directory.rglob(pattern)
            if p.is_file() and ".terraform" not in p.parts and "__pycache__" not in p.parts
        ]
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def plan():
    STATE.mkdir(parents=True, exist_ok=True)
    choice = os.getenv("S1_STS_GET_CALLER_IDENTITY_ONLY")
    if choice not in {"true", "false"}:
        raise RuntimeError("Set S1_STS_GET_CALLER_IDENTITY_ONLY to the reviewer-selected baseline")
    inputs = {"operator_cidr": direct_cidr(), "sts_get_caller_identity_only": choice == "true"}
    (STATE / "inputs.tfvars.json").write_text(json.dumps(inputs, indent=2))
    terraform("init", "-input=false")
    state_guard()
    terraform("fmt", "-check")
    terraform("validate")
    terraform(
        "plan",
        "-input=false",
        "-lock=false",
        f"-var-file={STATE / 'inputs.tfvars.json'}",
        f"-out={STATE / 'plan.tfplan'}",
    )
    record_plan()


def record_plan():
    (STATE / "plan-review.json").write_text(
        json.dumps(
            {
                "source_digest": source_digest(),
                "plan_digest": hashlib.sha256((STATE / "plan.tfplan").read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )


def connection():
    return json.loads((STATE / "connection.json").read_text())


def operator_kubeconfig(config):
    path = STATE / "operator-kubeconfig.json"
    value = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": config["cluster_arn"],
        "clusters": [
            {
                "name": config["cluster_arn"],
                "cluster": {
                    "server": config["cluster_endpoint"],
                    "certificate-authority-data": config["cluster_ca"],
                },
            }
        ],
        "contexts": [
            {
                "name": config["cluster_arn"],
                "context": {"cluster": config["cluster_arn"], "user": "operator"},
            }
        ],
        "users": [
            {
                "name": "operator",
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "aws",
                        "args": [
                            "--profile",
                            "agent-runtime",
                            "--region",
                            REGION,
                            "eks",
                            "get-token",
                            "--cluster-name",
                            CLUSTER,
                        ],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def kubectl(config, *args, manifest=None):
    command = [
        str(ROOT / ".local/bin/kubectl"),
        "--kubeconfig",
        str(operator_kubeconfig(config)),
        "--context",
        config["cluster_arn"],
        "--request-timeout=30s",
        *args,
    ]
    return subprocess.run(
        command,
        input=json.dumps(manifest) if manifest else None,
        text=True,
        capture_output=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        },
        check=True,
    ).stdout


def ssm(instance, commands, timeout=900):
    sent = aws_json(
        "ssm",
        "send-command",
        "--instance-ids",
        instance,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps({"commands": ["set -eu", *commands], "executionTimeout": [str(timeout)]}),
    )
    command_id = sent["Command"]["CommandId"]
    until = time.monotonic() + timeout + 60
    while time.monotonic() < until:
        time.sleep(2)
        # list-command-invocations is empty until SSM has delivered the command.
        items = aws_json(
            "ssm", "list-command-invocations", "--command-id", command_id, "--details"
        )["CommandInvocations"]
        if not items or items[0]["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        result = aws_json(
            "ssm", "get-command-invocation", "--command-id", command_id, "--instance-id", instance
        )
        if result["Status"] != "Success":
            raise RuntimeError(
                f"SSM command {command_id}: {result['Status']}; inspect its retained output"
            )
        return result["StandardOutputContent"]
    raise TimeoutError(f"SSM command {command_id} exceeded its deadline")


def wait_host(instance):
    until = time.monotonic() + 600
    while time.monotonic() < until:
        values = aws_json(
            "ssm",
            "describe-instance-information",
            "--filters",
            json.dumps([{"Key": "InstanceIds", "Values": [instance]}]),
        )["InstanceInformationList"]
        if values and values[0]["PingStatus"] == "Online":
            ssm(
                instance,
                [
                    "for n in $(seq 1 60); do test ! -f /var/lib/s1-ready || exit 0; sleep 5; done; exit 1"
                ],
                timeout=330,
            )
            return
        time.sleep(5)
    raise TimeoutError("Trusted host did not register with SSM")


def upload_source(config):
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in files:
            if name:
                archive.add(ROOT / name, arcname=name, recursive=False)
        for name in ("image.json", "host.json"):
            archive.add(STATE / name, arcname=f".local/s1/{name}")
    instance = config["host_instance_id"]
    ssm(
        instance,
        ["install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/s1", ": > /home/ubuntu/s1/source.tgz"],
    )
    payload = buffer.getvalue()
    for offset in range(0, len(payload), 18000):
        encoded = base64.b64encode(payload[offset : offset + 18000]).decode()
        ssm(
            instance,
            [f"printf %s {shlex.quote(encoded)} | base64 -d >> /home/ubuntu/s1/source.tgz"],
        )
    ssm(
        instance,
        [
            "tar xzf /home/ubuntu/s1/source.tgz -C /home/ubuntu/s1",
            "rm /home/ubuntu/s1/source.tgz",
            "chown -R ubuntu:ubuntu /home/ubuntu/s1",
            "runuser -u ubuntu -- sh -c 'cd /home/ubuntu/s1; uv sync --locked; K8S_PROFILE=eks uv run python -m scripts.k8s tools; uv run python -m scripts.s1_host'",
        ],
        timeout=600,
    )


def up():
    run_approved()
    state_guard()
    review = json.loads((STATE / "plan-review.json").read_text())
    if (
        review["source_digest"] != source_digest()
        or review["plan_digest"] != hashlib.sha256((STATE / "plan.tfplan").read_bytes()).hexdigest()
    ):
        raise RuntimeError("Source or saved plan changed since review; create a new reviewed plan")
    if direct_cidr() != json.loads((STATE / "inputs.tfvars.json").read_text())["operator_cidr"]:
        raise RuntimeError("Direct egress IP changed; re-plan before applying")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    terraform("apply", "-input=false", str(STATE / "plan.tfplan"))
    config = json.loads(terraform("output", "-json", "connection", capture=True))
    (STATE / "connection.json").write_text(json.dumps(config, indent=2))
    # A second inventory survives loss of Terraform outputs during partial teardown.
    (STATE / "resource-inventory.json").write_text(terraform("show", "-json", capture=True))
    eni = aws_json(
        "ec2",
        "describe-network-interfaces",
        "--network-interface-ids",
        *config["gateway_endpoint_enis"],
    )["NetworkInterfaces"]
    if len(eni) != 1:
        raise RuntimeError("S1 expects one callback endpoint ENI in 2a")
    (STATE / "host.json").write_text(
        json.dumps({"completion_url": f"http://{eni[0]['PrivateIpAddress']}:8001"})
    )
    from scripts import k8s

    k8s.VERSION = "v1.36.0"
    k8s.tools()
    kubectl(config, "apply", "-f", "-", manifest=manifests(config["pod_security_group_id"]))
    publish()
    wait_host(config["host_instance_id"])
    upload_source(config)
    print(
        "S1 ready. Run make s1-test for E1; E2-E10 remain explicit experiments. End with make s1-down."
    )


def test():
    run_approved()
    state_guard()
    config = connection()
    # Save the exit code before export, so failed E1 evidence remains available.
    command = "cd /home/ubuntu/s1; echo $$ > .local/s1/run.pid; K8S_PROFILE=eks uv run python -m scripts.k8s_preflight > .local/s1/e1.log 2>&1 && K8S_PROFILE=eks uv run pytest -m k8s -p scripts.s1_evidence --tb=short >> .local/s1/e1.log 2>&1; status=$?; rm -f .local/s1/run.pid; echo $status > .local/s1/e1.exit; exit 0"
    ssm(
        config["host_instance_id"],
        ["runuser -u ubuntu -- setsid sh -c " + shlex.quote(command)],
        timeout=7200,
    )
    export_evidence(config)
    result = ssm(config["host_instance_id"], ["cat /home/ubuntu/s1/.local/s1/e1.exit"]).strip()
    if result != "0":
        raise RuntimeError("E1 failed; inspect the exported e1.log and database/Pod evidence")


def export_evidence(config):
    instance = config["host_instance_id"]
    ssm(
        instance,
        [
            "cd /home/ubuntu/s1; mkdir -p .local/s1/evidence .local/s1/preflight; tar czf /tmp/s1-evidence.tgz .local/s1/evidence .local/s1/preflight $(test ! -f .local/s1/e1.log || printf '.local/s1/e1.log')"
        ],
    )
    size = int(ssm(instance, ["stat -c %s /tmp/s1-evidence.tgz"]).strip())
    with (STATE / "host-evidence.tgz").open("wb") as output:
        for index in range((size + 11999) // 12000):
            data = ssm(
                instance,
                [
                    f"dd if=/tmp/s1-evidence.tgz bs=12000 skip={index} count=1 status=none | base64 -w0"
                ],
            )
            output.write(base64.b64decode(data))
    if (STATE / "host-evidence.tgz").stat().st_size != size:
        raise RuntimeError("Evidence transfer was incomplete")
    logs = aws_json("logs", "filter-log-events", "--log-group-name", config["dns_log_group"])
    (STATE / "dns-query-events.json").write_text(json.dumps(logs, indent=2))


def down():
    run_approved()
    state_guard()
    config = connection() if (STATE / "connection.json").exists() else None
    clusters = aws_json("eks", "list-clusters")["clusters"]
    if config:
        try:
            ssm(
                config["host_instance_id"],
                [
                    "if test -f /home/ubuntu/s1/.local/s1/run.pid; then /bin/kill -TERM -- -$(cat /home/ubuntu/s1/.local/s1/run.pid); fi"
                ],
            )
            export_evidence(config)
        except (subprocess.SubprocessError, RuntimeError, TimeoutError) as exc:
            print(
                f"Evidence export failed ({type(exc).__name__}); retaining local evidence and continuing cleanup",
                file=sys.stderr,
            )
    if CLUSTER in clusters:
        cluster = aws_json("eks", "describe-cluster", "--name", CLUSTER)["cluster"]
        if (
            not {"lab": "agent-runtime", "experiment": "s1"}.items()
            <= cluster.get("tags", {}).items()
        ):
            raise RuntimeError("Refusing an EKS cluster without S1 ownership tags")
        current = {
            "cluster_arn": cluster["arn"],
            "cluster_endpoint": cluster["endpoint"],
            "cluster_ca": cluster["certificateAuthority"]["data"],
        }
        namespace = kubectl(
            current, "get", "namespace", "agent-exec", "--ignore-not-found", "-o", "json"
        )
        if namespace.strip():
            if (
                json.loads(namespace)["metadata"].get("labels", {}).get("lab.agent-runtime/owned")
                != "true"
            ):
                raise RuntimeError("Refusing to delete unmarked execution Pods")
            kubectl(
                current,
                "delete",
                "pods",
                "--all",
                "-n",
                "agent-exec",
                "--wait=true",
                "--timeout=600s",
            )
            if json.loads(kubectl(current, "get", "pods", "-n", "agent-exec", "-o", "json"))[
                "items"
            ]:
                raise RuntimeError(
                    "Execution Pods remain; keep network and credentials for cleanup"
                )
        profiles = aws_json("eks", "list-fargate-profiles", "--cluster-name", CLUSTER)[
            "fargateProfileNames"
        ]
        for profile in profiles:
            if profile != "agent-exec":
                raise RuntimeError("Unexpected Fargate profile on S1 cluster")
            aws(
                "eks",
                "delete-fargate-profile",
                "--cluster-name",
                CLUSTER,
                "--fargate-profile-name",
                profile,
            )
            aws(
                "eks",
                "wait",
                "fargate-profile-deleted",
                "--cluster-name",
                CLUSTER,
                "--fargate-profile-name",
                profile,
            )
        aws("eks", "delete-cluster", "--name", CLUSTER)
        aws("eks", "wait", "cluster-deleted", "--name", CLUSTER)
    terraform(
        "destroy", "-input=false", "-auto-approve", f"-var-file={STATE / 'inputs.tfvars.json'}"
    )
    leftovers()


def oidc_evidence(issuer):
    from urllib.parse import urlsplit

    parsed = urlsplit(issuer)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "oidc.eks.us-east-2.amazonaws.com"
        or not parsed.path.startswith("/id/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected the recorded S1 EKS issuer")
    arn = f"arn:aws:iam::{ACCOUNT}:oidc-provider/{issuer.removeprefix('https://')}"
    result = subprocess.run(
        [
            "aws",
            "--profile",
            "agent-runtime",
            "--region",
            REGION,
            "--no-cli-pager",
            "iam",
            "get-open-id-connect-provider",
            "--open-id-connect-provider-arn",
            arn,
            "--output",
            "json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        outcome = "present"
    elif "(NoSuchEntity)" in result.stderr:
        outcome = "absent"
    else:
        outcome = "unverified"
    return {
        "operation": "iam:GetOpenIDConnectProvider",
        "arn": arn,
        "outcome": outcome,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def live_tagged_resources(values):
    """The tagging index can retain deleted EC2 resources; verify their IDs."""
    lookups = {
        "security-group-rule": (
            "describe-security-group-rules",
            "security-group-rule-id",
            "SecurityGroupRules",
        ),
        "security-group": ("describe-security-groups", "group-id", "SecurityGroups"),
        "vpc-endpoint-service": (
            "describe-vpc-endpoint-service-configurations",
            "service-id",
            "ServiceConfigurations",
        ),
        "vpc-endpoint": ("describe-vpc-endpoints", "vpc-endpoint-id", "VpcEndpoints"),
        "instance": ("describe-instances", "instance-id", "Reservations"),
        "volume": ("describe-volumes", "volume-id", "Volumes"),
    }
    live, retired = [], []
    for value in values:
        arn = value["ResourceARN"]
        prefix = f"arn:aws:ec2:{REGION}:{ACCOUNT}:"
        kind, _, resource_id = arn.removeprefix(prefix).partition("/")
        if not arn.startswith(prefix) or kind not in lookups:
            live.append(value)
            continue
        operation, field, key = lookups[kind]
        records = aws_json("ec2", operation, "--filters", f"Name={field},Values={resource_id}")[key]
        if kind == "instance":
            records = [
                i for r in records for i in r["Instances"] if i["State"]["Name"] != "terminated"
            ]
        elif kind == "vpc-endpoint":
            records = [r for r in records if r["State"] != "deleted"]
        (live if records else retired).append(value)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "retired-tagged-resources.json").write_text(json.dumps(retired, indent=2))
    return live


def leftovers():
    caller = aws_json("sts", "get-caller-identity")
    if caller["Account"] != ACCOUNT:
        raise RuntimeError("Wrong AWS account")
    found = []

    def add(kind, values):
        for value in values:
            found.append({"kind": kind, "value": value})

    tags = [{"Key": "lab", "Values": ["agent-runtime"]}, {"Key": "experiment", "Values": ["s1"]}]
    add(
        "tagged-resource",
        live_tagged_resources(
            aws_json(
                "resourcegroupstaggingapi", "get-resources", "--tag-filters", json.dumps(tags)
            )["ResourceTagMappingList"]
        ),
    )
    filters = "Name=tag:experiment,Values=s1"
    for operation, key in (
        ("describe-vpcs", "Vpcs"),
        ("describe-subnets", "Subnets"),
        ("describe-route-tables", "RouteTables"),
        ("describe-security-groups", "SecurityGroups"),
        ("describe-network-interfaces", "NetworkInterfaces"),
        ("describe-vpc-endpoints", "VpcEndpoints"),
        ("describe-addresses", "Addresses"),
        ("describe-volumes", "Volumes"),
        ("describe-snapshots", "Snapshots"),
        ("describe-dhcp-options", "DhcpOptions"),
        ("describe-vpc-endpoint-service-configurations", "ServiceConfigurations"),
    ):
        add(operation, aws_json("ec2", operation, "--filters", filters)[key])
    add(
        "internet-gateway",
        aws_json("ec2", "describe-internet-gateways", "--filters", filters)["InternetGateways"],
    )
    add("nat-gateway", aws_json("ec2", "describe-nat-gateways", "--filter", filters)["NatGateways"])
    instances = aws_json("ec2", "describe-instances", "--filters", filters)["Reservations"]
    add(
        "instances",
        [i for r in instances for i in r["Instances"] if i["State"]["Name"] != "terminated"],
    )
    saved = connection() if (STATE / "connection.json").exists() else {}
    if saved.get("execution_vpc_id"):
        add(
            "service-created-eni",
            aws_json(
                "ec2",
                "describe-network-interfaces",
                "--filters",
                f"Name=vpc-id,Values={saved['execution_vpc_id']}",
            )["NetworkInterfaces"],
        )
    for field in ("host_security_group_id", "nlb_security_group_id", "cluster_security_group_id"):
        if saved.get(field):
            add(
                "recorded-security-group",
                aws_json(
                    "ec2",
                    "describe-security-groups",
                    "--filters",
                    f"Name=group-id,Values={saved[field]}",
                )["SecurityGroups"],
            )
            add(
                "recorded-group-eni",
                aws_json(
                    "ec2",
                    "describe-network-interfaces",
                    "--filters",
                    f"Name=group-id,Values={saved[field]}",
                )["NetworkInterfaces"],
            )
    if saved.get("endpoint_service_id"):
        add(
            "endpoint-connection",
            aws_json(
                "ec2",
                "describe-vpc-endpoint-connections",
                "--filters",
                f"Name=service-id,Values={saved['endpoint_service_id']}",
            )["VpcEndpointConnections"],
        )
    add("eks-cluster", [n for n in aws_json("eks", "list-clusters")["clusters"] if n == CLUSTER])
    if any(r["kind"] == "eks-cluster" for r in found):
        add(
            "fargate-profile",
            aws_json("eks", "list-fargate-profiles", "--cluster-name", CLUSTER)[
                "fargateProfileNames"
            ],
        )
        add(
            "access-entry",
            aws_json("eks", "list-access-entries", "--cluster-name", CLUSTER)["accessEntries"],
        )
    lbs = aws_json("elbv2", "describe-load-balancers")["LoadBalancers"]
    for lb in lbs:
        if lb["LoadBalancerName"].startswith("lab-s1-"):
            add("nlb", [lb])
            add(
                "listener",
                aws_json(
                    "elbv2", "describe-listeners", "--load-balancer-arn", lb["LoadBalancerArn"]
                )["Listeners"],
            )
    add(
        "target-group",
        [
            v
            for v in aws_json("elbv2", "describe-target-groups")["TargetGroups"]
            if v["TargetGroupName"].startswith("lab-s1-")
        ],
    )
    for operation, key in (
        ("list-firewall-rule-groups", "FirewallRuleGroups"),
        ("list-firewall-domain-lists", "FirewallDomainLists"),
        ("list-firewall-rule-group-associations", "FirewallRuleGroupAssociations"),
        ("list-resolver-query-log-configs", "ResolverQueryLogConfigs"),
    ):
        add(
            operation,
            [
                v
                for v in aws_json("route53resolver", operation)[key]
                if v.get("Name", "").startswith("lab-s1-")
            ],
        )
    add(
        "query-log-association",
        [
            v
            for v in aws_json("route53resolver", "list-resolver-query-log-config-associations")[
                "ResolverQueryLogConfigAssociations"
            ]
            if v.get("ResourceId") == saved.get("execution_vpc_id")
        ],
    )
    add(
        "log-group",
        aws_json("logs", "describe-log-groups", "--log-group-name-prefix", "/lab/s1/")["logGroups"],
    )
    add(
        "log-resource-policy",
        [
            v
            for v in aws_json("logs", "describe-resource-policies")["resourcePolicies"]
            if v["policyName"] == "lab-s1-dns"
        ],
    )
    add(
        "iam-role",
        [
            v["Arn"]
            for v in aws_json("iam", "list-roles")["Roles"]
            if v["RoleName"].startswith("lab-s1-")
            or v["RoleName"]
            in {"s1-harness", "AWSServiceRoleForAmazonEKS", "AWSServiceRoleForAmazonEKSForFargate"}
        ],
    )
    add(
        "instance-profile",
        [
            v["Arn"]
            for v in aws_json("iam", "list-instance-profiles")["InstanceProfiles"]
            if v["InstanceProfileName"].startswith("lab-s1-")
        ],
    )
    issuer = saved.get("cluster_oidc_issuer")
    if issuer:
        check = oidc_evidence(issuer)
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "oidc-evidence.json").write_text(json.dumps(check, indent=2))
        if check["outcome"] == "present":
            add("oidc-provider", [check])
        elif (
            check["outcome"] == "unverified"
            and "(AccessDenied)" in check.get("stderr", "")
            and "explicit deny in a service control policy" in check["stderr"]
            and "/service_control_policy/p-5fs30qru" in check["stderr"]
        ):
            # Reviewer accepts this visibility gap, not proof of provider absence.
            print(
                "OIDC absence UNVERIFIED: accepted SCP p-5fs30qru exception; see oidc-evidence.json"
            )
        elif check["outcome"] != "absent":
            add("inventory-error", [check])
    add(
        "canary-bucket",
        [
            v["Name"]
            for v in aws_json("s3api", "list-buckets")["Buckets"]
            if v["Name"] == "lab-s1-canary-729608197929-us-east-2"
        ],
    )
    repos = aws_json("ecr", "describe-repositories")["repositories"]
    for repo in repos:
        if repo["repositoryName"] == "agent-runtime/fake-agent":
            add("ecr-repository", [repo])
            add(
                "ecr-image",
                aws_json("ecr", "list-images", "--repository-name", repo["repositoryName"])[
                    "imageIds"
                ],
            )
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "leftovers.json").write_text(json.dumps(found, indent=2))
    print(json.dumps(found, indent=2))
    if found:
        raise RuntimeError(
            "S1 inventory is not clean: resources or incomplete checks; see .local/s1/leftovers.json"
        )


if __name__ == "__main__":
    commands = {"plan": plan, "up": up, "test": test, "down": down, "leftovers": leftovers}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit("Usage: python -m scripts.s1 plan|up|test|down|leftovers")
    commands[sys.argv[1]]()
