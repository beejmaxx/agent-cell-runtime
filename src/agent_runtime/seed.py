from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.dialects.postgresql import insert

from agent_runtime.config import Settings
from agent_runtime.db import Database

SCOPES = {
    "read_worker": "staffing",
    "read_compensation": "compensation",
    "request_time_off": "absence",
    "approve_time_off": "absence",
    "read_document": "documents",
}


def seed(db, base_url):
    with db.engine.begin() as conn:
        for slug in ("acme", "globex"):
            tenant_id = uuid5(NAMESPACE_URL, f"agent-runtime/{slug}")
            conn.execute(
                insert(db.tenants)
                .values(
                    id=tenant_id,
                    slug=slug,
                    mw_issuer=f"https://{slug}.mockworkday.local",
                    mw_base_url=base_url,
                    mw_host=f"{slug}.mockworkday.local",
                )
                .on_conflict_do_update(index_elements=["id"], set_={"mw_base_url": base_url})
            )
            conn.execute(
                insert(db.agents)
                .values(
                    tenant_id=tenant_id,
                    name="hr-assistant",
                    client_id="hr-assistant",
                    operations=list(SCOPES),
                )
                .on_conflict_do_nothing()
            )


if __name__ == "__main__":
    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.initialize()
    seed(db, settings.mw_base_url)
    db.engine.dispose()
