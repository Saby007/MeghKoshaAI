"""Pydantic models for the full multi-tab cost assessment report - mirrors the
original Excel workbook's tabs (PROJECT.md Part I): Executive Summary, Savings Roadmap,
Subscription Breakdown, Compute/Storage/Network Optimization, Advisor Reconciliation,
Governance & Risk, Action Plan. Field aliases are camelCase for the frontend.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from findings.models import FindingCategorySummary


class ExecutiveSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    current_monthly_spend: float = Field(alias="currentMonthlySpend")
    potential_savings_month: float = Field(alias="potentialSavingsMonth")
    potential_savings_year: float = Field(alias="potentialSavingsYear")
    pct_recoverable: float = Field(alias="pctRecoverable")
    estimated_wastage_month: float = Field(alias="estimatedWastageMonth")
    pct_wastage: float = Field(alias="pctWastage")
    active_resources: int = Field(alias="activeResources")
    idle_resources: int = Field(alias="idleResources")
    idle_review_candidates: int | None = Field(default=None, alias="idleReviewCandidates")
    idle_resource_percentage: float = Field(alias="idleResourcePercentage")
    spend_change_percentage: float | None = Field(alias="spendChangePercentage")


class SpendCategorySummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    monthly_spend: float = Field(alias="monthlySpend")
    pct_of_total: float = Field(alias="pctOfTotal")


class MonthlySpendPoint(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    month: str
    total: float
    category_spend: dict[str, float] = Field(alias="categorySpend")
    subscription_spend: dict[str, float] = Field(default_factory=dict, alias="subscriptionSpend")
    subscription_category_spend: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        alias="subscriptionCategorySpend",
    )


class SpendHistorySummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["complete", "partial"]
    status_message: str = Field(alias="statusMessage")
    months: list[MonthlySpendPoint]


class DailyCostPoint(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date: str
    total_cost: float = Field(alias="totalCost")
    average_hourly_cost: float = Field(alias="averageHourlyCost")


class DailyCostTrendSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["complete", "partial", "unavailable"]
    status_message: str = Field(alias="statusMessage")
    # average hourly cost is total_cost / 24, a derived split of Azure's daily-granularity
    # billing data - Cost Management has no true hourly cost dimension.
    days: list[DailyCostPoint]


class TagDailyCostSeries(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tag_key: str = Field(alias="tagKey", default="")
    tag_value: str = Field(alias="tagValue")
    total_cost: float = Field(alias="totalCost")
    days: list[DailyCostPoint]


class TagDailyCostTrendSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["complete", "partial", "unavailable"]
    status_message: str = Field(alias="statusMessage")
    tag_key: str = Field(alias="tagKey")
    available_tag_values: list[str] = Field(default_factory=list, alias="availableTagValues")
    series: list[TagDailyCostSeries]
    distribution_series: list[TagDailyCostSeries] = Field(default_factory=list, alias="distributionSeries")
    window_dates: list[str] = Field(default_factory=list, alias="windowDates")


class CostHierarchyItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    resource_group: str = Field(alias="resourceGroup")
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    resource_type: str = Field(alias="resourceType")
    monthly_spend: float = Field(alias="monthlySpend")
    pct_of_total: float = Field(alias="pctOfTotal")


class CostDetailRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    detail_id: str = Field(alias="detailId")
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    resource_type: str = Field(alias="resourceType")
    resource_group: str = Field(alias="resourceGroup")
    service_name: str = Field(alias="serviceName")
    region: str
    tags: dict[str, str]
    tag_attribution_source: str = Field(alias="tagAttributionSource")
    daily_costs: dict[str, float] = Field(alias="dailyCosts")


class CostDetailSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["complete", "unavailable"] = "unavailable"
    status_message: str = Field(default="Resource-level daily costs are unavailable in this snapshot. Run a new report.", alias="statusMessage")
    cost_basis: str = Field(default="EffectiveCost", alias="costBasis")
    granularity: Literal["daily"] = "daily"
    dates: list[str] = Field(default_factory=list)
    rows: list[CostDetailRow] = Field(default_factory=list)


class RegionSpendSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region: str
    monthly_spend: float = Field(alias="monthlySpend")
    pct_of_total: float = Field(alias="pctOfTotal")


class AdvisorScoreSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    available: bool
    score: float | None
    cost_score: float | None = Field(alias="costScore")
    monthly_change: float | None = Field(alias="monthlyChange")
    subscription_count: int = Field(alias="subscriptionCount")
    status: str


class StorageTierVolume(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tier: str
    bytes: float


class StorageAccountTierAnalysis(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    storage_account_id: str = Field(alias="storageAccountId")
    storage_account_name: str = Field(alias="storageAccountName")
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    resource_group: str = Field(alias="resourceGroup")
    location: str
    current_tier: str = Field(alias="currentTier")
    size_bytes: float | None = Field(alias="sizeBytes")
    tier_volumes: list[StorageTierVolume] = Field(alias="tierVolumes")
    access_pattern: str = Field(alias="accessPattern")
    read_transactions: float | None = Field(alias="readTransactions")
    recommended: str
    estimated_saving_month: float | None = Field(alias="estimatedSavingMonth")
    monthly_cost: float | None = Field(alias="monthlyCost")
    evidence_status: str = Field(alias="evidenceStatus")


class StorageOptimizationSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str
    accounts: list[StorageAccountTierAnalysis]
    current_tier_volumes: list[StorageTierVolume] = Field(alias="currentTierVolumes")
    recommended_tier_volumes: list[StorageTierVolume] = Field(alias="recommendedTierVolumes")


class AIModelDeploymentUsage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: str = Field(alias="accountId")
    account_name: str = Field(alias="accountName")
    subscription_id: str = Field(alias="subscriptionId")
    location: str
    deployment_name: str = Field(alias="deploymentName")
    model_name: str = Field(alias="modelName")
    model_version: str = Field(alias="modelVersion")
    sku_name: str = Field(alias="skuName")
    capacity: float | None
    input_tokens_per_day: float | None = Field(alias="inputTokensPerDay")
    output_tokens_per_day: float | None = Field(alias="outputTokensPerDay")
    total_tokens_per_day: float | None = Field(alias="totalTokensPerDay")
    estimated_cost_day: float | None = Field(alias="estimatedCostDay")
    trend_percentage: float | None = Field(alias="trendPercentage")
    trend_label: str = Field(alias="trendLabel")
    evidence_status: str = Field(alias="evidenceStatus")


class AIOptimizationOpportunity(BaseModel):
    deployment_name: str = Field(alias="deploymentName")
    category: str
    recommendation: str
    evidence: str
    priority: Literal["Low", "Medium", "High"]


class AIUsageSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str
    period_start: str = Field(alias="periodStart")
    period_end: str = Field(alias="periodEnd")
    deployments: list[AIModelDeploymentUsage]
    opportunities: list[AIOptimizationOpportunity]


class ChargebackRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dimension: str
    value: str
    monthly_cost: float = Field(alias="monthlyCost")
    pct_of_total: float = Field(alias="pctOfTotal")


class ChargebackSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    allocated_cost: float = Field(alias="allocatedCost")
    unallocated_cost: float = Field(alias="unallocatedCost")
    allocation_percentage: float = Field(alias="allocationPercentage")
    rows: list[ChargebackRow]


class TagValueCost(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: str
    monthly_cost: float = Field(alias="monthlyCost")
    pct_of_total: float = Field(alias="pctOfTotal")
    forecast_next_month: float | None = Field(alias="forecastNextMonth")


class TagDimensionCost(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tag_key: str = Field(alias="tagKey")
    unallocated_cost: float = Field(alias="unallocatedCost")
    rows: list[TagValueCost]


class TagCostSummary(BaseModel):
    """Cost grouped by every FOCUS tag key actually found (case-insensitive) - on a
    resource or inherited from its resource group - with a simple next-month forecast
    derived from the same MoM total-spend growth rate used for the executive summary."""

    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    total_spend: float = Field(alias="totalSpend")
    growth_rate: float | None = Field(alias="growthRate")
    dimensions: list[TagDimensionCost]


class ExtendedSupportRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    resource_group: str = Field(alias="resourceGroup")
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    resource_type: str = Field(alias="resourceType")
    monthly_cost: float = Field(alias="monthlyCost")


class ExtendedSupportSummary(BaseModel):
    """Resources billed for Azure Extended Security Updates (ESU) - identified from the
    FOCUS ServiceName/meter category/subcategory columns, not a Resource Graph query."""

    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    total_monthly_cost: float = Field(alias="totalMonthlyCost")
    rows: list[ExtendedSupportRow]


class OffHoursSavingsRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    resource_group: str = Field(alias="resourceGroup")
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    monthly_cost: float = Field(alias="monthlyCost")
    estimated_monthly_saving: float = Field(alias="estimatedMonthlySaving")


class OffHoursSavingsSummary(BaseModel):
    """Estimated saving if each running compute VM were stopped/deallocated outside a
    standard business-hours schedule. A scheduling estimate, not a measured-utilization
    finding - the FOCUS data has no historical VM power-state/CPU signal to verify from."""

    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    off_hours_fraction: float = Field(alias="offHoursFraction")
    total_monthly_cost: float = Field(alias="totalMonthlyCost")
    total_estimated_saving: float = Field(alias="totalEstimatedSaving")
    rows: list[OffHoursSavingsRow]


class ComplianceRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    total_resources: int = Field(alias="totalResources")
    untagged_resources: int = Field(alias="untaggedResources")
    tagging_percentage: float = Field(alias="taggingPercentage")
    compliant_evaluations: int = Field(alias="compliantEvaluations")
    non_compliant_evaluations: int = Field(alias="nonCompliantEvaluations")
    conflict_evaluations: int = Field(alias="conflictEvaluations")
    exempt_evaluations: int = Field(alias="exemptEvaluations")
    not_started_evaluations: int = Field(alias="notStartedEvaluations")
    non_compliant_resources: int = Field(alias="nonCompliantResources")
    policy_assignment_count: int = Field(alias="policyAssignmentCount")
    evaluation_compliance_percentage: float | None = Field(alias="evaluationCompliancePercentage")
    policy_data_available: bool = Field(alias="policyDataAvailable")
    status: str


class ComplianceSummary(BaseModel):
    available: bool
    status: str
    rows: list[ComplianceRow]


class ReportMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    period: str
    period_start: str = Field(alias="periodStart")
    period_end: str = Field(alias="periodEnd")
    cost_basis: str = Field(alias="costBasis")
    currency: str
    generated_at: str = Field(alias="generatedAt")
    source: str
    stale_days: int = Field(default=90, alias="staleDays")
    protected_tag_keys: list[str] = Field(default_factory=list, alias="protectedTagKeys")
    excluded_protected_resources: int = Field(default=0, alias="excludedProtectedResources")


class DataCompleteness(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    requested_subscriptions: int = Field(alias="requestedSubscriptions")
    available_subscriptions: int = Field(alias="availableSubscriptions")
    complete: bool
    status: str


class OperationalSignal(BaseModel):
    key: str
    label: str
    value: str
    detail: str
    tone: str


class ServiceSpendSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rank: int
    service_name: str = Field(alias="serviceName")
    display_name: str = Field(alias="displayName")
    monthly_spend: float = Field(alias="monthlySpend")
    pct_of_total: float = Field(alias="pctOfTotal")


class MetricCoverageSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    candidate_count: int = Field(alias="candidateCount")
    complete_count: int = Field(alias="completeCount")
    zero_traffic_count: int = Field(alias="zeroTrafficCount")
    unavailable_count: int = Field(alias="unavailableCount")
    period_start: str = Field(alias="periodStart")
    period_end: str = Field(alias="periodEnd")


class SubscriptionReference(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")


class ExecutiveFinding(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rank: int
    category: str
    finding: str
    evidence: str
    impact_type: Literal["potential_savings", "cost_at_risk", "inventory"] = Field(alias="impactType")
    monthly_saving: float | None = Field(alias="monthlySaving")
    monthly_cost_at_risk: float | None = Field(alias="monthlyCostAtRisk")
    severity: str


class DomainAdvisorRecommendation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    problem: str
    recommendation: str
    estimated_savings: float | None = Field(alias="estimatedSavings")
    savings_period: str = Field(alias="savingsPeriod")
    billed_cost: float | None = Field(alias="billedCost")


class DomainSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    domain: str
    domain_spend_month: float = Field(alias="domainSpendMonth")
    verified_saving_month: float = Field(alias="verifiedSavingMonth")
    verified_saving_year: float = Field(alias="verifiedSavingYear")
    monthly_cost_at_risk: float = Field(alias="monthlyCostAtRisk")
    pct_of_domain_spend: float = Field(alias="pctOfDomainSpend")
    pct_of_total_spend: float = Field(alias="pctOfTotalSpend")
    categories: list[FindingCategorySummary]
    advisor_recommendations: list[DomainAdvisorRecommendation] = Field(
        default_factory=list, alias="advisorRecommendations"
    )


class SubscriptionBreakdownRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    current_spend: float = Field(alias="currentSpend")
    category_costs: dict[str, float] = Field(alias="categoryCosts")
    total_waste: float = Field(alias="totalWaste")
    pct_saved: float = Field(alias="pctSaved")


class SavingsRoadmapItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    opportunity: str
    domain: str
    monthly: float
    annual: float
    resources: int
    recommended_action: str = Field(alias="recommendedAction")
    impact_type: Literal["potential_savings", "cost_at_risk", "inventory"] = Field(alias="impactType")
    immediate: bool
    risk: str
    effort: str
    prerequisites: list[str]
    affected_subscriptions: list[SubscriptionReference] = Field(alias="affectedSubscriptions")


class ActionPlanItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action_id: str = Field(default="", alias="actionId")
    action: str
    saving_month: float = Field(alias="savingMonth")
    prerequisite: str
    affected_subscriptions: list[SubscriptionReference] = Field(alias="affectedSubscriptions")


class AdvisorMeasure(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    measure: str
    value: str
    monetary_value: float | None = Field(default=None, alias="monetaryValue")
    explanation: str


class AdvisorReconciliation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    measures: list[AdvisorMeasure]
    recommendations: list[DomainAdvisorRecommendation]


class GovernanceRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    untagged_resources: int = Field(alias="untaggedResources")
    pct_of_estate: float = Field(alias="pctOfEstate")


class PricingSubscriptionSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    billed_cost: float = Field(alias="billedCost")
    effective_cost: float = Field(alias="effectiveCost")
    list_cost: float = Field(alias="listCost")
    contracted_cost: float = Field(alias="contractedCost")
    negotiated_discount: float = Field(alias="negotiatedDiscount")
    negotiated_discount_percentage: float = Field(alias="negotiatedDiscountPercentage")


class PricingSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    data_version: str | None = Field(alias="dataVersion")
    period: str | None
    billing_currency: str | None = Field(alias="billingCurrency")
    pricing_currencies: list[str] = Field(alias="pricingCurrencies")
    billed_cost: float = Field(alias="billedCost")
    effective_cost: float = Field(alias="effectiveCost")
    list_cost: float = Field(alias="listCost")
    contracted_cost: float = Field(alias="contractedCost")
    negotiated_discount: float = Field(alias="negotiatedDiscount")
    negotiated_discount_percentage: float = Field(alias="negotiatedDiscountPercentage")
    reconciliation_variance: float | None = Field(alias="reconciliationVariance")
    subscriptions: list[PricingSubscriptionSummary]


class CommitmentBenefitBreakdown(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    row_count: int = Field(alias="rowCount")
    used_effective_cost: float = Field(alias="usedEffectiveCost")
    unused_effective_cost: float = Field(alias="unusedEffectiveCost")
    realized_benefit: float = Field(alias="realizedBenefit")


class CommitmentSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    observed: bool
    status: str
    period: str
    currency: str
    reservations: CommitmentBenefitBreakdown
    savings_plans: CommitmentBenefitBreakdown = Field(alias="savingsPlans")


class ComputeCoverageSplit(BaseModel):
    """PAYG vs Savings Plan vs Reservation coverage of Compute VM FOCUS usage cost."""

    model_config = ConfigDict(populate_by_name=True)

    total_cost: float = Field(alias="totalCost")
    payg_cost: float = Field(alias="paygCost")
    savings_plan_cost: float = Field(alias="savingsPlanCost")
    reservation_cost: float = Field(alias="reservationCost")
    payg_percentage: float | None = Field(alias="paygPercentage")
    savings_plan_percentage: float | None = Field(alias="savingsPlanPercentage")
    reservation_percentage: float | None = Field(alias="reservationPercentage")


class CommitmentMonthPoint(BaseModel):
    """One month of MoM reservation/savings-plan/spot commitment economics, built from
    FOCUS history rows only - never combined with Advisor/Azure recommendation projections."""

    model_config = ConfigDict(populate_by_name=True)

    month: str
    currency: str

    reservation_committed_cost: float = Field(alias="reservationCommittedCost")
    reservation_used_cost: float = Field(alias="reservationUsedCost")
    reservation_unused_cost: float = Field(alias="reservationUnusedCost")
    reservation_realized_savings: float = Field(alias="reservationRealizedSavings")
    reservation_coverage_percentage: float | None = Field(alias="reservationCoveragePercentage")

    savings_plan_committed_cost: float = Field(alias="savingsPlanCommittedCost")
    savings_plan_used_cost: float = Field(alias="savingsPlanUsedCost")
    savings_plan_unused_cost: float = Field(alias="savingsPlanUnusedCost")
    savings_plan_realized_savings: float = Field(alias="savingsPlanRealizedSavings")

    acd_effective_cost: float = Field(alias="acdEffectiveCost")
    acd_least_price_cost: float = Field(alias="acdLeastPriceCost")
    acd_savings: float = Field(alias="acdSavings")
    acd_savings_percentage: float | None = Field(alias="acdSavingsPercentage")

    spot_effective_cost: float = Field(alias="spotEffectiveCost")
    spot_least_price_cost: float = Field(alias="spotLeastPriceCost")
    spot_savings: float = Field(alias="spotSavings")
    spot_savings_percentage: float | None = Field(alias="spotSavingsPercentage")

    compute_vm_coverage: ComputeCoverageSplit = Field(alias="computeVmCoverage")


class CommitmentInsights(BaseModel):
    """MoM commitment-discount insights: reservation/savings-plan realized savings, Compute
    VM PAYG vs commitment coverage, org-level reservation coverage, ACD vs least price, and
    Spot savings. Every figure is derived only from FOCUS history rows already collected for
    spendHistory - no additional Azure calls and no Advisor/rate-recommendation projections."""

    model_config = ConfigDict(populate_by_name=True)

    available: bool
    status: str
    months: list[CommitmentMonthPoint]


class RateOptimizationScenario(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["Single"] = "Single"
    look_back_period: Literal["Last7Days", "Last30Days", "Last60Days"] = Field(alias="lookBackPeriod")
    term: Literal["P1Y", "P3Y"]
    reservation_resource_type: Literal[
        "AppService",
        "AzureDataExplorer",
        "BlockBlob",
        "CosmosDB",
        "ManagedDisk",
        "MariaDB",
        "MySQL",
        "PostgreSQL",
        "RedHat",
        "RedisCache",
        "SQLDatabases",
        "SUSELinux",
        "SqlDataWarehouse",
        "VMwareCloudSimple",
        "VirtualMachines",
    ] = Field(alias="reservationResourceType")


class RateRecommendation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["reservation", "savings_plan"]
    recommendation_id: str = Field(alias="recommendationId")
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    sku: str
    region: str
    quantity: float | None
    hourly_commitment: float | None = Field(alias="hourlyCommitment")
    commitment_granularity: str = Field(alias="commitmentGranularity")
    term: str
    look_back_period: str = Field(alias="lookBackPeriod")
    scope: str
    resource_type: str = Field(alias="resourceType")
    currency: str
    cost_without_benefit: float | None = Field(alias="costWithoutBenefit")
    cost_with_benefit: float | None = Field(alias="costWithBenefit")
    projected_savings: float | None = Field(alias="projectedSavings")
    savings_percentage: float | None = Field(alias="savingsPercentage")
    coverage_percentage: float | None = Field(alias="coveragePercentage")
    utilization_percentage: float | None = Field(alias="utilizationPercentage")
    wastage_cost: float | None = Field(alias="wastageCost")
    first_usage_date: str = Field(alias="firstUsageDate")
    last_usage_date: str = Field(alias="lastUsageDate")
    total_hours: int | None = Field(alias="totalHours")
    overlap_group: str = Field(alias="overlapGroup")
    source: str
    review_url: str = Field(alias="reviewUrl")


class RecommendationSourceStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: Literal["reservation", "savings_plan"]
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(alias="subscriptionName")
    status: Literal["available", "no_recommendation", "unavailable"]
    message: str


class RateOptimizationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scenario: RateOptimizationScenario
    generated_at: str = Field(alias="generatedAt")
    reservations: list[RateRecommendation]
    savings_plans: list[RateRecommendation] = Field(alias="savingsPlans")
    source_status: list[RecommendationSourceStatus] = Field(alias="sourceStatus")
    projection_notice: str = Field(alias="projectionNotice")


class FullReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    executive_summary: ExecutiveSummary = Field(alias="executiveSummary")
    report_metadata: ReportMetadata = Field(alias="reportMetadata")
    completeness: DataCompleteness
    operational_signals: list[OperationalSignal] = Field(alias="operationalSignals")
    top_services: list[ServiceSpendSummary] = Field(alias="topServices")
    spend_categories: list[SpendCategorySummary] = Field(alias="spendCategories")
    spend_history: SpendHistorySummary = Field(alias="spendHistory")
    # default_factory keeps snapshots saved before these two fields existed loadable
    # (Pydantic would otherwise reject them as invalid on GET /api/report/latest).
    daily_cost_trend: DailyCostTrendSummary = Field(
        alias="dailyCostTrend",
        default_factory=lambda: DailyCostTrendSummary(
            status="unavailable", statusMessage="Not available in this report snapshot.", days=[]
        ),
    )
    tag_daily_cost_trend: TagDailyCostTrendSummary = Field(
        alias="tagDailyCostTrend",
        default_factory=lambda: TagDailyCostTrendSummary(
            status="unavailable", statusMessage="Not available in this report snapshot.", tagKey="application", availableTagValues=[], series=[]
        ),
    )
    cost_hierarchy: list[CostHierarchyItem] = Field(alias="costHierarchy")
    cost_details: CostDetailSummary = Field(default_factory=CostDetailSummary, alias="costDetails")
    region_spend: list[RegionSpendSummary] = Field(alias="regionSpend")
    advisor_score: AdvisorScoreSummary = Field(alias="advisorScore")
    storage_optimization: StorageOptimizationSummary = Field(alias="storageOptimization")
    ai_usage: AIUsageSummary = Field(
        default_factory=lambda: AIUsageSummary(
            status="Azure OpenAI usage was not collected.",
            periodStart="",
            periodEnd="",
            deployments=[],
            opportunities=[],
        ),
        alias="aiUsage",
    )
    chargeback: ChargebackSummary = Field(
        default_factory=lambda: ChargebackSummary(
            available=False,
            status="FOCUS tag allocation was not collected.",
            allocatedCost=0,
            unallocatedCost=0,
            allocationPercentage=0,
            rows=[],
        )
    )
    compliance: ComplianceSummary = Field(
        default_factory=lambda: ComplianceSummary(
            available=False,
            status="Azure Policy compliance was not collected.",
            rows=[],
        )
    )
    network_metric_coverage: list[MetricCoverageSummary] = Field(alias="networkMetricCoverage")
    prioritized_findings: list[ExecutiveFinding] = Field(alias="prioritizedFindings")
    savings_roadmap: list[SavingsRoadmapItem] = Field(alias="savingsRoadmap")
    subscription_breakdown: list[SubscriptionBreakdownRow] = Field(alias="subscriptionBreakdown")
    domains: dict[str, DomainSummary]
    action_plan: list[ActionPlanItem] = Field(alias="actionPlan")
    advisor_reconciliation: AdvisorReconciliation = Field(alias="advisorReconciliation")
    governance: list[GovernanceRow]
    pricing_summary: PricingSummary = Field(alias="pricingSummary")
    commitment_summary: CommitmentSummary = Field(alias="commitmentSummary")
    commitment_insights: CommitmentInsights = Field(
        default_factory=lambda: CommitmentInsights(
            available=False,
            status="FOCUS history was not collected.",
            months=[],
        ),
        alias="commitmentInsights",
    )
    tag_costs: TagCostSummary = Field(
        default_factory=lambda: TagCostSummary(
            available=False,
            status="FOCUS tag data was not collected.",
            totalSpend=0,
            growthRate=None,
            dimensions=[],
        ),
        alias="tagCosts",
    )
    extended_support: ExtendedSupportSummary = Field(
        default_factory=lambda: ExtendedSupportSummary(
            available=False,
            status="FOCUS cost data was not collected.",
            totalMonthlyCost=0,
            rows=[],
        ),
        alias="extendedSupport",
    )
    off_hours_savings: OffHoursSavingsSummary = Field(
        default_factory=lambda: OffHoursSavingsSummary(
            available=False,
            status="FOCUS cost data was not collected.",
            offHoursFraction=0.0,
            totalMonthlyCost=0,
            totalEstimatedSaving=0,
            rows=[],
        ),
        alias="offHoursSavings",
    )
    # Kept for POST /api/narrate, which expects this exact (smaller) shape.
    tier_a_categories: list[FindingCategorySummary] = Field(alias="tierACategories")


class ReportSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    snapshot_id: str = Field(alias="snapshotId")
    scope_hash: str = Field(alias="scopeHash")
    subscription_ids: list[str] = Field(alias="subscriptionIds")
    stale_days: int = Field(alias="staleDays")
    created_at: str = Field(alias="createdAt")
    report_schema_version: str = Field(alias="reportSchemaVersion")
    report: FullReport


class ReportSnapshotSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    snapshot_id: str = Field(alias="snapshotId")
    scope_hash: str = Field(alias="scopeHash")
    subscription_ids: list[str] = Field(alias="subscriptionIds")
    stale_days: int = Field(alias="staleDays")
    created_at: str = Field(alias="createdAt")
    period: str
    period_start: str = Field(alias="periodStart")
    period_end: str = Field(alias="periodEnd")
    currency: str
    cost_basis: str = Field(alias="costBasis")
    report_schema_version: str = Field(alias="reportSchemaVersion")


class FinOpsActionState(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope_hash: str = Field(alias="scopeHash")
    action_id: str = Field(alias="actionId")
    status: Literal["open", "in_progress", "completed", "dismissed"]
    owner: str
    due_date: str | None = Field(alias="dueDate")
    completed_at: str | None = Field(alias="completedAt")
    realized_saving_month: float | None = Field(alias="realizedSavingMonth")
    note: str
    updated_at: str = Field(alias="updatedAt")
    updated_by: str = Field(alias="updatedBy")
    version: int = Field(default=0, ge=0, strict=True)


class NativeBudgetSummary(BaseModel):
    """A monthly spend budget, backed directly by Azure Cost Management
    (Microsoft.Consumption/budgets) - the single source of truth for the Budgets tab,
    fully managed (CRUD) through the /api/budgets endpoints below."""

    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    name: str
    category: str
    amount: float
    currency: str
    time_grain: str = Field(alias="timeGrain")
    period_start: str = Field(alias="periodStart")
    period_end: str = Field(alias="periodEnd")
    current_spend: float | None = Field(alias="currentSpend")
    forecast_spend: float | None = Field(alias="forecastSpend")
    scope: str = ""
    filter: dict = Field(default_factory=dict)
    observed_at: str = Field(default="", alias="observedAt")
    cost_basis: str = Field(default="ActualCost", alias="costBasis")

