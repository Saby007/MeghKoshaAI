"""Quota-scope-aware pacing shared by assessment and export worker replicas."""

import asyncio
import logging
import math
import os
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from services import assessment_store, runtime_identity
from services.assessment_settings import scalable_mode, settings

logger = logging.getLogger(__name__)
_RETRY_HEADERS = (
    "Retry-After",
    "x-ms-user-quota-resets-after",
    "x-ms-ratelimit-microsoft.consumption-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
)


def enabled() -> bool:
    return scalable_mode() and os.environ.get("ASSESSMENT_DB_USER") == "meghkosha_worker"


def scope(path: str) -> tuple[str, float]:
    route = urlsplit(path).path.lower()
    subscription = re.search(r"/subscriptions/([^/]+)", route)
    target = subscription.group(1) if subscription else "tenant"
    configuration = settings()
    if route.startswith("/providers/microsoft.resourcegraph/"):
        kind, target, interval = "resource-graph", "identity", configuration.azure_graph_interval_ms
    elif "/providers/microsoft.insights/metrics" in route:
        kind, interval = "metrics", configuration.azure_metrics_interval_ms
    elif "/providers/microsoft.costmanagement/query" in route:
        kind, interval = "cost-query", configuration.azure_cost_interval_ms
    else:
        kind, interval = "arm", configuration.azure_arm_interval_ms
    return f"{runtime_identity.client_id()}|{kind}|{target}", interval / 1000


def retry_delay(headers, attempt: int) -> float:
    values = []
    now = datetime.now(timezone.utc)
    for name in _RETRY_HEADERS:
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value)
        except ValueError:
            try:
                if re.fullmatch(r"\d+:\d{2}:\d{2}(?:\.\d+)?", value):
                    hours, minutes, seconds_part = value.split(":")
                    seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds_part)
                else:
                    deadline = parsedate_to_datetime(value)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    seconds = (deadline - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                logger.warning("Azure returned an invalid %s retry header", name)
                continue
        if math.isfinite(seconds):
            values.append(max(0.0, seconds))
        else:
            logger.warning("Azure returned a non-finite %s retry header", name)
    return max(values) if values else min(2**attempt, 30.0)


async def wait(path: str) -> None:
    key, interval = scope(path)
    while True:
        delay = await assessment_store.azure_request_delay(key, interval)
        if delay <= 0:
            return
        await asyncio.sleep(delay + random.uniform(0, min(interval, 0.1)))


async def defer(path: str, delay: float) -> None:
    key, _ = scope(path)
    await assessment_store.defer_azure_requests(key, delay)
