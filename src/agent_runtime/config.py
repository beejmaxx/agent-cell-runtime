import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    mw_base_url: str = "http://127.0.0.1:18080"

    workload_backend: str = "fake"
    k8s_api_url: str = ""
    k8s_ca_file: str = ""
    k8s_token_file: str = ""
    k8s_namespace: str = "agent-exec"
    agent_image: str = "agent-runtime/fake-agent:r2"
    runtime_url: str = "http://192.168.5.2:8000"
    max_active_executions: int = 3
    k8s_profile: str = "colima"

    def __post_init__(self):
        if self.k8s_profile not in {"colima", "eks"}:
            raise ValueError("K8S_PROFILE must be colima or eks")
        if self.k8s_profile == "eks" and not re.fullmatch(
            r"729608197929\.dkr\.ecr\.us-east-2\.amazonaws\.com/agent-runtime/fake-agent"
            r"@sha256:[0-9a-f]{64}",
            self.agent_image,
        ):
            raise ValueError("S1 requires the experiment ECR repository pinned by sha256 digest")

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ["DATABASE_URL"],
            mw_base_url=os.getenv("MW_BASE_URL", cls.mw_base_url),
            workload_backend=os.getenv("WORKLOAD_BACKEND", cls.workload_backend),
            k8s_api_url=os.getenv("K8S_API_URL", cls.k8s_api_url),
            k8s_ca_file=os.getenv("K8S_CA_FILE", cls.k8s_ca_file),
            k8s_token_file=os.getenv("K8S_TOKEN_FILE", cls.k8s_token_file),
            k8s_namespace=os.getenv("K8S_NAMESPACE", cls.k8s_namespace),
            agent_image=os.getenv("AGENT_IMAGE", cls.agent_image),
            runtime_url=os.getenv("RUNTIME_URL", cls.runtime_url),
            k8s_profile=os.getenv("K8S_PROFILE", cls.k8s_profile),
            max_active_executions=int(
                os.getenv("MAX_ACTIVE_EXECUTIONS", cls.max_active_executions)
            ),
        )
