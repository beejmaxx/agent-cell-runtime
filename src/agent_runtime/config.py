import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    mw_base_url: str = "http://127.0.0.1:18080"

    @classmethod
    def from_env(cls):
        return cls(os.environ["DATABASE_URL"], os.getenv("MW_BASE_URL", cls.mw_base_url))
