import hashlib
import json
from pathlib import Path

from sqlalchemy import MetaData, create_engine, text


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Database:
    def __init__(self, url):
        self.engine = create_engine(
            url, pool_size=25, json_serializer=lambda v: canonical(v).decode()
        )
        self.metadata = MetaData()

    def initialize(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql(Path(__file__).with_name("schema.sql").read_text())
        self.metadata.reflect(self.engine)
        self.tenants = self.metadata.tables["tenants"]
        self.agents = self.metadata.tables["agents"]
        self.executions = self.metadata.tables["executions"]
        self.idempotency = self.metadata.tables["idempotency_records"]


def advisory_lock(conn, tenant, principal, key):
    value = int.from_bytes(
        hashlib.sha256(canonical([str(tenant), principal, key])).digest()[:8], "big", signed=True
    )
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})
