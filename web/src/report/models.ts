import type { FindingCategorySummary } from '../findings/models';

/** Mirrors api/reports/models.py's FullReport - the 9-tab assessment matching the
 * original Excel workbook (Executive Summary, Savings Roadmap, Subscription
 * Breakdown, Compute/Storage/Network Optimization, Advisor Reconciliation,
 * Governance & Risk, Action Plan).
 */
export type ExecutiveSummary = {
  currentMonthlySpend: number;
  potentialSavingsMonth: number;
  potentialSavingsYear: number;
  pctRecoverable: number;
  estimatedWastageMonth: number;
  pctWastage: number;
  activeResources: number;
  idleResources: number;
  idleReviewCandidates?: number | null;
  idleResourcePercentage: number;
  spendChangePercentage: number | null;
};

export type SpendCategorySummary = {
  category: string;
  monthlySpend: number;
  pctOfTotal: number;
};

export type MonthlySpendPoint = {
  month: string;
  total: number;
  categorySpend: Record<string, number>;
  subscriptionSpend: Record<string, number>;
  subscriptionCategorySpend: Record<string, Record<string, number>>;
};

export type SpendHistorySummary = {
  status: 'complete' | 'partial';
  statusMessage: string;
  months: MonthlySpendPoint[];
};

export type DailyCostPoint = {
  date: string;
  totalCost: number;
  averageHourlyCost: number;
};

export type DailyCostTrendSummary = {
  status: 'complete' | 'partial' | 'unavailable';
  statusMessage: string;
  days: DailyCostPoint[];
};

export type TagDailyCostSeries = {
  tagKey: string;
  tagValue: string;
  totalCost: number;
  days: DailyCostPoint[];
};

export type TagDailyCostTrendSummary = {
  status: 'complete' | 'partial' | 'unavailable';
  statusMessage: string;
  tagKey: string;
  availableTagValues: string[];
  series: TagDailyCostSeries[];
  distributionSeries?: TagDailyCostSeries[];
  windowDates?: string[];
};

export type CostHierarchyItem = {
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  monthlySpend: number;
  pctOfTotal: number;
};

export type RegionSpendSummary = {
  region: string;
  monthlySpend: number;
  pctOfTotal: number;
};

export type CostDetailRow = {
  detailId: string;
  subscriptionId: string;
  subscriptionName: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  resourceGroup: string;
  serviceName: string;
  region: string;
  tags: Record<string, string>;
  tagAttributionSource: string;
  dailyCosts: Record<string, number>;
};

export type CostDetailSummary = {
  status: 'complete' | 'unavailable';
  statusMessage: string;
  costBasis: string;
  granularity: 'daily';
  dates: string[];
  rows: CostDetailRow[];
};

export type AdvisorScoreSummary = {
  available: boolean;
  score: number | null;
  costScore: number | null;
  monthlyChange: number | null;
  subscriptionCount: number;
  status: string;
};

export type StorageTierVolume = {
  tier: string;
  bytes: number;
};

export type StorageAccountTierAnalysis = {
  storageAccountId: string;
  storageAccountName: string;
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  location: string;
  currentTier: string;
  sizeBytes: number | null;
  tierVolumes: StorageTierVolume[];
  accessPattern: string;
  readTransactions: number | null;
  recommended: string;
  estimatedSavingMonth: number | null;
  monthlyCost: number | null;
  evidenceStatus: string;
};

export type StorageOptimizationSummary = {
  status: string;
  accounts: StorageAccountTierAnalysis[];
  currentTierVolumes: StorageTierVolume[];
  recommendedTierVolumes: StorageTierVolume[];
};

export type AIModelDeploymentUsage = {
  accountId: string;
  accountName: string;
  subscriptionId: string;
  location: string;
  deploymentName: string;
  modelName: string;
  modelVersion: string;
  skuName: string;
  capacity: number | null;
  inputTokensPerDay: number | null;
  outputTokensPerDay: number | null;
  totalTokensPerDay: number | null;
  estimatedCostDay: number | null;
  trendPercentage: number | null;
  trendLabel: string;
  evidenceStatus: string;
};

export type AIOptimizationOpportunity = {
  deploymentName: string;
  category: string;
  recommendation: string;
  evidence: string;
  priority: 'Low' | 'Medium' | 'High';
};

export type AIUsageSummary = {
  status: string;
  periodStart: string;
  periodEnd: string;
  deployments: AIModelDeploymentUsage[];
  opportunities: AIOptimizationOpportunity[];
};

