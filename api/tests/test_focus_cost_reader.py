import csv
import gzip
import io
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from services import focus_cost_reader
from services.focus_cost_reader import (
    FocusCostDataError,
    build_focus_cost_data,
    reconcile_focus_to_amortized,
)


SUBSCRIPTION_ID = "616dc9b8-b4aa-415f-8dcb-71bc462916c5"
BLOB_NAME = (
    f"focus/{SUBSCRIPTION_ID}/focus-closed-month-meghkoshaai/"
    "20260701-20260731/run-1/part_0_0001.csv.gz"
)
REQUIRED_DEFAULTS = {
    "BillingPeriodStart": "2026-07-01T00:00:00Z",
    "BillingPeriodEnd": "2026-08-01T00:00:00Z",
    "ChargeClass": "",
    "ChargePeriodStart": "2026-07-01T00:00:00Z",
    "CommitmentDiscountCategory": "",
    "CommitmentDiscountId": "",
    "CommitmentDiscountName": "",
    "CommitmentDiscountStatus": "",
    "CommitmentDiscountType": "",
    "PricingCurrency": "USD",
    "PricingQuantity": "1",
    "PricingUnit": "1 Hour",
    "ResourceName": "vm-1",
    "ResourceType": "Microsoft.Compute/virtualMachines",
    "ServiceCategory": "Compute",
    "ServiceName": "Virtual Machines",
    "SkuId": "sku-1",
    "SkuPriceId": "price-1",
    "SubAccountId": f"/subscriptions/{SUBSCRIPTION_ID}",
    "SubAccountName": "Subscription One",
    "x_EffectiveUnitPrice": "8",
    "x_ResourceGroupName": "rg-1",
    "x_SkuMeterCategory": "Virtual Machines",
    "x_SkuMeterId": "meter-1",
    "x_SkuMeterSubcategory": "Dv5 Series",
    "x_SkuServiceFamily": "Compute",
}


def _gzip_csv(rows: list[dict]) -> bytes:
    columns = sorted(set(REQUIRED_DEFAULTS) | {key for row in rows for key in row})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({**REQUIRED_DEFAULTS, **row})
    return gzip.compress(output.getvalue().encode("utf-8"))


def test_parses_observed_focus_schema_and_aggregates_costs():
    content = _gzip_csv(
        [
            {
                "BilledCost": "10",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "9",
                "ContractedUnitPrice": "9",
                "EffectiveCost": "8",
                "ListCost": "12",
                "ListUnitPrice": "12",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": '{"Team":"Payments","Department":"Engineering","Project":"Checkout"}',
                "ResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
            },
            {
                "BilledCost": "0",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "CommitmentDiscountCategory": "Spend",
                "CommitmentDiscountStatus": "Used",
                "CommitmentDiscountType": "SavingsPlan",
                "ContractedCost": "4",
                "ContractedUnitPrice": "4",
                "EffectiveCost": "3",
                "ListCost": "5",
                "ListUnitPrice": "5",
                "PricingCategory": "Committed",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
            },
        ]
    )

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    assert data.data_version == "1.2-preview"
    assert data.period == "2026-07"
    assert data.row_count == 2
    assert data.billed_cost_by_subscription[SUBSCRIPTION_ID] == 10.0
    assert data.effective_cost_by_subscription[SUBSCRIPTION_ID] == 11.0
    assert data.list_cost_by_subscription[SUBSCRIPTION_ID] == 17.0
    assert data.contracted_cost_by_subscription[SUBSCRIPTION_ID] == 13.0
    assert data.negotiated_discount_by_subscription[SUBSCRIPTION_ID] == 4.0
    assert data.service_spend["Virtual Machines"] == 11.0
    assert data.region_spend == {"eastus": 11.0}
    assert len(data.resource_costs) == 1
    assert data.resource_costs[0].resource_group == "rg-1"
    assert data.resource_costs[0].region == "eastus"
    assert data.resource_costs[0].effective_cost == 11.0
    assert data.tag_spend["Team"] == {"Payments": 8.0, "Unallocated": 3.0}
    assert data.tag_spend["Department"] == {"Engineering": 8.0, "Unallocated": 3.0}
    assert data.tag_spend["Project"] == {"Checkout": 8.0, "Unallocated": 3.0}
    assert data.savings_plan_commitment.row_count == 1
    assert data.savings_plan_commitment.used_effective_cost == 3.0
    assert data.savings_plan_commitment.realized_benefit == 1.0
    assert data.savings_plan_commitment.unused_effective_cost == 0.0
    assert data.reservation_commitment.row_count == 0
    assert len(data.pricing_evidence_by_resource_id[next(iter(data.pricing_evidence_by_resource_id))]) == 2


