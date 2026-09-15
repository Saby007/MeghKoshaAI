"""Pydantic models for the full cost findings report, returned by POST /api/assessment.

Field aliases match the camelCase shape the React frontend already expects
(web/src/findings/models.ts) - this is a distinct, richer contract (includes
per-resource `lines`) from agents/cost_agent.py's CostFindingsReport, which is the
smaller, snake_case payload sent on to POST /api/narrate.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RemediationPlan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: Literal["preview", "inspect"]
    title: str
    risk: Literal["Low", "Medium", "High"]
    effort: Literal["Low", "Medium", "High"]
    immediate: bool
    prerequisites: list[str]
    script: str
    resource_count: int = Field(alias="resourceCount")


class ResourceCostEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    meter_id: str = Field(alias="meterId")
    meter_name: str = Field(alias="meterName")
    meter_category: str = Field(alias="meterCategory")
    meter_subcategory: str = Field(alias="meterSubCategory")
    product_id: str = Field(alias="productId")
    product_name: str = Field(alias="productName")
    pricing_model: str = Field(alias="pricingModel")
    reservation_id: str = Field(alias="reservationId")
    reservation_name: str = Field(alias="reservationName")
    benefit_id: str = Field(alias="benefitId")
    benefit_name: str = Field(alias="benefitName")
    monthly_cost: float = Field(alias="monthlyCost")


class FocusPricingEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sku_id: str = Field(alias="skuId")
    sku_price_id: str = Field(alias="skuPriceId")
    pricing_category: str = Field(alias="pricingCategory")
    pricing_currency: str = Field(alias="pricingCurrency")
    pricing_quantity: float = Field(alias="pricingQuantity")
    pricing_unit: str = Field(alias="pricingUnit")
    list_unit_price: float = Field(alias="listUnitPrice")
    contracted_unit_price: float = Field(alias="contractedUnitPrice")
    effective_unit_price: float = Field(alias="effectiveUnitPrice")
    list_cost: float = Field(alias="listCost")
    contracted_cost: float = Field(alias="contractedCost")
    effective_cost: float = Field(alias="effectiveCost")
    billed_cost: float = Field(alias="billedCost")
    commitment_discount_category: str = Field(alias="commitmentDiscountCategory")
    commitment_discount_type: str = Field(alias="commitmentDiscountType")
    commitment_discount_status: str = Field(alias="commitmentDiscountStatus")


class SqlOptimizationCheck(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_id: str = Field(alias="ruleId")
    rule_version: Literal["1.0"] = Field(default="1.0", alias="ruleVersion")
    title: str
    status: Literal["needs_evidence", "review", "not_applicable", "blocked"] = "needs_evidence"
    reason: str
    required_evidence: list[str] = Field(default_factory=list, alias="requiredEvidence")
    next_steps: list[str] = Field(default_factory=list, alias="nextSteps")
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    estimated_monthly_savings: float | None = Field(default=None, alias="estimatedMonthlySavings")


class SqlMetricEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    unit: str = "Percent"
    observed_days: int = Field(default=0, ge=0, alias="observedDays")
    maximum: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    average: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class SqlWorkloadEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    resource_id: str = Field(alias="resourceId")
    status: Literal["complete", "partial", "unavailable", "unsupported", "not_collected"]
    reason: str
    source: str = "Azure Monitor Metrics"
    window_start: str = Field(alias="windowStart")
    window_end: str = Field(alias="windowEnd")
    collected_at: str = Field(alias="collectedAt")
    expected_days: int = Field(default=30, alias="expectedDays")
    metrics: list[SqlMetricEvidence] = Field(default_factory=list)


class SqlResourceContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["1.0"] = Field(default="1.0", alias="schemaVersion")
    deployment_model: Literal[
        "single_database",
        "pooled_database",
        "elastic_pool",
        "managed_instance",
        "instance_pool",
        "sql_vm",
        "logical_server",
        "unknown",
    ] = Field(default="unknown", alias="deploymentModel")
    resource_type: str = Field(default="", alias="resourceType")
    pool_resource_id: str | None = Field(default=None, alias="poolResourceId")
    compute_resource_id: str | None = Field(default=None, alias="computeResourceId")
    classification_reason: str = Field(default="Metadata has not been classified", alias="classificationReason")
    evidence_gaps: list[str] = Field(default_factory=list, alias="evidenceGaps")
    configuration: dict[str, str] = Field(default_factory=dict)
    optimization_checks: list[SqlOptimizationCheck] = Field(default_factory=list, alias="optimizationChecks")
    workload_evidence: SqlWorkloadEvidence | None = Field(default=None, alias="workloadEvidence")


class IdleEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_version: Literal["1.0"] = Field(default="1.0", alias="ruleVersion")
    classification: Literal["candidate", "activity_observed", "zero_traffic_observed", "protected", "confirmed_idle"]
    reason: str
    window_start: str | None = Field(default=None, alias="windowStart")
    window_end: str | None = Field(default=None, alias="windowEnd")
    source: str
    safety_review_required: bool = Field(default=True, alias="safetyReviewRequired")


class FindingLine(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    subscription_id: str = Field(alias="subscriptionId")
    subscription_name: str = Field(default="", alias="subscriptionName")
    monthly_cost: float | None = Field(alias="monthlyCost")
    confidence: float
    evidence_type: Literal[
        "verified_cost",
        "advisor_estimate",
        "metrics_verified_idle",
        "inventory_candidate",
    ] = Field(alias="evidenceType")
    detail: str
    cost_evidence: list[ResourceCostEvidence] = Field(default_factory=list, alias="costEvidence")
    focus_pricing_evidence: list[FocusPricingEvidence] = Field(default_factory=list, alias="focusPricingEvidence")
    sql_context: SqlResourceContext | None = Field(default=None, alias="sqlContext")
    idle_evidence: IdleEvidence | None = Field(default=None, alias="idleEvidence")


class FindingCategorySummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: str
    display_name: str = Field(alias="displayName")
    count: int
    monthly_total: float = Field(alias="monthlyTotal")
    annual_total: float = Field(alias="annualTotal")
    impact_type: Literal["potential_savings", "cost_at_risk", "inventory"] = Field(alias="impactType")
    lines: list[FindingLine]
    remediation: RemediationPlan | None = None


class AssessmentReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscriptions: list[str]
    total_monthly_spend: float = Field(alias="totalMonthlySpend")
    tier_a_categories: list[FindingCategorySummary] = Field(alias="tierACategories", default_factory=list)
