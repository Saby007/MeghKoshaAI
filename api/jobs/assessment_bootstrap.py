"""Explicit, private-network-only database bootstrap under a dedicated Entra admin MI."""

import asyncio
import os
from pathlib import Path
from uuid import UUID

from services import assessment_store

RUNTIME_GRANTS = (
    'REVOKE ALL ON DATABASE meghkosha FROM PUBLIC;'
    'REVOKE CREATE ON SCHEMA public FROM PUBLIC;'
    'GRANT CONNECT ON DATABASE meghkosha TO meghkosha_api,meghkosha_worker;'
    'GRANT USAGE ON SCHEMA assessments TO meghkosha_api,meghkosha_worker;'
    'GRANT SELECT ON ALL TABLES IN SCHEMA assessments TO meghkosha_api;'
    'GRANT INSERT ON assessments.jobs TO meghkosha_api;'
    'GRANT UPDATE (status,phase,completed_at) ON assessments.jobs TO meghkosha_api;'
    'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA assessments TO meghkosha_worker;'
)


async def run() -> None:
    roles = {
        "meghkosha_api": str(UUID(os.environ["ASSESSMENT_API_PRINCIPAL_ID"])),
        "meghkosha_worker": str(UUID(os.environ["ASSESSMENT_WORKER_PRINCIPAL_ID"])),
    }
    try:
        db = await assessment_store.pool()
        principals = await db.fetch("SELECT * FROM pg_catalog.pgaadauth_list_principals(false)")
        existing = {row["rolename"]: {key.lower(): value for key, value in dict(row).items()}
                    for row in principals}
        for role, object_id in roles.items():
            if role in existing:
                principal = existing[role]
                if (str(principal["objectid"]).lower() != object_id
                        or principal["principaltype"] != "service" or principal["isadmin"]):
                    raise RuntimeError(f"Existing database role {role} has a different identity or elevated privileges.")
            else:
                await db.execute(
                    "SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid($1,$2,'service',false,false)",
                    role, object_id,
                )
    finally:
        await assessment_store.close()

    os.environ["ASSESSMENT_DB_NAME"] = os.environ["ASSESSMENT_TARGET_DB_NAME"]
    database = os.environ["ASSESSMENT_DB_NAME"]
    if database != "meghkosha":
        raise ValueError("Bootstrap target must be the deployment's meghkosha database.")
    try:
        db = await assessment_store.pool()
        async with db.acquire(timeout=10) as connection, connection.transaction():
            await connection.execute(
                (Path(__file__).parent.parent / "services" / "assessment_schema.sql").read_text(encoding="utf-8")
            )
            await connection.execute(RUNTIME_GRANTS)
    finally:
        await assessment_store.close()


if __name__ == "__main__":
    asyncio.run(run())
