"""Replay uploaded CSVs without materializing resource-by-day history in memory."""

import asyncio
import calendar
import hashlib
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

from anomalies.models import DailyCostRecord
from reports.models import CostDetailSummary
from services import assessment_store, focus_cost_reader, focus_uploads, workspace
from services.assessment_limits import active_budget, consume
from services.focus_cost_reader import FocusCostData, FocusCostDataError
from services.focus_history_reader import FocusHistoryData
from services.history_records import ReplayableRecords


def charge_day(value: str) -> str:
    try:
        if "T" not in value and " " not in value:
            return str(date.fromisoformat(value))
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("A charge timestamp requires a timezone.")
        return str(stamp.astimezone(timezone.utc).date())
    except (ValueError, TypeError, AttributeError):
        raise FocusCostDataError("A FOCUS row has an invalid UTC ChargePeriodStart.") from None


def daily_record(row: dict[str, str]) -> DailyCostRecord:
    return DailyCostRecord(
        date=charge_day(row["ChargePeriodStart"]),
        subscription_id=focus_cost_reader._subscription_id(row["SubAccountId"]),
        subscription_name=row.get("SubAccountName") or focus_cost_reader._subscription_id(row["SubAccountId"]),
        service_name=row.get("ServiceName") or "Other",
        resource_group=row.get("x_ResourceGroupName") or "Unassigned",
        resource_id=(row.get("ResourceId") or "").lower(),
        resource_name=row.get("ResourceName") or "Unattributed",
        effective_cost=float(focus_cost_reader._decimal(row.get("EffectiveCost"), "EffectiveCost")),
        service_category=row.get("ServiceCategory") or "Other",
        resource_type=row.get("x_ResourceType") or row.get("ResourceType") or "",
        region=focus_cost_reader._focus_region(row),
        charge_category=row.get("ChargeCategory") or "",
        list_cost=float(focus_cost_reader._decimal(row.get("ListCost"), "ListCost")),
        contracted_cost=float(focus_cost_reader._decimal(row.get("ContractedCost"), "ContractedCost")),
        commitment_discount_type=row.get("CommitmentDiscountType") or "",
        commitment_discount_status=row.get("CommitmentDiscountStatus") or "",
        pricing_category=row.get("PricingCategory") or "",
        tags=focus_cost_reader._tags(row.get("Tags")),
    )


class UploadedHistory(ReplayableRecords):
    def __init__(self, files: dict[str, Path], count: int):
        self.files = dict(files)
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[DailyCostRecord]:
        for name, path in self.files.items():
            for row in focus_cost_reader._csv_reader(path, name, account_input=False):
                yield daily_record(row)


@dataclass(frozen=True)
class Evidence:
    costs: FocusCostData
    history: FocusHistoryData
    details: CostDetailSummary
    coverage: dict


