"""Short-lived SQL operations for versioned control records and fenced leases."""

import json
from uuid import UUID

from services import assessment_store


async def read(tenant: str, namespace: str, key: str):
    return await (await assessment_store.pool()).fetchrow(
        "SELECT payload,version FROM assessments.control_state WHERE tenant_id=$1 AND namespace=$2 AND key=$3",
        tenant, namespace, key,
    )


async def create(tenant: str, namespace: str, key: str, payload: str) -> bool:
    json.loads(payload)
    result = await (await assessment_store.pool()).execute(
        "INSERT INTO assessments.control_state(tenant_id,namespace,key,payload) VALUES($1,$2,$3,$4::jsonb) "
        "ON CONFLICT DO NOTHING", tenant, namespace, key, payload,
    )
    return result == "INSERT 0 1"


async def acquire(tenant: str, namespace: str, key: str, token: UUID) -> bool:
    result = await (await assessment_store.pool()).execute(
        "UPDATE assessments.control_state SET lease_token=$4,lease_until=clock_timestamp()+interval '60 seconds' "
        "WHERE tenant_id=$1 AND namespace=$2 AND key=$3 "
        "AND (lease_until IS NULL OR lease_until<clock_timestamp())",
        tenant, namespace, key, token,
    )
    return result == "UPDATE 1"


async def save(tenant: str, namespace: str, key: str, token: UUID, payload: str) -> bool:
    json.loads(payload)
    result = await (await assessment_store.pool()).execute(
        "UPDATE assessments.control_state SET payload=$5::jsonb,version=version+1 "
        "WHERE tenant_id=$1 AND namespace=$2 AND key=$3 AND lease_token=$4 AND lease_until>clock_timestamp()",
        tenant, namespace, key, token, payload,
    )
    return result == "UPDATE 1"


async def release(tenant: str, namespace: str, key: str, token: UUID) -> None:
    await (await assessment_store.pool()).execute(
        "UPDATE assessments.control_state SET lease_token=NULL,lease_until=NULL "
        "WHERE tenant_id=$1 AND namespace=$2 AND key=$3 AND lease_token=$4", tenant, namespace, key, token,
    )


async def keys(tenant: str, namespace: str, limit: int) -> list[str]:
    rows = await (await assessment_store.pool()).fetch(
        "SELECT key FROM assessments.control_state WHERE tenant_id=$1 AND namespace=$2 ORDER BY key LIMIT $3",
        tenant, namespace, limit + 1,
    )
    if len(rows) > limit:
        raise ValueError("Control record count exceeds the configured processing limit.")
    return [row["key"] for row in rows]
