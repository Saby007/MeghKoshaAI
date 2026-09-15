"""Parse and reconcile Azure FOCUS 1.2-preview cost export files."""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from services import arm_client, focus_export_download
from services.focus_export_control import normalize_subscription_id

FOCUS_DATA_VERSION = "1.2-preview"
_PERIOD_PATTERN = re.compile(r"/(\d{8})-(\d{8})/")
_FOCUS_EXPORT_NAME = os.environ.get("COST_EXPORT_NAME", "focus-closed-month-meghkoshaai")
_cache: dict[tuple[str, ...], tuple[float, "FocusCostData"]] = {}
_CACHE_TTL_SECONDS = 600.0
_REQUIRED_COLUMNS = {
    "BilledCost",
    "BillingCurrency",
    "BillingPeriodEnd",
    "BillingPeriodStart",
    "ChargeCategory",
    "ChargeClass",
    "ChargePeriodStart",
    "CommitmentDiscountCategory",
    "CommitmentDiscountId",
    "CommitmentDiscountName",
    "CommitmentDiscountStatus",
    "CommitmentDiscountType",
    "ContractedCost",
    "ContractedUnitPrice",
    "EffectiveCost",
    "ListCost",
    "ListUnitPrice",
    "PricingCategory",
    "PricingCurrency",
    "PricingQuantity",
    "PricingUnit",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "ServiceCategory",
    "ServiceName",
    "SkuId",
    "SkuPriceId",
    "SubAccountId",
    "SubAccountName",
    "x_EffectiveUnitPrice",
    "x_ResourceGroupName",
    "x_SkuMeterCategory",
    "x_SkuMeterId",
    "x_SkuMeterSubcategory",
    "x_SkuServiceFamily",
}


class FocusCostDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class FocusPricingEvidence:
    sku_id: str
    sku_price_id: str
    pricing_category: str
    pricing_currency: str
    pricing_quantity: float
    pricing_unit: str
    list_unit_price: float
    contracted_unit_price: float
    effective_unit_price: float
    list_cost: float
    contracted_cost: float
    effective_cost: float
    billed_cost: float
    commitment_discount_category: str
    commitment_discount_type: str
    commitment_discount_status: str


@dataclass(frozen=True)
class FocusCommitmentBreakdown:
    row_count: int = 0
    used_effective_cost: float = 0.0
    unused_effective_cost: float = 0.0
    realized_benefit: float = 0.0


@dataclass(frozen=True)
class FocusResourceCost:
    subscription_id: str
    subscription_name: str
    resource_group: str
    resource_id: str
    resource_name: str
    resource_type: str
    region: str
    effective_cost: float


@dataclass(frozen=True)
class FocusCostData:
    data_version: str
    period: str
    period_start: str
    period_end: str
    currency: str
    pricing_currencies: list[str]
    subscription_ids: list[str]
    subscription_names: dict[str, str]
    row_count: int
    billed_cost_by_subscription: dict[str, float]
    effective_cost_by_subscription: dict[str, float]
    list_cost_by_subscription: dict[str, float]
    contracted_cost_by_subscription: dict[str, float]
    negotiated_discount_by_subscription: dict[str, float]
    billed_cost_by_resource_id: dict[str, float]
    effective_cost_by_resource_id: dict[str, float]
    service_spend: dict[str, float]
    service_category_spend: dict[str, float]
    pricing_evidence_by_resource_id: dict[str, list[FocusPricingEvidence]] = field(default_factory=dict)
    service_family_spend: dict[str, float] = field(default_factory=dict)
    provider_spend: dict[str, float] = field(default_factory=dict)
    region_spend: dict[str, float] = field(default_factory=dict)
    resource_costs: list[FocusResourceCost] = field(default_factory=list)
    extended_support_costs: list[FocusResourceCost] = field(default_factory=list)
    tag_spend: dict[str, dict[str, float]] = field(default_factory=dict)
    generated_at: str = ""
    reservation_commitment: FocusCommitmentBreakdown = field(default_factory=FocusCommitmentBreakdown)
    savings_plan_commitment: FocusCommitmentBreakdown = field(default_factory=FocusCommitmentBreakdown)


