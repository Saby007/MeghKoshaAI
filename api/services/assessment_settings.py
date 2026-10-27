"""Validated limits shared by the API, database client, and assessment worker."""

import os
from dataclasses import dataclass
from functools import lru_cache


def scalable_mode() -> bool:
    mode = os.environ.get("ASSESSMENT_MODE", "blob")
    if mode not in {"blob", "postgres"}:
        raise ValueError("ASSESSMENT_MODE must be blob or postgres")
    return mode == "postgres"


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class AssessmentSettings:
    pool_size: int
    queue_capacity: int
    lease_seconds: int
    timeout_seconds: int
    max_attempts: int
    poll_seconds: int
    api_concurrency: int
    max_subscriptions: int
    page_size: int
    response_bytes: int
    record_bytes: int
    inline_rows: int
    input_bytes: int
    spool_bytes: int
    expanded_bytes: int
    csv_rows: int
    inventory_rows: int
    history_rows: int
    snapshot_read_bytes: int
    interactive_rows: int
    work_concurrency: int
    read_pool_size: int
    azure_arm_interval_ms: int
    azure_graph_interval_ms: int
    azure_metrics_interval_ms: int
    azure_cost_interval_ms: int
    azure_get_retries: int
    detail_rows: int
    detail_points: int


@lru_cache(maxsize=1)
def settings() -> AssessmentSettings:
    capacity = _integer("ASSESSMENT_DB_POOL_SIZE", 5, 2, 50)
    configured = AssessmentSettings(
        pool_size=capacity,
        queue_capacity=_integer("ASSESSMENT_QUEUE_CAPACITY", 50, 1, 1000),
        lease_seconds=_integer("ASSESSMENT_LEASE_SECONDS", 120, 30, 600),
        timeout_seconds=_integer("ASSESSMENT_TIMEOUT_SECONDS", 3600, 60, 86400),
        max_attempts=_integer("ASSESSMENT_MAX_ATTEMPTS", 3, 1, 5),
        poll_seconds=_integer("ASSESSMENT_POLL_SECONDS", 5, 1, 60),
        api_concurrency=_integer("ASSESSMENT_API_CONCURRENCY", 20, 1, 50),
        max_subscriptions=_integer("ASSESSMENT_MAX_SUBSCRIPTIONS", 20, 1, 100),
        page_size=_integer("ASSESSMENT_PAGE_SIZE", 100, 1, 200),
        response_bytes=_integer("ASSESSMENT_RESPONSE_BYTES", 1048576, 65536, 2097152),
        record_bytes=_integer("ASSESSMENT_RECORD_BYTES", 262144, 1024, 524288),
        inline_rows=_integer("ASSESSMENT_INLINE_ROWS", 20, 1, 100),
        input_bytes=_integer("ASSESSMENT_INPUT_BYTES", 4 * 1024**3, 1024**2, 64 * 1024**3),
        spool_bytes=_integer("ASSESSMENT_SPOOL_BYTES", 6 * 1024**3, 1024**2, 64 * 1024**3),
        expanded_bytes=_integer("ASSESSMENT_EXPANDED_BYTES", 8 * 1024**3, 1024**2, 128 * 1024**3),
        csv_rows=_integer("ASSESSMENT_CSV_ROWS", 20_000_000, 1, 100_000_000),
        inventory_rows=_integer("ASSESSMENT_INVENTORY_ROWS", 500_000, 1, 5_000_000),
        history_rows=_integer("ASSESSMENT_HISTORY_ROWS", 2_000_000, 1, 10_000_000),
        snapshot_read_bytes=_integer("ASSESSMENT_SNAPSHOT_READ_BYTES", 4 * 1024**2, 1024, 16 * 1024**2),
        interactive_rows=_integer("ASSESSMENT_INTERACTIVE_ROWS", 1000, 1, 5000),
        work_concurrency=_integer("ASSESSMENT_WORK_CONCURRENCY", 4, 1, 20),
        read_pool_size=_integer("ASSESSMENT_READ_POOL_SIZE", min(3, capacity - 1), 1, 49),
        azure_arm_interval_ms=_integer("ASSESSMENT_ARM_INTERVAL_MS", 200, 10, 60000),
        azure_graph_interval_ms=_integer("ASSESSMENT_GRAPH_INTERVAL_MS", 400, 10, 60000),
        azure_metrics_interval_ms=_integer("ASSESSMENT_METRICS_INTERVAL_MS", 500, 10, 60000),
        azure_cost_interval_ms=_integer("ASSESSMENT_COST_INTERVAL_MS", 15000, 10, 60000),
        azure_get_retries=_integer("ASSESSMENT_AZURE_GET_RETRIES", 3, 0, 5),
        detail_rows=_integer("ASSESSMENT_DETAIL_ROWS", 50000, 1, 250000),
        detail_points=_integer("ASSESSMENT_DETAIL_POINTS", 5000000, 1, 20000000),
    )
    if configured.record_bytes + 4096 > configured.response_bytes:
        raise ValueError("ASSESSMENT_RECORD_BYTES must leave at least 4096 bytes within ASSESSMENT_RESPONSE_BYTES.")
    if configured.read_pool_size >= configured.pool_size:
        raise ValueError("ASSESSMENT_READ_POOL_SIZE must leave at least one connection for work operations.")
    return configured