def test_tags_cascade_down_from_resource_group_when_resource_has_none():
    content = _gzip_csv(
        [
            {
                "BilledCost": "10",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "10",
                "ContractedUnitPrice": "10",
                "EffectiveCost": "10",
                "ListCost": "10",
                "ListUnitPrice": "10",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": '{"Application":"Checkout"}',
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-tagged",
                "x_ResourceGroupName": "rg-1",
            },
            {
                "BilledCost": "5",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "5",
                "ContractedUnitPrice": "5",
                "EffectiveCost": "5",
                "ListCost": "5",
                "ListUnitPrice": "5",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-1/providers/Microsoft.Storage/storageAccounts/st-untagged",
                "x_ResourceGroupName": "rg-1",
            },
            {
                "BilledCost": "3",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "3",
                "ContractedUnitPrice": "3",
                "EffectiveCost": "3",
                "ListCost": "3",
                "ListUnitPrice": "3",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-2/providers/Microsoft.Storage/storageAccounts/st-other-rg",
                "x_ResourceGroupName": "rg-2",
            },
        ]
    )

    data = build_focus_cost_data(
        {BLOB_NAME: content},
        [SUBSCRIPTION_ID],
        resource_group_tags={(SUBSCRIPTION_ID, "rg-1"): {"application": "Checkout"}},
    )

    # The untagged resource in rg-1 inherits "Checkout" from its resource group's tag;
    # the resource in rg-2 (no group tag configured) remains Unallocated.
    assert data.tag_spend["Application"] == {"Checkout": 15.0, "Unallocated": 3.0}


def test_extended_security_update_charges_are_isolated_per_resource():
    content = _gzip_csv(
        [
            {
                "BilledCost": "50",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "50",
                "ContractedUnitPrice": "50",
                "EffectiveCost": "50",
                "ListCost": "50",
                "ListUnitPrice": "50",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-esu",
                "ResourceName": "vm-esu",
                "ServiceName": "Windows Server",
                "x_SkuMeterCategory": "Windows Server Extended Security Updates",
                "x_SkuMeterSubcategory": "2012 R2",
            },
            {
                "BilledCost": "30",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "30",
                "ContractedUnitPrice": "30",
                "EffectiveCost": "30",
                "ListCost": "30",
                "ListUnitPrice": "30",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-esu",
                "ResourceName": "vm-esu",
            },
            {
                "BilledCost": "12",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "12",
                "ContractedUnitPrice": "12",
                "EffectiveCost": "12",
                "ListCost": "12",
                "ListUnitPrice": "12",
                "PricingCategory": "Standard",
                "Region": "eastus",
                "Tags": "{}",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/vm-plain",
                "ResourceName": "vm-plain",
            },
        ]
    )

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    # Only the ESU-meter row's cost is counted, not the VM's other (non-ESU) charges,
    # and the plain VM with no ESU charge is excluded entirely.
    assert len(data.extended_support_costs) == 1
    esu_row = data.extended_support_costs[0]
    assert esu_row.resource_name == "vm-esu"
    assert esu_row.effective_cost == 50.0



def test_standard_focus_region_id_takes_precedence_over_legacy_location_columns():
    content = _gzip_csv(
        [
            {
                "BilledCost": "10",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ContractedCost": "10",
                "ContractedUnitPrice": "10",
                "EffectiveCost": "10",
                "ListCost": "10",
                "ListUnitPrice": "10",
                "PricingCategory": "Standard",
                "RegionId": "eastus2",
                "RegionName": "East US 2",
                "Region": "legacy-region",
                "x_ResourceLocation": "legacy-location",
                "ResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
            }
        ]
    )

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    assert data.region_spend == {"eastus2": 10.0}
    assert data.resource_costs[0].region == "eastus2"