@dataclass(frozen=True)
class FocusReconciliation:
    focus_effective_cost: float
    amortized_cost: float
    variance: float
    variance_percentage: float
    reconciled: bool


def _decimal(value: str | None, column: str) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation as error:
        raise FocusCostDataError(f"Invalid {column} value {value!r}") from error


def _add(target: dict[str, Decimal], key: str, value: Decimal) -> None:
    target[key] = target.get(key, Decimal()) + value


def _float_map(values: dict[str, Decimal]) -> dict[str, float]:
    return {key: float(value) for key, value in values.items()}


def _tags(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key).strip().lower(): str(tag_value).strip()
        for key, tag_value in parsed.items()
        if str(key).strip() and str(tag_value).strip()
    }


_CANONICAL_TAG_KEYS = {"team": "Team", "department": "Department", "project": "Project"}


def _display_tag_key(key: str) -> str:
    return _CANONICAL_TAG_KEYS.get(key, key.replace("_", " ").replace("-", " ").title())


_EXTENDED_SUPPORT_PATTERN = re.compile(r"extended security update", re.IGNORECASE)


def _is_extended_support_charge(row: dict[str, str]) -> bool:
    haystack = " ".join(
        row.get(column) or ""
        for column in ("ServiceName", "x_SkuMeterCategory", "x_SkuMeterSubcategory")
    )
    return bool(_EXTENDED_SUPPORT_PATTERN.search(haystack))


def _subscription_id(value: str) -> str:
    match = re.fullmatch(r"/subscriptions/([^/]+)", value.strip(), flags=re.IGNORECASE)
    if not match:
        raise FocusCostDataError(f"Invalid FOCUS SubAccountId {value!r}")
    try:
        return normalize_subscription_id(match.group(1))
    except ValueError as error:
        raise FocusCostDataError(f"Invalid FOCUS SubAccountId {value!r}") from error


def _commitment_kind(value: str) -> str:
    normalized = re.sub(r"[^a-z]", "", value.lower())
    if "savingsplan" in normalized:
        return "savings_plan"
    if "reservation" in normalized:
        return "reservation"
    return ""


def _focus_region(row: dict[str, str]) -> str:
    for column in ("RegionId", "RegionName", "Region", "x_ResourceLocation"):
        value = (row.get(column) or "").strip()
        if value:
            return value
    return "Unassigned"


def _period(blob_names: list[str]) -> tuple[str, str, str]:
    periods = set()
    for blob_name in blob_names:
        match = _PERIOD_PATTERN.search(blob_name)
        if not match:
            raise FocusCostDataError(f"FocusCost blob path has no period: {blob_name}")
        periods.add(match.groups())
    if len(periods) != 1:
        raise FocusCostDataError("FocusCost files contain multiple periods")
    start_raw, end_raw = periods.pop()
    start = f"{start_raw[:4]}-{start_raw[4:6]}-{start_raw[6:]}"
    end = f"{end_raw[:4]}-{end_raw[4:6]}-{end_raw[6:]}"
    if start[:7] != end[:7]:
        raise FocusCostDataError("FocusCost period must be one calendar month")
    return start[:7], start, end


def _csv_reader(content: bytes, blob_name: str) -> csv.DictReader:
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except gzip.BadGzipFile as error:
            raise FocusCostDataError(f"Invalid gzip FocusCost file {blob_name}") from error
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FocusCostDataError(f"FocusCost file is not UTF-8: {blob_name}") from error
    reader = csv.DictReader(io.StringIO(text))
    missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise FocusCostDataError(f"FocusCost file is missing columns: {', '.join(sorted(missing))}")
    return reader