export type ChargebackRow = {
  dimension: string;
  value: string;
  monthlyCost: number;
  pctOfTotal: number;
};

export type ChargebackSummary = {
  available: boolean;
  status: string;
  allocatedCost: number;
  unallocatedCost: number;
  allocationPercentage: number;
  rows: ChargebackRow[];
};

export type TagValueCost = {
  value: string;
  monthlyCost: number;
  pctOfTotal: number;
  forecastNextMonth: number | null;
};

export type TagDimensionCost = {
  tagKey: string;
  unallocatedCost: number;
  rows: TagValueCost[];
};

export type TagCostSummary = {
  available: boolean;
  status: string;
  totalSpend: number;
  growthRate: number | null;
  dimensions: TagDimensionCost[];
};

export type ExtendedSupportRow = {
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  monthlyCost: number;
};

export type ExtendedSupportSummary = {
  available: boolean;
  status: string;
  totalMonthlyCost: number;
  rows: ExtendedSupportRow[];
};

export type OffHoursSavingsRow = {
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  resourceId: string;
  resourceName: string;
  monthlyCost: number;
  estimatedMonthlySaving: number;
};

export type OffHoursSavingsSummary = {
  available: boolean;
  status: string;
  offHoursFraction: number;
  totalMonthlyCost: number;
  totalEstimatedSaving: number;
  rows: OffHoursSavingsRow[];
};

export type ReportMetadata = {
  period: string;
  periodStart: string;
  periodEnd: string;
  costBasis: string;
  currency: string;
  generatedAt: string;
  source: string;
  staleDays: number;
  protectedTagKeys: string[];
  excludedProtectedResources: number;
};

export type DataCompleteness = {
  requestedSubscriptions: number;
  availableSubscriptions: number;
  complete: boolean;
  status: string;
};

export type OperationalSignal = {
  key: string;
  label: string;
  value: string;
  detail: string;
  tone: string;
};

export type ServiceSpendSummary = {
  rank: number;
  serviceName: string;
  displayName: string;
  monthlySpend: number;
  pctOfTotal: number;
};

export type ExecutiveFinding = {
  rank: number;
  category: string;
  finding: string;
  evidence: string;
  impactType: 'potential_savings' | 'cost_at_risk' | 'inventory';
  monthlySaving: number | null;
  monthlyCostAtRisk: number | null;
  severity: string;
};

export type SubscriptionReference = {
  subscriptionId: string;
  subscriptionName: string;
};

export type DomainSummary = {
  domain: string;
  domainSpendMonth: number;
  verifiedSavingMonth: number;
  verifiedSavingYear: number;
  monthlyCostAtRisk: number;
  pctOfDomainSpend: number;
  pctOfTotalSpend: number;
  categories: FindingCategorySummary[];
  advisorRecommendations: DomainAdvisorRecommendation[];
};

export type DomainAdvisorRecommendation = {
  resourceId: string;
  resourceName: string;
  subscriptionId: string;
  subscriptionName: string;
  problem: string;
  recommendation: string;
  estimatedSavings: number | null;
  savingsPeriod: string;
  billedCost: number | null;
};

export type SubscriptionBreakdownRow = {
  subscriptionId: string;
  subscriptionName: string;
  currentSpend: number;
  categoryCosts: Record<string, number>;
  totalWaste: number;
  pctSaved: number;
};

export type SavingsRoadmapItem = {
  category: string;
  opportunity: string;
  domain: string;
  monthly: number;
  annual: number;
  resources: number;
  recommendedAction: string;
  impactType: 'potential_savings' | 'cost_at_risk' | 'inventory';
  immediate: boolean;
  risk: string;
  effort: string;
  prerequisites: string[];
  affectedSubscriptions: SubscriptionReference[];
};

export type ActionPlanItem = {
  actionId: string;
  action: string;
  savingMonth: number;
  prerequisite: string;
  affectedSubscriptions: SubscriptionReference[];
};

export type FinOpsActionState = {
  scopeHash: string;
  actionId: string;
  status: 'open' | 'in_progress' | 'completed' | 'dismissed';
  owner: string;
  dueDate: string | null;
  completedAt: string | null;
  realizedSavingMonth: number | null;
  note: string;
  updatedAt: string;
  updatedBy: string;
  version?: number;
};

export type Budget = {
  subscriptionId: string;
  name: string;
  category: string;
  amount: number;
  currency: string;
  timeGrain: string;
  periodStart: string;
  periodEnd: string;
  currentSpend: number | null;
  forecastSpend: number | null;
  scope?: string;
  filter?: Record<string, unknown>;
  observedAt?: string;
  costBasis?: string;
};