def test_repeated_focus_price_observations_are_aggregated():
    common = {
        "BilledCost": "0.12",
        "BillingCurrency": "USD",
        "ChargeCategory": "Usage",
        "ContractedCost": "0.12",
        "ContractedUnitPrice": "0.005",
        "EffectiveCost": "0.12",
        "ListCost": "0.12",
        "ListUnitPrice": "0.005",
        "PricingCategory": "Standard",
        "PricingQuantity": "24",
        "PricingUnit": "Hours",
        "RegionId": "eastus2",
        "ResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/ip-1",
        "SkuId": "standard-static",
        "SkuPriceId": "standard-static-hourly",
        "x_EffectiveUnitPrice": "0.005",
    }
    content = _gzip_csv([common, common, common])

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])
    evidence = data.pricing_evidence_by_resource_id[next(iter(data.pricing_evidence_by_resource_id))]

    assert len(evidence) == 1
    assert evidence[0].pricing_quantity == 72
    assert evidence[0].list_cost == pytest.approx(0.36)
    assert evidence[0].contracted_cost == pytest.approx(0.36)
    assert evidence[0].effective_cost == pytest.approx(0.36)
    assert evidence[0].billed_cost == pytest.approx(0.36)


def test_purchase_credit_and_unused_commitment_do_not_create_negotiated_discount():
    content = _gzip_csv(
        [
            {"BilledCost": "20", "BillingCurrency": "USD", "ChargeCategory": "Purchase", "ContractedCost": "0", "ContractedUnitPrice": "0", "EffectiveCost": "2", "ListCost": "20", "ListUnitPrice": "20", "PricingCategory": "Committed", "ResourceId": ""},
            {"BilledCost": "-5", "BillingCurrency": "USD", "ChargeCategory": "Credit", "ContractedCost": "0", "ContractedUnitPrice": "0", "EffectiveCost": "-5", "ListCost": "5", "ListUnitPrice": "5", "PricingCategory": "Standard", "ResourceId": ""},
            {"BilledCost": "0", "BillingCurrency": "USD", "ChargeCategory": "Usage", "CommitmentDiscountStatus": "Unused", "ContractedCost": "0", "ContractedUnitPrice": "0", "EffectiveCost": "1", "ListCost": "10", "ListUnitPrice": "10", "PricingCategory": "Committed", "ResourceId": ""},
        ]
    )

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    assert data.negotiated_discount_by_subscription.get(SUBSCRIPTION_ID, 0.0) == 0.0


def test_unused_commitment_cost_is_separate_from_realized_benefit():
    content = _gzip_csv(
        [
            {
                "BilledCost": "0",
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "CommitmentDiscountStatus": "Unused",
                "CommitmentDiscountType": "Reservation",
                "ContractedCost": "0",
                "ContractedUnitPrice": "0",
                "EffectiveCost": "4",
                "ListCost": "0",
                "ListUnitPrice": "0",
                "PricingCategory": "Committed",
                "ResourceId": "",
            }
        ]
    )

    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    assert data.reservation_commitment.row_count == 1
    assert data.reservation_commitment.unused_effective_cost == 4.0
    assert data.reservation_commitment.realized_benefit == 0.0


def test_rejects_missing_columns_wrong_version_and_multiple_currencies():
    incomplete = gzip.compress(b"BilledCost,BillingCurrency\n1,USD\n")
    with pytest.raises(FocusCostDataError, match="missing columns"):
        build_focus_cost_data({BLOB_NAME: incomplete}, [SUBSCRIPTION_ID])
    with pytest.raises(FocusCostDataError, match="Unsupported FOCUS"):
        build_focus_cost_data({BLOB_NAME: _gzip_csv([])}, [SUBSCRIPTION_ID], data_version="1.0")

    mixed = _gzip_csv(
        [
            {"BilledCost": "1", "BillingCurrency": "USD", "ChargeCategory": "Usage", "ContractedCost": "1", "ContractedUnitPrice": "1", "EffectiveCost": "1", "ListCost": "1", "ListUnitPrice": "1", "PricingCategory": "Standard", "ResourceId": ""},
            {"BilledCost": "1", "BillingCurrency": "EUR", "ChargeCategory": "Usage", "ContractedCost": "1", "ContractedUnitPrice": "1", "EffectiveCost": "1", "ListCost": "1", "ListUnitPrice": "1", "PricingCategory": "Standard", "ResourceId": ""},
        ]
    )
    with pytest.raises(FocusCostDataError, match="one billing currency"):
        build_focus_cost_data({BLOB_NAME: mixed}, [SUBSCRIPTION_ID])


def test_reconciliation_uses_effective_cost_and_tolerance():
    content = _gzip_csv(
        [{"BilledCost": "10", "BillingCurrency": "USD", "ChargeCategory": "Usage", "ContractedCost": "10", "ContractedUnitPrice": "10", "EffectiveCost": "9.999", "ListCost": "10", "ListUnitPrice": "10", "PricingCategory": "Standard", "ResourceId": ""}]
    )
    data = build_focus_cost_data({BLOB_NAME: content}, [SUBSCRIPTION_ID])

    reconciled = reconcile_focus_to_amortized(data, {SUBSCRIPTION_ID: 10.0})
    mismatch = reconcile_focus_to_amortized(data, {SUBSCRIPTION_ID: 12.0})

    assert reconciled.reconciled is True
    assert reconciled.variance == pytest.approx(-0.001)
    assert mismatch.reconciled is False