def build_focus_cost_data(
    files: dict[str, bytes],
    requested_subscription_ids: list[str],
    *,
    data_version: str = FOCUS_DATA_VERSION,
    generated_at: str = "",
    resource_group_tags: dict[tuple[str, str], dict[str, str]] | None = None,
) -> FocusCostData:
    if data_version != FOCUS_DATA_VERSION:
        raise FocusCostDataError(f"Unsupported FOCUS data version {data_version!r}")
    if not files:
        raise FocusCostDataError("No FocusCost files are available")

    requested = [normalize_subscription_id(value) for value in requested_subscription_ids]
    period, period_start, period_end = _period(list(files))
    currencies: set[str] = set()
    pricing_currencies: set[str] = set()
    subscription_names: dict[str, str] = {}
    observed_subscriptions: set[str] = set()
    row_count = 0
    billed_by_subscription: dict[str, Decimal] = {}
    effective_by_subscription: dict[str, Decimal] = {}
    list_by_subscription: dict[str, Decimal] = {}
    contracted_by_subscription: dict[str, Decimal] = {}
    discount_by_subscription: dict[str, Decimal] = {}
    billed_by_resource: dict[str, Decimal] = {}
    effective_by_resource: dict[str, Decimal] = {}
    service_spend: dict[str, Decimal] = {}
    category_spend: dict[str, Decimal] = {}
    family_spend: dict[str, Decimal] = {}
    provider_spend: dict[str, Decimal] = {}
    region_spend: dict[str, Decimal] = {}
    resource_costs: dict[str, dict[str, str | Decimal]] = {}
    extended_support_by_resource: dict[str, Decimal] = {}
    tag_spend: dict[str, dict[str, Decimal]] = {
        "Team": {},
        "Department": {},
        "Project": {},
    }
    # (subscription_id, resource_group_name_lower, resource's own lowercase tags, cost) -
    # buffered so tag dimensions discovered later in the file still get a correct
    # "Unallocated" bucket for every row that lacks them (see the pass below the loop).
    tag_rows: list[tuple[str, str, dict[str, str], Decimal]] = []
    evidence: dict[str, dict[tuple[str, ...], dict[str, str | Decimal]]] = {}
    commitment_totals = {
        "reservation": {"rows": 0, "used": Decimal(), "unused": Decimal(), "benefit": Decimal()},
        "savings_plan": {"rows": 0, "used": Decimal(), "unused": Decimal(), "benefit": Decimal()},
    }

    for blob_name, content in sorted(files.items()):
        for row in _csv_reader(content, blob_name):
            row_count += 1
            subscription_id = _subscription_id(row["SubAccountId"])
            if subscription_id not in requested:
                raise FocusCostDataError(f"FocusCost file contains unrequested subscription {subscription_id}")
            observed_subscriptions.add(subscription_id)
            subscription_names[subscription_id] = row.get("SubAccountName") or subscription_id
            currency = row.get("BillingCurrency") or ""
            if currency:
                currencies.add(currency)
            pricing_currency = row.get("PricingCurrency") or ""
            if pricing_currency:
                pricing_currencies.add(pricing_currency)

            billed = _decimal(row.get("BilledCost"), "BilledCost")
            effective = _decimal(row.get("EffectiveCost"), "EffectiveCost")
            list_cost = _decimal(row.get("ListCost"), "ListCost")
            contracted = _decimal(row.get("ContractedCost"), "ContractedCost")
            _add(billed_by_subscription, subscription_id, billed)
            _add(effective_by_subscription, subscription_id, effective)
            _add(list_by_subscription, subscription_id, list_cost)
            _add(contracted_by_subscription, subscription_id, contracted)

            is_usage = (row.get("ChargeCategory") or "").lower() == "usage"
            commitment_status = (row.get("CommitmentDiscountStatus") or "").lower()
            is_unused_commitment = commitment_status == "unused"
            if is_usage and not is_unused_commitment:
                _add(discount_by_subscription, subscription_id, list_cost - contracted)

            commitment_kind = _commitment_kind(row.get("CommitmentDiscountType") or "")
            if commitment_kind and commitment_status in {"used", "unused"}:
                commitment = commitment_totals[commitment_kind]
                commitment["rows"] += 1
                if is_usage and commitment_status == "used":
                    commitment["used"] += effective
                    commitment["benefit"] += max(contracted - effective, Decimal())
                elif commitment_status == "unused":
                    commitment["unused"] += effective

            resource_id = (row.get("ResourceId") or "").lower()
            region = _focus_region(row)
            _add(region_spend, region, effective)
            if resource_id:
                _add(billed_by_resource, resource_id, billed)
                _add(effective_by_resource, resource_id, effective)
                resource_cost = resource_costs.setdefault(
                    resource_id,
                    {
                        "subscription_id": subscription_id,
                        "subscription_name": subscription_names[subscription_id],
                        "resource_group": row.get("x_ResourceGroupName") or "Unassigned",
                        "resource_id": resource_id,
                        "resource_name": row.get("ResourceName") or resource_id.rsplit("/", 1)[-1],
                        "resource_type": row.get("x_ResourceType") or row.get("ResourceType") or "Unassigned",
                        "region": region,
                        "effective_cost": Decimal(),
                    },
                )
                resource_cost["effective_cost"] += effective
                if _is_extended_support_charge(row):
                    _add(extended_support_by_resource, resource_id, effective)
                price_values = {
                    "sku_id": row.get("SkuId") or "",
                    "sku_price_id": row.get("SkuPriceId") or "",
                    "pricing_category": row.get("PricingCategory") or "",
                    "pricing_currency": pricing_currency,
                    "pricing_unit": row.get("PricingUnit") or "",
                    "list_unit_price": _decimal(row.get("ListUnitPrice"), "ListUnitPrice"),
                    "contracted_unit_price": _decimal(row.get("ContractedUnitPrice"), "ContractedUnitPrice"),
                    "effective_unit_price": _decimal(row.get("x_EffectiveUnitPrice"), "x_EffectiveUnitPrice"),
                    "commitment_discount_category": row.get("CommitmentDiscountCategory") or "",
                    "commitment_discount_type": row.get("CommitmentDiscountType") or "",
                    "commitment_discount_status": row.get("CommitmentDiscountStatus") or "",
                }
                price_key = tuple(str(value) for value in price_values.values())
                observation = evidence.setdefault(resource_id, {}).setdefault(
                    price_key,
                    {
                        **price_values,
                        "pricing_quantity": Decimal(),
                        "list_cost": Decimal(),
                        "contracted_cost": Decimal(),
                        "effective_cost": Decimal(),
                        "billed_cost": Decimal(),
                    },
                )
                observation["pricing_quantity"] += _decimal(row.get("PricingQuantity"), "PricingQuantity")
                observation["list_cost"] += list_cost
                observation["contracted_cost"] += contracted
                observation["effective_cost"] += effective
                observation["billed_cost"] += billed

            _add(service_spend, row.get("ServiceName") or "Other", effective)
            _add(category_spend, row.get("ServiceCategory") or "Other", effective)
            _add(family_spend, row.get("x_SkuServiceFamily") or row.get("ServiceCategory") or "Other", effective)
            resource_type = row.get("x_ResourceType") or row.get("ResourceType") or ""
            provider = resource_type.split("/", 1)[0] if "/" in resource_type else ""
            _add(provider_spend, provider or row.get("ServiceName") or "Other", effective)
            tag_rows.append((
                subscription_id,
                (row.get("x_ResourceGroupName") or "").strip().lower(),
                _tags(row.get("Tags")),
                effective,
            ))

    missing_subscriptions = sorted(set(requested) - observed_subscriptions)
    if missing_subscriptions:
        raise FocusCostDataError(f"FocusCost files are missing subscriptions: {', '.join(missing_subscriptions)}")
    if len(currencies) != 1:
        raise FocusCostDataError(f"Expected one billing currency, found: {sorted(currencies)}")

    # Tags cascade down from the resource group: a resource with no tag of its own is
    # attributed to its resource group's tag value. Every tag KEY found anywhere (on a
    # resource or its resource group) becomes its own cost dimension - not just a fixed
    # Team/Department/Project/Application set - so "cost by tags" works for whatever
    # tagging convention is actually in use.
    dimension_keys = {"team", "department", "project"}
    for _, _, row_tags, _ in tag_rows:
        dimension_keys.update(row_tags)
    for group_tags in (resource_group_tags or {}).values():
        dimension_keys.update(group_tags)
    display_key_by_lower = {key: _display_tag_key(key) for key in dimension_keys}
    tag_spend = {display_key: {} for display_key in display_key_by_lower.values()}
    for row_subscription_id, resource_group_name, row_tags, cost in tag_rows:
        group_tags = (
            (resource_group_tags or {}).get((row_subscription_id, resource_group_name))
            if resource_group_name
            else None
        )
        for lower_key, display_key in display_key_by_lower.items():
            value = row_tags.get(lower_key) or (group_tags or {}).get(lower_key) or "Unallocated"
            _add(tag_spend[display_key], value, cost)

    return FocusCostData(
        data_version=data_version,
        period=period,
        period_start=period_start,
        period_end=period_end,
        currency=next(iter(currencies)),
        pricing_currencies=sorted(pricing_currencies),
        subscription_ids=requested,
        subscription_names=subscription_names,
        row_count=row_count,
        billed_cost_by_subscription=_float_map(billed_by_subscription),
        effective_cost_by_subscription=_float_map(effective_by_subscription),
        list_cost_by_subscription=_float_map(list_by_subscription),
        contracted_cost_by_subscription=_float_map(contracted_by_subscription),
        negotiated_discount_by_subscription=_float_map(discount_by_subscription),
        billed_cost_by_resource_id=_float_map(billed_by_resource),
        effective_cost_by_resource_id=_float_map(effective_by_resource),
        service_spend=_float_map(service_spend),
        service_category_spend=_float_map(category_spend),
        pricing_evidence_by_resource_id={
            resource_id: [
                FocusPricingEvidence(
                    sku_id=str(item["sku_id"]),
                    sku_price_id=str(item["sku_price_id"]),
                    pricing_category=str(item["pricing_category"]),
                    pricing_currency=str(item["pricing_currency"]),
                    pricing_quantity=float(item["pricing_quantity"]),
                    pricing_unit=str(item["pricing_unit"]),
                    list_unit_price=float(item["list_unit_price"]),
                    contracted_unit_price=float(item["contracted_unit_price"]),
                    effective_unit_price=float(item["effective_unit_price"]),
                    list_cost=float(item["list_cost"]),
                    contracted_cost=float(item["contracted_cost"]),
                    effective_cost=float(item["effective_cost"]),
                    billed_cost=float(item["billed_cost"]),
                    commitment_discount_category=str(item["commitment_discount_category"]),
                    commitment_discount_type=str(item["commitment_discount_type"]),
                    commitment_discount_status=str(item["commitment_discount_status"]),
                )
                for item in items.values()
            ]
            for resource_id, items in evidence.items()
        },
        service_family_spend=_float_map(family_spend),
        provider_spend=_float_map(provider_spend),
        region_spend=_float_map(region_spend),
        resource_costs=sorted(
            (
                FocusResourceCost(
                    subscription_id=str(item["subscription_id"]),
                    subscription_name=str(item["subscription_name"]),
                    resource_group=str(item["resource_group"]),
                    resource_id=str(item["resource_id"]),
                    resource_name=str(item["resource_name"]),
                    resource_type=str(item["resource_type"]),
                    region=str(item["region"]),
                    effective_cost=float(item["effective_cost"]),
                )
                for item in resource_costs.values()
            ),
            key=lambda item: (-item.effective_cost, item.resource_id),
        ),
        extended_support_costs=sorted(
            (
                FocusResourceCost(
                    subscription_id=str(resource_costs[resource_id]["subscription_id"]),
                    subscription_name=str(resource_costs[resource_id]["subscription_name"]),
                    resource_group=str(resource_costs[resource_id]["resource_group"]),
                    resource_id=resource_id,
                    resource_name=str(resource_costs[resource_id]["resource_name"]),
                    resource_type=str(resource_costs[resource_id]["resource_type"]),
                    region=str(resource_costs[resource_id]["region"]),
                    effective_cost=float(cost),
                )
                for resource_id, cost in extended_support_by_resource.items()
                if resource_id in resource_costs and cost > 0
            ),
            key=lambda item: (-item.effective_cost, item.resource_id),
        ),
        tag_spend={dimension: _float_map(values) for dimension, values in tag_spend.items()},
        generated_at=generated_at,
        reservation_commitment=FocusCommitmentBreakdown(
            row_count=int(commitment_totals["reservation"]["rows"]),
            used_effective_cost=float(commitment_totals["reservation"]["used"]),
            unused_effective_cost=float(commitment_totals["reservation"]["unused"]),
            realized_benefit=float(commitment_totals["reservation"]["benefit"]),
        ),
        savings_plan_commitment=FocusCommitmentBreakdown(
            row_count=int(commitment_totals["savings_plan"]["rows"]),
            used_effective_cost=float(commitment_totals["savings_plan"]["used"]),
            unused_effective_cost=float(commitment_totals["savings_plan"]["unused"]),
            realized_benefit=float(commitment_totals["savings_plan"]["benefit"]),
        ),
    )


