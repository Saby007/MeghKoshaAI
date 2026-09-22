"""Deterministic, preview-first PowerShell plans for verified report findings."""

from dataclasses import dataclass

from brand import BRAND_NAME
from findings.models import FindingLine, RemediationPlan


@dataclass(frozen=True)
class RemediationDefinition:
    mode: str
    title: str
    risk: str
    effort: str
    immediate: bool
    prerequisites: tuple[str, ...]
    expected_type: str


DEFINITIONS = {
    "stopped_vms": RemediationDefinition(
        mode="preview",
        title="Preview VM deallocation",
        risk="Medium",
        effort="Low",
        immediate=True,
        prerequisites=(
            "Confirm the VM owner and that no maintenance or recovery activity is pending.",
            "Verify the stopped state is intentional and deallocation will not breach an availability commitment.",
        ),
        expected_type="Microsoft.Compute/virtualMachines",
    ),
    "unattached_disks": RemediationDefinition(
        mode="preview",
        title="Preview unattached disk removal",
        risk="Medium",
        effort="Low",
        immediate=True,
        prerequisites=(
            "Confirm the disk has no owner, restore requirement, active backup, or planned reattachment.",
            "Create and validate a safety snapshot before applying removal when retention is uncertain.",
        ),
        expected_type="Microsoft.Compute/disks",
    ),
    "idle_public_ips": RemediationDefinition(
        mode="preview",
        title="Preview idle public IP release",
        risk="Medium",
        effort="Low",
        immediate=True,
        prerequisites=(
            "Confirm the address is not reserved for DNS, firewall allowlists, failover, or disaster recovery.",
            "Validate that no pending deployment is expected to attach the address.",
        ),
        expected_type="Microsoft.Network/publicIPAddresses",
    ),
    "old_snapshots": RemediationDefinition(
        mode="preview",
        title="Preview old snapshot removal",
        risk="Medium",
        effort="Low",
        immediate=True,
        prerequisites=(
            "Obtain owner sign-off and confirm no restore, legal hold, or retention requirement applies.",
            "Verify an alternative recovery point exists when the snapshot is the only retained copy.",
        ),
        expected_type="Microsoft.Compute/snapshots",
    ),
    "empty_backend_pools": RemediationDefinition(
        mode="inspect",
        title="Inspect empty Application Gateway pools",
        risk="High",
        effort="Medium",
        immediate=False,
        prerequisites=(
            "Review listeners, routing rules, redirects, probes, and planned cutovers with the application owner.",
            "Do not remove a gateway or pool until every dependent rule has been identified.",
        ),
        expected_type="Microsoft.Network/applicationGateways",
    ),
    "empty_load_balancer_backend_pools": RemediationDefinition(
        mode="inspect",
        title="Inspect empty Load Balancer pools",
        risk="High",
        effort="Medium",
        immediate=False,
        prerequisites=(
            "Review frontend IP configurations, rules, outbound rules, probes, and planned cutovers.",
            "Do not remove the Load Balancer until every dependent network flow has been identified.",
        ),
        expected_type="Microsoft.Network/loadBalancers",
    ),
    "idle_virtual_network_gateways": RemediationDefinition(
        mode="inspect",
        title="Inspect zero-traffic Virtual Network Gateways",
        risk="High",
        effort="Medium",
        immediate=False,
        prerequisites=(
            "Confirm tunnel ownership, failover, disaster recovery, and intermittent connectivity requirements.",
            "Review gateway connections and local network gateways before making any change.",
        ),
        expected_type="Microsoft.Network/virtualNetworkGateways",
    ),
    "idle_nat_gateways": RemediationDefinition(
        mode="inspect",
        title="Inspect zero-traffic NAT Gateways",
        risk="High",
        effort="Medium",
        immediate=False,
        prerequisites=(
            "Confirm attached subnets do not require deterministic outbound connectivity or allowlisted addresses.",
            "Review public IP and prefix dependencies before making any change.",
        ),
        expected_type="Microsoft.Network/natGateways",
    ),
    "idle_expressroute_circuits": RemediationDefinition(
        mode="inspect",
        title="Inspect zero-traffic ExpressRoute circuits",
        risk="High",
        effort="High",
        immediate=False,
        prerequisites=(
            "Confirm circuit ownership, contractual commitments, peerings, Global Reach, and failover use.",
            "Coordinate any cancellation or resizing with the connectivity provider and network owner.",
        ),
        expected_type="Microsoft.Network/expressRouteCircuits",
    ),
}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resource_array(lines: list[FindingLine]) -> str:
    values = "\n".join(f"    {_quote(line.resource_id)}" for line in lines)
    return f"$resourceIds = @(\n{values}\n)"


def _common_header(lines: list[FindingLine], definition: RemediationDefinition) -> str:
    apply_parameter = "\n[CmdletBinding()]\nparam([switch]$Apply)\n" if definition.mode == "preview" else "\n"
    return "\n".join(
        (
            f"# Generated by {BRAND_NAME} from the resources in this report.",
            "# Review the prerequisites in the UI. Nothing is changed unless -Apply is explicitly supplied."
            if definition.mode == "preview"
            else "# Inspection only. This script does not change Azure resources.",
            apply_parameter.rstrip(),
            "Set-StrictMode -Version Latest",
            "$ErrorActionPreference = 'Stop'",
            _resource_array(lines),
            f"$expectedType = {_quote(definition.expected_type)}",
            "",
            "function Get-VerifiedResource {",
            "    param([Parameter(Mandatory)][string]$ResourceId)",
            "    $resource = Get-AzResource -ResourceId $ResourceId -ErrorAction Stop",
            "    if ($resource.ResourceType -ine $expectedType) {",
            "        throw \"Unexpected resource type '$($resource.ResourceType)' for $ResourceId\"",
            "    }",
            "    return $resource",
            "}",
        )
    )