def _run(subscription_id: str, run_id: str, status: str = "Completed", period: str = "2026-07"):
    return {
        "name": run_id,
        "properties": {
            "status": status,
            "startDate": f"{period}-01T00:00:00",
            "endDate": f"{period}-31T00:00:00",
            "processingEndTime": "2026-08-16T08:20:37Z",
        },
    }


def test_selects_latest_common_completed_period_and_rejects_partial_scope():
    sub_2 = "6efdb685-f5ce-4be4-9841-83d09c4ac05a"
    runs = {
        SUBSCRIPTION_ID: [_run(SUBSCRIPTION_ID, "run-july"), _run(SUBSCRIPTION_ID, "run-aug", period="2026-08")],
        sub_2: [_run(sub_2, "run-july"), _run(sub_2, "queued", status="Queued", period="2026-08")],
    }

    period, selected, path = focus_cost_reader._select_common_runs([SUBSCRIPTION_ID, sub_2], runs)

    assert period == "2026-07"
    assert path == "20260701-20260731"
    assert selected[SUBSCRIPTION_ID]["name"] == "run-july"

    with pytest.raises(FocusCostDataError, match="every selected subscription"):
        focus_cost_reader._select_common_runs(
            [SUBSCRIPTION_ID, sub_2],
            {SUBSCRIPTION_ID: [_run(SUBSCRIPTION_ID, "run-aug", period="2026-08")], sub_2: runs[sub_2]},
        )


@pytest.mark.parametrize("status_code", [404, 403, 429])
def test_export_lookup_errors_distinguish_missing_setup_from_service_failures(monkeypatch, status_code):
    request = httpx.Request("GET", "https://management.azure.com/example/runHistory")
    response = httpx.Response(status_code, request=request)

    async def unavailable_runs(subscription_id, export_name):
        raise httpx.HTTPStatusError("Export lookup failed", request=request, response=response)

    monkeypatch.setattr(focus_cost_reader.arm_client, "list_cost_export_runs", unavailable_runs)
    focus_cost_reader._cache.clear()

    if status_code == 404:
        with pytest.raises(FocusCostDataError, match="Schedules"):
            asyncio.run(focus_cost_reader.load_latest_complete_focus_costs([SUBSCRIPTION_ID]))
    else:
        with pytest.raises(httpx.HTTPStatusError) as error:
            asyncio.run(focus_cost_reader.load_latest_complete_focus_costs([SUBSCRIPTION_ID]))
        assert error.value.response.status_code == status_code
    assert not focus_cost_reader._cache


def test_loads_latest_complete_focus_costs_from_run_history_and_blobs(monkeypatch):
    content = _gzip_csv(
        [{"BilledCost": "10", "BillingCurrency": "USD", "ChargeCategory": "Usage", "ContractedCost": "9", "ContractedUnitPrice": "9", "EffectiveCost": "8", "ListCost": "12", "ListUnitPrice": "12", "PricingCategory": "Standard", "ResourceId": ""}]
    )
    blob_name = BLOB_NAME.replace("run-1", "completed-run")

    async def list_runs(subscription_id, export_name):
        assert subscription_id == SUBSCRIPTION_ID
        assert export_name == "focus-closed-month-meghkoshaai"
        return [_run(subscription_id, "completed-run")]

    class Container:
        def list_blobs(self, name_starts_with):
            assert name_starts_with in blob_name
            return [
                SimpleNamespace(
                    name=blob_name,
                    size=len(content),
                    last_modified=datetime(2026, 8, 16, tzinfo=timezone.utc),
                )
            ]

        def download_blob(self, name):
            assert name == blob_name
            return SimpleNamespace(readall=lambda: content)

    monkeypatch.setattr(focus_cost_reader.arm_client, "list_cost_export_runs", list_runs)
    monkeypatch.setattr(focus_cost_reader.focus_export_download, "get_container_client", lambda: Container())
    focus_cost_reader._cache.clear()

    data = asyncio.run(focus_cost_reader.load_latest_complete_focus_costs([SUBSCRIPTION_ID]))

    assert data.period == "2026-07"
    assert data.effective_cost_by_subscription[SUBSCRIPTION_ID] == 8.0