def reconcile_focus_to_amortized(
    focus_data: FocusCostData,
    amortized_spend_by_subscription: dict[str, float],
    *,
    absolute_tolerance: float = 0.01,
    relative_tolerance: float = 0.0001,
) -> FocusReconciliation:
    focus_total = sum(focus_data.effective_cost_by_subscription.values())
    amortized_total = sum(amortized_spend_by_subscription.get(value, 0.0) for value in focus_data.subscription_ids)
    variance = focus_total - amortized_total
    denominator = abs(amortized_total)
    variance_percentage = abs(variance) / denominator if denominator else (0.0 if variance == 0 else 1.0)
    reconciled = abs(variance) <= absolute_tolerance or variance_percentage <= relative_tolerance
    return FocusReconciliation(
        focus_effective_cost=focus_total,
        amortized_cost=amortized_total,
        variance=variance,
        variance_percentage=variance_percentage,
        reconciled=reconciled,
    )


def _run_period(run: dict) -> tuple[str, str, str] | None:
    properties = run.get("properties") or {}
    if properties.get("status") != "Completed":
        return None
    try:
        start = date.fromisoformat(str(properties["startDate"])[:10])
        end = date.fromisoformat(str(properties["endDate"])[:10])
    except (KeyError, ValueError):
        return None
    if start.strftime("%Y-%m") != end.strftime("%Y-%m"):
        return None
    return start.strftime("%Y-%m"), start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _select_common_runs(
    subscription_ids: list[str],
    runs_by_subscription: dict[str, list[dict]],
) -> tuple[str, dict[str, dict], str]:
    completed_by_subscription: dict[str, dict[str, list[dict]]] = {}
    for subscription_id in subscription_ids:
        by_period: dict[str, list[dict]] = {}
        for run in runs_by_subscription.get(subscription_id, []):
            period = _run_period(run)
            if period:
                by_period.setdefault(period[0], []).append(run)
        completed_by_subscription[subscription_id] = by_period

    common_periods = set.intersection(
        *(set(completed_by_subscription[value]) for value in subscription_ids)
    ) if subscription_ids else set()
    if not common_periods:
        raise FocusCostDataError("No completed FocusCost period contains every selected subscription")

    period = max(common_periods)
    selected: dict[str, dict] = {}
    period_path = ""
    for subscription_id in subscription_ids:
        candidates = completed_by_subscription[subscription_id][period]
        run = max(
            candidates,
            key=lambda item: str((item.get("properties") or {}).get("processingEndTime") or ""),
        )
        run_period = _run_period(run)
        assert run_period is not None
        selected[subscription_id] = run
        period_path = f"{run_period[1]}-{run_period[2]}"
    return period, selected, period_path