def _preview_body(category: str) -> str:
    if category == "stopped_vms":
        action = (
            "    Write-Host \"Previewing deallocation: $($resource.Name)\"\n"
            "    Stop-AzVM -Id $resourceId -WhatIf:(-not $Apply)"
        )
    elif category == "unattached_disks":
        action = (
            "    $disk = Get-AzDisk -ResourceGroupName $resource.ResourceGroupName -DiskName $resource.Name\n"
            "    if ($disk.ManagedBy) {\n"
            "        Write-Warning \"Skipping attached disk $($resource.Name): $($disk.ManagedBy)\"\n"
            "        continue\n"
            "    }\n"
            "    Remove-AzResource -ResourceId $resourceId -WhatIf:(-not $Apply)"
        )
    elif category == "idle_public_ips":
        action = (
            "    $publicIp = Get-AzPublicIpAddress -ResourceGroupName $resource.ResourceGroupName -Name $resource.Name\n"
            "    if ($null -ne $publicIp.IpConfiguration) {\n"
            "        Write-Warning \"Skipping attached public IP $($resource.Name)\"\n"
            "        continue\n"
            "    }\n"
            "    Remove-AzResource -ResourceId $resourceId -WhatIf:(-not $Apply)"
        )
    elif category == "old_snapshots":
        action = (
            "    $snapshot = Get-AzSnapshot -ResourceGroupName $resource.ResourceGroupName -SnapshotName $resource.Name\n"
            "    Write-Host \"Snapshot $($snapshot.Name), created $($snapshot.TimeCreated)\"\n"
            "    Remove-AzResource -ResourceId $resourceId -WhatIf:(-not $Apply)"
        )
    else:
        raise ValueError(f"Unsupported preview remediation category: {category}")

    return "\n".join(
        (
            "",
            "foreach ($resourceId in $resourceIds) {",
            "    $resource = Get-VerifiedResource -ResourceId $resourceId",
            action,
            "}",
        )
    )


def _inspection_body(category: str) -> str:
    if category == "empty_backend_pools":
        inspection = (
            "    $gateway = Get-AzApplicationGateway -ResourceGroupName $resource.ResourceGroupName -Name $resource.Name",
            "    $emptyPools = @($gateway.BackendAddressPools | Where-Object { @($_.BackendAddresses).Count -eq 0 })",
            "    [pscustomobject]@{ Resource = $gateway.Name; EmptyPools = @($emptyPools.Name) -join ', '; Dependencies = @($gateway.RequestRoutingRules.Name) -join ', ' } | Format-List",
        )
    elif category == "empty_load_balancer_backend_pools":
        inspection = (
            "    $loadBalancer = Get-AzLoadBalancer -ResourceGroupName $resource.ResourceGroupName -Name $resource.Name",
            "    [pscustomobject]@{ Resource = $loadBalancer.Name; BackendPools = @($loadBalancer.BackendAddressPools.Name) -join ', '; Rules = @($loadBalancer.LoadBalancingRules.Name) -join ', ' } | Format-List",
        )
    elif category == "idle_virtual_network_gateways":
        inspection = (
            "    $gateway = Get-AzVirtualNetworkGateway -ResourceGroupName $resource.ResourceGroupName -Name $resource.Name",
            "    $gateway | Select-Object Name, GatewayType, VpnType, EnableBgp, ProvisioningState | Format-List",
        )
    elif category == "idle_nat_gateways":
        inspection = (
            "    $resource | Select-Object Name, ResourceGroupName, ResourceType, Location, Properties | Format-List",
        )
    elif category == "idle_expressroute_circuits":
        inspection = (
            "    $circuit = Get-AzExpressRouteCircuit -ResourceGroupName $resource.ResourceGroupName -Name $resource.Name",
            "    $circuit | Select-Object Name, ServiceProviderProvisioningState, CircuitProvisioningState, Peerings, Sku | Format-List",
        )
    else:
        raise ValueError(f"Unsupported inspection remediation category: {category}")
    return "\n".join(
        (
            "",
            "foreach ($resourceId in $resourceIds) {",
            "    $resource = Get-VerifiedResource -ResourceId $resourceId",
            *inspection,
            "}",
            "",
            "Write-Warning 'Inspection only: review routing dependencies before changing any gateway or pool.'",
        )
    )


def build_remediation_plan(category: str, lines: list[FindingLine]) -> RemediationPlan:
    definition = DEFINITIONS.get(category)
    if definition is None:
        raise ValueError(f"Unsupported remediation category: {category}")

    body = _inspection_body(category) if definition.mode == "inspect" else _preview_body(category)
    script = f"{_common_header(lines, definition)}\n{body}\n"
    return RemediationPlan(
        mode=definition.mode,
        title=definition.title,
        risk=definition.risk,
        effort=definition.effort,
        immediate=definition.immediate,
        prerequisites=list(definition.prerequisites),
        script=script,
        resource_count=len(lines),
    )