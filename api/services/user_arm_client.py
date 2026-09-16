"""Managed-identity ARM discovery filtered by the signed-in user's app policy."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import ManagedIdentityCredential
from fastapi import HTTPException

from services import access_control
from services.auth import ClientPrincipal
from services.entra_tokens import configured_uuid

ARM_BASE = "https://management.azure.com"
ARM_SCOPE = f"{ARM_BASE}/.default"
MAX_PAGES = 100
MAX_SUBSCRIPTIONS = 5000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DISCOVERY_TIMEOUT_SECONDS = 30
ACCESS_CHECK_TIMEOUT_SECONDS = 60

# The live cost-access probe (probe_cost=True) hits Cost Management's query API, which
# throttles far more aggressively than plain ARM reads. Export setup/configure/retry actions
# each re-run this probe for the same subscription in quick succession; cache a verified-True
# result briefly so those repeated manual actions don't re-trigger the throttle. Failures are
# never cached, so a just-fixed permission/config problem is reflected on the very next check.
_LIVE_ACCESS_CACHE_TTL_SECONDS = 300.0
_live_access_cache: dict[tuple[str, str, str], float] = {}

# Cost Management applies its rate limit across all callers, not just one request at a
# time (Phase1's services/arm_client.py hit the identical behavior). A global minimum
# interval between calls, serialized by a lock, avoids most 429s outright instead of
# relying only on reacting to them after the fact. A short bounded retry absorbs a
# transient miss; a long throttle window still fails immediately rather than holding a
# live button-click request open indefinitely.
_cost_query_lock = asyncio.Lock()
_COST_MIN_REQUEST_INTERVAL_SECONDS = 15.0
_COST_QUERY_RETRY_THRESHOLD_SECONDS = 15
_last_cost_request_at = 0.0


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Azure subscription discovery is temporarily unavailable.")


def _identity_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "code": "azure_managed_identity_unavailable",
        "message": "The backend managed identity could not authenticate to Azure. Contact the deployment administrator.",
    })


def _check_response(response: httpx.Response, tenant_id: str, *, operation: str = "Azure subscription discovery") -> None:
    if response.status_code == 401:
        raise _identity_unavailable()
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail={
            "code": "azure_managed_identity_forbidden",
            "message": "Azure denied access to the backend managed identity. Verify its existing subscription permissions.",
        })
    if response.status_code == 429:
        delays = [value for name in (
            "retry-after", "x-ms-ratelimit-microsoft.consumption-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
            "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
        ) if (value := response.headers.get(name, "")).isascii() and value.isdigit() and len(value) <= 7]
        retry_after = max(1, max(map(int, delays), default=60))
        raise HTTPException(status_code=503, detail=f"{operation} was throttled. Retry after {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})
    if response.status_code >= 500:
        raise _unavailable()
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Azure returned an unexpected subscription response.")


def _page_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Invalid pagination link")
    parsed = urlsplit(value)
    if ((parsed.scheme, parsed.netloc) not in (("", ""), ("https", "management.azure.com"))
            or parsed.path != "/subscriptions" or parsed.fragment):
        raise ValueError("Pagination left the subscription collection")
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


async def _list_subscriptions(client: httpx.AsyncClient, token: str, tenant_id: str) -> list[dict]:
    path = "/subscriptions?api-version=2022-12-01"
    visited: set[str] = set()
    subscriptions: dict[str, dict] = {}
    while path:
        if path in visited or len(visited) >= MAX_PAGES:
            raise ValueError("Subscription pagination limit exceeded")
        visited.add(path)
        async with client.stream("GET", path, headers={"Authorization": f"Bearer {token}"}) as response:
            _check_response(response, tenant_id)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("Subscription response limit exceeded")
                content.extend(chunk)
        payload = json.loads(content)
        rows = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Invalid subscription collection")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid subscription")
            subscription_id = configured_uuid(row.get("subscriptionId"))
            row_tenant = configured_uuid(row.get("tenantId"))
            if row_tenant != tenant_id:
                continue
            name = row.get("displayName")
            state = row.get("state")
            if not isinstance(name, str) or not name.strip() or len(name) > 512 or state not in {
                "Enabled", "Warned", "PastDue", "Disabled", "Deleted",
            }:
                raise ValueError("Invalid subscription metadata")
            item = {"subscriptionId": subscription_id, "displayName": name, "tenantId": row_tenant, "state": state}
            if subscription_id in subscriptions and subscriptions[subscription_id] != item:
                raise ValueError("Conflicting subscription pages")
            subscriptions[subscription_id] = item
            if len(subscriptions) > MAX_SUBSCRIPTIONS:
                raise ValueError("Subscription limit exceeded")
        next_link = payload.get("nextLink")
        path = "" if next_link is None or next_link == "" else _page_path(next_link)
    return list(subscriptions.values())


@asynccontextmanager
async def _managed_identity_client(principal: ClientPrincipal, timeout_seconds: int = DISCOVERY_TIMEOUT_SECONDS):
    try:
        tenant_id = configured_uuid(os.environ.get("AZURE_TENANT_ID"))
        managed_identity_client_id = configured_uuid(os.environ.get("AZURE_CLIENT_ID"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="The backend runtime managed identity is not configured.") from None
    if principal.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="This account is not authorized for this deployment.")
    try:
        async with asyncio.timeout(timeout_seconds):
            async with ManagedIdentityCredential(client_id=managed_identity_client_id) as credential:
                token = await credential.get_token(ARM_SCOPE)
                async with httpx.AsyncClient(base_url=ARM_BASE, timeout=10, follow_redirects=False) as client:
                    yield client, token.token
    except HTTPException:
        raise
    except ClientAuthenticationError:
        raise _identity_unavailable() from None
    except (httpx.HTTPError, TimeoutError):
        raise _unavailable() from None
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(status_code=502, detail="Azure returned an invalid subscription collection.") from None


async def _authorized_subscriptions(principal: ClientPrincipal, subscriptions: list[dict]) -> list[dict]:
    authorized = []
    for offset in range(0, len(subscriptions), 4):
        batch = subscriptions[offset:offset + 4]
        allowed = set(await access_control.authorized_subscription_ids(
            principal.entra_object_id, [item["subscriptionId"] for item in batch], fresh=True,
        ))
        authorized.extend(item for item in batch if item["subscriptionId"] in allowed)
    return authorized


async def discover_subscriptions(principal: ClientPrincipal) -> list[dict]:
    async with _managed_identity_client(principal, DISCOVERY_TIMEOUT_SECONDS) as (client, token):
        subscriptions = await _list_subscriptions(client, token, principal.tenant_id)
        return await _authorized_subscriptions(principal, subscriptions)


async def _probe_json(client, token: str, tenant_id: str, path: str, *, body: dict | None = None):
    async with client.stream("POST" if body is not None else "GET", path,
                             headers={"Authorization": f"Bearer {token}"}, json=body) as response:
        if response.status_code == 403:
            return None
        _check_response(response, tenant_id, operation="Cost Management access check" if body is not None else "Azure subscription access check")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("Eligibility response limit exceeded")
            content.extend(chunk)
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Invalid eligibility response")
    return payload


async def _wait_for_cost_query_slot() -> None:
    global _last_cost_request_at
    delay = _COST_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_cost_request_at)
    if delay > 0:
        await asyncio.sleep(delay)
    _last_cost_request_at = time.monotonic()


async def _probe_cost_access(client, token: str, tenant_id: str, scope: str):
    path = f"{scope}/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
    body = {
        "type": "ActualCost", "timeframe": "TheLastMonth",
        "dataset": {"granularity": "None", "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}}},
    }
    async with _cost_query_lock:
        for attempt in range(2):
            await _wait_for_cost_query_slot()
            try:
                return await _probe_json(client, token, tenant_id, path, body=body)
            except HTTPException as error:
                retry_after = (error.headers or {}).get("Retry-After")
                if (attempt == 0 and error.status_code == 503 and retry_after and retry_after.isdigit()
                        and int(retry_after) <= _COST_QUERY_RETRY_THRESHOLD_SECONDS):
                    await asyncio.sleep(int(retry_after))
                    continue
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


async def _has_read_and_cost_access(
    client, token: str, tenant_id: str, subscription_id: str, *, probe_cost: bool = True,
) -> bool:
    scope = f"/subscriptions/{configured_uuid(subscription_id)}"
    collection = f"{scope}/providers/Microsoft.Authorization/permissions"
    path = f"{collection}?api-version=2022-04-01"
    visited = set()
    required = {"microsoft.resources/subscriptions/read", "microsoft.resources/subscriptions/resourcegroups/read"}
    if not probe_cost:
        required.add("microsoft.costmanagement/query/read")
    granted = set()
    while path:
        if path in visited or len(visited) >= MAX_PAGES:
            raise ValueError("Permission pagination limit exceeded")
        visited.add(path)
        payload = await _probe_json(client, token, tenant_id, path)
        if payload is None:
            return False
        permissions = payload.get("value")
        if not isinstance(permissions, list):
            raise ValueError("Invalid permissions")
        for permission in permissions:
            if not isinstance(permission, dict):
                raise ValueError("Invalid permission")
            actions, excluded = permission.get("actions"), permission.get("notActions", [])
            if not isinstance(actions, list) or not isinstance(excluded, list) or any(
                not isinstance(value, str) for value in actions + excluded
            ):
                raise ValueError("Invalid permission actions")
            if permission.get("condition") or permission.get("conditionVersion"):
                continue
            granted.update(action for action in required if any(fnmatchcase(action, value.lower()) for value in actions)
                           and not any(fnmatchcase(action, value.lower()) for value in excluded))
        next_link = payload.get("nextLink")
        if not next_link:
            break
        if not isinstance(next_link, str) or len(next_link) > 8192:
            raise ValueError("Invalid permission pagination")
        parsed = urlsplit(next_link)
        if (parsed.scheme, parsed.netloc) not in (("", ""), ("https", "management.azure.com")) or parsed.path != collection or parsed.fragment:
            raise ValueError("Permission pagination left the selected subscription")
        path = urlunsplit(("", "", parsed.path, parsed.query, ""))
    if not required.issubset(granted):
        return False
    resources = await _probe_json(client, token, tenant_id, f"{scope}/resourcegroups?api-version=2021-04-01&$top=1")
    if resources is None:
        return False
    if not isinstance(resources.get("value"), list):
        raise ValueError("Invalid resource read response")
    if not probe_cost:
        return True
    cost = await _probe_cost_access(client, token, tenant_id, scope)
    if cost is None:
        return False
    properties = cost.get("properties")
    if not isinstance(properties, dict) or not isinstance(properties.get("columns"), list) or not isinstance(properties.get("rows"), list):
        raise ValueError("Invalid cost access response")
    return True


async def discover_schedule_subscriptions(
    principal: ClientPrincipal, subscription_ids: list[str] | None = None, *, probe_cost: bool = True,
) -> list[dict]:
    # Export create/run only ever calls Microsoft.CostManagement/exports (a separate quota from
    # .../query), so those callers pass probe_cost=False to avoid tripping on the query throttle
    # for an access level they don't actually need; a cheap RBAC-permissions check still applies.
    live_probe = subscription_ids is not None and probe_cost
    async with _managed_identity_client(principal, 90) as (client, token):
        subscriptions = await _list_subscriptions(client, token, principal.tenant_id)
        subscriptions = await _authorized_subscriptions(principal, subscriptions)
        candidates = validate_selection(subscriptions, subscription_ids) if subscription_ids is not None else [
            item for item in subscriptions if item["state"] == "Enabled"
        ]
        async def check(item):
            cache_key = (principal.tenant_id, principal.entra_object_id, item["subscriptionId"])
            if live_probe:
                cached_at = _live_access_cache.get(cache_key)
                if cached_at is not None and time.monotonic() - cached_at < _LIVE_ACCESS_CACHE_TTL_SECONDS:
                    return True
            async with asyncio.timeout(ACCESS_CHECK_TIMEOUT_SECONDS):
                result = await _has_read_and_cost_access(
                    client, token, principal.tenant_id, item["subscriptionId"], probe_cost=live_probe,
                )
            if result and live_probe:
                _live_access_cache[cache_key] = time.monotonic()
            return result

        verified = []
        for offset in range(0, len(candidates), 4):
            batch = candidates[offset:offset + 4]
            results = await asyncio.gather(*(check(item) for item in batch), return_exceptions=True)
            for item, result in zip(batch, results):
                if isinstance(result, BaseException):
                    if subscription_ids is not None or not isinstance(result, (
                        HTTPException, httpx.HTTPError, ValueError, TypeError, RecursionError, TimeoutError,
                    )):
                        raise result
                    message = "The runtime managed identity's subscription access could not be verified. Retry later."
                    if isinstance(result, HTTPException):
                        message = result.detail if isinstance(result.detail, str) else result.detail.get("message", message)
                    verified.append({**item, "readAccess": False, "costAccess": False,
                                     "accessCheckMode": "permissions", "accessIssue": message})
                    continue
                if result:
                    verified.append({**item, "readAccess": True, "costAccess": True,
                                     "accessCheckMode": "live" if live_probe else "permissions"})
                elif subscription_ids is None:
                    verified.append({**item, "readAccess": False, "costAccess": False,
                                     "accessCheckMode": "permissions",
                                     "accessIssue": "The runtime managed identity does not have the required subscription read and cost-query permissions."})
        if subscription_ids is not None and len(verified) != len(subscription_ids):
            raise HTTPException(status_code=403, detail="Read and cost access could not be verified for the selected subscription.")
        return verified


def validate_selection(subscriptions: list[dict], subscription_ids: list[str]) -> list[dict]:
    try:
        selected = [configured_uuid(value) for value in subscription_ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Subscription selection is invalid.") from None
    if not selected or len(selected) > 100 or len(set(selected)) != len(selected):
        raise HTTPException(status_code=400, detail="Select between one and 100 distinct subscriptions.")
    available = {item["subscriptionId"]: item for item in subscriptions if item["state"] == "Enabled"}
    if any(value not in available for value in selected):
        raise HTTPException(status_code=403, detail="The selection includes subscriptions unavailable to this user.")
    return [available[value] for value in selected]