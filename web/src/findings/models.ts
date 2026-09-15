export type ResourceCostEvidence = {
  meterId: string;
  meterName: string;
  meterCategory: string;
  meterSubCategory: string;
  productId: string;
  productName: string;
  pricingModel: string;
  reservationId: string;
  reservationName: string;
  benefitId: string;
  benefitName: string;
  monthlyCost: number;
};

export type FocusPricingEvidence = {
  skuId: string;
  skuPriceId: string;
  pricingCategory: string;
  pricingCurrency: string;
  pricingQuantity: number;
  pricingUnit: string;
  listUnitPrice: number;
  contractedUnitPrice: number;
  effectiveUnitPrice: number;
  listCost: number;
  contractedCost: number;
  effectiveCost: number;
  billedCost: number;
  commitmentDiscountCategory: string;
  commitmentDiscountType: string;
  commitmentDiscountStatus: string;
};

export type SqlOptimizationCheck = {
  ruleId: string;
  ruleVersion: '1.0';
  title: string;
  status: 'needs_evidence' | 'review' | 'not_applicable' | 'blocked';
  reason: string;
  requiredEvidence: string[];
  nextSteps: string[];
  dependsOn: string[];
  estimatedMonthlySavings: number | null;
};

export type SqlWorkloadEvidence = {
  resourceId: string;
  status: 'complete' | 'partial' | 'unavailable' | 'unsupported' | 'not_collected';
  reason: string;
  source: string;
  windowStart: string;
  windowEnd: string;
  collectedAt: string;
  expectedDays: number;
  metrics: { name: string; unit: string; observedDays: number; maximum: number | null; average: number | null }[];
};

export type SqlResourceContext = {
  schemaVersion: '1.0';
  deploymentModel: 'single_database' | 'pooled_database' | 'elastic_pool' | 'managed_instance' | 'instance_pool' | 'sql_vm' | 'logical_server' | 'unknown';
  resourceType: string;
  poolResourceId: string | null;
  computeResourceId: string | null;
  classificationReason: string;
  evidenceGaps: string[];
  configuration?: Record<string, string>;
  optimizationChecks?: SqlOptimizationCheck[];
  workloadEvidence?: SqlWorkloadEvidence | null;
};

export type FindingLine = {
  category: string;
  resourceId: string;
  resourceName: string;
  subscriptionId: string;
  subscriptionName: string;
  monthlyCost: number | null;
  confidence: number;
  evidenceType: 'verified_cost' | 'advisor_estimate' | 'metrics_verified_idle' | 'inventory_candidate';
  detail: string;
  costEvidence: ResourceCostEvidence[];
  focusPricingEvidence: FocusPricingEvidence[];
  sqlContext?: SqlResourceContext | null;
  idleEvidence?: {
    ruleVersion: '1.0';
    classification: 'candidate' | 'activity_observed' | 'zero_traffic_observed' | 'protected' | 'confirmed_idle';
    reason: string;
    windowStart: string | null;
    windowEnd: string | null;
    source: string;
    safetyReviewRequired: boolean;
  } | null;
};

export type RemediationPlan = {
  mode: 'preview' | 'inspect';
  title: string;
  risk: 'Low' | 'Medium' | 'High';
  effort: 'Low' | 'Medium' | 'High';
  immediate: boolean;
  prerequisites: string[];
  script: string;
  resourceCount: number;
};

export type FindingCategorySummary = {
  category: string;
  displayName: string;
  count: number;
  monthlyTotal: number;
  annualTotal: number;
  impactType: 'potential_savings' | 'cost_at_risk' | 'inventory';
  lines: FindingLine[];
  remediation: RemediationPlan | null;
};

/** Payload sent to POST /api/narrate - the Cost Agent never sees line-level detail, only category totals. */
export function toNarratePayload(subscriptions: string[], totalMonthlySpend: number, tierACategories: FindingCategorySummary[]) {
  return {
    subscriptions,
    total_monthly_spend: totalMonthlySpend,
    tier_a_categories: tierACategories.map((c) => ({
      category: c.category,
      display_name: c.displayName,
      count: c.count,
      monthly_total: c.monthlyTotal,
      annual_total: c.annualTotal,
      impact_type: c.impactType,
    })),
  };
}

