"""Cost assessment API with independently validated Entra bearer authentication.

Azure calls use the runtime managed identity with separate per-user authorization.
Subscription access is configured manually outside the application. AI is optional
and disabled by default; this core does not depend on a Static Web Apps session.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool
from pydantic import field_validator

from agents.cost_agent import CostFindingsReport, narrate, narration_status
from anomalies.engine import detect_anomalies
from anomalies.models import AnomalySummary
from reports.builder import build_full_report
from reports.cost_details import build_cost_detail_export
from reports import exports as report_exports
from reports.models import (
    DataCompleteness,
    FullReport,
    FinOpsActionState,
    NativeBudgetSummary,
    RateOptimizationResponse,
    RateOptimizationScenario,
    ReportMetadata,
    ReportSnapshot,
    ReportSnapshotSummary,
)
from services import arm_client
from services import user_arm_client
from services import access_control
from services import chat_responder
from services import exchange_rates as exchange_rate_service
from services import focus_export_download
from services import focus_export_control
from services import focus_schedules
from services import focus_history_reader
from services import finops_actions
from services import rate_optimization
from services import report_email
from services import report_snapshots
from services.entra_tokens import configured_uuid, identity_configuration, REQUIRED_SCOPE
from services.auth import ClientPrincipal, require_tenant_principal
from services import sql_metrics
from services import resource_uptime
from services import service_retirements
from azure.core.exceptions import AzureError
from services.focus_cost_reader import (
    FocusCostDataError,
    load_latest_complete_focus_costs,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.middleware("http")
async def verify_api_identity(request: Request, call_next):
    public_paths = {"/api/health", "/api/auth/config", "/api/storage-onboarding/template"}
    if request.url.path.startswith("/api/") and request.url.path not in public_paths:
        try:
            request.state.verified_principal = await asyncio.to_thread(
                require_tenant_principal, request, _expected_tenant_id(),
            )
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail},
                                headers={"Cache-Control": "no-store", "Vary": "Authorization",
                                         **({"WWW-Authenticate": "Bearer"} if error.status_code == 401 else {}),
                                         **(error.headers or {})})
    response = await call_next(request)
    if request.url.path.startswith(("/api/auth/", "/api/subscriptions", "/api/schedules")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Authorization"
    return response


@app.get("/api/auth/config")
def get_identity_configuration():
    configuration = identity_configuration(_expected_tenant_id())
    return JSONResponse({
        "tenantId": configuration.tenant_id,
        "apiClientId": configuration.api_client_id,
        "webClientId": configuration.web_client_id,
        "scope": f"api://{configuration.api_client_id}/{REQUIRED_SCOPE}",
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/me")
def get_verified_identity(request: Request):
    principal = _control_principal(request)
    return JSONResponse({"userId": principal.user_id, "userDetails": principal.user_details,
                         "tenantId": principal.tenant_id,
                         "features": {"aiNarration": narration_status() == "configured"}},
                        headers={"Cache-Control": "no-store"})


_KQL_DIR = Path(__file__).parent / "kql"
_ADVISOR_SCORE_QUERY = (_KQL_DIR / "advisor_score.kql").read_text()
_STORAGE_ACCOUNTS_QUERY = (_KQL_DIR / "storage_accounts.kql").read_text()
_POLICY_COMPLIANCE_QUERY = (_KQL_DIR / "policy_compliance.kql").read_text()
_QUERIES = {
    "unattached_disks": (_KQL_DIR / "unattached_disks.kql").read_text(),
    "stopped_vms": (_KQL_DIR / "stopped_vms.kql").read_text(),
    "idle_public_ips": (_KQL_DIR / "idle_public_ips.kql").read_text(),
    "empty_backend_pools": (_KQL_DIR / "empty_backend_pools.kql").read_text(),
    "empty_load_balancer_backend_pools": (_KQL_DIR / "empty_load_balancer_backend_pools.kql").read_text(),
    "idle_virtual_network_gateways": (_KQL_DIR / "idle_virtual_network_gateways.kql").read_text(),
    "idle_nat_gateways": (_KQL_DIR / "idle_nat_gateways.kql").read_text(),
    "idle_expressroute_circuits": (_KQL_DIR / "idle_expressroute_circuits.kql").read_text(),
    "old_snapshots": (_KQL_DIR / "old_snapshots.kql").read_text(),
    "unattached_network_interfaces": (_KQL_DIR / "unattached_network_interfaces.kql").read_text(),
    "unassociated_network_security_groups": (_KQL_DIR / "unassociated_network_security_groups.kql").read_text(),
    "unassociated_route_tables": (_KQL_DIR / "unassociated_route_tables.kql").read_text(),
    "empty_availability_sets": (_KQL_DIR / "empty_availability_sets.kql").read_text(),
    "deallocated_virtual_machines": (_KQL_DIR / "deallocated_virtual_machines.kql").read_text(),
    "zero_instance_vm_scale_sets": (_KQL_DIR / "zero_instance_vm_scale_sets.kql").read_text(),
    "empty_app_service_plans": (_KQL_DIR / "empty_app_service_plans.kql").read_text(),
    "stopped_web_apps": (_KQL_DIR / "stopped_web_apps.kql").read_text(),
    "empty_virtual_networks": (_KQL_DIR / "empty_virtual_networks.kql").read_text(),
    "disconnected_private_endpoints": (_KQL_DIR / "disconnected_private_endpoints.kql").read_text(),
    "stopped_aks_clusters": (_KQL_DIR / "stopped_aks_clusters.kql").read_text(),
    "empty_resource_groups": (_KQL_DIR / "empty_resource_groups.kql").read_text(),
    "old_custom_images": (_KQL_DIR / "old_custom_images.kql").read_text(),
    "sql_databases_and_pools": (_KQL_DIR / "sql_databases_and_pools.kql").read_text(),
    "sql_managed_instances_and_pools": (_KQL_DIR / "sql_managed_instances_and_pools.kql").read_text(),
    "sql_virtual_machines": (_KQL_DIR / "sql_virtual_machines.kql").read_text(),
    "compute_ahb_candidates": (_KQL_DIR / "compute_ahb_candidates.kql").read_text(),
    "ai_cognitive_accounts": (_KQL_DIR / "ai_cognitive_accounts.kql").read_text(),
    "ai_foundry_projects": (_KQL_DIR / "ai_foundry_projects.kql").read_text(),
    "ai_ml_workspaces": (_KQL_DIR / "ai_ml_workspaces.kql").read_text(),
    "ai_search_services": (_KQL_DIR / "ai_search_services.kql").read_text(),
}
_METRIC_NETWORK_CATEGORIES = (
    "idle_virtual_network_gateways",
    "idle_nat_gateways",
    "idle_expressroute_circuits",
)
_PROTECTED_TAG_KEYS = ("donotdelete",)
_STALE_CATEGORIES = (
    "unattached_disks",
    "stopped_vms",
    "idle_public_ips",
    "empty_backend_pools",
    "empty_load_balancer_backend_pools",
    "idle_virtual_network_gateways",
    "idle_nat_gateways",
    "idle_expressroute_circuits",
    "old_snapshots",
    "unattached_network_interfaces",
    "unassociated_network_security_groups",
    "unassociated_route_tables",
    "empty_availability_sets",
    "deallocated_virtual_machines",
    "zero_instance_vm_scale_sets",
    "empty_app_service_plans",
    "stopped_web_apps",
    "empty_virtual_networks",
    "disconnected_private_endpoints",
    "stopped_aks_clusters",
    "empty_resource_groups",
    "old_custom_images",
)


class AssessmentRequest(BaseModel):
    subscription_ids: list[str] = Field(alias="subscriptionIds")
    stale_days: Literal[7, 14, 30, 60, 90, 180, 365] = Field(default=90, alias="staleDays")


class RateOptimizationRequest(RateOptimizationScenario):
    subscription_ids: list[str] = Field(alias="subscriptionIds")


class AnomalyRequest(BaseModel):
    subscription_ids: list[str] = Field(alias="subscriptionIds")


class CustomReportRequest(BaseModel):
    snapshot_ids: list[str] = Field(alias="snapshotIds", min_length=1, max_length=12)
    modules: list[str] = Field(min_length=1, max_length=13)

    @field_validator("snapshot_ids")
    @classmethod
    def unique_snapshot_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Snapshot IDs must be unique")
        return values


class FinOpsActionUpdateRequest(BaseModel):
    snapshot_id: str = Field(alias="snapshotId", min_length=8, max_length=64)
    expected_version: int = Field(alias="expectedVersion", ge=0, strict=True)
    status: Literal["open", "in_progress", "completed", "dismissed"]
    owner: str = Field(default="", max_length=320)
    due_date: str | None = Field(default=None, alias="dueDate", max_length=10)
    realized_saving_month: float | None = Field(default=None, ge=0, alias="realizedSavingMonth")
    note: str = Field(default="", max_length=2000)

    @field_validator("due_date")
    @classmethod
    def valid_due_date(cls, value: str | None) -> str | None:
        if value:
            from datetime import date

            date.fromisoformat(value)
        return value


class ReportEmailRequest(BaseModel):
    snapshot_id: str = Field(alias="snapshotId", min_length=8, max_length=64)
    report_type: Literal["executive", "full", "chargeback", "compliance", "finops"] = Field(alias="type")


class BudgetWriteRequest(BaseModel):
    subscription_id: str = Field(default="", alias="subscriptionId")
    name: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_-]+$")
    amount: float = Field(gt=0)
    time_grain: Literal["Monthly", "Quarterly", "Annually"] = Field(alias="timeGrain")
    start_date: str = Field(alias="startDate", min_length=10, max_length=10)
    alert_threshold_percent: float | None = Field(default=None, alias="alertThresholdPercent", gt=0, le=1000)
    alert_email: str | None = Field(default=None, alias="alertEmail", max_length=320)


def _budget_properties(body: BudgetWriteRequest) -> dict:
    properties: dict = {
        "category": "Cost",
        "amount": body.amount,
        "timeGrain": body.time_grain,
        "timePeriod": {"startDate": f"{body.start_date}T00:00:00Z"},
        "filter": {},
        "notifications": {},
    }
    if body.alert_threshold_percent and body.alert_email:
        properties["notifications"] = {
            "actual_GreaterThan_threshold": {
                "enabled": True,
                "operator": "GreaterThan",
                "threshold": body.alert_threshold_percent,
                "contactEmails": [body.alert_email],
                "contactRoles": [],
                "contactGroups": [],
                "thresholdType": "Actual",
            }
        }
    return properties


def _native_budget_summary(subscription_id: str, item: dict) -> NativeBudgetSummary:
    properties = item.get("properties") or {}
    time_period = properties.get("timePeriod") or {}
    current_spend = properties.get("currentSpend") or {}
    forecast_spend = properties.get("forecastSpend") or {}
    return NativeBudgetSummary(
        subscriptionId=subscription_id,
        name=str(item.get("name") or ""),
        category=str(properties.get("category") or ""),
        amount=float(properties.get("amount") or 0),
        currency=str(current_spend.get("unit") or forecast_spend.get("unit") or "USD"),
        timeGrain=str(properties.get("timeGrain") or ""),
        periodStart=str(time_period.get("startDate") or ""),
        periodEnd=str(time_period.get("endDate") or ""),
        currentSpend=(float(current_spend["amount"]) if "amount" in current_spend else None),
        forecastSpend=(float(forecast_spend["amount"]) if "amount" in forecast_spend else None),
        scope=str(item.get("id") or f"/subscriptions/{subscription_id}").split("/providers/Microsoft.Consumption/budgets")[0],
        filter=properties.get("filter") or {},
        observedAt=datetime.now(timezone.utc).isoformat(),
    )


def _render_queries(stale_days: int) -> dict[str, str]:
    return {
        category: query.replace("{StaleDays}", str(stale_days))
        for category, query in _QUERIES.items()
    }


def _exclude_protected_resources(rows: list[dict]) -> tuple[list[dict], int]:
    included: list[dict] = []
    excluded = 0
    for row in rows:
        tags = row.get("tags")
        protected = isinstance(tags, dict) and any(
            str(key).lower() in _PROTECTED_TAG_KEYS for key in tags
        )
        if protected:
            excluded += 1
        else:
            included.append(row)
    return included, excluded


class ExportConfigureRequest(BaseModel):
    model_config = {"extra": "forbid"}
    allow_destination_role_assignment: StrictBool = Field(alias="allowDestinationRoleAssignment")

    @field_validator("allow_destination_role_assignment")
    @classmethod
    def require_destination_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("The export destination-container role assignment must be explicitly confirmed")
        return value


class ScheduleCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}
    subscription_id: str = Field(alias="subscriptionId")
    schedule_start_at: str = Field(alias="scheduleStartAt")

    @field_validator("subscription_id")
    @classmethod
    def validate_subscription(cls, value: str) -> str:
        return configured_uuid(value)


class ScheduleStateRequest(BaseModel):
    model_config = {"extra": "forbid"}
    state: Literal["active", "paused"]
    schedule_start_at: str | None = Field(default=None, alias="scheduleStartAt")


class RunAllExportsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    subscription_ids: list[str] = Field(alias="subscriptionIds", min_length=1, max_length=100)

    @field_validator("subscription_ids")
    @classmethod
    def validate_subscriptions(cls, values: list[str]) -> list[str]:
        normalized = [configured_uuid(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Duplicate subscription selection")
        return normalized


class ExchangeRatesResponse(BaseModel):
    base_currency: str = Field(alias="baseCurrency")
    provider: str
    provider_url: str = Field(alias="providerUrl")
    published_date: str = Field(alias="publishedDate")
    fetched_at: str = Field(alias="fetchedAt")
    stale: bool
    rates: dict[str, float]
    disclaimer: str


def _expected_tenant_id() -> str:
    tenant_id = os.environ.get("AZURE_TENANT_ID", "").strip()
    if not tenant_id:
        raise HTTPException(status_code=503, detail="AZURE_TENANT_ID is not configured")
    return tenant_id


async def _previous_report_snapshot(
    subscription_ids: list[str],
    stale_days: int,
    period_start: str,
):
    try:
        catalog = await report_snapshots.list_snapshots(subscription_ids, limit=50)
        previous = next(
            (
                item for item in catalog
                if item.stale_days == stale_days and item.period_start < period_start
            ),
            None,
        )
        if previous:
            return await report_snapshots.load_snapshot(previous.snapshot_id)
        latest = await report_snapshots.load_latest_snapshot(subscription_ids, stale_days)
        if latest.report.report_metadata.period_start < period_start:
            return latest
    except (report_snapshots.ReportSnapshotNotFoundError, report_snapshots.ReportSnapshotIntegrityError):
        return None
    except Exception:
        logger.warning("Previous completed report snapshot unavailable", exc_info=True)
    return None


def _apply_advisor_score_change(report: FullReport, previous_snapshot) -> FullReport:
    current_score = report.advisor_score.score
    previous_score = previous_snapshot.report.advisor_score.score if previous_snapshot else None
    if current_score is None or previous_score is None:
        return report
    previous_period = previous_snapshot.report.report_metadata.period
    current_period = report.report_metadata.period
    if not previous_period or previous_period == current_period:
        return report
    advisor_score = report.advisor_score.model_copy(update={
        "monthly_change": current_score - previous_score,
        "status": f"{report.advisor_score.status} Compared with completed period {previous_period}.",
    })
    return report.model_copy(update={"advisor_score": advisor_score})


@app.get("/api/exchange-rates", response_model=ExchangeRatesResponse, response_model_by_alias=True)
async def get_exchange_rates(base: str = "USD"):
    try:
        return await exchange_rate_service.get_exchange_rates(base)
    except exchange_rate_service.UnsupportedCurrencyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except exchange_rate_service.ExchangeRateUnavailableError as error:
        raise HTTPException(status_code=503, detail="Exchange rates are temporarily unavailable") from error


@app.post(
    "/api/rate-optimization",
    response_model=RateOptimizationResponse,
    response_model_by_alias=True,
)
async def get_rate_optimization(request: RateOptimizationRequest, http_request: Request):
    principal = _control_principal(http_request)
    request.subscription_ids = await access_control.authorized_subscription_ids(
        principal.entra_object_id, request.subscription_ids
    )
    try:
        subscriptions = await arm_client.list_subscriptions()
    except Exception as error:
        logger.exception("Subscription discovery failed for rate optimization")
        raise HTTPException(status_code=502, detail="Rate optimization scope is unavailable") from error

    subscription_names = {
        item["subscriptionId"]: item["displayName"]
        for item in subscriptions
        if item.get("subscriptionId") in request.subscription_ids
    }
    return await rate_optimization.collect_rate_optimization(
        request.subscription_ids,
        subscription_names,
        RateOptimizationScenario.model_validate(request.model_dump(by_alias=True)),
    )


@app.post("/api/anomalies", response_model=AnomalySummary, response_model_by_alias=True)
async def get_cost_anomalies(request: AnomalyRequest, http_request: Request):
    principal = _control_principal(http_request)
    request.subscription_ids = await access_control.authorized_subscription_ids(
        principal.entra_object_id, request.subscription_ids
    )
    try:
        history = await focus_history_reader.load_complete_focus_history(
            request.subscription_ids,
            required_days=60,
        )
    except FocusCostDataError as error:
        logger.warning("Complete FOCUS anomaly history unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={"message": str(error), "subscriptionIds": request.subscription_ids},
        ) from error
    except Exception as error:
        logger.exception("Cost anomaly detection failed")
        raise HTTPException(status_code=502, detail="Cost anomaly detection failed") from error
    return await asyncio.to_thread(detect_anomalies, history.records, history.currency)


def _control_principal(request: Request) -> ClientPrincipal:
    if isinstance(request, Request):
        verified = getattr(request.state, "verified_principal", None)
        if isinstance(verified, ClientPrincipal):
            return verified
    return require_tenant_principal(request, _expected_tenant_id())


async def _authorized_onboarded_subscription_ids(principal: ClientPrincipal) -> list[str]:
    all_subs = await arm_client.list_subscriptions()
    return await access_control.authorized_subscription_ids(
        principal.entra_object_id, [s["subscriptionId"] for s in all_subs]
    )


async def _authorized_scope(
    principal: ClientPrincipal, subscription_ids: list[str] | None
) -> list[str]:
    """Resolves an optional client-supplied subscription scope down to only the
    subscriptions the signed-in user has Azure RBAC on. A `None` scope (no explicit
    filter) is resolved to the user's full authorized set rather than left unscoped,
    since an unscoped lookup would otherwise return org-wide data (see
    report_snapshots._load_latest / _list_snapshots, which treat an empty/None list
    as "no filter", not "nothing")."""
    if subscription_ids is None:
        return await _authorized_onboarded_subscription_ids(principal)
    return await access_control.authorized_subscription_ids(principal.entra_object_id, subscription_ids)


async def _require_onboarded_subscription(principal: ClientPrincipal, subscription_id: str) -> str:
    try:
        normalized = focus_export_control.normalize_subscription_id(subscription_id)
        await arm_client.get_subscription(normalized)
    except (ValueError, httpx.HTTPStatusError) as error:
        raise HTTPException(status_code=404, detail="Onboarded subscription was not found") from error
    await access_control.require_subscription_access(principal.entra_object_id, normalized)
    return normalized


@app.get("/api/focus-exports")
async def list_focus_exports(request: Request, subscription_id: str = Query(alias="subscriptionId")):
    principal = _control_principal(request)
    subscription_id = await _require_onboarded_subscription(principal, subscription_id)
    files = await focus_export_download.list_focus_files(subscription_id)
    return {
        "subscriptionId": subscription_id,
        "files": [
            {
                "blobName": item.blob_name,
                "fileName": item.file_name,
                "size": item.size,
                "lastModified": item.last_modified,
                "contentType": item.content_type,
            }
            for item in files
        ],
    }


@app.get("/api/focus-exports/download")
async def download_focus_export(
    request: Request,
    subscription_id: str = Query(alias="subscriptionId"),
    blob_name: str = Query(alias="blobName"),
):
    principal = _control_principal(request)
    subscription_id = await _require_onboarded_subscription(principal, subscription_id)
    try:
        item = await focus_export_download.get_focus_file(subscription_id, blob_name)
    except focus_export_download.FocusExportPathError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except focus_export_download.FocusExportFileNotFoundError as error:
        raise HTTPException(status_code=404, detail="FocusCost file was not found") from error

    file_name = item.file_name.replace('"', "")
    return StreamingResponse(
        focus_export_download.stream_focus_file(subscription_id, item.blob_name),
        media_type=item.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Content-Length": str(item.size),
            "Cache-Control": "private, no-store",
        },
    )


def _storage_onboarding_template() -> dict:
    principal_id = os.environ.get("COST_CONTROL_PRINCIPAL_ID", "").strip()
    if not principal_id:
        raise HTTPException(status_code=503, detail="COST_CONTROL_PRINCIPAL_ID is not configured")
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "metadata": {
            "description": "Customer-run Blob Inventory onboarding. Last-access tracking is enabled separately to preserve existing Blob service settings.",
        },
        "parameters": {
            "storageAccountName": {"type": "string", "minLength": 3, "maxLength": 24},
            "inventoryContainerName": {"type": "string", "defaultValue": "meghkosha-inventory"},
        },
        "variables": {
            "readerPrincipalId": principal_id,
            "storageBlobDataReaderRoleId": "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",
            "containerResourceId": "[resourceId('Microsoft.Storage/storageAccounts/blobServices/containers', parameters('storageAccountName'), 'default', parameters('inventoryContainerName'))]",
        },
        "resources": [
            {
                "type": "Microsoft.Storage/storageAccounts/blobServices/containers",
                "apiVersion": "2025-01-01",
                "name": "[format('{0}/default/{1}', parameters('storageAccountName'), parameters('inventoryContainerName'))]",
                "properties": {"publicAccess": "None"},
            },
            {
                "type": "Microsoft.Storage/storageAccounts/inventoryPolicies",
                "apiVersion": "2025-01-01",
                "name": "[format('{0}/default', parameters('storageAccountName'))]",
                "dependsOn": ["[variables('containerResourceId')]"],
                "properties": {
                    "policy": {
                        "enabled": True,
                        "type": "Inventory",
                        "rules": [
                            {
                                "enabled": True,
                                "name": "meghkosha-blob-access",
                                "destination": "[parameters('inventoryContainerName')]",
                                "definition": {
                                    "format": "Csv",
                                    "objectType": "Blob",
                                    "schedule": "Weekly",
                                    "schemaFields": [
                                        "Name", "Creation-Time", "Last-Modified", "Content-Length",
                                        "BlobType", "AccessTier", "AccessTierChangeTime", "LastAccessTime",
                                        "Content-Type", "ArchiveStatus", "EncryptionScope",
                                    ],
                                    "filters": {
                                        "blobTypes": ["blockBlob"],
                                        "includeBlobVersions": False,
                                        "includeDeleted": False,
                                        "includeSnapshots": False,
                                    },
                                },
                            }
                        ],
                    }
                },
            },
            {
                "type": "Microsoft.Authorization/roleAssignments",
                "apiVersion": "2022-04-01",
                "name": "[guid(variables('containerResourceId'), variables('readerPrincipalId'), variables('storageBlobDataReaderRoleId'))]",
                "scope": "[format('Microsoft.Storage/storageAccounts/{0}/blobServices/default/containers/{1}', parameters('storageAccountName'), parameters('inventoryContainerName'))]",
                "dependsOn": ["[variables('containerResourceId')]"],
                "properties": {
                    "principalId": "[variables('readerPrincipalId')]",
                    "principalType": "ServicePrincipal",
                    "roleDefinitionId": "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('storageBlobDataReaderRoleId'))]",
                },
            },
        ],
        "outputs": {
            "inventoryContainerName": {"type": "string", "value": "[parameters('inventoryContainerName')]"},
            "nextStep": {
                "type": "string",
                "value": "[format('az storage account blob-service-properties update --resource-group {0} --account-name {1} --enable-last-access-tracking true', resourceGroup().name, parameters('storageAccountName'))]",
            },
        },
    }


@app.get("/api/subscriptions")
async def get_subscriptions(request: Request):
    principal = _control_principal(request)
    try:
        subs = await arm_client.list_subscriptions()
    except Exception as e:
        logger.exception("Listing subscriptions failed")
        raise HTTPException(status_code=502, detail="Listing subscriptions failed") from e
    authorized = set(
        await access_control.authorized_subscription_ids(
            principal.entra_object_id,
            [s["subscriptionId"] for s in subs],
        )
    )
    return [
        {"subscriptionId": s["subscriptionId"], "displayName": s["displayName"], "state": s.get("state")}
        for s in subs
        if s["subscriptionId"] in authorized
    ]


@app.get("/api/storage-onboarding/template", include_in_schema=False)
async def get_storage_onboarding_template():
    return JSONResponse(
        _storage_onboarding_template(),
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/storage-onboarding/guidance")
async def get_storage_onboarding_guidance(request: Request):
    _control_principal(request)
    return {
        "inventoryContainerName": "meghkosha-inventory",
        "lastAccessCommand": "az storage account blob-service-properties update --resource-group <resource-group> --account-name <storage-account> --enable-last-access-tracking true",
        "networkNotice": "Inventory files stay in the customer storage account. Private-only accounts require a customer-provided network path before this application can read them.",
        "billingNotice": "Blob Inventory is billed per objects scanned and also incurs normal storage/operation charges for generated reports.",
    }


@app.get("/api/schedules")
async def get_schedules(request: Request):
    principal = _control_principal(request)
    try:
        subscriptions = await user_arm_client.discover_schedule_subscriptions(principal)
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Schedule subscription discovery failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Subscription discovery is unavailable. Retry later.") from error
    lookup_limit = asyncio.Semaphore(4)

    async def load(item: dict) -> dict:
        async with lookup_limit:
            return await focus_schedules.load(item)

    return await asyncio.gather(*(load(item) for item in subscriptions))


async def _verified_schedule_subscription(principal: ClientPrincipal, subscription_id: str, *, operation: access_control.SubscriptionOperation | None = None) -> dict:
    try:
        subscription_id = configured_uuid(subscription_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="A valid Azure subscription ID is required.") from None
    if operation is not None:
        await access_control.require_subscription_operation(principal.entra_object_id, subscription_id, operation)
    selected = await user_arm_client.discover_schedule_subscriptions(principal, [subscription_id])
    if len(selected) != 1 or selected[0]["subscriptionId"] != subscription_id:
        raise HTTPException(status_code=403, detail="The subscription is unavailable to this account or the backend managed identity.")
    return selected[0]


@app.get("/api/schedules/{subscription_id}/export")
async def get_export_configuration(request: Request, subscription_id: str):
    principal = _control_principal(request)
    try:
        async with asyncio.timeout(90):
            subscription = await _verified_schedule_subscription(principal, subscription_id, operation="export_write")
            return await focus_schedules.export_configuration(subscription)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="FOCUS export setup checks timed out. No export was created.") from None


@app.put("/api/schedules/{subscription_id}/export")
async def configure_schedule_export(request: Request, subscription_id: str, body: ExportConfigureRequest):
    principal = _control_principal(request)
    try:
        async with asyncio.timeout(90):
            subscription = await _verified_schedule_subscription(principal, subscription_id, operation="export_write")
            result = await focus_schedules.configure_export(subscription, allow_destination_role_assignment=body.allow_destination_role_assignment)
            logger.info("FOCUS export setup for subscription %s by %s; created=%s", subscription["subscriptionId"], principal.entra_object_id, result["created"])
            return result
    except TimeoutError:
        raise HTTPException(status_code=503, detail="FOCUS export configuration timed out. Refresh its status before trying again; no automatic retry was performed.") from None


@app.post("/api/schedules", status_code=201)
async def create_schedule(request: Request, body: ScheduleCreateRequest):
    principal = _control_principal(request)
    subscription = await _verified_schedule_subscription(principal, body.subscription_id)
    try:
        return await focus_schedules.create(subscription, principal.entra_object_id, body.schedule_start_at)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@app.patch("/api/schedules/{subscription_id}")
async def update_schedule(request: Request, subscription_id: str, body: ScheduleStateRequest):
    principal = _control_principal(request)
    subscription = await _verified_schedule_subscription(principal, subscription_id)
    if body.state not in {"active", "paused"}:
        raise HTTPException(status_code=422, detail="Schedule state must be active or paused")
    try:
        return await focus_schedules.update(subscription, principal.entra_object_id, body.state, body.schedule_start_at)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@app.delete("/api/schedules/{subscription_id}", status_code=204)
async def delete_schedule(request: Request, subscription_id: str):
    principal = _control_principal(request)
    subscription = await _verified_schedule_subscription(principal, subscription_id)
    await focus_schedules.update(subscription, principal.entra_object_id, "deleted")


async def _ensure_export_and_schedule(subscription: dict, actor: str) -> None:
    # The Schedule tab's single Export action must be able to cold-start a subscription that has
    # never been configured before: create the export if missing, then make sure a schedule
    # record exists so advance(force=True) has something to lock and overwrite with a fresh cycle.
    await focus_schedules.configure_export(subscription, allow_destination_role_assignment=True)
    start = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    try:
        await focus_schedules.create(subscription, actor, start)
    except HTTPException as error:
        if error.status_code != 409:
            raise


@app.post("/api/schedules/{subscription_id}/run", status_code=202)
async def run_schedule_now(request: Request, subscription_id: str):
    principal = _control_principal(request)
    subscription = await _verified_schedule_subscription(principal, subscription_id, operation="export_write")
    await _ensure_export_and_schedule(subscription, principal.entra_object_id)
    return await focus_schedules.advance(subscription["subscriptionId"], force=True)


@app.post("/api/schedules/run-all", status_code=202)
async def run_all_schedules(request: Request, body: RunAllExportsRequest):
    principal = _control_principal(request)
    selected = await user_arm_client.discover_schedule_subscriptions(principal, body.subscription_ids)
    if {item["subscriptionId"] for item in selected} != set(body.subscription_ids):
        raise HTTPException(status_code=403, detail="The selection includes a subscription outside your verified scope.")
    for item in selected:
        await access_control.require_subscription_operation(principal.entra_object_id, item["subscriptionId"], "export_write")
    results = []
    for item in selected:
        try:
            await _ensure_export_and_schedule(item, principal.entra_object_id)
            results.append(await focus_schedules.advance(item["subscriptionId"], force=True))
        except HTTPException as error:
            results.append({"subscriptionId": item["subscriptionId"], "status": "failed", "error": error.detail})
    return results


@app.get("/api/schedules/{subscription_id}/runs")
async def get_schedule_runs(request: Request, subscription_id: str):
    principal = _control_principal(request)
    subscription = await _verified_schedule_subscription(principal, subscription_id)
    try:
        return await focus_schedules.history(subscription["subscriptionId"])
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Execution history is unavailable. Retry later.") from error


@app.get("/api/report/latest", response_model=ReportSnapshot, response_model_by_alias=True)
async def get_latest_report(
    request: Request,
    subscription_ids: list[str] | None = Query(default=None, alias="subscriptionId"),
    stale_days: int = Query(default=90, alias="staleDays"),
):
    principal = _control_principal(request)
    stale_days = stale_days if isinstance(stale_days, int) else 90
    if stale_days not in {7, 14, 30, 60, 90, 180, 365}:
        raise HTTPException(status_code=422, detail="staleDays must be one of 7, 14, 30, 60, 90, 180, or 365")
    subscription_ids = await _authorized_scope(principal, subscription_ids)
    if not subscription_ids:
        raise HTTPException(
            status_code=404,
            detail="No completed report snapshot is available for this scope",
        )
    try:
        return await report_snapshots.load_latest_snapshot(subscription_ids, stale_days)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="No completed report snapshot is available for this scope",
        ) from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        logger.exception("Completed report snapshot failed integrity validation")
        raise HTTPException(status_code=503, detail="Completed report snapshot is unavailable") from error


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    snapshot_id: str | None = Field(default=None, alias="snapshotId", min_length=8, max_length=64)
    history: list[chat_responder.ChatTurn] = Field(default_factory=list, max_length=6)


@app.post("/api/chat", response_model=chat_responder.ChatAnswer, response_model_by_alias=True)
async def chat(request: Request, body: ChatRequest):
    principal = _control_principal(request)
    try:
        if body.snapshot_id:
            snapshot = await report_snapshots.load_snapshot(body.snapshot_id)
            await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
        else:
            authorized_ids = await _authorized_onboarded_subscription_ids(principal)
            if not authorized_ids:
                raise HTTPException(status_code=404, detail="No completed report snapshot is available")
            snapshot = await report_snapshots.load_latest_snapshot(authorized_ids)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="No completed report snapshot is available") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        logger.exception("Chat report snapshot failed integrity validation")
        raise HTTPException(status_code=503, detail="Completed report snapshot is unavailable") from error
    return await chat_responder.respond_to_question(body.question, snapshot.report, body.history)


@app.get("/api/report/export")
async def export_report(
    request: Request,
    snapshot_id: str = Query(alias="snapshotId", min_length=8, max_length=64),
    report_type: Literal["executive", "full", "chargeback", "compliance", "finops"] = Query(alias="type"),
    subscription_id: str | None = Query(default=None, alias="subscriptionId"),
):
    return await export_snapshot_report(request, snapshot_id, report_type, subscription_id)


async def _build_report_artifact(snapshot: ReportSnapshot, report_type: str, subscription_id: str | None = None):
    # Guards direct (non-ASGI) test invocation, where an omitted Query() parameter is not
    # resolved to its default and the FieldInfo object itself is passed through instead.
    subscription_id = subscription_id if isinstance(subscription_id, str) else None
    if subscription_id is not None:
        subscription_id = subscription_id.strip()
        if report_type != "full":
            raise ValueError("Subscription-scoped export is only supported for the Full report")
    if report_type == "finops":
        catalog = await report_snapshots.list_snapshots(snapshot.subscription_ids, limit=50)
        previous_summary = next(
            (
                item for item in catalog
                if item.snapshot_id != snapshot.snapshot_id
                and item.period_start < snapshot.report.report_metadata.period_start
            ),
            None,
        )
        previous = await report_snapshots.load_snapshot(previous_summary.snapshot_id) if previous_summary else None
        states = await finops_actions.list_actions(snapshot.scope_hash)
        return await asyncio.to_thread(report_exports.build_finops_monthly_xlsx, snapshot, previous, states)
    if report_type == "full":
        return await asyncio.to_thread(report_exports.build_full_xlsx, snapshot, subscription_id)
    builder = {
        "executive": report_exports.build_executive_pdf,
        "chargeback": report_exports.build_chargeback_xlsx,
        "compliance": report_exports.build_compliance_xlsx,
    }[report_type]
    return await asyncio.to_thread(builder, snapshot)


@app.get("/api/report/export/snapshot")
async def export_snapshot_report(
    request: Request,
    snapshot_id: str = Query(alias="snapshotId", min_length=8, max_length=64),
    report_type: Literal["executive", "full", "chargeback", "compliance", "finops"] = Query(alias="type"),
    subscription_id: str | None = Query(default=None, alias="subscriptionId"),
):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(snapshot_id)
        await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
        artifact = await _build_report_artifact(snapshot, report_type, subscription_id)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot failed integrity validation") from error
    except (AzureError, finops_actions.ActionIntegrityError) as error:
        raise HTTPException(status_code=503, detail="Report action data is unavailable. Retry later.") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.file_name}"',
            "Cache-Control": "private, no-store",
            "X-Report-Snapshot-Id": snapshot.snapshot_id,
        },
    )


@app.get("/api/report/retirements")
async def get_report_retirements(
    request: Request,
    snapshot_id: str = Query(alias="snapshotId", min_length=8, max_length=64),
    subscription_id: str | None = Query(default=None, alias="subscriptionId"),
    export: bool = Query(default=False),
):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(snapshot_id)
        await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
        summary = await service_retirements.get_retirements(snapshot, subscription_id)
        if not export:
            return summary.model_dump(by_alias=True)
        artifact = await asyncio.to_thread(service_retirements.export_retirements, summary)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except (report_snapshots.ReportSnapshotIntegrityError, AzureError) as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot is unavailable") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(content=artifact.content, media_type=artifact.media_type, headers={
        "Content-Disposition": f'attachment; filename="{artifact.file_name}"', "Cache-Control": "private, no-store",
    })


@app.get("/api/report/resource-availability", response_model=resource_uptime.ResourceAvailability, response_model_by_alias=True)
async def get_report_resource_availability(
    request: Request,
    snapshot_id: str = Query(alias="snapshotId", min_length=8, max_length=64),
    resource_id: str = Query(alias="resourceId", min_length=1, max_length=2048),
    billing_date: date = Query(alias="date"),
):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(snapshot_id)
        await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except (report_snapshots.ReportSnapshotIntegrityError, AzureError) as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot is unavailable") from error
    details = snapshot.report.cost_details
    if details.status != "complete" or str(billing_date) not in details.dates or not any(row.resource_id.lower() == resource_id.lower() for row in details.rows):
        raise HTTPException(status_code=404, detail="Resource and date were not found in this snapshot's cost evidence")
    return await resource_uptime.get_resource_availability(resource_id, billing_date)


class CostDetailExportRequest(BaseModel):
    snapshot_id: str = Field(alias="snapshotId", min_length=8, max_length=64)
    start_date: date = Field(alias="startDate")
    end_date: date = Field(alias="endDate")
    filters: dict[str, str] = Field(default_factory=dict, max_length=8)
    previous_start: date | None = Field(default=None, alias="previousStart")
    previous_end: date | None = Field(default=None, alias="previousEnd")
    selected_dates: list[date] | None = Field(default=None, alias="selectedDates", min_length=1, max_length=366)


@app.post("/api/report/cost-details/export")
async def export_cost_details(request: Request, body: CostDetailExportRequest):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(body.snapshot_id)
        await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
        artifact = await asyncio.to_thread(
            build_cost_detail_export, snapshot, body.start_date, body.end_date,
            body.filters, body.previous_start, body.previous_end, body.selected_dates,
        )
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except (report_snapshots.ReportSnapshotIntegrityError, AzureError) as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot is unavailable") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(content=artifact.content, media_type=artifact.media_type, headers={
        "Content-Disposition": f'attachment; filename="{artifact.file_name}"',
        "Cache-Control": "private, no-store", "X-Report-Snapshot-Id": snapshot.snapshot_id,
    })


@app.post("/api/report/email")
async def email_report(request: Request, body: ReportEmailRequest):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(body.snapshot_id)
        await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
        delivery = await report_email.send_report_link(
            snapshot,
            body.report_type,
            principal.user_details,
            principal.actor,
        )
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot failed integrity validation") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except report_email.ReportEmailConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except report_email.ReportEmailDeliveryError as error:
        logger.exception("Report email delivery failed")
        raise HTTPException(status_code=502, detail="Report email delivery failed") from error
    return {
        "attemptId": delivery.attempt_id,
        "operationId": delivery.operation_id,
        "recipient": delivery.recipient,
        "status": delivery.status,
    }


@app.get("/api/report/actions", response_model=list[FinOpsActionState], response_model_by_alias=True)
async def get_finops_actions(
    request: Request,
    snapshot_id: str = Query(alias="snapshotId", min_length=8, max_length=64),
):
    principal = _control_principal(request)
    try:
        snapshot = await report_snapshots.load_snapshot(snapshot_id)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot failed integrity validation") from error
    await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
    try:
        return await finops_actions.list_actions(snapshot.scope_hash)
    except (AzureError, finops_actions.ActionIntegrityError) as error:
        raise HTTPException(status_code=503, detail="Saved action state is unavailable. Retry later.") from error


@app.put("/api/report/actions/{action_id}", response_model=FinOpsActionState, response_model_by_alias=True)
async def update_finops_action(
    request: Request,
    action_id: str,
    body: FinOpsActionUpdateRequest,
):
    principal = _control_principal(request)
    if os.environ.get("MEGHKOSHA_ACTION_WRITES_PAUSED", "").lower() == "true":
        raise HTTPException(status_code=503, detail="Action saves are temporarily paused for a release. Your draft is unchanged; retry shortly.",
                            headers={"Retry-After": "30"})
    try:
        snapshot = await report_snapshots.load_snapshot(body.snapshot_id)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="Completed report snapshot was not found") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        raise HTTPException(status_code=503, detail="Completed report snapshot failed integrity validation") from error
    await access_control.require_all_subscription_access(principal.entra_object_id, snapshot.subscription_ids)
    available = {item.action_id for item in snapshot.report.action_plan if item.action_id}
    if action_id not in available:
        raise HTTPException(status_code=404, detail="Action is not present in the selected report snapshot")
    try:
        state = finops_actions.build_state(
            scope_hash=snapshot.scope_hash,
            action_id=action_id,
            status=body.status,
            owner=body.owner,
            due_date=body.due_date,
            realized_saving_month=body.realized_saving_month,
            note=body.note,
            actor=principal.actor,
        )
        state = await finops_actions.save_action(state, expected_version=body.expected_version)
    except finops_actions.ActionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (AzureError, finops_actions.ActionIntegrityError) as error:
        raise HTTPException(status_code=503, detail="The action save could not be confirmed. Reload saved actions before retrying.") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return state


@app.get("/api/budgets", response_model=list[NativeBudgetSummary], response_model_by_alias=True)
async def get_budgets(
    request: Request,
    subscription_ids: list[str] = Query(alias="subscriptionId"),
):
    principal = _control_principal(request)
    normalized = []
    for raw in subscription_ids:
        try:
            normalized.append(focus_export_control.normalize_subscription_id(raw))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="A valid Azure subscription ID is required") from error
    normalized = await access_control.authorized_subscription_ids(principal.entra_object_id, normalized)
    results = await asyncio.gather(
        *(arm_client.list_native_budgets(subscription_id) for subscription_id in normalized),
        return_exceptions=True,
    )
    summaries: list[NativeBudgetSummary] = []
    for subscription_id, budgets in zip(normalized, results):
        if isinstance(budgets, BaseException):
            logger.warning("Budgets unavailable for %s", subscription_id)
            raise HTTPException(status_code=503, detail="Azure budget data could not be read for the complete requested scope. Retry later.")
        summaries.extend(_native_budget_summary(subscription_id, item) for item in budgets)
    return summaries


@app.post("/api/budgets", response_model=NativeBudgetSummary, response_model_by_alias=True, status_code=201)
async def create_budget(request: Request, body: BudgetWriteRequest):
    principal = _control_principal(request)
    try:
        subscription_id = focus_export_control.normalize_subscription_id(body.subscription_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="A valid Azure subscription ID is required") from error
    await access_control.require_subscription_operation(principal.entra_object_id, subscription_id, "budget_write")
    try:
        result = await arm_client.put_native_budget(subscription_id, body.name, _budget_properties(body), e_tag=None)
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail="Creating the Azure budget failed") from error
    return _native_budget_summary(subscription_id, result)


@app.put("/api/budgets/{subscription_id}/{budget_name}", response_model=NativeBudgetSummary, response_model_by_alias=True)
async def update_budget(request: Request, subscription_id: str, budget_name: str, body: BudgetWriteRequest):
    principal = _control_principal(request)
    try:
        normalized = focus_export_control.normalize_subscription_id(subscription_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="A valid Azure subscription ID is required") from error
    await access_control.require_subscription_operation(principal.entra_object_id, normalized, "budget_write")
    existing = await arm_client.get_native_budget(normalized, budget_name)
    if existing is None:
        raise HTTPException(status_code=404, detail="Azure budget was not found")
    try:
        result = await arm_client.put_native_budget(
            normalized,
            budget_name,
            _budget_properties(body),
            e_tag=existing.get("eTag"),
        )
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail="Updating the Azure budget failed") from error
    return _native_budget_summary(normalized, result)


@app.delete("/api/budgets/{subscription_id}/{budget_name}", status_code=204)
async def remove_budget(request: Request, subscription_id: str, budget_name: str):
    principal = _control_principal(request)
    try:
        normalized = focus_export_control.normalize_subscription_id(subscription_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="A valid Azure subscription ID is required") from error
    await access_control.require_subscription_operation(principal.entra_object_id, normalized, "budget_delete")
    try:
        await arm_client.delete_native_budget(normalized, budget_name)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Azure budget was not found") from error
        raise HTTPException(status_code=502, detail="Deleting the Azure budget failed") from error


@app.get("/api/report/snapshots", response_model=list[ReportSnapshotSummary], response_model_by_alias=True)
async def get_report_snapshots(
    request: Request,
    subscription_ids: list[str] | None = Query(default=None, alias="subscriptionId"),
    limit: int = Query(default=50, ge=1, le=100),
):
    principal = _control_principal(request)
    subscription_ids = await _authorized_scope(principal, subscription_ids)
    if not subscription_ids:
        return []
    return await report_snapshots.list_snapshots(subscription_ids, limit=limit)


@app.post("/api/report/export/custom")
async def export_custom_report(request: Request, body: CustomReportRequest):
    principal = _control_principal(request)
    invalid = sorted(set(body.modules) - report_exports.CUSTOM_REPORT_MODULES)
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unsupported custom modules: {', '.join(invalid)}")
    try:
        snapshots = await asyncio.gather(*(report_snapshots.load_snapshot(value) for value in body.snapshot_ids))
        combined_subscription_ids = sorted({sub_id for snap in snapshots for sub_id in snap.subscription_ids})
        await access_control.require_all_subscription_access(principal.entra_object_id, combined_subscription_ids)
        artifact = await asyncio.to_thread(report_exports.build_custom_xlsx, list(snapshots), body.modules)
    except report_snapshots.ReportSnapshotNotFoundError as error:
        raise HTTPException(status_code=404, detail="A selected completed snapshot was not found") from error
    except report_snapshots.ReportSnapshotIntegrityError as error:
        raise HTTPException(status_code=503, detail="A selected completed snapshot failed integrity validation") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.file_name}"',
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/api/report", response_model=FullReport, response_model_by_alias=True)
async def run_report(request: AssessmentRequest, http_request: Request):
    principal = _control_principal(http_request)
    request.subscription_ids = await access_control.authorized_subscription_ids(
        principal.entra_object_id, request.subscription_ids
    )
    if not request.subscription_ids:
        raise HTTPException(status_code=403, detail="You do not have Azure access to any of the requested subscriptions")
    return await _build_and_publish_report(request.subscription_ids, request.stale_days)


async def _build_and_publish_report(subscription_ids: list[str], stale_days: int) -> FullReport:
    try:
        focus_cost_data = await load_latest_complete_focus_costs(subscription_ids)
        all_subs = await arm_client.list_subscriptions()
        subscription_names = {
            s["subscriptionId"]: s["displayName"]
            for s in all_subs
            if s["subscriptionId"] in subscription_ids
        }

        async def _collect_advisor(subscription_id: str):
            try:
                return await arm_client.list_advisor_recommendations(subscription_id)
            except Exception:
                logger.warning("Advisor recommendations unavailable for %s", subscription_id, exc_info=True)
                return []

        async def _collect_history():
            try:
                return await focus_history_reader.load_available_focus_history(
                    subscription_ids,
                    max_periods=12,
                )
            except Exception:
                logger.warning("Executive spend history unavailable", exc_info=True)
                return None

        async def _collect_resource_graph_optional(query: str, label: str):
            try:
                return await arm_client.query_resource_graph(query, subscription_ids)
            except Exception:
                logger.warning("%s unavailable", label, exc_info=True)
                return []

        rendered_queries = _render_queries(stale_days)
        (
            resource_graph_lists,
            advisor_lists,
            untagged_counts,
            focus_history,
            advisor_score_rows,
            storage_accounts,
            policy_compliance_rows,
        ) = await asyncio.gather(
            asyncio.gather(*(arm_client.query_resource_graph(query, subscription_ids) for query in rendered_queries.values())),
            asyncio.gather(*(_collect_advisor(sid) for sid in subscription_ids)),
            arm_client.get_untagged_resource_counts(subscription_ids),
            _collect_history(),
            _collect_resource_graph_optional(_ADVISOR_SCORE_QUERY, "Advisor Score"),
            _collect_resource_graph_optional(_STORAGE_ACCOUNTS_QUERY, "Storage account inventory"),
            _collect_resource_graph_optional(_POLICY_COMPLIANCE_QUERY, "Azure Policy compliance states"),
        )
        resource_graph_rows = dict(zip(_QUERIES.keys(), resource_graph_lists))
        excluded_protected_resources = 0
        for category in _STALE_CATEGORIES:
            filtered, excluded = _exclude_protected_resources(resource_graph_rows.get(category, []))
            resource_graph_rows[category] = filtered
            excluded_protected_resources += excluded
        metric_results = await asyncio.gather(
            *(
                arm_client.collect_zero_traffic_findings(
                    category,
                    resource_graph_rows.get(category, []),
                    focus_cost_data.period_start,
                    focus_cost_data.period_end,
                )
                for category in _METRIC_NETWORK_CATEGORIES
            )
        )
        network_metric_coverage = []
        for category, (findings, coverage) in zip(_METRIC_NETWORK_CATEGORIES, metric_results):
            resource_graph_rows[category] = findings
            network_metric_coverage.append(coverage)
        for subscription_id, export_name in focus_cost_data.subscription_names.items():
            subscription_names.setdefault(subscription_id, export_name)
        advisor_recommendations = [recommendation for advisor in advisor_lists for recommendation in advisor]
        storage_metrics, ai_usage_data, resource_graph_rows = await asyncio.gather(
            arm_client.collect_storage_account_metrics(
                storage_accounts,
                focus_cost_data.period_start,
                focus_cost_data.period_end,
            ),
            arm_client.collect_ai_deployment_usage(
                resource_graph_rows.get("ai_cognitive_accounts", []),
            ),
            sql_metrics.enrich_sql_inventory(resource_graph_rows, subscription_ids),
        )
    except FocusCostDataError as e:
        logger.warning("Complete FocusCost export unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={"message": str(e), "subscriptionIds": subscription_ids},
        ) from e
    except Exception as e:
        logger.exception("Cost assessment failed")
        raise HTTPException(status_code=502, detail="Cost assessment failed") from e

    report = build_full_report(
        subscription_ids,
        subscription_names,
        focus_cost_data.effective_cost_by_subscription,
        resource_graph_rows,
        focus_cost_data.effective_cost_by_resource_id,
        focus_cost_data.service_family_spend,
        advisor_recommendations,
        untagged_counts,
        service_spend=focus_cost_data.provider_spend,
        cost_evidence_by_resource_id={},
        network_metric_coverage=network_metric_coverage,
        report_metadata=ReportMetadata(
            period=focus_cost_data.period,
            period_start=focus_cost_data.period_start,
            period_end=focus_cost_data.period_end,
            cost_basis="FocusCost (EffectiveCost)",
            currency=focus_cost_data.currency,
            generated_at=focus_cost_data.generated_at,
            source="Private completed FocusCost exports",
            stale_days=stale_days,
            protected_tag_keys=list(_PROTECTED_TAG_KEYS),
            excluded_protected_resources=excluded_protected_resources,
        ),
        completeness=DataCompleteness(
            requested_subscriptions=len(subscription_ids),
            available_subscriptions=len(focus_cost_data.subscription_ids),
            complete=True,
            status="Reconciled",
        ),
        focus_cost_data=focus_cost_data,
        focus_history=focus_history,
        advisor_score_rows=advisor_score_rows,
        storage_accounts=storage_accounts,
        storage_metrics=storage_metrics,
        ai_usage_data=ai_usage_data,
        policy_compliance_rows=policy_compliance_rows,
        focus_reconciliation=None,
    )
    previous_snapshot = await _previous_report_snapshot(
        subscription_ids,
        stale_days,
        report.report_metadata.period_start,
    )
    report = _apply_advisor_score_change(report, previous_snapshot)
    try:
        snapshot = report_snapshots.build_snapshot(
            report,
            subscription_ids,
            stale_days,
        )
        await report_snapshots.publish_snapshot(snapshot)
    except Exception:
        # Deliberately non-fatal: the caller still gets the freshly computed report even if
        # persistence fails. Logged at ERROR (not warning) since a silent failure here means
        # "Open saved report" and Chat have nothing to load until the next successful run.
        logger.error("Completed report snapshot publication failed", exc_info=True)
    return report



@app.post("/api/narrate")
async def narrate_findings(request: Request):
    status = narration_status()
    if status != "configured":
        raise HTTPException(status_code=503, detail=f"AI narration is {status} for this environment.")
    try:
        body = await request.json()
        report = CostFindingsReport.model_validate(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        agent_output = await narrate(report)
    except Exception as e:
        logger.exception("Narration failed")
        raise HTTPException(status_code=500, detail="Narration failed") from e

    return agent_output.model_dump() if agent_output else None


@app.get("/api/health")
async def health():
    return {"status": "ok", "features": {"aiNarration": narration_status()}}