def parse_files(files: dict[str, Path], subscription_id: str, generated_at: str) -> Evidence:
    observed_days: set[str] = set()
    currencies: set[str] = set()
    names: dict[str, str] = {}
    count = 0
    for name, path in files.items():
        for row in focus_cost_reader._csv_reader(path, name):
            if None in row or any(value is None for value in row.values()):
                raise FocusCostDataError(f"FOCUS CSV row shape does not match the header: {name}")
            identifier = focus_cost_reader._subscription_id(row["SubAccountId"])
            if identifier != subscription_id:
                raise FocusCostDataError("The uploaded report contains a subscription other than the target in Admin.")
            if not row.get("BillingCurrency") or not row.get("EffectiveCost") or not row.get("BilledCost"):
                raise FocusCostDataError("BillingCurrency, BilledCost and EffectiveCost must be present on every row.")
            for column in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost", "PricingQuantity",
                           "ListUnitPrice", "ContractedUnitPrice", "x_EffectiveUnitPrice"):
                amount = focus_cost_reader._decimal(row.get(column), column)
                if not amount.is_finite() or not math.isfinite(float(amount)):
                    raise FocusCostDataError(f"FOCUS {column} contains a non-finite or out-of-range amount.")
            record = daily_record(row)
            resource_parts = record.resource_id.split("/")
            if record.resource_id.startswith("/subscriptions/") and resource_parts[2] != subscription_id:
                raise FocusCostDataError("A resource ID is outside the configured target subscription.")
            if row.get("ChargePeriodEnd"):
                try:
                    start = datetime.fromisoformat(row["ChargePeriodStart"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(row["ChargePeriodEnd"].replace("Z", "+00:00"))
                    if end <= start:
                        raise ValueError("Unordered charge interval.")
                except (ValueError, TypeError):
                    raise FocusCostDataError("A FOCUS charge interval has an invalid end date.") from None
            observed_days.add(record.date)
            currencies.add(row["BillingCurrency"])
            names[identifier] = record.subscription_name
            count += 1
            consume("history_rows")
    if not count:
        raise FocusCostDataError("The uploaded FOCUS report contains no billing rows.")
    if len(currencies) != 1:
        raise FocusCostDataError("Upload files with one billing currency; mixed currencies cannot be added.")
    days = sorted(observed_days)
    latest_month = days[-1][:7]
    latest_days = [day for day in days if day.startswith(latest_month)]
    last = date.fromisoformat(latest_days[-1])
    complete_month_days = calendar.monthrange(last.year, last.month)[1]
    partial = len(latest_days) != complete_month_days or not latest_days[0].endswith("-01")
    costs = focus_cost_reader.build_focus_cost_data(
        files, [subscription_id], generated_at=generated_at,
        period_bounds=(latest_days[0], latest_days[-1]),
        row_filter=lambda row: charge_day(row["ChargePeriodStart"]).startswith(latest_month),
        account_input=False,
    )
    periods = sorted({day[:7] for day in days})
    history = FocusHistoryData(
        currency=next(iter(currencies)), history_start=days[0], history_end=days[-1],
        complete_days=len(days), periods=periods, subscription_ids=[subscription_id],
        subscription_names=names, records=UploadedHistory(files, count),
        partial_period=None,
    )
    details = CostDetailSummary(
        status="complete", dates=days, partialPeriod=history.partial_period,
        statusMessage="Uploaded daily FOCUS charges. Unobserved dates and unavailable comparisons are not zero.",
    )
    coverage = {
        "startDate": days[0], "endDate": days[-1], "observedDays": len(days),
        "rowCount": count, "currency": history.currency, "assessmentMonth": latest_month,
        "assessmentMonthPartial": partial, "fileCount": len(files),
    }
    return Evidence(costs, history, details, coverage)


async def load(dataset_id: UUID, subscription_id: str) -> Evidence:
    upload = await focus_uploads._owned(dataset_id)
    if upload["subscription_id"] != subscription_id or upload["status"] == "uploading":
        raise FocusCostDataError("The selected dataset is incomplete or belongs to another subscription.")
    files = await (await assessment_store.pool()).fetch(
        "SELECT * FROM assessments.upload_files WHERE dataset_id=$1 ORDER BY ordinal", dataset_id,
    )
    budget = active_budget.get()
    if budget is None:
        raise RuntimeError("Uploaded FOCUS reports must be processed in an isolated budgeted worker.")
    downloaded: dict[str, Path] = {}
    digests: set[str] = set()
    async with focus_uploads.container() as storage:
        for item in files:
            path = budget.spool_directory / f"focus-{item['file_id']}.data"
            blob = storage.get_blob_client(focus_uploads.blob_name(dataset_id, item["file_id"]))
            digest = hashlib.sha256()
            size = 0
            downloader = await blob.download_blob(max_concurrency=1)
            with path.open("wb") as stream:
                async for chunk in downloader.chunks():
                    consume("input_bytes", len(chunk))
                    consume("spool_bytes", len(chunk))
                    size += len(chunk)
                    digest.update(chunk)
                    stream.write(chunk)
            value = digest.hexdigest()
            if size != item["total_bytes"]:
                raise FocusCostDataError("An uploaded file does not match its finalized byte length.")
            if item["sha256"] is not None and value != item["sha256"]:
                raise FocusCostDataError("An uploaded file failed its stored content integrity check.")
            if value in digests:
                raise FocusCostDataError("The report contains duplicate file content. Upload each partition only once.")
            digests.add(value)
            await (await assessment_store.pool()).execute(
                "UPDATE assessments.upload_files SET sha256=$2 WHERE file_id=$1", item["file_id"], value,
            )
            downloaded[f"{item['file_id']}-{item['name']}"] = path
    return await asyncio.to_thread(parse_files, downloaded, subscription_id, upload["created_at"].isoformat())