def _download_run_files(
    subscription_ids: list[str],
    selected_runs: dict[str, dict],
    period_path: str,
) -> dict[str, bytes]:
    container = focus_export_download.get_container_client()
    files: dict[str, bytes] = {}
    for subscription_id in subscription_ids:
        run_id = str(selected_runs[subscription_id].get("name") or "")
        if not run_id:
            raise FocusCostDataError(f"Completed FocusCost run has no ID for {subscription_id}")
        prefix = f"focus/{subscription_id}/{_FOCUS_EXPORT_NAME}/{period_path}/{run_id}/"
        blobs = [
            blob
            for blob in container.list_blobs(name_starts_with=prefix)
            if blob.name.lower().endswith((".csv", ".csv.gz"))
        ]
        if not blobs:
            raise FocusCostDataError(f"Completed FocusCost run has no CSV files for {subscription_id}")
        for blob in blobs:
            content = container.download_blob(blob.name).readall()
            if len(content) != int(blob.size or 0):
                raise FocusCostDataError(f"FocusCost blob size check failed for {blob.name}")
            files[blob.name] = content
    return files


async def load_latest_complete_focus_costs(subscription_ids: list[str]) -> FocusCostData:
    normalized = [normalize_subscription_id(value) for value in subscription_ids]
    cache_key = tuple(sorted(normalized))
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        run_lists = await asyncio.gather(
            *(arm_client.list_cost_export_runs(value, _FOCUS_EXPORT_NAME) for value in normalized)
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
        raise FocusCostDataError(
            f"FOCUS export '{_FOCUS_EXPORT_NAME}' is not configured for one or more selected subscriptions. "
            "Open Schedules, create and run the exports, then retry after successful completion."
        ) from error
    runs_by_subscription = dict(zip(normalized, run_lists))
    period, selected_runs, period_path = _select_common_runs(normalized, runs_by_subscription)
    generated_at = max(
        str((run.get("properties") or {}).get("processingEndTime") or "")
        for run in selected_runs.values()
    )
    files = await asyncio.to_thread(
        _download_run_files,
        normalized,
        selected_runs,
        period_path,
    )
    resource_group_tags = await _load_resource_group_tags(normalized)
    result = build_focus_cost_data(files, normalized, generated_at=generated_at, resource_group_tags=resource_group_tags)
    if result.period != period:
        raise FocusCostDataError("FocusCost run history and blob periods do not match")
    _cache[cache_key] = (now, result)
    return result


async def _load_resource_group_tags(subscription_ids: list[str]) -> dict[tuple[str, str], dict[str, str]]:
    groups_by_subscription = await asyncio.gather(
        *(arm_client.list_resource_groups(subscription_id) for subscription_id in subscription_ids),
        return_exceptions=True,
    )
    tags: dict[tuple[str, str], dict[str, str]] = {}
    for subscription_id, groups in zip(subscription_ids, groups_by_subscription):
        if isinstance(groups, BaseException):
            continue
        for group in groups:
            name = str(group.get("name") or "").strip().lower()
            if not name:
                continue
            raw_tags = group.get("tags") or {}
            tags[(subscription_id, name)] = {
                str(key).strip().lower(): str(value).strip()
                for key, value in raw_tags.items()
                if str(key).strip() and str(value).strip()
            }
    return tags