export type BudgetTimeGrain = 'Monthly' | 'Quarterly' | 'Annually';

export type BudgetWriteRequest = {
  subscriptionId: string;
  name: string;
  amount: number;
  timeGrain: BudgetTimeGrain;
  startDate: string;
  alertThresholdPercent?: number | null;
  alertEmail?: string | null;
};

export type AdvisorMeasure = {
  measure: string;
  value: string;
  monetaryValue: number | null;
  explanation: string;
};
export type AdvisorReconciliation = {
  measures: AdvisorMeasure[];
  recommendations: DomainAdvisorRecommendation[];
};

export type MetricCoverageSummary = {
  category: string;
  candidateCount: number;
  completeCount: number;
  zeroTrafficCount: number;
  unavailableCount: number;
  periodStart: string;
  periodEnd: string;
};

export type GovernanceRow = {
  subscriptionId: string;
  subscriptionName: string;
  untaggedResources: number;
  pctOfEstate: number;
};

export type PricingSubscriptionSummary = {
  subscriptionId: string;
  subscriptionName: string;
  billedCost: number;
  effectiveCost: number;
  listCost: number;
  contractedCost: number;
  negotiatedDiscount: number;
  negotiatedDiscountPercentage: number;
};

export type PricingSummary = {
  available: boolean;
  status: string;
  dataVersion: string | null;
  period: string | null;
  billingCurrency: string | null;
  pricingCurrencies: string[];
  billedCost: number;
  effectiveCost: number;
  listCost: number;
  contractedCost: number;
  negotiatedDiscount: number;
  negotiatedDiscountPercentage: number;
  reconciliationVariance: number | null;
  subscriptions: PricingSubscriptionSummary[];
};

export type CommitmentBenefitBreakdown = {
  rowCount: number;
  usedEffectiveCost: number;
  unusedEffectiveCost: number;
  realizedBenefit: number;
};

export type CommitmentSummary = {
  observed: boolean;
  status: string;
  period: string;
  currency: string;
  reservations: CommitmentBenefitBreakdown;
  savingsPlans: CommitmentBenefitBreakdown;
};

export type ComputeCoverageSplit = {
  totalCost: number;
  paygCost: number;
  savingsPlanCost: number;
  reservationCost: number;
  paygPercentage: number | null;
  savingsPlanPercentage: number | null;
  reservationPercentage: number | null;
};

export type CommitmentMonthPoint = {
  month: string;
  currency: string;
  reservationCommittedCost: number;
  reservationUsedCost: number;
  reservationUnusedCost: number;
  reservationRealizedSavings: number;
  reservationCoveragePercentage: number | null;
  savingsPlanCommittedCost: number;
  savingsPlanUsedCost: number;
  savingsPlanUnusedCost: number;
  savingsPlanRealizedSavings: number;
  acdEffectiveCost: number;
  acdLeastPriceCost: number;
  acdSavings: number;
  acdSavingsPercentage: number | null;
  spotEffectiveCost: number;
  spotLeastPriceCost: number;
  spotSavings: number;
  spotSavingsPercentage: number | null;
  computeVmCoverage: ComputeCoverageSplit;
};

export type CommitmentInsights = {
  available: boolean;
  status: string;
  months: CommitmentMonthPoint[];
};

export type RecommendationLookBack = 'Last7Days' | 'Last30Days' | 'Last60Days';
export type RecommendationTerm = 'P1Y' | 'P3Y';
export type ReservationResourceType =
  | 'AppService'
  | 'AzureDataExplorer'
  | 'BlockBlob'
  | 'CosmosDB'
  | 'ManagedDisk'
  | 'MariaDB'
  | 'MySQL'
  | 'PostgreSQL'
  | 'RedHat'
  | 'RedisCache'
  | 'SQLDatabases'
  | 'SUSELinux'
  | 'SqlDataWarehouse'
  | 'VMwareCloudSimple'
  | 'VirtualMachines';

export type RateOptimizationScenario = {
  scope: 'Single';
  lookBackPeriod: RecommendationLookBack;
  term: RecommendationTerm;
  reservationResourceType: ReservationResourceType;
};

