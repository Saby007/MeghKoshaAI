import type { AnomalySummary, DomainSummary, FullReport, RateOptimizationResponse, ReportSnapshot } from './models';
import type { FindingCategorySummary } from '../findings/models';

const days = Array.from({ length: 69 }, (_, index) => {
  const date = new Date(Date.UTC(2026, 6, 1 + index));
  const totalCost = [0, 6].includes(date.getUTCDay()) ? 48 : 144;
  return { date: date.toISOString().slice(0, 10), totalCost, averageHourlyCost: totalCost / 24 };
});
const domain = (name: string): DomainSummary => ({ domain: name, domainSpendMonth: 0, verifiedSavingMonth: 0, verifiedSavingYear: 0, monthlyCostAtRisk: 0, pctOfDomainSpend: 0, pctOfTotalSpend: 0, categories: [], advisorRecommendations: [] });

export const reportFixture: FullReport = {
  executiveSummary: { currentMonthlySpend: 3120, potentialSavingsMonth: 240, potentialSavingsYear: 2880, pctRecoverable: 0.077, estimatedWastageMonth: 320, pctWastage: 0.103, activeResources: 42, idleResources: 0, idleReviewCandidates: 3, idleResourcePercentage: 0, spendChangePercentage: -0.12 },
  reportMetadata: { period: '2026-08', periodStart: '2026-08-01', periodEnd: '2026-08-31', costBasis: 'EffectiveCost', currency: 'USD', generatedAt: '2026-09-09T10:00:00Z', source: 'Synthetic FOCUS validation fixture', staleDays: 90, protectedTagKeys: [], excludedProtectedResources: 0 },
  completeness: { requestedSubscriptions: 1, availableSubscriptions: 1, complete: true, status: 'Complete' },
  operationalSignals: [], topServices: [],
  spendCategories: [{ category: 'Compute', monthlySpend: 2200, pctOfTotal: 0.7 }, { category: 'Storage', monthlySpend: 920, pctOfTotal: 0.3 }],
  spendHistory: { status: 'complete', statusMessage: 'Two complete synthetic months', months: [
    { month: '2026-07', total: 3500, categorySpend: { Compute: 2500, Storage: 1000 }, subscriptionSpend: { 'sub-1': 3500 }, subscriptionCategorySpend: {} },
    { month: '2026-08', total: 3120, categorySpend: { Compute: 2200, Storage: 920 }, subscriptionSpend: { 'sub-1': 3120 }, subscriptionCategorySpend: {} },
  ] },
  dailyCostTrend: { status: 'complete', statusMessage: 'Synthetic verified export-calendar dates', days },
  tagDailyCostTrend: { status: 'complete', statusMessage: 'Independent tag-attributed series; non-additive.', tagKey: '', availableTagValues: ['Application: Finance', 'Team: Platform'], series: [
    { tagKey: '', tagValue: 'Application: Finance', totalCost: 99999, days: days.map((day) => ({ ...day, totalCost: day.totalCost * 0.75, averageHourlyCost: day.averageHourlyCost * 0.75 })) },
    { tagKey: '', tagValue: 'Team: Platform', totalCost: 99999, days },
  ], windowDates: days.map((day) => day.date), distributionSeries: [{ tagKey: 'Tag set', tagValue: 'Application: Finance; Team: Platform', totalCost: 99999, days }] },
  costHierarchy: [], regionSpend: [], advisorScore: { available: true, score: 88, costScore: 85, monthlyChange: 2, subscriptionCount: 1, status: 'Available' },
  storageOptimization: { status: 'No synthetic accounts', accounts: [], currentTierVolumes: [], recommendedTierVolumes: [] }, networkMetricCoverage: [], prioritizedFindings: [], savingsRoadmap: [],
  subscriptionBreakdown: [{ subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', currentSpend: 3120, categoryCosts: {}, totalWaste: 320, pctSaved: 0.077 }],
  domains: Object.fromEntries(['compute', 'storage', 'network', 'sql', 'ai'].map((name) => [name, domain(name)])),
  actionPlan: [], advisorReconciliation: { measures: [], recommendations: [] }, governance: [],
  pricingSummary: { available: false, status: 'Synthetic fixture', dataVersion: null, period: null, billingCurrency: null, pricingCurrencies: [], billedCost: 0, effectiveCost: 0, listCost: 0, contractedCost: 0, negotiatedDiscount: 0, negotiatedDiscountPercentage: 0, reconciliationVariance: null, subscriptions: [] },
  commitmentSummary: { observed: false, status: 'Synthetic fixture', period: '', currency: 'USD', reservations: { rowCount: 0, usedEffectiveCost: 0, unusedEffectiveCost: 0, realizedBenefit: 0 }, savingsPlans: { rowCount: 0, usedEffectiveCost: 0, unusedEffectiveCost: 0, realizedBenefit: 0 } },
  commitmentInsights: { available: false, status: 'Synthetic fixture', months: [] }, tagCosts: { available: false, status: 'Synthetic fixture', totalSpend: 0, growthRate: null, dimensions: [] }, tierACategories: [],
};

export const detailReportFixture: FullReport = {
  ...reportFixture,
  costDetails: {
    status: 'complete', statusMessage: 'Synthetic daily resource evidence', costBasis: 'EffectiveCost', granularity: 'daily', dates: days.map((day) => day.date),
    rows: [
      { detailId: 'vm-costs', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', resourceId: '/subscriptions/sub-1/resourceGroups/finance/providers/Microsoft.Compute/virtualMachines/finance-vm', resourceName: 'finance-vm', resourceType: 'Microsoft.Compute/virtualMachines', resourceGroup: 'finance', serviceName: 'Virtual Machines', region: 'centralindia', tags: { application: 'Finance', owner: 'Finance team' }, tagAttributionSource: 'exported_resource_tags', dailyCosts: Object.fromEntries(days.map((day) => [day.date, day.date >= '2026-09-01' ? day.totalCost * 2 : day.totalCost * .75])) },
      { detailId: 'disk-costs', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', resourceId: '/subscriptions/sub-1/resourceGroups/platform/providers/Microsoft.Compute/disks/shared', resourceName: 'shared-disk', resourceType: 'Microsoft.Compute/disks', resourceGroup: 'platform', serviceName: 'Storage', region: 'eastus2', tags: { application: 'Platform', owner: 'Platform team' }, tagAttributionSource: 'exported_resource_tags', dailyCosts: Object.fromEntries(days.map((day) => [day.date, day.totalCost * .25])) },
    ],
  },
};

export const tagReportFixture = {
  ...reportFixture,
  tagCosts: {
    available: true, status: 'Available', totalSpend: 250, growthRate: 0.1,
    dimensions: [
      { tagKey: 'Team', unallocatedCost: 50, rows: [
        { value: 'Platform', monthlyCost: 120, pctOfTotal: 0.48, forecastNextMonth: 132 },
        { value: 'Sales', monthlyCost: 80, pctOfTotal: 0.32, forecastNextMonth: 88 },
      ] },
      { tagKey: 'Environment', unallocatedCost: 0, rows: [
        { value: 'Production', monthlyCost: 250, pctOfTotal: 1, forecastNextMonth: null },
      ] },
    ],
  },
};

export const pricingReportFixture: FullReport = {
  ...reportFixture,
  pricingSummary: {
    available: true, status: 'Reconciled synthetic pricing evidence', dataVersion: '1.2', period: '2026-08', billingCurrency: 'USD', pricingCurrencies: ['USD'],
    billedCost: 3120, effectiveCost: 3120, listCost: 4000, contractedCost: 3500, negotiatedDiscount: 500, negotiatedDiscountPercentage: 0.125, reconciliationVariance: 0,
    subscriptions: [{ subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', listCost: 4000, contractedCost: 3500, effectiveCost: 3120, billedCost: 3120, negotiatedDiscount: 500, negotiatedDiscountPercentage: 0.125 }],
  },
  commitmentSummary: {
    observed: true, status: 'Synthetic observed commitments', period: '2026-08', currency: 'USD',
    reservations: { rowCount: 4, usedEffectiveCost: 240, unusedEffectiveCost: 40, realizedBenefit: 100 },
    savingsPlans: { rowCount: 3, usedEffectiveCost: 120, unusedEffectiveCost: 10, realizedBenefit: 50 },
  },
  commitmentInsights: { available: true, status: 'Synthetic FOCUS commitment history', months: [{
    month: '2026-08', currency: 'USD', reservationCommittedCost: 280, reservationUsedCost: 240, reservationUnusedCost: 40, reservationRealizedSavings: 100, reservationCoveragePercentage: 0.5,
    savingsPlanCommittedCost: 130, savingsPlanUsedCost: 120, savingsPlanUnusedCost: 10, savingsPlanRealizedSavings: 50,
    acdEffectiveCost: 410, acdLeastPriceCost: 560, acdSavings: 150, acdSavingsPercentage: 150 / 560,
    spotEffectiveCost: 15, spotLeastPriceCost: 30, spotSavings: 15, spotSavingsPercentage: 0.5,
    computeVmCoverage: { totalCost: 600, paygCost: 240, savingsPlanCost: 120, reservationCost: 240, paygPercentage: 0.4, savingsPlanPercentage: 0.2, reservationPercentage: 0.4 },
  }] },
};

export const rateOptimizationFixture: RateOptimizationResponse = {
  scenario: { scope: 'Single', lookBackPeriod: 'Last30Days', term: 'P1Y', reservationResourceType: 'VirtualMachines' },
  generatedAt: '2026-09-10T08:00:00Z',
  reservations: [{
    kind: 'reservation', recommendationId: 'synthetic-reservation', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription',
    sku: 'Standard_D2s_v5', region: 'centralindia', quantity: 1, hourlyCommitment: null, commitmentGranularity: '', term: 'P1Y', lookBackPeriod: 'Last30Days', scope: 'Single', resourceType: 'VirtualMachines', currency: 'USD',
    costWithoutBenefit: 100, costWithBenefit: 60, projectedSavings: 40, savingsPercentage: 0.4, coveragePercentage: 0.8, utilizationPercentage: 0.9, wastageCost: 2,
    firstUsageDate: '2026-08-01', lastUsageDate: '2026-08-30', totalHours: 720, overlapGroup: '', source: 'Synthetic Azure recommendation', reviewUrl: 'https://portal.azure.com/',
  }],
  savingsPlans: [],
  sourceStatus: [
    { source: 'reservation', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', status: 'available', message: 'Synthetic recommendation available' },
    { source: 'savings_plan', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', status: 'no_recommendation', message: 'No recommendation for this scenario' },
  ],
  projectionNotice: 'Azure projected savings are estimates, separate from realized FOCUS benefits.',
};

const diskCategory: FindingCategorySummary = {
  category: 'unattached_disks', displayName: 'Unattached disks', count: 1, monthlyTotal: 120, annualTotal: 1440, impactType: 'potential_savings',
  lines: [{ category: 'unattached_disks', resourceId: '/subscriptions/sub-1/resourceGroups/finance-platform/providers/Microsoft.Compute/disks/finance-archive-evidence-disk', resourceName: 'finance-archive-evidence-disk', subscriptionId: 'sub-1', subscriptionName: 'Finance platform subscription', monthlyCost: 120, confidence: 1, evidenceType: 'verified_cost', detail: 'Synthetic detached disk with reconciled closed-period charges. Owner approval remains required.', costEvidence: [], focusPricingEvidence: [] }],
  remediation: { mode: 'inspect', title: 'Inspect unattached disk', risk: 'Low', effort: 'Low', immediate: false, prerequisites: ['Owner approval'], script: 'Get-AzDisk', resourceCount: 1 },
};
const sqlCategory: FindingCategorySummary = {
  category: 'sql_databases', displayName: 'SQL databases', count: 1, monthlyTotal: 350, annualTotal: 4200, impactType: 'inventory', remediation: null,
  lines: [{ category: 'sql_databases', resourceId: '/subscriptions/sub-1/resourceGroups/finance-platform/providers/Microsoft.Sql/servers/finance/databases/billing', resourceName: 'billing', subscriptionId: 'sub-1', subscriptionName: 'Finance platform subscription', monthlyCost: 350, confidence: 1, evidenceType: 'verified_cost', detail: 'Synthetic provisioned SQL database.', costEvidence: [], focusPricingEvidence: [], sqlContext: {
    schemaVersion: '1.0', deploymentModel: 'single_database', resourceType: 'Microsoft.Sql/servers/databases', poolResourceId: null, computeResourceId: null, classificationReason: 'Synthetic single database configuration.', evidenceGaps: ['Workload telemetry'], configuration: { tier: 'GeneralPurpose', capacity: '2 vCores' },
    optimizationChecks: [{ ruleId: 'SQL-CAPACITY', ruleVersion: '1.0', title: 'Review database capacity', status: 'needs_evidence', reason: 'Representative workload evidence is required before resizing.', requiredEvidence: ['workload_metrics'], nextSteps: ['Review a complete workload period.'], dependsOn: [], estimatedMonthlySavings: null }], workloadEvidence: null,
  } }],
};

export const resourceReportFixture: FullReport = {
  ...pricingReportFixture,
  tagCosts: tagReportFixture.tagCosts,
  executiveSummary: { ...reportFixture.executiveSummary, potentialSavingsMonth: 120, potentialSavingsYear: 1440, pctRecoverable: 120 / 3120 },
  domains: {
    compute: { ...domain('compute'), domainSpendMonth: 1800 },
    storage: { ...domain('storage'), domainSpendMonth: 700, verifiedSavingMonth: 120, verifiedSavingYear: 1440, pctOfDomainSpend: 120 / 700, pctOfTotalSpend: 120 / 3120, categories: [diskCategory] },
    network: { ...domain('network'), domainSpendMonth: 120 },
    sql: { ...domain('sql'), domainSpendMonth: 350, categories: [sqlCategory] },
    ai: { ...domain('ai'), domainSpendMonth: 150 },
  },
  tierACategories: [diskCategory, sqlCategory],
  storageOptimization: {
    status: 'Synthetic storage capacity evidence; blob-level savings are not quantified.',
    currentTierVolumes: [{ tier: 'Hot', bytes: 107374182400 }], recommendedTierVolumes: [],
    accounts: [{ storageAccountId: '/subscriptions/sub-1/resourceGroups/finance-platform/providers/Microsoft.Storage/storageAccounts/financearchive', storageAccountName: 'financearchive', subscriptionId: 'sub-1', subscriptionName: 'Finance platform subscription', resourceGroup: 'finance-platform', location: 'centralindia', currentTier: 'Hot', sizeBytes: 107374182400, tierVolumes: [{ tier: 'Hot', bytes: 107374182400 }], accessPattern: 'Synthetic low read activity', readTransactions: 150, recommended: 'Review access history', estimatedSavingMonth: null, monthlyCost: 580, evidenceStatus: 'Last-access inventory not collected' }],
  },
  governance: [{ subscriptionId: 'sub-1', subscriptionName: 'Finance platform subscription', untaggedResources: 2, pctOfEstate: 2 / 42 }],
  actionPlan: [{ actionId: 'review-disk', action: 'Review unattached disk', savingMonth: 120, prerequisite: 'Owner approval', affectedSubscriptions: [{ subscriptionId: 'sub-1', subscriptionName: 'Finance platform subscription' }] }],
};

export const snapshotFixture: ReportSnapshot = { snapshotId: 'visual-report-1', scopeHash: 'visual-scope', subscriptionIds: ['sub-1'], staleDays: 90, createdAt: '2026-09-09T10:00:00Z', reportSchemaVersion: '1.0', report: reportFixture };

export const anomalyFixture: AnomalySummary = {
  algorithmVersion: 'mkai-weekday-mad-v1', label: 'Application detector', status: 'ready', statusMessage: 'Synthetic validation fixture', historyStart: '2026-07-01', historyEnd: '2026-09-07', completeDays: 69, requiredDays: 35, currency: 'USD', generatedAt: '2026-09-09T10:00:00Z',
  trend: days.map((day) => ({ date: day.date, actualCost: day.totalCost, expectedCost: 120, expectedLower: 70, expectedUpper: 160 })),
  anomalies: [{ anomalyId: 'demo-1', date: '2026-09-07', firstDetectedDate: '2026-09-07', lastDetectedDate: '2026-09-07', durationDays: 1, anomalyType: 'spike', dimensionType: 'subscription', dimensionName: 'Demo subscription', dimensionId: 'sub-1', subscriptionId: 'sub-1', subscriptionName: 'Demo subscription', actualCost: 240, expectedCost: 120, expectedLower: 70, expectedUpper: 160, absoluteDelta: 120, percentageDelta: 1, severity: 'High', baselineSamples: 8, contributors: [], investigationUrl: 'https://portal.azure.com/' }],
};