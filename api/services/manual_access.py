"""Authorize only the single subscription configured in the active workspace."""

from fastapi import HTTPException

from services import target_connection
from services.access_control import SubscriptionOperation


async def authorized_subscription_ids(
    principal_object_id: str, subscription_ids: list[str], *, fresh: bool = False,
) -> list[str]:
    configuration = await target_connection.load(required=False)
    if configuration is None:
        return []
    return [value for value in dict.fromkeys(item.lower() for item in subscription_ids)
            if value == configuration.subscription_id]


async def require_subscription_access(principal_object_id: str, subscription_id: str) -> None:
    await target_connection.require_subscription(subscription_id)


async def require_all_subscription_access(principal_object_id: str, subscription_ids: list[str]) -> None:
    if len(subscription_ids) != 1:
        raise HTTPException(403, "Select exactly the one subscription configured in Admin.")
    await target_connection.require_subscription(subscription_ids[0])


async def require_subscription_operation(principal_object_id: str, subscription_id: str, operation: str) -> None:
    if operation != "report_read":
        raise HTTPException(403, "This application does not modify target Azure resources, budgets, exports or roles.")
    await target_connection.require_subscription(subscription_id)