export type RateRecommendation = {
  kind: 'reservation' | 'savings_plan';
  recommendationId: string;
  subscriptionId: string;
  subscriptionName: string;
  sku: string;
  region: string;
  quantity: number | null;
  hourlyCommitment: number | null;
  commitmentGranularity: string;
  term: string;
  lookBackPeriod: string;
  scope: string;
  resourceType: string;
  currency: string;
  costWithoutBenefit: number | null;
  costWithBenefit: number | null;
  projectedSavings: number | null;
  savingsPercentage: number | null;
  coveragePercentage: number | null;
  utilizationPercentage: number | null;
  wastageCost: number | null;
  firstUsageDate: string;
  lastUsageDate: string;
  totalHours: number | null;
  overlapGroup: string;
  source: string;
  reviewUrl: string;
};

export type RecommendationSourceStatus = {
  source: 'reservation' | 'savings_plan';
  subscriptionId: string;
  subscriptionName: string;
  status: 'available' | 'no_recommendation' | 'unavailable';
  message: string;
};

export type RateOptimizationResponse = {
  scenario: RateOptimizationScenario;
  generatedAt: string;
  reservations: RateRecommendation[];
  savingsPlans: RateRecommendation[];
  sourceStatus: RecommendationSourceStatus[];
  projectionNotice: string;
};

export type AnomalyTrendPoint = {
  date: string;
  actualCost: number;
  expectedCost: number | null;
  expectedLower: number | null;
  expectedUpper: number | null;
};

export type CostAnomaly = {
  anomalyId: string;
  date: string;
  firstDetectedDate: string;
  lastDetectedDate: string;
  durationDays: number;
  anomalyType: 'spike' | 'drop' | 'new_resource';
  dimensionType: 'subscription' | 'service' | 'resource_group' | 'resource';
  dimensionName: string;
  dimensionId: string;
  subscriptionId: string;
  subscriptionName: string;
  actualCost: number;
  expectedCost: number;
  expectedLower: number;
  expectedUpper: number;
  absoluteDelta: number;
  percentageDelta: number | null;
  severity: 'Low' | 'Medium' | 'High';
  baselineSamples: number;
  contributors: { name: string; resourceId: string; cost: number }[];
  investigationUrl: string;
};

export type AnomalySummary = {
  algorithmVersion: string;
  label: string;
  status: 'ready' | 'insufficient_history';
  statusMessage: string;
  historyStart: string;
  historyEnd: string;
  completeDays: number;
  requiredDays: number;
  currency: string;
  generatedAt: string;
  trend: AnomalyTrendPoint[];
  anomalies: CostAnomaly[];
  aiAnomalies?: CostAnomaly[] | null;
};

export type FullReport = {
  executiveSummary: ExecutiveSummary;
  reportMetadata: ReportMetadata;
  completeness: DataCompleteness;
  operationalSignals: OperationalSignal[];
  topServices: ServiceSpendSummary[];
  spendCategories: SpendCategorySummary[];
  spendHistory: SpendHistorySummary;
  dailyCostTrend: DailyCostTrendSummary;
  tagDailyCostTrend: TagDailyCostTrendSummary;
  costHierarchy: CostHierarchyItem[];
  costDetails?: CostDetailSummary;
  regionSpend: RegionSpendSummary[];
  advisorScore: AdvisorScoreSummary;
  storageOptimization: StorageOptimizationSummary;
  aiUsage?: AIUsageSummary;
  chargeback?: ChargebackSummary;
  networkMetricCoverage: MetricCoverageSummary[];
  prioritizedFindings: ExecutiveFinding[];
  savingsRoadmap: SavingsRoadmapItem[];
  subscriptionBreakdown: SubscriptionBreakdownRow[];
  domains: Record<string, DomainSummary>;
  actionPlan: ActionPlanItem[];
  advisorReconciliation: AdvisorReconciliation;
  governance: GovernanceRow[];
  pricingSummary: PricingSummary;
  commitmentSummary: CommitmentSummary;
  commitmentInsights: CommitmentInsights;
  tagCosts?: TagCostSummary;
  extendedSupport?: ExtendedSupportSummary;
  offHoursSavings?: OffHoursSavingsSummary;
  tierACategories: FindingCategorySummary[];
};

export type ReportSnapshot = {
  snapshotId: string;
  scopeHash: string;
  subscriptionIds: string[];
  staleDays: number;
  createdAt: string;
  reportSchemaVersion: string;
  report: FullReport;
};

export type ReportSnapshotSummary = {
  snapshotId: string;
  scopeHash: string;
  subscriptionIds: string[];
  staleDays: number;
  createdAt: string;
  period: string;
  periodStart: string;
  periodEnd: string;
  currency: string;
  costBasis: string;
  reportSchemaVersion: string;
};
