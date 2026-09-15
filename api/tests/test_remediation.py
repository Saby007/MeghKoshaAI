import pytest

from findings.models import FindingLine
from reports.remediation import build_remediation_plan


def line(category: str, resource_type: str, name: str = "resource-1") -> FindingLine:
    return FindingLine(
        category=category,
        resourceId=(
            f"/subscriptions/sub-1/resourceGroups/rg-1/providers/{resource_type}/{name}"
        ),
        resourceName=name,
        subscriptionId="sub-1",
        monthlyCost=10.0,
        confidence=0.9,
        evidenceType="verified_cost",
        detail="verified evidence",
    )


@pytest.mark.parametrize(
    ("category", "resource_type", "expected_command"),
    [
        ("stopped_vms", "Microsoft.Compute/virtualMachines", "Stop-AzVM -Id"),
        ("unattached_disks", "Microsoft.Compute/disks", "Get-AzDisk"),
        ("idle_public_ips", "Microsoft.Network/publicIPAddresses", "Get-AzPublicIpAddress"),
        ("old_snapshots", "Microsoft.Compute/snapshots", "Get-AzSnapshot"),
    ],
)
def test_preview_plans_are_apply_opt_in(category, resource_type, expected_command):
    plan = build_remediation_plan(category, [line(category, resource_type)])

    assert plan.mode == "preview"
    assert plan.immediate is True
    assert "param([switch]$Apply)" in plan.script
    assert "-WhatIf:(-not $Apply)" in plan.script
    assert expected_command in plan.script
    assert "Invoke-Expression" not in plan.script
    assert "-Force" not in plan.script


def test_unattached_disk_plan_rechecks_attachment_before_previewing_removal():
    plan = build_remediation_plan(
        "unattached_disks", [line("unattached_disks", "Microsoft.Compute/disks")]
    )

    assert "if ($disk.ManagedBy)" in plan.script
    assert "Remove-AzResource -ResourceId" in plan.script


@pytest.mark.parametrize(
    ("category", "resource_type", "expected_command"),
    [
        ("empty_backend_pools", "Microsoft.Network/applicationGateways", "Get-AzApplicationGateway"),
        ("empty_load_balancer_backend_pools", "Microsoft.Network/loadBalancers", "Get-AzLoadBalancer"),
        ("idle_virtual_network_gateways", "Microsoft.Network/virtualNetworkGateways", "Get-AzVirtualNetworkGateway"),
        ("idle_nat_gateways", "Microsoft.Network/natGateways", "Select-Object"),
        ("idle_expressroute_circuits", "Microsoft.Network/expressRouteCircuits", "Get-AzExpressRouteCircuit"),
    ],
)
def test_network_cost_at_risk_plans_are_inspection_only(category, resource_type, expected_command):
    plan = build_remediation_plan(
        category,
        [line(category, resource_type)],
    )

    assert plan.mode == "inspect"
    assert plan.immediate is False
    assert expected_command in plan.script
    assert "Remove-Az" not in plan.script
    assert "$Apply" not in plan.script


def test_resource_ids_are_escaped_and_all_included():
    lines = [
        line("old_snapshots", "Microsoft.Compute/snapshots", "snapshot-one"),
        line("old_snapshots", "Microsoft.Compute/snapshots", "snapshot'two"),
    ]

    plan = build_remediation_plan("old_snapshots", lines)

    assert plan.resource_count == 2
    assert "snapshot-one" in plan.script
    assert "snapshot''two" in plan.script


def test_unknown_category_is_rejected():
    with pytest.raises(ValueError, match="Unsupported remediation category"):
        build_remediation_plan("unknown", [])