import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { BarChart3, Boxes, ChartNoAxesCombined, Check, ChevronRight, ClipboardList, Copy, Download, ExternalLink, FileCode2, Gauge, Inbox, LayoutDashboard, LoaderCircle, Mail, Menu, Plus, RefreshCw, Search, ShieldCheck, TrendingDown, TrendingUp, X } from 'lucide-react';
import worldMapUrl from '@svg-maps/world/world.svg?url';
import {
  downloadCustomReport,
  downloadReportArtifact,
  emailReportArtifact,
  downloadFocusExport,
  getExchangeRates,
  getFinOpsActions,
  getStorageOnboardingGuidance,
  getRateOptimization,
  listFocusExportFiles,
  listReportSnapshots,
  storageOnboardingTemplateUrl,
  updateFinOpsAction,
  listBudgets,
  createBudget,
  updateBudget,
  deleteBudget,
  type CostAgentOutput,
  type ExchangeRates,
  type FocusExportFile,
  type ReportExportType,
  type StorageOnboardingGuidance,
} from '../api';
import type { FocusPricingEvidence, RemediationPlan } from '../findings/models';
import type {
  AnomalyTrendPoint,
  Budget,
  BudgetTimeGrain,
  BudgetWriteRequest,
  CommitmentMonthPoint,
  CostHierarchyItem,
  CostAnomaly,
  DomainSummary,
  FullReport,
  FinOpsActionState,
  MetricCoverageSummary,
  RateOptimizationResponse,
  RateRecommendation,
  ReportSnapshotSummary,
  RecommendationLookBack,
  RecommendationTerm,
  ReservationResourceType,
  SubscriptionReference,
} from '../report/models';
import { CATEGORY_DISPLAY_NAMES } from '../findings/categories';
import { SqlOptimization } from './SqlOptimization';
import { BillingHistoryTab, HourlyCostPanel } from './BillingHistory';
import { AICostAlerts, AnomalyOverview, useAnomalySummary, type AnomalyState } from './AnomalyOverview';
import { billingWindow } from '../report/billingHistory';
import { tagDistribution } from '../report/tagDistribution';
import { CostExportButton, CostFilters, CostWindowOverview, DailySubscriptionValues, PeriodCostAnomalies, RequiredTagCosts, SubscriptionCostBreakdown, type SelectedDay } from './CostExplorer';
import { GroupedCostBreakdown } from './CostBreakdown';
import { matchesCostFilter, presetCostWindow, type CostDimension, type CostFilter, type CostWindow } from '../report/costDetails';
import { BudgetContext, BudgetDailyChart, budgetThreshold, relateBudgets, useBudgetSummary, type BudgetState } from './BudgetContext';
import { ServiceRetirements } from './ServiceRetirements';
import { BRAND_NAME } from '../brand';
import './region-map.css';

const percent = (n: number) => `${(n * 100).toFixed(1)}%`;
const reportDate = (value: string) => new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});
const reportCurrencyFallback = (currency: string) => currency || 'USD';

function aggregateFocusPricingEvidence(items: FocusPricingEvidence[]): FocusPricingEvidence[] {
  const grouped = new Map<string, FocusPricingEvidence>();
  items.forEach((item) => {
    const key = JSON.stringify([
      item.skuId,
      item.skuPriceId,
      item.pricingCategory,
      item.pricingCurrency,
      item.pricingUnit,
      item.listUnitPrice,
      item.contractedUnitPrice,
      item.effectiveUnitPrice,
      item.commitmentDiscountCategory,
      item.commitmentDiscountType,
      item.commitmentDiscountStatus,
    ]);
    const current = grouped.get(key);
    grouped.set(key, current ? {
      ...current,
      pricingQuantity: current.pricingQuantity + item.pricingQuantity,
      listCost: current.listCost + item.listCost,
      contractedCost: current.contractedCost + item.contractedCost,
      effectiveCost: current.effectiveCost + item.effectiveCost,
      billedCost: current.billedCost + item.billedCost,
    } : { ...item });
  });
  return [...grouped.values()];
}

function FocusPricingEvidenceDetails({
  items,
  displayCurrency,
}: {
  items: FocusPricingEvidence[];
  displayCurrency: string;
}) {
  const aggregated = aggregateFocusPricingEvidence(items);
  if (aggregated.length === 0) return null;
  return (
    <ul className="focus-pricing-list" aria-label="Distinct FOCUS pricing evidence">
      {aggregated.map((evidence) => {
        const sourceCurrency = evidence.pricingCurrency || reportCurrencyFallback(displayCurrency);
        const sourceFormatter = new Intl.NumberFormat(undefined, {
          style: 'currency',
          currency: sourceCurrency,
          maximumFractionDigits: 6,
        });
        return (
          <li key={JSON.stringify([
            evidence.skuId,
            evidence.skuPriceId,
            evidence.pricingCategory,
            evidence.pricingCurrency,
            evidence.pricingUnit,
            evidence.listUnitPrice,
            evidence.contractedUnitPrice,
            evidence.effectiveUnitPrice,
            evidence.commitmentDiscountCategory,
            evidence.commitmentDiscountType,
            evidence.commitmentDiscountStatus,
          ])}>
            <span>
              <b>{evidence.pricingCategory || 'FOCUS price'}</b>
              <small>{evidence.pricingQuantity.toLocaleString()} {evidence.pricingUnit} · {sourceCurrency}</small>
            </span>
            <span>
              <small>List {sourceFormatter.format(evidence.listUnitPrice)}</small>
              <small>Contracted {sourceFormatter.format(evidence.contractedUnitPrice)}</small>
              <small>Effective {sourceFormatter.format(evidence.effectiveUnitPrice)}</small>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

const TABS = [
  'Executive Summary',
  'Savings Roadmap',
  'Stale Resources',
  'Subscription Breakdown',
  'History',
  'Cost by Hour',
  'EA Pricing',
  'Rate Optimization',
  'Cost Anomalies',
  'Compute Optimization',
  'Storage Optimization',
  'Network Optimization',
  'Azure SQL Optimization',
  'AI Optimization',
  'Advisor Reconciliation',
  'Governance & Risk',
  'Cost by Tags/Application',
  'Budgets',
  'Action Plan',
] as const;

type Tab = (typeof TABS)[number];
type MoneyFormatter = (value: number) => string;

function EvidenceState({ title, detail, loading = false }: { title: string; detail: string; loading?: boolean }) {
  const Icon = loading ? LoaderCircle : Inbox;
  return (
    <div className="evidence-state" role="status" aria-busy={loading}>
      <span className="evidence-state-icon" aria-hidden="true"><Icon className={loading ? 'spin' : undefined} size={21} /></span>
      <div><h3>{title}</h3><p>{detail}</p></div>
    </div>
  );
}

function useDialogFocus(onClose: () => void) {
  const dialogRef = useRef<HTMLElement>(null);
  const trigger = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const controls = () => [...dialog.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, summary, [tabindex]')]
      .filter((element) => !element.matches(':disabled, [tabindex="-1"]') && element.getClientRects().length > 0);
    (controls()[0] ?? dialog).focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
      if (event.key !== 'Tab') return;
      const items = controls();
      if (!items.length) { event.preventDefault(); dialog.focus(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };
    dialog.addEventListener('keydown', handleKey);
    return () => {
      dialog.removeEventListener('keydown', handleKey);
      document.body.style.overflow = previousOverflow;
      if (trigger.current?.isConnected) trigger.current.focus({ preventScroll: true });
    };
  }, [onClose]);
  return dialogRef;
}

type PrimaryNav = 'dashboard' | 'costManagement' | 'resources' | 'analytics' | 'recommendations' | 'reports';

const PRIMARY_NAV_ORDER: PrimaryNav[] = ['dashboard', 'costManagement', 'resources', 'analytics', 'recommendations', 'reports'];

const PRIMARY_NAV_LABELS: Record<PrimaryNav, string> = {
  dashboard: 'Dashboard',
  costManagement: 'Cost Management',
  resources: 'Resources',
  analytics: 'Analytics',
  recommendations: 'Recommendations',
  reports: 'Reports',
};

const PRIMARY_NAV_ICONS = {
  dashboard: LayoutDashboard,
  costManagement: BarChart3,
  resources: Boxes,
  analytics: ChartNoAxesCombined,
  recommendations: Gauge,
  reports: ClipboardList,
};

const PRIMARY_NAV_TABS: Record<PrimaryNav, Tab[]> = {
  dashboard: ['Executive Summary'],
  costManagement: ['Subscription Breakdown', 'History', 'Cost by Hour', 'Cost by Tags/Application', 'EA Pricing', 'Rate Optimization', 'Cost Anomalies', 'Budgets'],
  resources: ['Stale Resources', 'Governance & Risk'],
  analytics: ['Advisor Reconciliation'],
  recommendations: ['Savings Roadmap', 'Compute Optimization', 'Storage Optimization', 'Network Optimization', 'Azure SQL Optimization', 'AI Optimization'],
  reports: ['Action Plan'],
};

const DOMAIN_TABS: Record<string, Tab> = {
  compute: 'Compute Optimization',
  storage: 'Storage Optimization',
  network: 'Network Optimization',
  sql: 'Azure SQL Optimization',
  ai: 'AI Optimization',
};

const DOMAIN_HEADINGS: Record<string, string> = {
  compute: 'Compute',
  storage: 'Storage',
  network: 'Network',
  sql: 'Azure SQL',
  ai: 'AI',
};

const EVIDENCE_LABELS = {
  verified_cost: 'Verified cost',
  advisor_estimate: 'Advisor estimate',
  metrics_verified_idle: 'Metrics verified',
  inventory_candidate: 'Inventory candidate',
} as const;

const EVIDENCE_TITLES = {
  verified_cost: 'Resource matched to the complete closed-period Cost Details export.',
  advisor_estimate: 'Potential saving is reported by Azure Advisor and is not a reconciled billed amount.',
  metrics_verified_idle: 'Idle status is supported by complete Azure Monitor coverage for the assessment period.',
  inventory_candidate: 'Resource matched an inventory rule but has no matched closed-period resource cost.',
} as const;

const NETWORK_METRIC_LABELS: Record<string, string> = {
  idle_virtual_network_gateways: 'Virtual Network Gateways',
  idle_nat_gateways: 'NAT Gateways',
  idle_expressroute_circuits: 'ExpressRoute circuits',
};

const STALE_CATEGORIES = new Set([
  'unattached_disks',
  'stopped_vms',
  'idle_public_ips',
  'empty_backend_pools',
  'empty_load_balancer_backend_pools',
  'idle_virtual_network_gateways',
  'idle_nat_gateways',
  'idle_expressroute_circuits',
  'old_snapshots',
  'unattached_network_interfaces',
  'unassociated_network_security_groups',
  'unassociated_route_tables',
  'empty_availability_sets',
  'deallocated_virtual_machines',
  'zero_instance_vm_scale_sets',
  'empty_app_service_plans',
  'stopped_web_apps',
  'empty_virtual_networks',
  'disconnected_private_endpoints',
  'stopped_aks_clusters',
  'empty_resource_groups',
  'old_custom_images',
]);

function azurePortalResourceUrl(resourceId: string) {
  const normalizedId = resourceId.startsWith('/') ? resourceId : `/${resourceId}`;
  return `https://portal.azure.com/#@/resource${encodeURI(normalizedId)}/overview`;
}

function SubscriptionReferences({ subscriptions }: { subscriptions: SubscriptionReference[] }) {
  return (
    <span className="subscription-reference-list">
      {subscriptions.map((subscription) => (
        <span className="subscription-reference" key={subscription.subscriptionId}>
          <strong>{subscription.subscriptionName}</strong>
          <small title={subscription.subscriptionId}>{subscription.subscriptionId}</small>
        </span>
      ))}
    </span>
  );
}

// Persistent left-side navigation (overrides the earlier "top-only nav, no left
// sidebar" decision per explicit ADO Task 781 instruction, comment 8538467, item 8).
export function LeftNavSidebar({ activeTab, onSelect }: { activeTab: Tab; onSelect: (tab: Tab) => void }) {
  const [expandedGroups, setExpandedGroups] = useState<Set<PrimaryNav>>(() => new Set(['dashboard', 'costManagement']));
  const [mobileOpen, setMobileOpen] = useState(false);
  const [pageQuery, setPageQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const query = pageQuery.trim().toLocaleLowerCase();
  const groups = PRIMARY_NAV_ORDER.map((group) => ({
    group,
    pages: PRIMARY_NAV_TABS[group].filter((page) => !query || `${PRIMARY_NAV_LABELS[group]} ${page}`.toLocaleLowerCase().includes(query)),
  })).filter(({ pages }) => pages.length > 0);
  useEffect(() => {
    const activeGroup = PRIMARY_NAV_ORDER.find((group) => PRIMARY_NAV_TABS[group].includes(activeTab));
    if (activeGroup) setExpandedGroups(new Set(['dashboard', 'costManagement', activeGroup]));
  }, [activeTab]);
  function selectPage(page: Tab) {
    setPageQuery('');
    setMobileOpen(false);
    onSelect(page);
  }
  return (
    <nav className={`left-nav-sidebar${mobileOpen ? ' is-open' : ''}`} aria-label="Report navigation">
      <button type="button" className="report-nav-mobile-toggle" aria-label="Report pages" aria-expanded={mobileOpen} aria-controls="report-nav-groups" onClick={() => setMobileOpen((open) => !open)}>
        <Menu size={18} aria-hidden="true" /><span>{activeTab}</span><ChevronRight size={16} aria-hidden="true" />
      </button>
      <div id="report-nav-groups" className="report-nav-groups">
        <div className="report-nav-search">
          <Search size={16} aria-hidden="true" />
          <input ref={searchRef} type="search" aria-label="Find a report page" placeholder="Find a page" value={pageQuery} onChange={(event) => setPageQuery(event.target.value)} onKeyDown={(event) => {
            if (event.key === 'Escape') setPageQuery('');
            if (event.key === 'Enter' && query && groups.length) { event.preventDefault(); selectPage(groups[0].pages[0]); }
          }} />
          {pageQuery && <button type="button" aria-label="Clear page search" title="Clear page search" onClick={() => { setPageQuery(''); searchRef.current?.focus(); }}><X size={14} aria-hidden="true" /></button>}
        </div>
        {groups.length === 0 && <p className="report-nav-empty" role="status">No matching pages</p>}
      {groups.map(({ group, pages }) => {
        const Icon = PRIMARY_NAV_ICONS[group];
        return (
        <div className="left-nav-group" key={group}>
          <button
            type="button"
            className="left-nav-group-toggle"
            aria-expanded={!!query || expandedGroups.has(group)}
            aria-controls={`report-nav-${group}`}
            disabled={!!query}
            onClick={() => setExpandedGroups((current) => {
              const next = new Set(current);
              if (next.has(group)) next.delete(group);
              else next.add(group);
              return next;
            })}
          >
            <Icon className="report-nav-group-icon" size={16} aria-hidden="true" />
            <span>{PRIMARY_NAV_LABELS[group]}</span>
            <ChevronRight className="report-nav-chevron" size={14} aria-hidden="true" />
          </button>
          <ul id={`report-nav-${group}`} className="left-nav-children" hidden={!query && !expandedGroups.has(group)}>
            {pages.map((childTab) => (
              <li key={childTab}>
                <button
                  type="button"
                  className={childTab === activeTab ? 'active' : ''}
                  onClick={() => selectPage(childTab)}
                  aria-current={childTab === activeTab ? 'page' : undefined}
                >
                  {childTab}
                </button>
              </li>
            ))}
          </ul>
        </div>
        );
      })}
      </div>
    </nav>
  );
}

// Headline metrics that stay visible across every tab, instead of only living
// inside the Executive Summary panel.
function TopSummaryBar({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const s = report.executiveSummary;
  const metadata = report.reportMetadata;
  const completeness = report.completeness;
  const months = report.spendHistory.months;
  const lastComplete = months.length > 0 ? months[months.length - 1] : null;
  return (
    <div className="summary-strip" aria-label="Report headline metrics">
      <div className="summary-tile">
        <span className="summary-tile-label">Assessed month</span>
        <strong className="summary-tile-value">{formatMoney(s.currentMonthlySpend)}</strong>
        <small>
          {s.spendChangePercentage === null
            ? 'vs previous month pending'
            : `${s.spendChangePercentage >= 0 ? '▲' : '▼'} ${Math.abs(s.spendChangePercentage * 100).toFixed(1)}% vs previous`}
        </small>
      </div>
      <div className="summary-tile">
        <span className="summary-tile-label">Last complete month</span>
        <strong className="summary-tile-value">{lastComplete ? formatMoney(lastComplete.total) : '—'}</strong>
        <small>
          {lastComplete
            ? new Date(`${lastComplete.month}-01T00:00:00Z`).toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' })
            : report.spendHistory.statusMessage}
        </small>
      </div>
      <div className="summary-tile positive">
        <span className="summary-tile-label">Potential savings</span>
        <strong className="summary-tile-value">{formatMoney(s.potentialSavingsMonth)}</strong>
        <small>{percent(s.pctRecoverable)} of spend recoverable</small>
      </div>
      <div className="summary-tile">
        <span className="summary-tile-label">Advisor score</span>
        <strong className="summary-tile-value">{report.advisorScore.score === null ? '—' : `${report.advisorScore.score.toFixed(0)}/100`}</strong>
        <small>{report.advisorScore.status}</small>
      </div>
      <div className="summary-tile">
        <span className="summary-tile-label">Report coverage</span>
        <strong className="summary-tile-value">{completeness.availableSubscriptions}/{completeness.requestedSubscriptions}</strong>
        <small>{reportDate(metadata.periodStart)} – {reportDate(metadata.periodEnd)}</small>
      </div>
    </div>
  );
}

const CATEGORY_PILL_DOMAINS: { domain: string; label: string }[] = [
  { domain: 'compute', label: 'Compute' },
  { domain: 'storage', label: 'Storage' },
  { domain: 'network', label: 'Networking' },
  { domain: 'sql', label: 'Azure SQL' },
  { domain: 'ai', label: 'AI & ML' },
];

// A persistent, clickable "spend by category" strip that jumps straight to the
// matching domain tab — quick-glance nav that stays put regardless of the
// active sidebar section.
function CategorySpendPills({
  report,
  formatMoney,
  activeTab,
  onSelect,
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  activeTab: Tab;
  onSelect: (t: Tab) => void;
}) {
  const domainEntries = CATEGORY_PILL_DOMAINS.map(({ domain, label }) => ({
    domain,
    label,
    spend: report.domains[domain]?.domainSpendMonth ?? 0,
    tab: DOMAIN_TABS[domain],
  }));
  const domainTotal = domainEntries.reduce((sum, item) => sum + item.spend, 0);
  const otherSpend = Math.max(report.executiveSummary.currentMonthlySpend - domainTotal, 0);
  return (
    <nav className="category-pill-row" aria-label="Spend by category">
      <button
        type="button"
        aria-pressed={activeTab === 'Executive Summary'}
        className={`category-pill ${activeTab === 'Executive Summary' ? 'active' : ''}`}
        onClick={() => onSelect('Executive Summary')}
      >
        <span>All spend</span>
        <strong>{formatMoney(report.executiveSummary.currentMonthlySpend)}</strong>
      </button>
      {domainEntries.map((item) => (
        <button
          key={item.domain}
          type="button"
          aria-pressed={activeTab === item.tab}
          className={`category-pill ${activeTab === item.tab ? 'active' : ''}`}
          onClick={() => onSelect(item.tab)}
        >
          <span>{item.label}</span>
          <strong>{formatMoney(item.spend)}</strong>
        </button>
      ))}
      {otherSpend > 0 && (
        <div className="category-pill category-pill-static">
          <span>Other services</span>
          <strong>{formatMoney(otherSpend)}</strong>
        </div>
      )}
    </nav>
  );
}

export function ReportView({ report, narration, snapshotId, snapshotCreatedAt, costWindow: controlledCostWindow, onCostWindowChange }: { report: FullReport; narration: CostAgentOutput; snapshotId: string | null; snapshotCreatedAt?: string; costWindow?: CostWindow; onCostWindowChange?: (value: CostWindow) => void }) {
  const [tab, setTab] = useState<Tab>('Executive Summary');
  /* The cost window is now presented once, in the saved-report strip, which is
     owned by App - so in the running application this is a controlled value.
     The uncontrolled fallback keeps ReportView complete on its own, which is
     how it is mounted in tests and how a caller that does not want to own a
     window can still use it. Same contract as a React input with value vs
     defaultValue. */
  const [uncontrolledCostWindow, setUncontrolledCostWindow] = useState<CostWindow>(() => presetCostWindow(report.costDetails?.dates ?? report.dailyCostTrend.days.map((day) => day.date), 30));
  const costWindow = controlledCostWindow ?? uncontrolledCostWindow;
  const setCostWindow = onCostWindowChange ?? setUncontrolledCostWindow;
  const [costFilters, setCostFilters] = useState<CostFilter>({});
  useEffect(() => setCostFilters({}), [report]);
  useEffect(() => setUncontrolledCostWindow(presetCostWindow(report.costDetails?.dates ?? report.dailyCostTrend.days.map((day) => day.date), 30)), [report]);
  const anomalyState = useAnomalySummary(report);
  const budgetState = useBudgetSummary(report);
  const [expandedCategory, setExpandedCategory] = useState<string | null>(null);
  const sourceCurrency = report.reportMetadata.currency.toUpperCase();
  const [displayCurrency, setDisplayCurrency] = useState(sourceCurrency);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRates | null>(null);
  const [exchangeError, setExchangeError] = useState<string | null>(null);
  const [loadingExchangeRates, setLoadingExchangeRates] = useState(true);
  const [activeRemediation, setActiveRemediation] = useState<{ finding: string; plan: RemediationPlan } | null>(null);
  const [showFocusDownloads, setShowFocusDownloads] = useState(false);
  const [showReportExports, setShowReportExports] = useState(false);
  const [showCustomReportBuilder, setShowCustomReportBuilder] = useState(false);
  const narrativeByCategory = new Map((narration?.prioritized_findings ?? []).map((p) => [p.category, p]));
  const categoryTabs = new Map<string, Tab>();
  Object.entries(report.domains).forEach(([domain, summary]) => {
    summary.categories.forEach((category) => categoryTabs.set(category.category, DOMAIN_TABS[domain]));
  });

  useEffect(() => {
    if (!expandedCategory) return;
    document.getElementById(`finding-${expandedCategory}`)?.scrollIntoView({ block: 'nearest' });
  }, [expandedCategory, tab]);

  useEffect(() => {
    const controller = new AbortController();
    setDisplayCurrency(sourceCurrency);
    setExchangeRates(null);
    setExchangeError(null);
    setLoadingExchangeRates(true);
    getExchangeRates(sourceCurrency, controller.signal)
      .then(setExchangeRates)
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setExchangeError(error instanceof Error ? error.message : 'Exchange rates are unavailable.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingExchangeRates(false);
      });
    return () => controller.abort();
  }, [sourceCurrency]);

  useEffect(() => {
    if (!activeRemediation) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setActiveRemediation(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [activeRemediation]);

  const conversionRate = displayCurrency === sourceCurrency
    ? 1
    : exchangeRates?.rates[displayCurrency] ?? 1;
  const numberFormatter = new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: displayCurrency,
    currencyDisplay: 'narrowSymbol',
    maximumFractionDigits: 0,
  });
  const formatMoney: MoneyFormatter = (value) => numberFormatter.format(value * conversionRate);
  // Whole-dollar rounding hides real per-hour figures (most resources cost well
  // under $1/hr) - use a separate formatter with decimal precision for those.
  const hourlyFormatter = new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: displayCurrency,
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
  const formatHourlyMoney: MoneyFormatter = (value) => hourlyFormatter.format(value * conversionRate);
  const currencyNames = new Intl.DisplayNames(undefined, { type: 'currency' });
  const currencyOptions = exchangeRates ? Object.keys(exchangeRates.rates).sort() : [sourceCurrency];

  function selectTab(t: Tab) {
    setTab(t);
    requestAnimationFrame(() => document.getElementById('report-page-heading')?.focus({ preventScroll: true }));
  }

  function openFinding(category: string) {
    const targetTab = category === 'untagged_resources' ? 'Governance & Risk' : categoryTabs.get(category);
    if (!targetTab) return;
    selectTab(targetTab);
    setExpandedCategory(category);
  }

  function openRemediation(finding: string, plan: RemediationPlan | null) {
    if (plan) setActiveRemediation({ finding, plan });
  }

  return (
    <div className="dashboard-frame" data-report-page={tab}>
      <LeftNavSidebar activeTab={tab} onSelect={selectTab} />
      <div className="dashboard-main">
      <div className="dashboard-titlebar">
        <div className="report-page-title">
          <span>{PRIMARY_NAV_LABELS[PRIMARY_NAV_ORDER.find((group) => PRIMARY_NAV_TABS[group].includes(tab)) ?? 'dashboard']}</span>
          <h1 id="report-page-heading" tabIndex={-1}>{tab}</h1>
        </div>
        <span className="report-period-badge">{report.reportMetadata.period}</span>
        <button
          className="focus-download-control"
          type="button"
          title="Download FocusCost files"
          aria-label="Download FocusCost files"
          onClick={() => setShowFocusDownloads(true)}
        >
          <Download size={17} />
        </button>
        <button
          className="report-download-control"
          type="button"
          title={snapshotId ? 'Download stakeholder reports' : 'Run or reload a persisted report to enable downloads'}
          aria-label="Download stakeholder reports"
          disabled={!snapshotId}
          onClick={() => setShowReportExports(true)}
        >
          <FileCode2 size={17} />
        </button>
        <label className="currency-control">
          <span>Display currency</span>
          <select
            value={displayCurrency}
            disabled={loadingExchangeRates || exchangeRates === null}
            onChange={(event) => setDisplayCurrency(event.target.value)}
            aria-label="Display currency"
          >
            {currencyOptions.map((currencyCode) => (
              <option value={currencyCode} key={currencyCode}>
                {currencyCode} · {currencyNames.of(currencyCode) ?? currencyCode}
              </option>
            ))}
          </select>
        </label>
      </div>
      <details className="report-context-details" aria-label="Report context">
        <summary>
          <span>Report context</span>
          <span>{snapshotId ? 'Saved snapshot' : 'Unsaved assessment'} · {report.subscriptionBreakdown.length} subscription{report.subscriptionBreakdown.length === 1 ? '' : 's'}</span>
          {exchangeError && <span className="context-warning">Exchange rates unavailable</span>}
        </summary>
        <div className="report-context-body">
          <dl className="report-context-metadata">
            <div><dt>Assessment completed</dt><dd>{new Date(snapshotCreatedAt ?? report.reportMetadata.generatedAt).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}</dd></div>
            <div><dt>Billing period</dt><dd>{reportDate(report.reportMetadata.periodStart)} - {reportDate(report.reportMetadata.periodEnd)}</dd></div>
            <div><dt>Cost basis</dt><dd>{report.reportMetadata.costBasis} · {sourceCurrency}</dd></div>
          </dl>
          <div className={`currency-provenance ${exchangeError ? 'has-error' : ''}`} role="status" aria-live="polite">
        {loadingExchangeRates && <span>Loading ECB reference rates</span>}
        {!loadingExchangeRates && exchangeError && (
          <span>{sourceCurrency} billing values shown · {exchangeError}</span>
        )}
        {!loadingExchangeRates && exchangeRates && (
          <>
            <span>
              {displayCurrency === sourceCurrency
                ? `${sourceCurrency} billing currency · no conversion applied`
                : `1 ${sourceCurrency} = ${conversionRate.toLocaleString(undefined, { maximumFractionDigits: 6 })} ${displayCurrency}`}
            </span>
            <span>{exchangeRates.provider} · {reportDate(exchangeRates.publishedDate)}{exchangeRates.stale ? ' · cached rate' : ''}</span>
            <a href={exchangeRates.providerUrl} target="_blank" rel="noreferrer">
              Indicative rate <ExternalLink size={12} aria-hidden="true" />
            </a>
          </>
        )}
          </div>
          <TopSummaryBar report={report} formatMoney={formatMoney} />
        </div>
      </details>
      {activeRemediation && (
        <RemediationDialog
          finding={activeRemediation.finding}
          plan={activeRemediation.plan}
          onClose={() => setActiveRemediation(null)}
        />
      )}
      {showFocusDownloads && (
        <FocusExportDialog
          subscriptions={report.subscriptionBreakdown.map((item) => ({
            subscriptionId: item.subscriptionId,
            subscriptionName: item.subscriptionName,
          }))}
          onClose={() => setShowFocusDownloads(false)}
        />
      )}
      {showReportExports && snapshotId && (
        <ReportExportDialog
          snapshotId={snapshotId}
          report={report}
          onOpenCustom={() => {
            setShowReportExports(false);
            setShowCustomReportBuilder(true);
          }}
          onClose={() => setShowReportExports(false)}
        />
      )}
      {showCustomReportBuilder && snapshotId && (
        <CustomReportDialog
          currentSnapshotId={snapshotId}
          subscriptionIds={report.subscriptionBreakdown.map((item) => item.subscriptionId)}
          onClose={() => setShowCustomReportBuilder(false)}
        />
      )}
      <CategorySpendPills report={report} formatMoney={formatMoney} activeTab={tab} onSelect={selectTab} />
      <div className="dashboard-body">
        <div className="tab-panel" key={tab}>
          {tab === 'Executive Summary' && (
            <ExecutiveSummaryTab
              report={report}
              narrativeSummary={narration?.executive_summary}
              onOpenFinding={openFinding}
              formatMoney={formatMoney}
              formatHourlyMoney={formatHourlyMoney}
              displayCurrency={displayCurrency}
              anomalyState={anomalyState}
              onOpenAnomalies={() => selectTab('Cost Anomalies')}
              snapshotId={snapshotId}
              costWindow={costWindow}
              onCostWindowChange={setCostWindow}
              costFilters={costFilters}
              onCostFiltersChange={setCostFilters}
              budgetState={budgetState}
            />
          )}
          {tab === 'Savings Roadmap' && (
            <SavingsRoadmapTab report={report} onOpenFinding={openFinding} onOpenRemediation={openRemediation} formatMoney={formatMoney} displayCurrency={displayCurrency} />
          )}
          {tab === 'Stale Resources' && (
            <StaleResourcesTab report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
          )}
          {tab === 'Subscription Breakdown' && <SubscriptionCostBreakdown report={report} snapshotId={snapshotId} window={costWindow} onChange={setCostWindow} formatMoney={formatHourlyMoney} filters={costFilters} onFiltersChange={setCostFilters} />}
          {tab === 'History' && (
            <BillingHistoryTab
              report={report}
              formatMoney={formatHourlyMoney}
              details={report.costDetails}
              costWindow={costWindow}
              costFilters={costFilters}
              onCostFiltersChange={setCostFilters}
              displayCurrency={displayCurrency}
            />
          )}
          {tab === 'Cost by Hour' && <HourlyCostPanel report={report} formatMoney={formatHourlyMoney} formatHourlyMoney={formatHourlyMoney} snapshotId={snapshotId} budgetState={budgetState} costWindow={costWindow} onWindowChange={setCostWindow} costFilters={costFilters} onFiltersChange={setCostFilters} displayCurrency={displayCurrency} />}
          {tab === 'EA Pricing' && <PricingTab report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />}
          {tab === 'Rate Optimization' && (
            <RateOptimizationTab report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
          )}
          {tab === 'Cost Anomalies' && (
            <>
            <PeriodCostAnomalies report={report} window={costWindow} onChange={setCostWindow} formatMoney={formatHourlyMoney} filters={costFilters} onFiltersChange={setCostFilters} snapshotId={snapshotId} />
            <CostAnomaliesTab state={anomalyState} formatMoney={formatMoney} displayCurrency={displayCurrency} />
            </>
          )}
          {tab === 'Compute Optimization' && (
            <>
              <ComputeCostInsights report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
              <DomainTab
                domain={report.domains.compute}
                narrativeByCategory={narrativeByCategory}
                expandedCategory={expandedCategory}
                formatMoney={formatMoney}
                displayCurrency={displayCurrency}
                onOpenRemediation={openRemediation}
                onToggleCategory={(category) => setExpandedCategory((current) => current === category ? null : category)}
              />
            </>
          )}
          {tab === 'Storage Optimization' && (
            <>
              <StorageTierAnalysis report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
              <DomainTab
                domain={report.domains.storage}
                narrativeByCategory={narrativeByCategory}
                expandedCategory={expandedCategory}
                formatMoney={formatMoney}
                displayCurrency={displayCurrency}
                onOpenRemediation={openRemediation}
                onToggleCategory={(category) => setExpandedCategory((current) => current === category ? null : category)}
              />
            </>
          )}
          {tab === 'Network Optimization' && (
            <DomainTab
              domain={report.domains.network}
              narrativeByCategory={narrativeByCategory}
              expandedCategory={expandedCategory}
              formatMoney={formatMoney}
              displayCurrency={displayCurrency}
              onOpenRemediation={openRemediation}
              metricCoverage={report.networkMetricCoverage}
              onToggleCategory={(category) => setExpandedCategory((current) => current === category ? null : category)}
            />
          )}
          {tab === 'Azure SQL Optimization' && (
            <>
            <SqlOptimization lines={report.domains.sql.categories.flatMap(category => category.lines)} />
            <DomainTab
              domain={report.domains.sql}
              narrativeByCategory={narrativeByCategory}
              expandedCategory={expandedCategory}
              formatMoney={formatMoney}
              displayCurrency={displayCurrency}
              onOpenRemediation={openRemediation}
              onToggleCategory={(category) => setExpandedCategory((current) => current === category ? null : category)}
            />
            </>
          )}
          {tab === 'AI Optimization' && (
            <>
              <AICostAlerts state={anomalyState} formatMoney={formatHourlyMoney} displayCurrency={displayCurrency} />
              <AIUsageAnalysis report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
              <DomainTab
                domain={report.domains.ai}
                narrativeByCategory={narrativeByCategory}
                expandedCategory={expandedCategory}
                formatMoney={formatMoney}
                displayCurrency={displayCurrency}
                onOpenRemediation={openRemediation}
                onToggleCategory={(category) => setExpandedCategory((current) => current === category ? null : category)}
              />
            </>
          )}
          {tab === 'Advisor Reconciliation' && <AdvisorTab report={report} formatMoney={formatMoney} />}
          {tab === 'Governance & Risk' && <><RequiredTagCosts report={report} snapshotId={snapshotId} window={costWindow} formatMoney={formatHourlyMoney} filters={costFilters} /><GovernanceTab report={report} /><ServiceRetirements report={report} snapshotId={snapshotId} /></>}
          {tab === 'Cost by Tags/Application' && (
            <CostByTagsTab
              report={report}
              formatMoney={formatMoney}
              displayCurrency={displayCurrency}
              costWindow={costWindow}
              costFilters={costFilters}
              onCostFiltersChange={setCostFilters}
              budgetState={budgetState}
            />
          )}
          {tab === 'Budgets' && (
            <BudgetsTab report={report} costWindow={costWindow} formatMoney={formatMoney} />
          )}
          {tab === 'Action Plan' && <ActionPlanTab report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} snapshotId={snapshotId} />}
        </div>
      </div>
      </div>
    </div>
  );
}

type FocusExportSubscription = {
  subscriptionId: string;
  subscriptionName: string;
};

type ReportExportState = {
  state: 'idle' | 'downloading' | 'done' | 'failed';
  elapsedMs?: number;
  bytes?: number;
  error?: string;
};

type ReportEmailState = {
  state: 'idle' | 'sending' | 'done' | 'failed';
  recipient?: string;
  error?: string;
};

function ReportExportDialog({
  snapshotId,
  report,
  onOpenCustom,
  onClose,
}: {
  snapshotId: string;
  report: FullReport;
  onOpenCustom: () => void;
  onClose: () => void;
}) {
  const dialogRef = useDialogFocus(onClose);
  const [states, setStates] = useState<Record<ReportExportType, ReportExportState>>({
    executive: { state: 'idle' },
    full: { state: 'idle' },
    chargeback: { state: 'idle' },
    compliance: { state: 'idle' },
    finops: { state: 'idle' },
  });
  const [emailStates, setEmailStates] = useState<Record<ReportExportType, ReportEmailState>>({
    executive: { state: 'idle' },
    full: { state: 'idle' },
    chargeback: { state: 'idle' },
    compliance: { state: 'idle' },
    finops: { state: 'idle' },
  });
  const [fullSubscriptionId, setFullSubscriptionId] = useState('');

  async function download(reportType: ReportExportType) {
    setStates((current) => ({ ...current, [reportType]: { state: 'downloading' } }));
    try {
      const result = await downloadReportArtifact(
        snapshotId,
        reportType,
        reportType === 'full' && fullSubscriptionId ? fullSubscriptionId : undefined,
      );
      setStates((current) => ({ ...current, [reportType]: { state: 'done', ...result } }));
    } catch (error) {
      setStates((current) => ({
        ...current,
        [reportType]: {
          state: 'failed',
          error: error instanceof Error ? error.message : 'Report download failed.',
        },
      }));
    }
  }

  async function email(reportType: ReportExportType) {
    setEmailStates((current) => ({ ...current, [reportType]: { state: 'sending' } }));
    try {
      const result = await emailReportArtifact(snapshotId, reportType);
      setEmailStates((current) => ({
        ...current,
        [reportType]: { state: 'done', recipient: result.recipient },
      }));
    } catch (error) {
      setEmailStates((current) => ({
        ...current,
        [reportType]: {
          state: 'failed',
          error: error instanceof Error ? error.message : 'Report email failed.',
        },
      }));
    }
  }

  const exports: { type: ReportExportType; title: string; format: string; description: string }[] = [
    {
      type: 'executive',
      title: 'Executive Summary',
      format: 'PDF · one page',
      description: 'Key metrics, spend mix, top five savings and risk findings, and assessment provenance.',
    },
    {
      type: 'full',
      title: 'Full Assessment',
      format: 'XLSX · 13 worksheets',
      description: 'Subscriptions, roadmap, findings, domain resource evidence, AI usage, governance, and action plan.',
    },
    {
      type: 'chargeback',
      title: 'Chargeback',
      format: 'XLSX · tag allocation',
      description: 'Monthly cost by Team, Department, and Project tags, with missing allocation preserved as Unallocated.',
    },
    {
      type: 'compliance',
      title: 'Compliance',
      format: 'XLSX · governance',
      description: 'Resource tagging coverage and Azure Policy evaluation states, exemptions, conflicts, and affected resources.',
    },
    {
      type: 'finops',
      title: 'FinOps Monthly',
      format: 'XLSX · month over month',
      description: 'Current versus prior complete period, potential savings, and tracked action owner/status/realized outcomes.',
    },
  ];

  return (
    <div className="remediation-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={dialogRef} tabIndex={-1} className="remediation-dialog report-export-dialog" role="dialog" aria-modal="true" aria-labelledby="report-export-title">
        <header className="remediation-dialog-header">
          <div>
            <span>Stakeholder communication</span>
            <h2 id="report-export-title">Share reports</h2>
            <p>{report.reportMetadata.period} · {report.reportMetadata.currency} · snapshot {snapshotId}</p>
          </div>
          <button autoFocus type="button" onClick={onClose} title="Close" aria-label="Close report downloads"><X size={18} /></button>
        </header>
        <div className="report-export-body">
          <p className="report-export-integrity">
            Each artifact is generated from this exact persisted snapshot. If a newer assessment replaced it, reload before exporting.
          </p>
          <div className="report-export-options">
            {exports.map((item) => {
              const status = states[item.type];
              const emailStatus = emailStates[item.type];
              return (
                <article key={item.type}>
                  <span>{item.format}</span>
                  <h3>{item.title}</h3>
                  <p>{item.description}</p>
                  {item.type === 'full' && (
                    <label className="report-export-scope">
                      <span>Scope</span>
                      <select value={fullSubscriptionId} onChange={(event) => setFullSubscriptionId(event.target.value)}>
                        <option value="">All subscriptions (combined file)</option>
                        {report.subscriptionBreakdown.map((subscription) => (
                          <option value={subscription.subscriptionId} key={subscription.subscriptionId}>{subscription.subscriptionName}</option>
                        ))}
                      </select>
                    </label>
                  )}
                  {status.state === 'done' && (
                    <small className="download-timing">
                      Downloaded {fileSize(status.bytes ?? 0)} in {((status.elapsedMs ?? 0) / 1000).toFixed(2)}s
                    </small>
                  )}
                  {status.state === 'failed' && <small className="download-error">{status.error}</small>}
                  {emailStatus.state === 'done' && <small className="email-delivered">Sent to {emailStatus.recipient}</small>}
                  {emailStatus.state === 'failed' && <small className="download-error">{emailStatus.error}</small>}
                  <div className="report-export-actions">
                    <button type="button" disabled={status.state === 'downloading'} onClick={() => void download(item.type)}>
                      {status.state === 'downloading' ? <RefreshCw className="spin" size={15} /> : <Download size={15} />}
                      {status.state === 'downloading' ? 'Generating' : 'Download'}
                    </button>
                    <button type="button" disabled={emailStatus.state === 'sending'} onClick={() => void email(item.type)}>
                      {emailStatus.state === 'sending' ? <RefreshCw className="spin" size={15} /> : <Mail size={15} />}
                      {emailStatus.state === 'sending' ? 'Sending' : 'Email me'}
                    </button>
                  </div>
                </article>
              );
            })}
            <article className="custom-report-entry">
              <span>XLSX · configurable</span>
              <h3>Custom Report Builder</h3>
              <p>Select persisted complete periods and the exact modules to include. Selected periods must share one subscription scope, currency, and cost basis.</p>
              <small>Uses saved snapshots only · no Azure rescan</small>
              <button type="button" onClick={onOpenCustom}>
                <FileCode2 size={15} /> Open builder
              </button>
            </article>
          </div>
        </div>
      </section>
    </div>
  );
}

const CUSTOM_MODULES = [
  ['summary', 'Summary'],
  ['subscriptions', 'Subscriptions'],
  ['savings', 'Savings roadmap'],
  ['findings', 'Prioritized findings'],
  ['compute', 'Compute'],
  ['storage', 'Storage'],
  ['network', 'Network'],
  ['sql', 'Azure SQL'],
  ['ai', 'AI'],
  ['governance', 'Governance'],
  ['actionPlan', 'Action plan'],
  ['chargeback', 'Chargeback'],
  ['compliance', 'Compliance'],
] as const;

function CustomReportDialog({
  currentSnapshotId,
  subscriptionIds,
  onClose,
}: {
  currentSnapshotId: string;
  subscriptionIds: string[];
  onClose: () => void;
}) {
  const dialogRef = useDialogFocus(onClose);
  const [snapshots, setSnapshots] = useState<ReportSnapshotSummary[]>([]);
  const [selectedSnapshots, setSelectedSnapshots] = useState<Set<string>>(new Set([currentSnapshotId]));
  const [selectedModules, setSelectedModules] = useState<Set<string>>(new Set(['summary', 'subscriptions', 'savings', 'findings', 'actionPlan']));
  const [loading, setLoading] = useState(true);
  const [downloadState, setDownloadState] = useState<ReportExportState>({ state: 'idle' });

  useEffect(() => {
    const controller = new AbortController();
    listReportSnapshots(subscriptionIds, controller.signal)
      .then((items) => {
        setSnapshots(items);
        const available = new Set(items.map((item) => item.snapshotId));
        setSelectedSnapshots(available.has(currentSnapshotId)
          ? new Set([currentSnapshotId])
          : items[0] ? new Set([items[0].snapshotId]) : new Set());
      })
      .catch((error) => setDownloadState({
        state: 'failed',
        error: error instanceof Error ? error.message : 'Snapshot catalog is unavailable.',
      }))
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [currentSnapshotId, subscriptionIds]);

  function toggleSnapshot(snapshotId: string) {
    setSelectedSnapshots((current) => {
      const next = new Set(current);
      if (next.has(snapshotId)) next.delete(snapshotId);
      else if (next.size < 12) next.add(snapshotId);
      return next;
    });
  }

  function toggleModule(module: string) {
    setSelectedModules((current) => {
      const next = new Set(current);
      if (next.has(module)) next.delete(module);
      else next.add(module);
      return next;
    });
  }

  async function download() {
    setDownloadState({ state: 'downloading' });
    try {
      const orderedSnapshots = snapshots
        .filter((item) => selectedSnapshots.has(item.snapshotId))
        .sort((left, right) => left.periodStart.localeCompare(right.periodStart))
        .map((item) => item.snapshotId);
      const result = await downloadCustomReport(orderedSnapshots, [...selectedModules]);
      setDownloadState({ state: 'done', ...result });
    } catch (error) {
      setDownloadState({ state: 'failed', error: error instanceof Error ? error.message : 'Custom report failed.' });
    }
  }

  return (
    <div className="remediation-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={dialogRef} tabIndex={-1} className="remediation-dialog custom-report-dialog" role="dialog" aria-modal="true" aria-labelledby="custom-report-title">
        <header className="remediation-dialog-header">
          <div>
            <span>Persisted evidence only</span>
            <h2 id="custom-report-title">Custom Report Builder</h2>
            <p>{subscriptionIds.length} subscription{subscriptionIds.length === 1 ? '' : 's'} · up to 12 complete periods</p>
          </div>
          <button autoFocus type="button" onClick={onClose} title="Close" aria-label="Close custom report builder"><X size={18} /></button>
        </header>
        <div className="custom-report-body">
          <section>
            <h3>Complete periods</h3>
            {loading ? <p className="loading-state">Loading persisted snapshots...</p> : snapshots.length === 0 ? (
              <p className="empty-state">No cataloged completed snapshots are available. Run the report once to publish a catalog entry.</p>
            ) : (
              <div className="custom-period-list">
                {snapshots.map((snapshot) => (
                  <label key={snapshot.snapshotId}>
                    <input type="checkbox" checked={selectedSnapshots.has(snapshot.snapshotId)} onChange={() => toggleSnapshot(snapshot.snapshotId)} />
                    <span><strong>{snapshot.period}</strong><small>{snapshot.periodStart} – {snapshot.periodEnd} · {new Date(snapshot.createdAt).toLocaleString()}</small></span>
                  </label>
                ))}
              </div>
            )}
          </section>
          <section>
            <h3>Modules</h3>
            <div className="custom-module-grid">
              {CUSTOM_MODULES.map(([module, label]) => (
                <label key={module}>
                  <input type="checkbox" checked={selectedModules.has(module)} onChange={() => toggleModule(module)} />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </section>
          {downloadState.state === 'failed' && <p className="download-error">{downloadState.error}</p>}
          {downloadState.state === 'done' && <p className="download-timing">Downloaded {fileSize(downloadState.bytes ?? 0)} in {((downloadState.elapsedMs ?? 0) / 1000).toFixed(2)}s</p>}
          <button className="custom-report-download" type="button" disabled={selectedSnapshots.size === 0 || selectedModules.size === 0 || downloadState.state === 'downloading'} onClick={() => void download()}>
            {downloadState.state === 'downloading' ? <RefreshCw className="spin" size={16} /> : <Download size={16} />}
            {downloadState.state === 'downloading' ? 'Generating workbook' : 'Download custom XLSX'}
          </button>
        </div>
      </section>
    </div>
  );
}

type FocusExportResult = FocusExportSubscription & {
  files: FocusExportFile[];
  error: string | null;
};

const fileSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

function FocusExportDialog({
  subscriptions,
  onClose,
}: {
  subscriptions: FocusExportSubscription[];
  onClose: () => void;
}) {
  const dialogRef = useDialogFocus(onClose);
  const [results, setResults] = useState<FocusExportResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [downloads, setDownloads] = useState<Record<string, { state: 'downloading' | 'done' | 'failed'; elapsedMs?: number; error?: string }>>({});

  async function download(result: FocusExportResult, file: FocusExportFile) {
    setDownloads((current) => ({ ...current, [file.blobName]: { state: 'downloading' } }));
    try {
      const completed = await downloadFocusExport(result.subscriptionId, file);
      setDownloads((current) => ({ ...current, [file.blobName]: { state: 'done', elapsedMs: completed.elapsedMs } }));
    } catch (error) {
      setDownloads((current) => ({
        ...current,
        [file.blobName]: {
          state: 'failed',
          error: error instanceof Error ? error.message : 'Download failed.',
        },
      }));
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    Promise.all(subscriptions.map(async (subscription) => {
      try {
        const response = await listFocusExportFiles(subscription.subscriptionId, controller.signal);
        return { ...subscription, files: response.files, error: null };
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error;
        return {
          ...subscription,
          files: [],
          error: error instanceof Error ? error.message : 'FocusCost files are unavailable.',
        };
      }
    }))
      .then(setResults)
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          setResults(subscriptions.map((subscription) => ({
            ...subscription,
            files: [],
            error: 'FocusCost files are unavailable.',
          })));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [subscriptions]);

  return (
    <div className="remediation-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={dialogRef} tabIndex={-1} className="remediation-dialog focus-export-dialog" role="dialog" aria-modal="true" aria-labelledby="focus-export-title">
        <header className="remediation-dialog-header">
          <div>
            <span>Private cost evidence</span>
            <h2 id="focus-export-title">FocusCost files</h2>
            <p>Files are streamed through the authenticated {BRAND_NAME} API.</p>
          </div>
          <button autoFocus type="button" onClick={onClose} title="Close" aria-label="Close FocusCost files"><X size={18} /></button>
        </header>
        <div className="focus-export-body" aria-live="polite">
          {loading ? (
            <p className="focus-export-loading">Checking private export storage...</p>
          ) : results.map((result) => (
            <section className="focus-export-subscription" key={result.subscriptionId}>
              <header>
                <strong>{result.subscriptionName}</strong>
                <small>{result.subscriptionId}</small>
              </header>
              {result.error ? (
                <p className="focus-export-empty">{result.error}</p>
              ) : result.files.length === 0 ? (
                <p className="focus-export-empty">No FocusCost file is available yet.</p>
              ) : (
                <ul className="focus-export-files">
                  {result.files.map((file) => (
                    <li key={file.blobName}>
                      <span>
                        <strong>{file.fileName}</strong>
                        <small>{fileSize(file.size)} · {file.lastModified ? new Date(file.lastModified).toLocaleString() : 'Timestamp unavailable'}</small>
                        {downloads[file.blobName]?.state === 'done' && <small className="download-timing">Downloaded in {((downloads[file.blobName].elapsedMs ?? 0) / 1000).toFixed(2)}s</small>}
                        {downloads[file.blobName]?.state === 'failed' && <small className="download-error">{downloads[file.blobName].error}</small>}
                      </span>
                      <button type="button" onClick={() => void download(result, file)} disabled={downloads[file.blobName]?.state === 'downloading'}>
                        {downloads[file.blobName]?.state === 'downloading' ? <RefreshCw className="spin" size={15} aria-hidden="true" /> : <Download size={15} aria-hidden="true" />}
                        {downloads[file.blobName]?.state === 'downloading' ? 'Downloading' : 'Download'}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}

function RemediationDialog({
  finding,
  plan,
  onClose,
}: {
  finding: string;
  plan: RemediationPlan;
  onClose: () => void;
}) {
  const dialogRef = useDialogFocus(onClose);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  async function copyScript() {
    try {
      await navigator.clipboard.writeText(plan.script);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  }

  return (
    <div className="remediation-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={dialogRef} tabIndex={-1} className="remediation-dialog" role="dialog" aria-modal="true" aria-labelledby="remediation-title">
        <header className="remediation-dialog-header">
          <div>
            <span>{plan.mode === 'preview' ? 'PowerShell preview' : 'PowerShell inspection'}</span>
            <h2 id="remediation-title">{plan.title}</h2>
            <p>{finding} · {plan.resourceCount} resource{plan.resourceCount === 1 ? '' : 's'}</p>
          </div>
          <button autoFocus type="button" onClick={onClose} title="Close" aria-label="Close remediation plan"><X size={18} /></button>
        </header>

        <div className="remediation-safety">
          <ShieldCheck size={18} aria-hidden="true" />
          <span>
            <strong>Nothing runs from {BRAND_NAME}.</strong>
            {plan.mode === 'preview'
              ? ' The copied script uses WhatIf unless you explicitly run it with -Apply in your own PowerShell session.'
              : ' This script only reads configuration and lists dependencies.'}
          </span>
        </div>

        <div className="remediation-metadata">
          <span><b>Risk</b>{plan.risk}</span>
          <span><b>Effort</b>{plan.effort}</span>
          <span><b>Mode</b>{plan.mode === 'preview' ? 'Preview first' : 'Inspect only'}</span>
        </div>

        <div className="remediation-prerequisites">
          <h3>Before you begin</h3>
          <ul>{plan.prerequisites.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>

        <div className="script-heading">
          <span>PowerShell</span>
          <button type="button" onClick={() => void copyScript()}>
            {copyState === 'copied' ? <Check size={15} /> : <Copy size={15} />}
            {copyState === 'copied' ? 'Copied' : copyState === 'failed' ? 'Copy failed' : 'Copy script'}
          </button>
        </div>
        <pre className="remediation-script" tabIndex={0}><code>{plan.script}</code></pre>
      </section>
    </div>
  );
}

type TimeRangeDays = 7 | 30 | 60 | 90;
const TIME_RANGE_OPTIONS: TimeRangeDays[] = [7, 30, 60, 90];

// Global time-range control (ADO Task 781, comment 8538467, item 1). Drives the
// Daily Cost Trend chart and the range KPI strip below it; extending this to every
// other tab (many of which are point-in-time inventory findings, not time-series)
// is a materially larger follow-up, not done here.
function TimeRangeSelector({ days, onChange }: { days: TimeRangeDays; onChange: (value: TimeRangeDays) => void }) {
  return (
    <div className="time-range-selector" role="group" aria-label="Time range">
      {TIME_RANGE_OPTIONS.map((option) => (
        <button
          key={option}
          type="button"
          className={option === days ? 'active' : ''}
          onClick={() => onChange(option)}
        >
          {option}d
        </button>
      ))}
    </div>
  );
}

function RangeSpendSummary({ report, formatMoney, rangeDays }: { report: FullReport; formatMoney: MoneyFormatter; rangeDays: TimeRangeDays }) {
  const window = billingWindow(report, { rangeDays });
  const avgDaily = window.totalCost === null ? null : window.totalCost / window.coveredDays;
  return (
    <div className="kpi-grid range-spend-kpi-grid">
      <div className="kpi-card">
        <div className="kpi-label">Spend, last {rangeDays} days</div>
        <div className="kpi-value">{window.totalCost === null ? 'Unavailable' : formatMoney(window.totalCost)}</div>
        <div className="kpi-note">{window.coveredDays}/{window.days.length} covered export-calendar days</div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">Average daily spend</div>
        <div className="kpi-value">{avgDaily === null ? 'Unavailable' : formatMoney(avgDaily)}</div>
        <div className="kpi-note">Average hourly: {window.averageHourlyCost === null ? 'Unavailable' : formatMoney(window.averageHourlyCost)}</div>
      </div>
    </div>
  );
}

/* A categorised, collapsible block. The executive view previously ran as one
   continuous column of tables and charts separated only by full-bleed dividers,
   so nothing signalled where one subject ended and the next began. Grouping the
   same content under named sections gives the page a spine, and making each one
   collapsible lets a reader close what they are not asking about.

   Built on details/summary so expansion, keyboard operation and find-in-page
   come from the platform rather than from bespoke state. */
function ReportSection({
  id,
  title,
  caption,
  meta,
  defaultOpen = true,
  children,
}: {
  id: string;
  title: string;
  caption?: string;
  meta?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details className="report-section" open={defaultOpen} data-section={id}>
      <summary className="report-section-summary">
        <ChevronRight className="report-section-chevron" size={16} aria-hidden="true" />
        <span className="report-section-heading">
          <strong>{title}</strong>
          {caption && <small>{caption}</small>}
        </span>
        {meta !== undefined && meta !== null && <span className="report-section-meta">{meta}</span>}
      </summary>
      <div className="report-section-body">{children}</div>
    </details>
  );
}

/* A top-level, always-visible section of the dashboard. Distinct from
   ReportSection, which is the collapsible block used inside a section: this
   one names a phase of reading the report and never hides its contents.
   The header carries the title, an optional one-line caption, and an
   optional figure or control on the trailing edge. */
function DashboardSection({
  id,
  title,
  caption,
  aside,
  children,
}: {
  id: string;
  title: string;
  caption?: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  const headingId = `dashboard-section-${id}`;
  return (
    <section className="dashboard-section" aria-labelledby={headingId} data-dashboard-section={id}>
      <header className="dashboard-section-header">
        <div className="dashboard-section-heading">
          <h2 id={headingId}>{title}</h2>
          {caption && <p>{caption}</p>}
        </div>
        {aside && <div className="dashboard-section-aside">{aside}</div>}
      </header>
      <div className="dashboard-section-body">{children}</div>
    </section>
  );
}

function ExecutiveSummaryTab({
  report,
  narrativeSummary,
  onOpenFinding,
  formatMoney,
  formatHourlyMoney,
  displayCurrency,
  anomalyState,
  onOpenAnomalies,
  snapshotId,
  costWindow,
  onCostWindowChange,
  costFilters,
  onCostFiltersChange,
  budgetState,
}: {
  report: FullReport;
  narrativeSummary?: string;
  onOpenFinding: (category: string) => void;
  formatMoney: MoneyFormatter;
  formatHourlyMoney: MoneyFormatter;
  displayCurrency: string;
  anomalyState: AnomalyState;
  onOpenAnomalies: () => void;
  snapshotId: string | null;
  costWindow: CostWindow;
  onCostWindowChange: (value: CostWindow) => void;
  costFilters: CostFilter;
  onCostFiltersChange: (value: CostFilter) => void;
  budgetState?: BudgetState;
}) {
  const s = report.executiveSummary;
  const metadata = report.reportMetadata;
  const completeness = report.completeness;
  const [rangeDays, setRangeDays] = useState<TimeRangeDays>(30);
  const [costDetailsOpened, setCostDetailsOpened] = useState(false);
  /* The daily figures sit at the end of the report rather than under the
     chart, so the day selection they drive is owned here and handed to the
     cost window instead of living inside it. */
  const [selectedDay, setSelectedDay] = useState<SelectedDay | null>(null);
  return (
    <div className="panel executive-report">
      <DashboardSection
        id="cost-overview"
        title="Cost Overview"
        caption={`${reportDate(metadata.periodStart)} – ${reportDate(metadata.periodEnd)} · ${metadata.costBasis} · ${metadata.currency}`}
        aside={
          <div className={`completeness-stamp ${completeness.complete ? 'complete' : ''}`}>
            <span>{completeness.status}</span>
            <strong>{completeness.availableSubscriptions}/{completeness.requestedSubscriptions}</strong>
            <small>subscriptions</small>
          </div>
        }
      >
        <div className="kpi-grid executive-hero-grid">
          <div className="kpi-card">
            <div className="kpi-label">Total monthly spend</div>
            <div className="kpi-value">{formatMoney(s.currentMonthlySpend)}</div>
            <div className="kpi-note">
              {s.spendChangePercentage === null
                ? `${completeness.availableSubscriptions} complete exports · comparison accrues next month`
                : `${s.spendChangePercentage >= 0 ? '▲' : '▼'} ${Math.abs(s.spendChangePercentage * 100).toFixed(1)}% vs previous complete month`}
            </div>
          </div>
          <div className="kpi-card risk">
            <div className="kpi-label">Estimated wastage</div>
            <div className="kpi-value">{formatMoney(s.estimatedWastageMonth)}</div>
            <div className="kpi-note">{percent(s.pctWastage)} of total · includes billed cost at risk</div>
          </div>
          <div className="kpi-card positive">
            <div className="kpi-label">Potential savings</div>
            <div className="kpi-value">{formatMoney(s.potentialSavingsMonth)}</div>
            <div className="kpi-note">{percent(s.pctRecoverable)} of total bill · estimated / month</div>
          </div>
          <div className="kpi-card">
            <div className="kpi-label">{s.idleReviewCandidates == null ? 'Active resources' : 'Other billed resources'}</div>
            <div className="kpi-value">{s.activeResources.toLocaleString()}</div>
            <div className="kpi-note">Cost-bearing resources across {completeness.availableSubscriptions} subscriptions</div>
          </div>
          <div className="kpi-card risk">
            <div className="kpi-label">{s.idleReviewCandidates == null ? 'Idle resources (legacy)' : 'Confirmed idle resources'}</div>
            <div className="kpi-value">{s.idleResources.toLocaleString()}</div>
            {s.idleReviewCandidates != null && <div className="kpi-note">{s.idleReviewCandidates.toLocaleString()} candidates require evidence or owner review</div>}
            <div className="kpi-note">{percent(s.idleResourcePercentage)} of assessed active + idle resources</div>
          </div>
          <div className="kpi-card">
            <div className="kpi-label">Advisor score</div>
            <div className="kpi-value">{report.advisorScore.score === null ? '—' : `${report.advisorScore.score.toFixed(0)} / 100`}</div>
            <div className="kpi-note">
              {report.advisorScore.monthlyChange === null
                ? report.advisorScore.status
                : `${report.advisorScore.monthlyChange >= 0 ? '▲' : '▼'} ${Math.abs(report.advisorScore.monthlyChange).toFixed(1)} pts this month`}
            </div>
          </div>
        </div>

        <details className="overview-details report-evidence-details">
          <summary>Report details <span>Source, integrity and narrative</span></summary>
          <div className="executive-provenance">
            <span><b>Period</b>{metadata.period}</span>
            <span><b>Cost basis</b>{metadata.costBasis}</span>
            <span><b>Source</b>{metadata.source}</span>
            <span><b>Integrity</b>Completed runs + blob size verified</span>
          </div>
          {narrativeSummary && <div className="executive-narrative">{narrativeSummary}</div>}
        </details>
      </DashboardSection>

      <DashboardSection
        id="cost-comparison"
        title="Cost Comparison"
        caption="Selected period against the preceding one"
        aside={<CostExportButton report={report} snapshotId={snapshotId} window={costWindow} filters={costFilters} />}
      >
        <CostWindowOverview
          report={report}
          snapshotId={snapshotId}
          window={costWindow}
          onChange={onCostWindowChange}
          formatMoney={formatHourlyMoney}
          onOpenAnomalies={onOpenAnomalies}
          budgetState={budgetState}
          filters={costFilters}
          onFiltersChange={onCostFiltersChange}
          showBudget={false}
          showDailyValues={false}
          showHeading={false}
          selectedDay={selectedDay}
          onSelectDay={setSelectedDay}
        />
      </DashboardSection>

      <DashboardSection
        id="insights-findings"
        title="Insights & Findings"
        caption="Anomalies, distribution and prioritised actions"
      >
        <AnomalyOverview state={anomalyState} onOpenDetails={onOpenAnomalies} formatMoney={formatMoney} />

        <details className="overview-details cost-analysis-details" onToggle={(event) => {
          if (event.currentTarget.open) setCostDetailsOpened(true);
        }}>
      <summary>Explore costs and findings <span>Charts, hourly costs, subscriptions and evidence</span></summary>
      {costDetailsOpened && <>
      <div className="analysis-scope-bar">
        <span className="analysis-scope-label">Analysis range</span>
        <TimeRangeSelector days={rangeDays} onChange={setRangeDays} />
      </div>

      <div className="report-section-stack">
        <ReportSection
          id="spend-over-time"
          title="Spend over time"
          caption="Range totals and hourly cost"
          meta={`Last ${rangeDays} days`}
        >
          <RangeSpendSummary report={report} formatMoney={formatHourlyMoney} rangeDays={rangeDays} />
          <HourlyCostPanel report={report} formatMoney={formatHourlyMoney} formatHourlyMoney={formatHourlyMoney} rangeDays={rangeDays} embedded />
        </ReportSection>

        <ReportSection
          id="spend-distribution"
          title="Spend distribution"
          caption="Where the money goes, by type, tag and region"
          meta={formatMoney(s.currentMonthlySpend)}
        >
          <ExecutiveSpendVisuals report={report} formatMoney={formatMoney} formatHourlyMoney={formatHourlyMoney} rangeDays={rangeDays} />
        </ReportSection>

        {report.operationalSignals.length > 0 && (
          <ReportSection
            id="operational-signals"
            title="Operational signals"
            caption="Platform observations across the assessed estate"
            defaultOpen={false}
            meta={`${report.operationalSignals.length} ${report.operationalSignals.length === 1 ? 'signal' : 'signals'}`}
          >
            <div className="executive-signal-grid">
              {report.operationalSignals.map((signal) => (
                <div className={`executive-signal ${signal.tone}`} key={signal.key}>
                  <span>{signal.label}</span>
                  <strong>{signal.value}</strong>
                  <small>{signal.detail}</small>
                </div>
              ))}
            </div>
          </ReportSection>
        )}

        {report.topServices.length > 0 && (
          <ReportSection
            id="top-services"
            title="Top Azure services"
            caption="Ranked by closed-period spend"
            defaultOpen={false}
            meta={`${report.topServices.length} ${report.topServices.length === 1 ? 'service' : 'services'}`}
          >
            <div className="top-services" aria-label="Top Azure services by monthly spend">
              {report.topServices.map((service) => (
                <div className="top-service-row" key={service.serviceName}>
                  <span className="top-service-rank">{String(service.rank).padStart(2, '0')}</span>
                  <span className="top-service-name">
                    <strong>{service.displayName}</strong>
                    <small>{service.serviceName}</small>
                  </span>
                  <span className="top-service-bar" aria-hidden="true">
                    <i style={{ width: `${Math.min(service.pctOfTotal * 100, 100)}%` }} />
                  </span>
                  <strong className="top-service-spend">{formatMoney(service.monthlySpend)}</strong>
                  <span className="top-service-share">{percent(service.pctOfTotal)}</span>
                </div>
              ))}
            </div>
          </ReportSection>
        )}

        <ReportSection
          id="subscriptions"
          title="Subscriptions"
          caption="Spend against verified saving"
          defaultOpen={false}
          meta={`${report.subscriptionBreakdown.length} ${report.subscriptionBreakdown.length === 1 ? 'subscription' : 'subscriptions'}`}
        >
          <div className="executive-table-scroll">
            <table className="report-table executive-subscription-table">
              <thead>
                <tr>
                  <th>Subscription</th>
                  <th className="num">Effective spend ({displayCurrency})</th>
                  <th className="num">Verified saving ({displayCurrency})</th>
                  <th className="num">% recoverable</th>
                </tr>
              </thead>
              <tbody>
                {report.subscriptionBreakdown.map((row) => (
                  <tr key={row.subscriptionId}>
                    <td>{row.subscriptionName}</td>
                    <td className="num">{formatMoney(row.currentSpend)}</td>
                    <td className="num positive-text">{formatMoney(row.totalWaste)}</td>
                    <td className="num">{percent(row.pctSaved)}</td>
                  </tr>
                ))}
                <tr className="executive-total-row">
                  <td>Total</td>
                  <td className="num">{formatMoney(s.currentMonthlySpend)}</td>
                  <td className="num">{formatMoney(s.potentialSavingsMonth)}</td>
                  <td className="num">{percent(s.pctRecoverable)}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </ReportSection>

        <ReportSection
          id="prioritised-findings"
          title="Prioritised findings"
          caption="Ranked by monthly impact"
          defaultOpen={false}
          meta={report.prioritizedFindings.length === 0 ? 'None' : `${report.prioritizedFindings.length} ${report.prioritizedFindings.length === 1 ? 'finding' : 'findings'}`}
        >
          <div className="executive-table-scroll">
            <table className="report-table executive-findings-table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Finding</th>
                  <th>Evidence and impact</th>
                  <th className="num">Monthly impact</th>
                  <th>Severity</th>
                </tr>
              </thead>
              <tbody>
                {report.prioritizedFindings.length === 0 && (
                  <tr><td colSpan={5}>No prioritised findings in this snapshot. Review coverage and domain evidence before concluding that no action is needed.</td></tr>
                )}
                {report.prioritizedFindings.map((finding) => (
                  <tr key={finding.category}>
                    <td className="num finding-rank">{finding.rank}</td>
                    <td>
                      <button className="finding-link" type="button" onClick={() => onOpenFinding(finding.category)}>
                        <strong>{finding.finding}</strong><ChevronRight size={15} aria-hidden="true" />
                      </button>
                    </td>
                    <td className="finding-evidence">{finding.evidence}</td>
                    <td className="num">
                      {finding.impactType === 'cost_at_risk'
                        ? finding.monthlyCostAtRisk === null ? '—' : formatMoney(finding.monthlyCostAtRisk)
                        : finding.monthlySaving === null ? '—' : formatMoney(finding.monthlySaving)}
                      <small className={`impact-label ${finding.impactType}`}>
                        {finding.impactType === 'cost_at_risk' ? 'Cost at risk' : 'Potential saving'}
                      </small>
                    </td>
                    <td><span className={`severity severity-${finding.severity.toLowerCase()}`}>{finding.severity}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ReportSection>
      </div>
      </>}
      </details>
      </DashboardSection>

      {budgetState && (
        <DashboardSection
          id="budget-context"
          title="Budget Context"
          caption="Native Azure budgets against the assessed scope"
          aside={
            <button
              type="button"
              className="ghost-button"
              disabled={budgetState.loading}
              onClick={budgetState.refresh}
              aria-label="Refresh budget context"
              title="Refresh budget context"
            >
              <RefreshCw size={15} aria-hidden="true" />
            </button>
          }
        >
          <BudgetContext state={budgetState} details={report.costDetails} filters={costFilters} showHeading={false} window={costWindow} formatMoney={formatMoney} />
        </DashboardSection>
      )}

      <DashboardSection
        id="daily-breakdown"
        title="Daily Cost Breakdown"
        caption="Per-day figures behind the comparison"
      >
        <DailySubscriptionValues
          details={report.costDetails}
          window={costWindow}
          filters={costFilters}
          formatMoney={formatHourlyMoney}
          onSelectDay={(date, previousDate, subscriptionId) => setSelectedDay({ date, previousDate, subscriptionId })}
        />
      </DashboardSection>
    </div>
  );
}

const SPEND_CATEGORY_ORDER = ['Compute', 'Storage', 'Networking', 'Databases', 'AI/ML', 'Other'];
const spendCategoryClass = (category: string) => `spend-${category.toLowerCase().replaceAll('/', '-').replaceAll(' ', '-')}`;

function SpendCategoryDonut({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const categories = report.spendCategories.filter((item) => item.monthlySpend > 0);
  const total = categories.reduce((sum, item) => sum + item.monthlySpend, 0);
  const radius = 48;
  const circumference = 2 * Math.PI * radius;
  let cumulative = 0;
  const segments = categories.map((item) => {
    const share = total > 0 ? item.monthlySpend / total : 0;
    const segment = { ...item, share, offset: cumulative };
    cumulative += share;
    return segment;
  });
  return (
    <section className="executive-visual executive-donut-panel">
      <header><span>Spend by resource type</span><strong>{formatMoney(total)}</strong></header>
      {total > 0 ? (
        <div className="donut-layout">
          <div className="donut-chart">
            <svg viewBox="0 0 120 120" role="img" aria-label="Spend by resource type donut chart">
              <circle className="donut-track" cx="60" cy="60" r={radius} />
              {segments.map((segment) => (
                <circle
                  className={`donut-segment ${spendCategoryClass(segment.category)}`}
                  cx="60"
                  cy="60"
                  r={radius}
                  key={segment.category}
                  strokeDasharray={`${segment.share * circumference} ${circumference}`}
                  strokeDashoffset={-segment.offset * circumference}
                  transform="rotate(-90 60 60)"
                >
                  <title>{segment.category}: {formatMoney(segment.monthlySpend)} ({percent(segment.pctOfTotal)})</title>
                </circle>
              ))}
            </svg>
            <span><strong>{categories.length}</strong><small>cost groups</small></span>
          </div>
          <div className="spend-legend">
            {SPEND_CATEGORY_ORDER.map((category) => {
              const item = report.spendCategories.find((value) => value.category === category);
              return (
                <span key={category}>
                  <i className={spendCategoryClass(category)} />
                  <b>{category}</b>
                  <strong>{formatMoney(item?.monthlySpend ?? 0)}</strong>
                  <small>{percent(item?.pctOfTotal ?? 0)}</small>
                </span>
              );
            })}
          </div>
        </div>
      ) : <p className="visual-empty">No positive resource-type spend is available.</p>}
    </section>
  );
}

function MonthlySpendChart({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const months = report.spendHistory.months;
  const maximum = Math.max(...months.map((item) => item.total), 0);
  return (
    <section className="executive-visual executive-history-panel">
      <header>
        <span>Month-over-month spend</span>
        <small>{report.spendHistory.statusMessage}</small>
      </header>
      {months.length > 0 ? (
        <div className="monthly-chart-scroll">
          <div className="monthly-chart" style={{ gridTemplateColumns: `repeat(${months.length}, minmax(48px, 1fr))` }}>
            {months.map((month) => (
              <div className="monthly-column" key={month.month}>
                <strong>{formatMoney(month.total)}</strong>
                <div className="monthly-bar-frame">
                  <div className="monthly-stack" style={{ height: `${maximum > 0 ? (month.total / maximum) * 100 : 0}%` }}>
                    {SPEND_CATEGORY_ORDER.map((category) => {
                      const spend = month.categorySpend[category] ?? 0;
                      return spend > 0 ? (
                        <i
                          className={spendCategoryClass(category)}
                          key={category}
                          style={{ height: `${month.total > 0 ? (spend / month.total) * 100 : 0}%` }}
                          title={`${category}: ${formatMoney(spend)}`}
                        />
                      ) : null;
                    })}
                  </div>
                </div>
                <span>{new Date(`${month.month}-01T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' })}</span>
              </div>
            ))}
          </div>
        </div>
      ) : <p className="visual-empty">Monthly export history is not available.</p>}
    </section>
  );
}
function ApplicationHourlyCostDonut({ report, formatMoney, rangeDays }: { report: FullReport; formatMoney: MoneyFormatter; rangeDays: TimeRangeDays }) {
  const { available, signed, total, days, items } = tagDistribution(report.tagDailyCostTrend, rangeDays);
  const radius = 48;
  const circumference = 2 * Math.PI * radius;
  let cumulative = 0;
  const segments = items.map((item, index) => {
    const share = total > 0 ? item.avgHourly / total : 0;
    const segment = { ...item, share, offset: cumulative, color: TREND_SERIES_PALETTE[index % TREND_SERIES_PALETTE.length] };
    cumulative += share;
    return segment;
  });
  return (
    <section className="executive-visual executive-donut-panel">
      <header><span>Average hourly cost by tag set · {days} export days</span><strong>{available ? `${formatMoney(total)}/hr` : 'Unavailable'}</strong></header>
      {available && !signed && total > 0 ? (
        <div className="donut-layout">
          <div className="donut-chart">
            <svg viewBox="0 0 120 120" role="img" aria-label="Average hourly cost by non-overlapping tag set">
              <circle className="donut-track" cx="60" cy="60" r={radius} />
              {segments.map((segment) => (
                <circle
                  className="donut-segment"
                  cx="60"
                  cy="60"
                  r={radius}
                  key={segment.tagValue}
                  style={{ stroke: segment.color }}
                  strokeDasharray={`${segment.share * circumference} ${circumference}`}
                  strokeDashoffset={-segment.offset * circumference}
                  transform="rotate(-90 60 60)"
                >
                  <title>{`${segment.tagValue} — ${formatMoney(segment.avgHourly)}/hr (${percent(segment.share)})`}</title>
                </circle>
              ))}
            </svg>
            <span><strong>{segments.length}</strong><small>tag sets</small></span>
          </div>
          <div className="spend-legend">
            {segments.map((segment) => (
              <span key={segment.tagValue}>
                <i style={{ background: segment.color }} />
                <b>{segment.tagValue}</b>
                <strong>{formatMoney(segment.avgHourly)}/hr</strong>
                <small>{percent(segment.share)}</small>
              </span>
            ))}
          </div>
        </div>
      ) : signed ? <div className="spend-legend">{items.map(item => <span key={item.tagValue}><b>{item.tagValue}</b><strong>{formatMoney(item.avgHourly)}/hr</strong></span>)}</div>
        : <p className="visual-empty">{available ? 'No net cost in the selected export window.' : 'Non-overlapping tag distribution is unavailable for this snapshot.'}</p>}
    </section>
  );
}

// Merged daily spend trend: always shows the tenant-wide total, plus any
// user-added per-application (tag-value) series overlaid in distinct colors.
const TREND_SERIES_PALETTE = [
  'var(--color-metric-green)',
  'var(--color-category-networking)',
  'var(--color-category-databases)',
  'var(--color-category-ai)',
  'var(--color-periwinkle-glow)',
];

function DailySpendTrendChart({ report, formatMoney, formatHourlyMoney, rangeDays }: { report: FullReport; formatMoney: MoneyFormatter; formatHourlyMoney: MoneyFormatter; rangeDays: TimeRangeDays }) {
  const [selectedValues, setSelectedValues] = useState<string[]>([]);
  const tagTrend = report.tagDailyCostTrend;
  const availableToAdd = tagTrend.availableTagValues.filter((value) => !selectedValues.includes(value) && tagTrend.series.some(item => item.tagValue === value));

  const currentDays = report.dailyCostTrend.days.slice(-rangeDays);
  const previousDays = report.dailyCostTrend.days.slice(-(rangeDays * 2), -rangeDays);
  const dateToSlot = new Map(currentDays.map((day, index) => [day.date, index]));

  const appSeries = selectedValues.map((value, index) => {
    const match = tagTrend.series.find((item) => item.tagValue === value);
    return {
      key: value,
      label: value,
      color: TREND_SERIES_PALETTE[index % TREND_SERIES_PALETTE.length],
      days: (match?.days ?? []).filter((day) => dateToSlot.has(day.date)),
    };
  });

  const maximum = Math.max(
    ...currentDays.map((day) => day.totalCost),
    ...previousDays.map((day) => day.totalCost),
    ...appSeries.flatMap((item) => item.days.map((day) => day.totalCost)),
    0.01,
  );
  const width = Math.max(rangeDays * 34, 480);
  const height = 240;
  /* One label per day only fits if the label is tiny; at the interface's
     caption size a date needs roughly 40px, while a day column is ~34px.
     Thinning to every Nth day buys each surviving label the room to be
     read, and the blank cells keep the row aligned to the plot above. */
  const dailyLabelStep = Math.max(1, Math.ceil(currentDays.length / 10));
  const padX = 28;
  const padY = 24;
  const baselineY = height - padY;
  const stepX = rangeDays > 1 ? (width - padX * 2) / (rangeDays - 1) : 0;

  function xForSlot(slot: number): number {
    return padX + slot * stepX;
  }
  function yFor(value: number): number {
    return baselineY - (value / maximum) * (height - padY * 2);
  }
  function pathFor(points: { x: number; y: number }[]): string {
    return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
  }

  const currentPoints = currentDays.map((day, index) => ({ x: xForSlot(index), y: yFor(day.totalCost), day }));
  // Right-align the previous window so its most recent day sits under the current
  // window's most recent day, even if there isn't enough history for a full window.
  const previousOffset = rangeDays - previousDays.length;
  const previousPoints = previousDays.map((day, index) => ({ x: xForSlot(previousOffset + index), y: yFor(day.totalCost), day }));
  const areaPath = currentPoints.length > 0
    ? `${pathFor(currentPoints)} L ${currentPoints[currentPoints.length - 1].x.toFixed(1)} ${baselineY} L ${currentPoints[0].x.toFixed(1)} ${baselineY} Z`
    : '';
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
    value: maximum * fraction,
    y: yFor(maximum * fraction),
  }));

  return (
    <section className="executive-visual executive-history-panel daily-trend-panel-wide">
      <header>
        <span>Daily spend &amp; average hourly cost, per tag — last {rangeDays} days vs the {rangeDays} before</span>
        <small>{report.dailyCostTrend.statusMessage} {tagTrend.statusMessage}</small>
      </header>
      <div className="daily-trend-controls">
        <div className="daily-trend-legend">
          <span className="daily-trend-legend-item">
            <i style={{ background: 'var(--color-signal-orange)' }} aria-hidden="true" />
            This period
          </span>
          {previousPoints.length > 0 && (
            <span className="daily-trend-legend-item">
              <i className="dashed" aria-hidden="true" />
              Previous period
            </span>
          )}
          {appSeries.map((item) => (
            <span className="daily-trend-legend-item" key={item.key}>
              <i style={{ background: item.color }} aria-hidden="true" />
              {item.label}
              <button
                type="button"
                onClick={() => setSelectedValues((current) => current.filter((value) => value !== item.key))}
                aria-label={`Remove ${item.label}`}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
        {availableToAdd.length > 0 && (
          <select
            className="resource-hourly-picker"
            value=""
            onChange={(event) => {
              if (event.target.value) setSelectedValues((current) => [...current, event.target.value]);
            }}
            aria-label="Add tag"
          >
            <option value="">+ Add tag</option>
            {availableToAdd.map((value) => (
              <option value={value} key={value}>{value}</option>
            ))}
          </select>
        )}
      </div>
      {currentPoints.length > 0 ? (
        <div className="daily-line-chart-scroll">
          <div className="daily-line-chart-axis-labels" style={{ height }}>
            {yTicks.slice().reverse().map((tick) => (
              <span key={tick.value}>{formatMoney(tick.value)}</span>
            ))}
          </div>
          <div className="daily-line-chart-main">
            <svg className="daily-line-chart" viewBox={`0 0 ${width} ${height}`} width={width} height={height} style={{ width: '100%', minWidth: width }} preserveAspectRatio="none" role="img" aria-label="Daily cost trend">
              {yTicks.map((tick) => (
                <line key={tick.value} className="daily-line-chart-grid" x1={padX} x2={width - padX} y1={tick.y} y2={tick.y} />
              ))}
              <path d={areaPath} className="daily-line-chart-area" />
              {previousPoints.length > 0 && (
                <path d={pathFor(previousPoints)} className="daily-line-chart-path daily-line-chart-path-previous" />
              )}
              <path d={pathFor(currentPoints)} className="daily-line-chart-path" style={{ stroke: 'var(--color-signal-orange)' }} />
              {currentPoints.map((point) => (
                <circle className="daily-line-chart-point" key={point.day.date} cx={point.x} cy={point.y} r={3} style={{ fill: 'var(--color-signal-orange)' }}>
                  <title>{`This period · ${reportDate(point.day.date)}: ${formatMoney(point.day.totalCost)} total (${formatHourlyMoney(point.day.averageHourlyCost)}/hr average)`}</title>
                </circle>
              ))}
              {appSeries.map((item) => {
                const points = item.days.map((day) => ({ x: xForSlot(dateToSlot.get(day.date) ?? 0), y: yFor(day.totalCost), day }));
                if (points.length === 0) return null;
                return (
                  <g key={item.key}>
                    <path d={pathFor(points)} className="daily-line-chart-path" style={{ stroke: item.color }} />
                    {points.map((point) => (
                      <circle className="daily-line-chart-point" key={`${item.key}-${point.day.date}`} cx={point.x} cy={point.y} r={3} style={{ fill: item.color }}>
                        <title>{`${item.label} · ${reportDate(point.day.date)}: ${formatMoney(point.day.totalCost)} total (${formatHourlyMoney(point.day.averageHourlyCost)}/hr average)`}</title>
                      </circle>
                    ))}
                  </g>
                );
              })}
            </svg>
            <div className="daily-line-chart-labels" style={{ gridTemplateColumns: `repeat(${currentDays.length}, minmax(34px, 1fr))`, minWidth: width }}>
              {currentDays.map((day, index) => (
                <span key={day.date}>{index % dailyLabelStep === 0 ? new Date(`${day.date}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }) : ''}</span>
              ))}
            </div>
          </div>
        </div>
      ) : <p className="visual-empty">Daily FOCUS export history is not available.</p>}
    </section>
  );
}

type TreemapNode = {
  key: string;
  label: string;
  detail: string;
  spend: number;
  resource?: CostHierarchyItem;
};

function CostTreemap({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const [subscriptionId, setSubscriptionId] = useState<string | null>(null);
  const [resourceGroup, setResourceGroup] = useState<string | null>(null);
  const selectedSubscription = report.costHierarchy.find((item) => item.subscriptionId === subscriptionId);
  const visible = report.costHierarchy.filter((item) => (
    (!subscriptionId || item.subscriptionId === subscriptionId)
    && (!resourceGroup || item.resourceGroup === resourceGroup)
  ));
  const nodesByKey = new Map<string, TreemapNode>();
  for (const item of visible) {
    const key = !subscriptionId ? item.subscriptionId : !resourceGroup ? item.resourceGroup : item.resourceId;
    const current = nodesByKey.get(key);
    nodesByKey.set(key, {
      key,
      label: !subscriptionId ? item.subscriptionName : !resourceGroup ? item.resourceGroup : item.resourceName,
      detail: !subscriptionId ? 'Subscription' : !resourceGroup ? 'Resource group' : item.resourceType,
      spend: (current?.spend ?? 0) + item.monthlySpend,
      resource: resourceGroup ? item : undefined,
    });
  }
  const nodes = [...nodesByKey.values()].sort((left, right) => right.spend - left.spend).slice(0, 24);
  const total = nodes.reduce((sum, node) => sum + node.spend, 0);
  function selectNode(node: TreemapNode) {
    if (!subscriptionId) setSubscriptionId(node.key);
    else if (!resourceGroup) setResourceGroup(node.key);
  }
  return (
    <section className="executive-visual executive-treemap-panel">
      <header>
        <span>Spend hierarchy</span>
        <nav className="treemap-breadcrumb" aria-label="Spend hierarchy level">
          <button type="button" onClick={() => { setSubscriptionId(null); setResourceGroup(null); }}>Subscriptions</button>
          {subscriptionId && <><ChevronRight size={12} /><button type="button" onClick={() => setResourceGroup(null)}>{selectedSubscription?.subscriptionName ?? subscriptionId}</button></>}
          {resourceGroup && <><ChevronRight size={12} /><b>{resourceGroup}</b></>}
        </nav>
      </header>
      {nodes.length > 0 ? (
        <div className="cost-treemap">
          {nodes.map((node, index) => {
            const content = <><strong>{node.label}</strong><span>{formatMoney(node.spend)}</span><small>{node.detail}</small></>;
            const style = { flexGrow: Math.max(node.spend, 1), flexBasis: `${Math.max(16, total > 0 ? (node.spend / total) * 100 : 16)}%` };
            return node.resource ? (
              <a
                className={`treemap-cell tone-${index % 5}`}
                href={azurePortalResourceUrl(node.resource.resourceId)}
                target="_blank"
                rel="noreferrer"
                style={style}
                key={node.key}
                title={`${node.label}: ${formatMoney(node.spend)}`}
              >{content}</a>
            ) : (
              <button
                className={`treemap-cell tone-${index % 5}`}
                type="button"
                style={style}
                key={node.key}
                title={`Drill into ${node.label}`}
                onClick={() => selectNode(node)}
              >{content}</button>
            );
          })}
        </div>
      ) : <p className="visual-empty">Resource-level cost attribution is not available.</p>}
    </section>
  );
}

const REGION_POINTS: Record<string, [number, number]> = {
  westus: [145, 265], westus2: [143, 245], westus3: [138, 276], centralus: [220, 258],
  northcentralus: [230, 235], southcentralus: [230, 292], eastus: [287, 270], eastus2: [292, 251],
  canadacentral: [270, 205], brazilsouth: [350, 470], northeurope: [485, 190], westeurope: [485, 220],
  uksouth: [470, 210], francecentral: [493, 235], germanywestcentral: [510, 215], swedencentral: [520, 165],
  norwayeast: [500, 150], southafricanorth: [545, 480], uaenorth: [630, 310], centralindia: [700, 345],
  southindia: [690, 405], westindia: [675, 350], eastasia: [815, 295], southeastasia: [800, 410],
  japaneast: [900, 300], koreacentral: [850, 290], australiaeast: [880, 505],
};
const normalizedRegion = (region: string) => region.toLowerCase().replaceAll(' ', '').replaceAll('-', '');
const displayRegion = (region: string) => ({
  westus: 'West US', westus2: 'West US 2', westus3: 'West US 3', centralus: 'Central US',
  northcentralus: 'North Central US', southcentralus: 'South Central US', eastus: 'East US', eastus2: 'East US 2',
  canadacentral: 'Canada Central', brazilsouth: 'Brazil South', northeurope: 'North Europe', westeurope: 'West Europe',
  uksouth: 'UK South', francecentral: 'France Central', germanywestcentral: 'Germany West Central',
  swedencentral: 'Sweden Central', norwayeast: 'Norway East', southafricanorth: 'South Africa North',
  uaenorth: 'UAE North', centralindia: 'Central India', southindia: 'South India', westindia: 'West India',
  eastasia: 'East Asia', southeastasia: 'Southeast Asia', japaneast: 'Japan East',
  koreacentral: 'Korea Central', australiaeast: 'Australia East', global: 'Global', unassigned: 'Unassigned',
}[normalizedRegion(region)] ?? region);

function RegionSpendMap({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const plotted = report.regionSpend.filter((item) => REGION_POINTS[normalizedRegion(item.region)] && item.monthlySpend > 0);
  const maximum = Math.max(...plotted.map((item) => item.monthlySpend), 0);
  const nonGeographic = report.regionSpend.filter((item) => !REGION_POINTS[normalizedRegion(item.region)] && item.monthlySpend > 0);
  return (
    <section className="executive-visual executive-region-panel">
      <header>
        <span>Spend by Azure region</span>
        <strong>{plotted.length} mapped{nonGeographic.length > 0 ? ` · ${nonGeographic.length} global/unassigned` : ''}</strong>
      </header>
      <div className="region-panel-body">
        <svg className="region-map" viewBox="0 0 1010 666" role="img" aria-label="Azure region spend world map">
            <image className="map-land-image" href={worldMapUrl} width="1010" height="666" />
            {plotted.map((item) => {
              const [x, y] = REGION_POINTS[normalizedRegion(item.region)];
              const intensity = maximum > 0 ? item.monthlySpend / maximum : 0;
              return (
                <circle
                  className="region-heat"
                  cx={x}
                  cy={y}
                  r={7 + Math.sqrt(intensity) * 17}
                  key={item.region}
                  style={{ opacity: 0.42 + intensity * 0.58 }}
                >
                  <title>{displayRegion(item.region)}: {formatMoney(item.monthlySpend)} ({percent(item.pctOfTotal)})</title>
                </circle>
              );
            })}
        </svg>
        <div className="region-ranking">
          {report.regionSpend.slice(0, 6).map((item) => (
            <span key={item.region}><b>{displayRegion(item.region)}</b><i><em style={{ width: `${Math.max(2, item.pctOfTotal * 100)}%` }} /></i><strong>{formatMoney(item.monthlySpend)}</strong></span>
          ))}
        </div>
      </div>
      <a className="map-attribution" href="https://github.com/VictorCazanave/svg-maps" target="_blank" rel="noreferrer">
        Map data: SVG Maps · CC BY 4.0
      </a>
    </section>
  );
}

function ExecutiveSpendVisuals({ report, formatMoney, formatHourlyMoney, rangeDays }: { report: FullReport; formatMoney: MoneyFormatter; formatHourlyMoney: MoneyFormatter; rangeDays: TimeRangeDays }) {
  return (
    <div className="executive-visual-grid">
      <SpendCategoryDonut report={report} formatMoney={formatMoney} />
      <MonthlySpendChart report={report} formatMoney={formatMoney} />
      <DailySpendTrendChart report={report} formatMoney={formatMoney} formatHourlyMoney={formatHourlyMoney} rangeDays={rangeDays} />
      <ApplicationHourlyCostDonut report={report} formatMoney={formatHourlyMoney} rangeDays={rangeDays} />
      <CostTreemap report={report} formatMoney={formatMoney} />
      <RegionSpendMap report={report} formatMoney={formatMoney} />
    </div>
  );
}

const formatDataSize = (bytes: number | null) => {
  if (bytes === null) return 'Not available';
  if (bytes >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(1)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${bytes.toLocaleString()} B`;
};

function StorageTierBar({ volumes }: { volumes: { tier: string; bytes: number }[] }) {
  const total = volumes.reduce((sum, item) => sum + item.bytes, 0);
  return total > 0 ? (
    <div className="storage-tier-bar" aria-label={`Total tiered volume ${formatDataSize(total)}`}>
      {volumes.map((item) => (
        <i
          className={`tier-${item.tier.toLowerCase()}`}
          key={item.tier}
          style={{ width: `${(item.bytes / total) * 100}%` }}
          title={`${item.tier}: ${formatDataSize(item.bytes)}`}
        />
      ))}
    </div>
  ) : <span className="storage-tier-pending">Tier-volume metrics unavailable</span>;
}

function ComputeCostInsights({ report, formatMoney, displayCurrency }: { report: FullReport; formatMoney: MoneyFormatter; displayCurrency: string }) {
  const extendedSupport = report.extendedSupport;
  const offHours = report.offHoursSavings;
  return (
    <div className="panel">
      <h2 className="section-title">Extended Support Cost</h2>
      <p className="section-subtitle">Resources billed for Azure Extended Security Updates (ESU); upgrading the OS or SQL Server version removes this recurring charge.</p>
      {!extendedSupport?.available || extendedSupport.rows.length === 0 ? (
        <p className="empty-state">{extendedSupport?.status ?? 'No Extended Security Updates charges were found.'}</p>
      ) : (
        <table className="report-table">
          <thead>
            <tr>
              <th>Resource</th>
              <th>Subscription</th>
              <th className="num">ESU cost ({displayCurrency}/mo)</th>
            </tr>
          </thead>
          <tbody>
            {extendedSupport.rows.map((row) => (
              <tr key={row.resourceId}>
                <td>
                  <a className="resource-link" href={azurePortalResourceUrl(row.resourceId)} target="_blank" rel="noreferrer">
                    <span><strong>{row.resourceName}</strong><small>{row.resourceGroup}</small></span>
                    <ExternalLink size={13} aria-hidden="true" />
                  </a>
                </td>
                <td>{row.subscriptionName}</td>
                <td className="num">{formatMoney(row.monthlyCost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="section-title">Off-Hours Shutdown Savings</h2>
      <p className="section-subtitle">
        {offHours?.available
          ? `Estimated saving if each VM is stopped/deallocated outside a standard Mon-Fri business-hours schedule (${percent(offHours.offHoursFraction)} of the week) - based on current spend, not measured utilization.`
          : 'Estimated saving if each VM is stopped/deallocated outside a standard business-hours schedule.'}
      </p>
      {!offHours?.available || offHours.rows.length === 0 ? (
        <p className="empty-state">{offHours?.status ?? 'No running virtual machines were found.'}</p>
      ) : (
        <table className="report-table">
          <thead>
            <tr>
              <th>VM</th>
              <th>Subscription</th>
              <th className="num">Current cost ({displayCurrency}/mo)</th>
              <th className="num">Estimated saving ({displayCurrency}/mo)</th>
            </tr>
          </thead>
          <tbody>
            {offHours.rows.map((row) => (
              <tr key={row.resourceId}>
                <td>
                  <a className="resource-link" href={azurePortalResourceUrl(row.resourceId)} target="_blank" rel="noreferrer">
                    <span><strong>{row.resourceName}</strong><small>{row.resourceGroup}</small></span>
                    <ExternalLink size={13} aria-hidden="true" />
                  </a>
                </td>
                <td>{row.subscriptionName}</td>
                <td className="num">{formatMoney(row.monthlyCost)}</td>
                <td className="num">{formatMoney(row.estimatedMonthlySaving)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function StorageTierAnalysis({ report, formatMoney, displayCurrency }: { report: FullReport; formatMoney: MoneyFormatter; displayCurrency: string }) {
  const summary = report.storageOptimization;
  const [showOnboarding, setShowOnboarding] = useState(false);
  return (
    <div className="panel storage-tier-analysis">
      <h2 className="section-title">Storage Account Tier Analysis</h2>
      <p className="section-subtitle">Closed-period blob capacity and read activity, with lifecycle recommendations gated by last-access evidence.</p>
      <div className="storage-analysis-toolbar">
        <div className="storage-analysis-status">{summary.status}</div>
        <button type="button" onClick={() => setShowOnboarding(true)}><FileCode2 size={15} /> Configure Blob Inventory</button>
      </div>
      {(summary.currentTierVolumes.length > 0 || summary.recommendedTierVolumes.length > 0) && <div className="storage-tier-comparison">
        <div><span>Current tier volume</span><StorageTierBar volumes={summary.currentTierVolumes} /></div>
        <div><span>Recommended tier volume</span><StorageTierBar volumes={summary.recommendedTierVolumes} /></div>
        <div className="tier-legend">
          {['Hot', 'Cool', 'Cold', 'Archive'].map((tier) => <span key={tier}><i className={`tier-${tier.toLowerCase()}`} />{tier}</span>)}
        </div>
      </div>}
      {summary.accounts.length > 0 ? (
        <div className="storage-table-scroll">
          <table className="report-table storage-tier-table">
            <thead>
              <tr>
                <th>Storage account</th>
                <th className="num">Size</th>
                <th>Current tier</th>
                <th>Access pattern</th>
                <th>Recommended</th>
                <th className="num">Saving/mo ({displayCurrency})</th>
              </tr>
            </thead>
            <tbody>
              {summary.accounts.map((account) => (
                <tr key={account.storageAccountId}>
                  <td>
                    <a className="resource-link" href={azurePortalResourceUrl(account.storageAccountId)} target="_blank" rel="noreferrer">
                      <span><strong>{account.storageAccountName}</strong><small>{account.subscriptionName} · {account.resourceGroup}</small></span>
                      <ExternalLink size={13} aria-hidden="true" />
                    </a>
                  </td>
                  <td className="num">{formatDataSize(account.sizeBytes)}<small>{account.monthlyCost === null ? '' : `${formatMoney(account.monthlyCost)} billed`}</small></td>
                  <td>{account.currentTier}<StorageTierBar volumes={account.tierVolumes} /></td>
                  <td>{account.accessPattern}<small>{account.evidenceStatus}</small></td>
                  <td>{account.recommended}</td>
                  <td className="num">{account.estimatedSavingMonth === null ? 'Not quantified' : formatMoney(account.estimatedSavingMonth)}<small>Eligible blob inventory + retrieval cost required</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EvidenceState title="No storage accounts" detail="No storage accounts were found in the selected subscriptions." />}
      {showOnboarding && <StorageOnboardingDialog onClose={() => setShowOnboarding(false)} />}
    </div>
  );
}

function StorageOnboardingDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useDialogFocus(onClose);
  const [guidance, setGuidance] = useState<StorageOnboardingGuidance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getStorageOnboardingGuidance(controller.signal)
      .then(setGuidance)
      .catch((loadError) => {
        if (loadError instanceof DOMException && loadError.name === 'AbortError') return;
        setError(loadError instanceof Error ? loadError.message : 'Storage onboarding guidance is unavailable.');
      });
    return () => controller.abort();
  }, []);

  async function copyCommand() {
    if (!guidance) return;
    try {
      await navigator.clipboard.writeText(guidance.lastAccessCommand);
      setCopied(true);
    } catch {
      setError('The last-access command could not be copied.');
    }
  }

  return (
    <div className="remediation-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section ref={dialogRef} tabIndex={-1} className="remediation-dialog storage-onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="storage-onboarding-title">
        <header className="remediation-dialog-header">
          <div><span>Customer-run configuration</span><h2 id="storage-onboarding-title">Blob Inventory onboarding</h2><p>No customer storage account is changed by {BRAND_NAME}.</p></div>
          <button autoFocus type="button" onClick={onClose} aria-label="Close Blob Inventory onboarding"><X size={18} /></button>
        </header>
        <div className="storage-onboarding-body">
          <ol>
            <li><strong>Deploy the template</strong><span>Creates a private inventory container, weekly CSV inventory policy, and container-scoped reader role.</span></li>
            <li><strong>Enable last-access tracking</strong><span>Run the targeted CLI update below so existing retention, CORS, change-feed, and versioning settings are preserved.</span></li>
            <li><strong>Wait for inventory</strong><span>Weekly reports must accrue before blob-level tier recommendations can be calculated.</span></li>
          </ol>
          {guidance ? (
            <>
              <div className="storage-onboarding-warning"><ShieldCheck size={17} /><span>{guidance.billingNotice}</span></div>
              <div className="storage-onboarding-warning"><ShieldCheck size={17} /><span>{guidance.networkNotice}</span></div>
              <pre><code>{guidance.lastAccessCommand}</code></pre>
              <div className="storage-onboarding-actions">
                <a href={storageOnboardingTemplateUrl()} target="_blank" rel="noreferrer">Open Azure deployment <ExternalLink size={14} /></a>
                <button type="button" onClick={() => void copyCommand()}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? 'Copied' : 'Copy command'}</button>
              </div>
            </>
          ) : error ? <p className="download-error">{error}</p> : <p className="loading-state">Loading guidance...</p>}
        </div>
      </section>
    </div>
  );
}

const compactNumber = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const formatTokens = (value: number | null) => value === null ? 'Unavailable' : compactNumber.format(value);

function AIUsageAnalysis({ report, formatMoney, displayCurrency }: { report: FullReport; formatMoney: MoneyFormatter; displayCurrency: string }) {
  const usage = report.aiUsage ?? {
    status: 'Azure OpenAI usage is not present in this completed report snapshot.',
    periodStart: '',
    periodEnd: '',
    deployments: [],
    opportunities: [],
  };
  return (
    <div className="panel ai-usage-analysis">
      <h2 className="section-title">Azure OpenAI Usage Analysis</h2>
      <p className="section-subtitle">
        Deployment-level Azure Monitor token evidence
        {usage.periodStart && usage.periodEnd ? ` · ${reportDate(usage.periodStart)} – ${reportDate(usage.periodEnd)}` : ''}
      </p>
      <div className="ai-usage-status">{usage.status}</div>
      {usage.deployments.length > 0 ? (
        <div className="ai-usage-table-scroll">
          <table className="report-table ai-usage-table">
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Model</th>
                <th className="num">Input/day</th>
                <th className="num">Output/day</th>
                <th className="num">Total/day</th>
                <th className="num">Estimated cost/day ({displayCurrency})</th>
                <th>Trend</th>
              </tr>
            </thead>
            <tbody>
              {usage.deployments.map((deployment) => (
                <tr key={`${deployment.accountId}-${deployment.deploymentName}`}>
                  <td>
                    <strong>{deployment.deploymentName}</strong>
                    <small>{deployment.accountName} · {deployment.location}</small>
                  </td>
                  <td>
                    {deployment.modelName}
                    <small>{deployment.modelVersion || 'Version unavailable'} · {deployment.skuName || 'SKU unavailable'}</small>
                  </td>
                  <td className="num">{formatTokens(deployment.inputTokensPerDay)}</td>
                  <td className="num">{formatTokens(deployment.outputTokensPerDay)}</td>
                  <td className="num">{formatTokens(deployment.totalTokensPerDay)}</td>
                  <td className="num">
                    {deployment.estimatedCostDay === null ? 'Pricing pending' : formatMoney(deployment.estimatedCostDay)}
                    <small>{deployment.estimatedCostDay === null ? 'No fabricated allocation' : 'Estimated from supported price meters'}</small>
                  </td>
                  <td>
                    <span className={`ai-trend ${deployment.trendLabel.replaceAll(' ', '-')}`}>
                      {deployment.trendLabel === 'increasing' ? '▲' : deployment.trendLabel === 'decreasing' ? '▼' : deployment.trendLabel === 'stable' ? '→' : '•'}
                      {deployment.trendPercentage === null ? deployment.trendLabel : ` ${Math.abs(deployment.trendPercentage * 100).toFixed(1)}%`}
                    </span>
                    <small>{deployment.evidenceStatus}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EvidenceState title="Usage evidence unavailable" detail="No deployment-level Azure OpenAI usage evidence is available." />}

      <section className="ai-opportunities">
        <header>
          <span>Optimization opportunities</span>
          <strong>{usage.opportunities.length}</strong>
        </header>
        {usage.opportunities.length > 0 ? (
          <div>
            {usage.opportunities.map((opportunity) => (
              <article key={`${opportunity.deploymentName}-${opportunity.category}`}>
                <span className={`severity severity-${opportunity.priority.toLowerCase()}`}>{opportunity.priority}</span>
                <strong>{opportunity.category}</strong>
                <b>{opportunity.deploymentName}</b>
                <p>{opportunity.recommendation}</p>
                <small>{opportunity.evidence}</small>
              </article>
            ))}
          </div>
        ) : <p>No deterministic optimization opportunity is supported by the available token evidence.</p>}
      </section>
    </div>
  );
}

function SavingsRoadmapTab({
  report,
  onOpenFinding,
  onOpenRemediation,
  formatMoney,
  displayCurrency,
}: {
  report: FullReport;
  onOpenFinding: (category: string) => void;
  onOpenRemediation: (finding: string, plan: RemediationPlan | null) => void;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const categories = new Map(report.tierACategories.map((category) => [category.category, category]));
  const immediateActions = report.savingsRoadmap.filter((item) => item.immediate);
  const immediateMonthly = immediateActions.reduce((sum, item) => sum + item.monthly, 0);
  return (
    <div className="panel">
      <h2 className="section-title">Prioritised Savings Roadmap</h2>
      <p className="section-subtitle">Verified opportunities ordered by actionability, then monthly saving.</p>
      <div className="immediate-actions-summary">
        <span><b>Immediate actions</b>Low-effort previews ready for owner validation</span>
        <strong>{immediateActions.length}</strong>
        <span><b>Verified monthly saving</b>{formatMoney(immediateMonthly)}</span>
      </div>
      {report.savingsRoadmap.length === 0 ? (
        <EvidenceState title="No prioritized findings" detail="No findings for the selected subscriptions." />
      ) : (
        <div className="roadmap-table-scroll"><table className="report-table roadmap-table">
          <thead>
            <tr>
              <th>Optimisation opportunity</th>
              <th>Domain</th>
              <th>Subscriptions</th>
              <th className="num">Monthly impact ({displayCurrency})</th>
              <th className="num">Annual ({displayCurrency})</th>
              <th className="num">Resources</th>
              <th>Actionability</th>
              <th>Recommended action</th>
              <th><span className="sr-only">Tools</span></th>
            </tr>
          </thead>
          <tbody>
            {report.savingsRoadmap.map((item) => (
              <tr key={item.opportunity}>
                <td>
                  <button
                    className="finding-link"
                    type="button"
                    onClick={() => onOpenFinding(item.category)}
                  >
                    <strong>{item.opportunity}</strong><ChevronRight size={15} aria-hidden="true" />
                  </button>
                </td>
                <td>{item.domain}</td>
                <td><SubscriptionReferences subscriptions={item.affectedSubscriptions} /></td>
                <td className="num">
                  {formatMoney(item.monthly)}
                  <small className={`impact-label ${item.impactType}`}>
                    {item.impactType === 'cost_at_risk' ? 'Cost at risk' : 'Potential saving'}
                  </small>
                </td>
                <td className="num">{formatMoney(item.annual)}</td>
                <td className="num">{item.resources}</td>
                <td>
                  <span className={`actionability ${item.immediate ? 'immediate' : 'review'}`}>
                    {item.immediate ? 'Quick win' : 'Review'}
                  </span>
                  <small className="risk-effort">{item.risk} risk · {item.effort} effort</small>
                </td>
                <td>{item.recommendedAction}</td>
                <td className="roadmap-tools">
                  <button
                    type="button"
                    onClick={() => onOpenRemediation(item.opportunity, categories.get(item.category)?.remediation ?? null)}
                  >
                    <FileCode2 size={15} /> {item.immediate ? 'Preview' : 'Inspect'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </div>
  );
}

function CommitmentActivitySection({
  report,
  formatMoney,
  displayCurrency,
  title = 'Realized commitment activity',
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
  title?: string;
}) {
  const summary = report.commitmentSummary;
  const commitments = [
    { label: 'Reservations (RI)', benefit: summary.reservations },
    { label: 'Savings Plans', benefit: summary.savingsPlans },
  ];
  return (
    <section className="rate-section realized-commitments" aria-label="Realized commitment activity">
      <h3>{title}</h3>
      {!summary.observed ? (
        <div className="pricing-unavailable">
          <strong>No realized commitment activity</strong>
          <span>{summary.status}</span>
        </div>
      ) : (
        <div className="rate-table-scroll" tabIndex={0} role="region" aria-label="Commitment evidence">
          <table className="report-table rate-table commitment-table">
            <thead><tr><th scope="col">Commitment</th><th scope="col" className="num">Used effective cost ({displayCurrency})</th><th scope="col" className="num">Realized benefit ({displayCurrency})</th><th scope="col" className="num">Unused commitment cost ({displayCurrency})</th><th scope="col">Evidence</th></tr></thead>
            <tbody>
              {commitments.map(({ label, benefit }) => (
                <tr key={label}>
                  <th scope="row">{label}</th>
                  <td className="num">{formatMoney(benefit.usedEffectiveCost)}</td>
                  <td className="num">{formatMoney(benefit.realizedBenefit)}</td>
                  <td className="num">{formatMoney(benefit.unusedEffectiveCost)}</td>
                  <td>{benefit.rowCount} FOCUS rows<small>{summary.period} · Source currency {summary.currency}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="rate-scope-note">Existing FOCUS benefits, not new purchase recommendations. Realized benefit uses eligible Used rows; unused commitment cost is never counted as savings.</p>
    </section>
  );
}

function PricingTab({
  report,
  formatMoney,
  displayCurrency,
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const pricing = report.pricingSummary;
  return (
    <div className="panel pricing-panel">
      <h2 className="section-title">Enterprise Agreement Pricing</h2>
      <p className="section-subtitle">List, negotiated contracted, and effective costs from reconciled FOCUS pricing evidence.</p>
      {!pricing.available ? (
        <div className="pricing-unavailable">
          <strong>Pricing evidence unavailable</strong>
          <span>{pricing.status}</span>
        </div>
      ) : (
        <>
          <div className="pricing-metadata">
            <span><b>Period</b>{pricing.period}</span>
            <span><b>FOCUS schema</b>{pricing.dataVersion}</span>
            <span><b>Billing currency</b>{pricing.billingCurrency}</span>
            <span><b>Pricing currencies</b>{pricing.pricingCurrencies.join(', ') || 'Not reported'}</span>
            <span><b>Reconciliation variance</b>{pricing.reconciliationVariance === null ? 'Unavailable' : formatMoney(pricing.reconciliationVariance)}</span>
          </div>
          <div className="kpi-grid pricing-kpi-grid">
            <div className="kpi-card"><div className="kpi-label">List cost</div><div className="kpi-value">{formatMoney(pricing.listCost)}</div></div>
            <div className="kpi-card"><div className="kpi-label">EA contracted cost</div><div className="kpi-value">{formatMoney(pricing.contractedCost)}</div></div>
            <div className="kpi-card"><div className="kpi-label">Effective cost</div><div className="kpi-value">{formatMoney(pricing.effectiveCost)}</div></div>
            <div className="kpi-card positive"><div className="kpi-label">Existing negotiated discount</div><div className="kpi-value">{formatMoney(pricing.negotiatedDiscount)}</div><div className="kpi-note">{percent(pricing.negotiatedDiscountPercentage)} of list cost</div></div>
          </div>
          <div className="pricing-note">Negotiated discount is an existing contract benefit, not a new savings opportunity.</div>
          <div className="pricing-table-scroll">
            <table className="report-table pricing-table">
              <thead>
                <tr>
                  <th>Subscription</th>
                  <th className="num">List cost ({displayCurrency})</th>
                  <th className="num">Contracted cost ({displayCurrency})</th>
                  <th className="num">Effective cost ({displayCurrency})</th>
                  <th className="num">Billed cost ({displayCurrency})</th>
                  <th className="num">Negotiated discount</th>
                </tr>
              </thead>
              <tbody>
                {pricing.subscriptions.map((subscription) => (
                  <tr key={subscription.subscriptionId}>
                    <td><span className="subscription-reference"><strong>{subscription.subscriptionName}</strong><small>{subscription.subscriptionId}</small></span></td>
                    <td className="num">{formatMoney(subscription.listCost)}</td>
                    <td className="num">{formatMoney(subscription.contractedCost)}</td>
                    <td className="num">{formatMoney(subscription.effectiveCost)}</td>
                    <td className="num">{formatMoney(subscription.billedCost)}</td>
                    <td className="num">{formatMoney(subscription.negotiatedDiscount)}<small>{percent(subscription.negotiatedDiscountPercentage)}</small></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <CommitmentActivitySection report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} title="Reservations (RI) and Savings Plans" />
    </div>
  );
}

const RESERVATION_RESOURCE_TYPES: ReservationResourceType[] = [
  'VirtualMachines',
  'ManagedDisk',
  'SQLDatabases',
  'AppService',
  'BlockBlob',
  'CosmosDB',
  'PostgreSQL',
  'MySQL',
  'MariaDB',
  'RedisCache',
  'SqlDataWarehouse',
  'AzureDataExplorer',
  'RedHat',
  'SUSELinux',
  'VMwareCloudSimple',
];

function RateOptimizationTab({
  report,
  formatMoney,
  displayCurrency,
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const [lookBackPeriod, setLookBackPeriod] = useState<RecommendationLookBack>('Last30Days');
  const [term, setTerm] = useState<RecommendationTerm>('P1Y');
  const [resourceType, setResourceType] = useState<ReservationResourceType>('VirtualMachines');
  const [scenarioVersion, setScenarioVersion] = useState(0);
  const [result, setResult] = useState<RateOptimizationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getRateOptimization(
      report.subscriptionBreakdown.map((item) => item.subscriptionId),
      lookBackPeriod,
      term,
      resourceType,
      controller.signal,
    )
      .then(setResult)
      .catch((requestError) => {
        if (requestError instanceof DOMException && requestError.name === 'AbortError') return;
        setError(requestError instanceof Error ? requestError.message : 'Rate recommendations are unavailable.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [lookBackPeriod, report.subscriptionBreakdown, resourceType, scenarioVersion, term]);

  const overlapping = result?.reservations.some((reservation) =>
    reservation.overlapGroup && result.savingsPlans.some((plan) => plan.overlapGroup === reservation.overlapGroup)
  );
  const formatProjection = (value: number | null, currency: string) => {
    if (value === null) return 'Not reported';
    const sourceCurrency = currency || report.reportMetadata.currency;
    const sourceAmount = new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: sourceCurrency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 3,
    }).format(value);
    if (displayCurrency === sourceCurrency) return sourceAmount;
    return `${sourceAmount} · ${formatMoney(value)}`;
  };

  return (
    <div className="panel rate-optimization-panel">
      <div className="section-title-row">
        <h2 className="section-title">Rate Optimization</h2>
        {/* live: fetched from Azure on every visit, unlike the rest of this report (snapshot-based) */}
        <span className="live-badge" title="Fetched live from Azure, not from the report snapshot">Live</span>
      </div>
      <p className="section-subtitle">Azure purchase recommendations for one explicit, subscription-scoped scenario.</p>
      <div className="rate-controls">
        <label>
          <span>Lookback</span>
          <select value={lookBackPeriod} onChange={(event) => setLookBackPeriod(event.target.value as RecommendationLookBack)}>
            <option value="Last7Days">7 days</option>
            <option value="Last30Days">30 days</option>
            <option value="Last60Days">60 days</option>
          </select>
        </label>
        <label>
          <span>Term</span>
          <select value={term} onChange={(event) => setTerm(event.target.value as RecommendationTerm)}>
            <option value="P1Y">1 year</option>
            <option value="P3Y">3 years</option>
          </select>
        </label>
        <label>
          <span>Reservation resource</span>
          <select value={resourceType} onChange={(event) => setResourceType(event.target.value as ReservationResourceType)}>
            {RESERVATION_RESOURCE_TYPES.map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </label>
        <button type="button" className="rate-refresh" onClick={() => setScenarioVersion((value) => value + 1)} disabled={loading}>
          <RefreshCw size={15} aria-hidden="true" /> Refresh
        </button>
      </div>
      <div className="rate-scope-note">Scope: each selected subscription independently. Shared and billing-account scopes are not queried.</div>
      <CommitmentActivitySection report={report} formatMoney={formatMoney} displayCurrency={displayCurrency} />
      <CommitmentInsightsSection report={report} formatMoney={formatMoney} />
      {loading && <EvidenceState loading title="Checking recommendations" detail="Loading Azure recommendation scenarios" />}
      {!loading && error && <p className="pricing-unavailable" role="alert">{error}</p>}
      {!loading && result && (
        <>
          <div className="pricing-note">{result.projectionNotice}</div>
          {overlapping && (
            <div className="rate-overlap-note">Compute reservations and savings plans overlap for this scope. Treat them as alternatives, not additive savings.</div>
          )}
          <div className="rate-source-status" aria-label="Recommendation source status">
            {result.sourceStatus.map((status) => (
              <span className={`rate-status ${status.status}`} key={`${status.source}-${status.subscriptionId}`}>
                <b>{status.subscriptionName}</b>
                {status.source === 'reservation' ? 'Reservations' : 'Savings plans'} · {status.message}
              </span>
            ))}
          </div>
          <RateRecommendationSection
            title="Reservations"
            recommendations={result.reservations}
            emptyMessage="Azure returned no reservation recommendation for this scenario."
            displayCurrency={displayCurrency}
            formatProjection={formatProjection}
          />
          <RateRecommendationSection
            title="Savings Plans"
            recommendations={result.savingsPlans}
            emptyMessage="Azure returned no savings-plan recommendation for this scenario."
            displayCurrency={displayCurrency}
            formatProjection={formatProjection}
          />
          <p className="rate-generated">Source checked {new Date(result.generatedAt).toLocaleString()}</p>
        </>
      )}
    </div>
  );
}

function RateRecommendationSection({
  title,
  recommendations,
  emptyMessage,
  displayCurrency,
  formatProjection,
}: {
  title: string;
  recommendations: RateRecommendation[];
  emptyMessage: string;
  displayCurrency: string;
  formatProjection: (value: number | null, currency: string) => string;
}) {
  return (
    <section className="rate-section">
      <h3>{title}</h3>
      {recommendations.length === 0 ? <p className="empty-state">{emptyMessage}</p> : (
        <div className="rate-table-scroll">
          <table className="report-table rate-table">
            <thead><tr><th>Subscription / SKU</th><th>Scenario</th><th>Commitment</th><th className="num">Azure projected saving</th><th>Coverage / utilization</th><th>Review</th></tr></thead>
            <tbody>
              {recommendations.map((item) => (
                <tr key={`${item.kind}-${item.recommendationId}-${item.subscriptionId}`}>
                  <td><span className="subscription-reference"><strong>{item.subscriptionName}</strong><small>{item.sku || item.resourceType}{item.region ? ` · ${item.region}` : ''}</small></span></td>
                  <td>{item.lookBackPeriod.replace('Last', '').replace('Days', ' days')} · {item.term === 'P1Y' ? '1 year' : '3 years'}<small>{item.scope} scope · {item.resourceType}</small></td>
                  <td>{item.quantity !== null ? `${item.quantity} units` : `${formatProjection(item.hourlyCommitment, item.currency)} / hour`}<small>{item.costWithBenefit === null ? 'Benefit cost not reported' : `${formatProjection(item.costWithBenefit, item.currency)} with benefit`}</small></td>
                  <td className="num"><strong>Projected {formatProjection(item.projectedSavings, item.currency)}</strong><small>{item.savingsPercentage === null ? 'Percentage not reported' : percent(item.savingsPercentage)}</small></td>
                  <td>{item.coveragePercentage === null ? 'Not reported' : `${percent(item.coveragePercentage)} coverage`}<small>{item.utilizationPercentage === null ? 'Utilization not reported' : `${percent(item.utilizationPercentage)} utilization`}{item.wastageCost === null ? '' : ` · ${formatProjection(item.wastageCost, item.currency)} wastage`}</small></td>
                  <td><a className="portal-link" href={item.reviewUrl} target="_blank" rel="noreferrer">Open Azure <ExternalLink size={13} aria-hidden="true" /></a><small>{item.firstUsageDate && item.lastUsageDate ? `${reportDate(item.firstUsageDate.slice(0, 10))}–${reportDate(item.lastUsageDate.slice(0, 10))}` : item.source}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CommitmentActualCards({ latest, formatMoney }: { latest: CommitmentMonthPoint | undefined; formatMoney: MoneyFormatter }) {
  if (!latest) return null;
  return (
    <div className="commitment-actual-cards">
      <div className="commitment-actual-card">
        <span>Actual Reservation savings · {latest.month}</span>
        <strong>{formatMoney(latest.reservationRealizedSavings)}</strong>
        <small>Committed {formatMoney(latest.reservationCommittedCost)} · Unused {formatMoney(latest.reservationUnusedCost)}</small>
      </div>
      <div className="commitment-actual-card">
        <span>Actual Savings Plan savings · {latest.month}</span>
        <strong>{formatMoney(latest.savingsPlanRealizedSavings)}</strong>
        <small>Committed {formatMoney(latest.savingsPlanCommittedCost)} · Unused {formatMoney(latest.savingsPlanUnusedCost)}</small>
      </div>
    </div>
  );
}

function CommitmentCoveragePanel({ latest, formatMoney }: { latest: CommitmentMonthPoint | undefined; formatMoney: MoneyFormatter }) {
  if (!latest) {
    return (
      <section className="executive-visual commitment-coverage-panel">
        <header><span>Compute VM coverage</span></header>
        <p className="empty-state">No FOCUS history month is available yet.</p>
      </section>
    );
  }
  const coverage = latest.computeVmCoverage;
  const segments: { label: string; value: number; pct: number | null; className: string }[] = [
    { label: 'Reservation', value: coverage.reservationCost, pct: coverage.reservationPercentage, className: 'coverage-reservation' },
    { label: 'Savings Plan', value: coverage.savingsPlanCost, pct: coverage.savingsPlanPercentage, className: 'coverage-savings-plan' },
    { label: 'PAYG', value: coverage.paygCost, pct: coverage.paygPercentage, className: 'coverage-payg' },
  ];
  return (
    <section className="executive-visual commitment-coverage-panel">
      <header>
        <span>Compute VM coverage: PAYG vs Savings Plan vs Reservation</span>
        <small>{latest.month} · {formatMoney(coverage.totalCost)} total Compute VM effective cost</small>
      </header>
      {coverage.totalCost <= 0 ? (
        <p className="empty-state">No Compute VM usage was observed in this FOCUS history month.</p>
      ) : (
        <>
          <div className="coverage-bar">
            {segments.filter((segment) => segment.value > 0).map((segment) => (
              <i
                key={segment.label}
                className={segment.className}
                style={{ width: `${(segment.pct ?? 0) * 100}%` }}
                title={`${segment.label}: ${formatMoney(segment.value)} (${percent(segment.pct ?? 0)})`}
              />
            ))}
          </div>
          <ul className="coverage-legend">
            {segments.map((segment) => (
              <li key={segment.label}><i className={segment.className} />{segment.label}<strong>{segment.pct === null ? 'Not reported' : percent(segment.pct)}</strong></li>
            ))}
          </ul>
        </>
      )}
      <div className="coverage-org-reservation">
        <span>Org-level reservation coverage</span>
        {latest.reservationCoveragePercentage === null ? (
          <strong>No reservation activity this period</strong>
        ) : (
          <>
            <div className="coverage-bar single">
              <i className="coverage-reservation" style={{ width: `${latest.reservationCoveragePercentage * 100}%` }} />
            </div>
            <strong>{percent(latest.reservationCoveragePercentage)} of reservation-eligible spend</strong>
          </>
        )}
      </div>
    </section>
  );
}

function CommitmentTrendChart({
  title,
  subtitle,
  months,
  baseValue,
  savingsValue,
  baseLabel,
  savingsLabel,
  formatMoney,
}: {
  title: string;
  subtitle: string;
  months: CommitmentMonthPoint[];
  baseValue: (month: CommitmentMonthPoint) => number;
  savingsValue: (month: CommitmentMonthPoint) => number;
  baseLabel: string;
  savingsLabel: string;
  formatMoney: MoneyFormatter;
}) {
  const totals = months.map((month) => baseValue(month) + savingsValue(month));
  const maximum = Math.max(...totals, 0);
  return (
    <section className="executive-visual commitment-trend-panel">
      <header>
        <span>{title}</span>
        <small>{subtitle}</small>
      </header>
      {months.length === 0 ? (
        <p className="empty-state">No FOCUS history month is available yet.</p>
      ) : (
        <div className="monthly-chart-scroll">
          <div className="monthly-chart" style={{ gridTemplateColumns: `repeat(${months.length}, minmax(96px, 1fr))` }}>
            {months.map((month) => {
              const base = baseValue(month);
              const savings = savingsValue(month);
              const total = base + savings;
              return (
                <div className="monthly-column" key={month.month}>
                  <strong title={formatMoney(total)}>{formatMoney(total)}</strong>
                  <div className="monthly-bar-frame">
                    <div className="monthly-stack" style={{ height: `${maximum > 0 ? (total / maximum) * 100 : 0}%` }}>
                      {base > 0 && (
                        <i
                          className="commitment-bar-base"
                          style={{ height: `${total > 0 ? (base / total) * 100 : 0}%` }}
                          title={`${baseLabel}: ${formatMoney(base)}`}
                        />
                      )}
                      {savings > 0 && (
                        <i
                          className="commitment-bar-savings"
                          style={{ height: `${total > 0 ? (savings / total) * 100 : 0}%` }}
                          title={`${savingsLabel}: ${formatMoney(savings)}`}
                        />
                      )}
                    </div>
                  </div>
                  <span>{new Date(`${month.month}-01T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' })}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <p className="commitment-legend"><i className="commitment-bar-base" />{baseLabel}<i className="commitment-bar-savings" />{savingsLabel}</p>
    </section>
  );
}

function CommitmentInsightsSection({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  const insights = report.commitmentInsights;
  const latest = insights.months.at(-1);
  return (
    <section className="rate-section commitment-insights">
      <h3>Month-on-month commitment insights</h3>
      {!insights.available ? (
        <p className="empty-state">{insights.status}</p>
      ) : (
        <>
          <CommitmentActualCards latest={latest} formatMoney={formatMoney} />
          <CommitmentCoveragePanel latest={latest} formatMoney={formatMoney} />
          <CommitmentTrendChart
            title="Azure Committed Discount (ACD) vs least price"
            subtitle="Effective cost paid under Reservations/Savings Plans vs FOCUS least (list) price"
            months={insights.months}
            baseValue={(m) => m.acdEffectiveCost}
            savingsValue={(m) => m.acdSavings}
            baseLabel="Paid under ACD"
            savingsLabel="Saved vs least price"
            formatMoney={formatMoney}
          />
          <CommitmentTrendChart
            title="Reservation commitment vs savings"
            subtitle="Committed cost (used + unused) vs realized RI savings"
            months={insights.months}
            baseValue={(m) => m.reservationCommittedCost}
            savingsValue={(m) => m.reservationRealizedSavings}
            baseLabel="RI commitment cost"
            savingsLabel="RI savings"
            formatMoney={formatMoney}
          />
          <CommitmentTrendChart
            title="Savings Plan commitment vs savings"
            subtitle="Committed cost (used + unused) vs realized Savings Plan savings"
            months={insights.months}
            baseValue={(m) => m.savingsPlanCommittedCost}
            savingsValue={(m) => m.savingsPlanRealizedSavings}
            baseLabel="Savings Plan commitment cost"
            savingsLabel="Savings Plan savings"
            formatMoney={formatMoney}
          />
          <CommitmentTrendChart
            title="Spot savings"
            subtitle="Effective cost paid for Spot capacity vs FOCUS least (list) price"
            months={insights.months}
            baseValue={(m) => m.spotEffectiveCost}
            savingsValue={(m) => m.spotSavings}
            baseLabel="Paid for Spot"
            savingsLabel="Saved vs least price"
            formatMoney={formatMoney}
          />
        </>
      )}
      <p className="rate-scope-note">
        Commitment insights reuse the same closed-period FOCUS history collected for month-over-month spend - no
        additional Azure calls. Reservation/Savings Plan realized savings equal ContractedCost − EffectiveCost on
        `Used` rows; unused commitment cost is never counted as savings, and these figures are never combined with
        Advisor estimates or Azure rate-recommendation projections above.
      </p>
    </section>
  );
}

function CostAnomaliesTab({
  state,
  formatMoney,
  displayCurrency,
}: {
  state: AnomalyState;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const { result, error, loading, refresh } = state;

  const subscriptionSignals = result?.anomalies.filter((item) => item.dimensionType === 'subscription') ?? [];
  const spikeImpact = subscriptionSignals.reduce((sum, item) => sum + Math.max(item.absoluteDelta, 0), 0);
  const dropImpact = subscriptionSignals.reduce((sum, item) => sum + Math.abs(Math.min(item.absoluteDelta, 0)), 0);
  const highSignals = result?.anomalies.filter((item) => item.severity === 'High').length ?? 0;

  return (
    <div className="panel anomaly-panel">
      <div className="anomaly-heading">
        <div>
          <h2 className="section-title">Cost Anomalies</h2>
          <p className="section-subtitle">{BRAND_NAME} detection over complete daily FOCUS EffectiveCost history.</p>
        </div>
        <button className="rate-refresh" type="button" disabled={loading} onClick={refresh}>
          <RefreshCw size={15} aria-hidden="true" /> Refresh
        </button>
      </div>
      <div className="anomaly-source-note">Transparent application detector · not Azure Cost Management anomaly detection · read-only</div>
      {loading && <p className="empty-state" role="status">Analyzing complete daily FOCUS history</p>}
      {!loading && error && <div className="pricing-unavailable" role="alert"><strong>Detection unavailable</strong><span>{error}</span></div>}
      {!loading && result && result.status === 'insufficient_history' && (
        <div className="pricing-unavailable"><strong>Insufficient complete history</strong><span>{result.statusMessage}</span></div>
      )}
      {!loading && result && result.status === 'ready' && (
        <>
          <div className="pricing-metadata anomaly-metadata">
            <span><b>History</b>{reportDate(result.historyStart)}–{reportDate(result.historyEnd)}</span>
            <span><b>Complete days</b>{result.completeDays}</span>
            <span><b>Algorithm</b>{result.algorithmVersion}</span>
            <span><b>Generated</b>{new Date(result.generatedAt).toLocaleString()}</span>
          </div>
          <div className="kpi-grid anomaly-kpi-grid">
            <div className="kpi-card"><div className="kpi-label">Detected signals</div><div className="kpi-value">{result.anomalies.length}</div><div className="kpi-note">Across four dimensions</div></div>
            <div className="kpi-card risk"><div className="kpi-label">Subscription spike impact</div><div className="kpi-value">{formatMoney(spikeImpact)}</div><div className="kpi-note">Unexpected cost, not savings</div></div>
            <div className="kpi-card"><div className="kpi-label">Subscription drop impact</div><div className="kpi-value">{formatMoney(dropImpact)}</div><div className="kpi-note">Investigate service or usage change</div></div>
            <div className="kpi-card"><div className="kpi-label">High severity</div><div className="kpi-value">{highSignals}</div><div className="kpi-note">Deterministic threshold</div></div>
          </div>
          <AnomalyTrend trend={result.trend} anomalies={result.anomalies} formatMoney={formatMoney} displayCurrency={displayCurrency} />
          <section className="anomaly-results">
            <h3>Detected signals</h3>
            {result.anomalies.length === 0 ? (
              <p className="empty-state">No cost anomalies crossed the configured absolute and robust-baseline thresholds.</p>
            ) : (
              <div className="anomaly-table-scroll">
                <table className="report-table anomaly-table">
                  <thead><tr><th>Date / severity</th><th>Signal</th><th>Dimension</th><th className="num">Actual</th><th className="num">Expected</th><th className="num">Delta</th><th>Contributors</th><th>Investigate</th></tr></thead>
                  <tbody>
                    {result.anomalies.slice(0, 100).map((item) => (
                      <AnomalyRow item={item} formatMoney={formatMoney} key={item.anomalyId} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function AnomalyTrend({
  trend,
  anomalies,
  formatMoney,
  displayCurrency,
}: {
  trend: AnomalyTrendPoint[];
  anomalies: CostAnomaly[];
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const width = 900;
  const height = 250;
  const padding = 30;
  const maximum = Math.max(1, ...trend.flatMap((item) => [item.actualCost, item.expectedUpper ?? 0]));
  const x = (index: number) => padding + (index * (width - padding * 2)) / Math.max(1, trend.length - 1);
  const y = (value: number) => height - padding - (Math.max(0, value) * (height - padding * 2)) / maximum;
  const actualPoints = trend.map((item, index) => `${x(index)},${y(item.actualCost)}`).join(' ');
  const expected = trend.map((item, index) => ({ item, index })).filter(({ item }) => item.expectedCost !== null);
  const expectedPoints = expected.map(({ item, index }) => `${x(index)},${y(item.expectedCost ?? 0)}`).join(' ');
  const bandPoints = [
    ...expected.map(({ item, index }) => `${x(index)},${y(item.expectedUpper ?? 0)}`),
    ...expected.slice().reverse().map(({ item, index }) => `${x(index)},${y(item.expectedLower ?? 0)}`),
  ].join(' ');
  const anomalyDates = new Set(anomalies.filter((item) => item.dimensionType === 'subscription').map((item) => item.date));
  return (
    <div className="anomaly-chart-wrap">
      <div className="anomaly-chart-header"><h3>Daily cost trend</h3><span>Actual <i className="actual-key" /> Expected <i className="expected-key" /> Range <i className="range-key" /></span></div>
      <svg className="anomaly-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Daily actual and expected cost in ${displayCurrency}`}>
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} className="chart-axis" />
        {bandPoints && <polygon points={bandPoints} className="chart-range" />}
        {expectedPoints && <polyline points={expectedPoints} className="chart-expected" />}
        <polyline points={actualPoints} className="chart-actual" />
        {trend.map((item, index) => anomalyDates.has(item.date) ? (
          <circle key={item.date} cx={x(index)} cy={y(item.actualCost)} r="4" className="chart-anomaly"><title>{item.date}: {formatMoney(item.actualCost)}</title></circle>
        ) : null)}
        <text x={padding} y={height - 8} className="chart-label">{trend[0]?.date}</text>
        <text x={width - padding} y={height - 8} textAnchor="end" className="chart-label">{trend.at(-1)?.date}</text>
        <text x={padding} y={18} className="chart-label">{formatMoney(maximum)}</text>
      </svg>
    </div>
  );
}

function AnomalyRow({ item, formatMoney }: { item: CostAnomaly; formatMoney: MoneyFormatter }) {
  const typeLabel = item.anomalyType === 'new_resource' ? 'New resource' : item.anomalyType === 'spike' ? 'Spike' : 'Drop';
  return (
    <tr>
      <td><strong>{reportDate(item.date)}</strong><small>{item.durationDays > 1 ? `${item.durationDays}-day incident · ${reportDate(item.firstDetectedDate)}–${reportDate(item.lastDetectedDate)}` : 'Single-day incident'}</small><small className={`anomaly-severity ${item.severity.toLowerCase()}`}>{item.severity}</small></td>
      <td>{typeLabel}<small>{item.baselineSamples} same-weekday baseline samples</small></td>
      <td><strong>{item.dimensionName}</strong><small>{item.dimensionType.replace('_', ' ')} · {item.subscriptionName || item.subscriptionId}</small></td>
      <td className="num">{formatMoney(item.actualCost)}</td>
      <td className="num">{formatMoney(item.expectedCost)}<small>{formatMoney(item.expectedLower)}–{formatMoney(item.expectedUpper)}</small></td>
      <td className={`num anomaly-delta ${item.absoluteDelta >= 0 ? 'increase' : 'decrease'}`}><strong>{item.absoluteDelta >= 0 ? '+' : ''}{formatMoney(item.absoluteDelta)}</strong><small>{item.percentageDelta === null ? 'New baseline' : `${item.percentageDelta >= 0 ? '+' : ''}${percent(item.percentageDelta)}`}</small></td>
      <td>{item.contributors.map((contributor) => <span className="anomaly-contributor" key={`${contributor.resourceId}-${contributor.name}`}><b>{contributor.name}</b><small>{formatMoney(contributor.cost)}</small></span>)}</td>
      <td><a className="portal-link" href={item.investigationUrl} target="_blank" rel="noreferrer">Cost Analysis <ExternalLink size={13} aria-hidden="true" /></a></td>
    </tr>
  );
}

function StaleResourcesTab({
  report,
  formatMoney,
  displayCurrency,
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
}) {
  const categories = report.tierACategories.filter((category) => STALE_CATEGORIES.has(category.category));
  const lines = categories.flatMap((category) => category.lines.map((line) => ({ category, line })));
  const verifiedSaving = categories
    .filter((category) => category.impactType === 'potential_savings')
    .reduce((sum, category) => sum + category.monthlyTotal, 0);
  const costAtRisk = categories
    .filter((category) => category.impactType === 'cost_at_risk')
    .reduce((sum, category) => sum + category.monthlyTotal, 0);
  return (
    <div className="panel">
      <h2 className="section-title">Stale and Orphaned Resources</h2>
      <p className="section-subtitle">
        Inventory threshold {report.reportMetadata.staleDays} days · protected tags {report.reportMetadata.protectedTagKeys.join(', ') || 'none'}
      </p>
      <div className="kpi-grid stale-kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">Flagged resources</div>
          <div className="kpi-value">{lines.length}</div>
        </div>
        <div className="kpi-card positive">
          <div className="kpi-label">Verified saving / month</div>
          <div className="kpi-value">{formatMoney(verifiedSaving)}</div>
        </div>
        <div className="kpi-card risk">
          <div className="kpi-label">Billed cost at risk / month</div>
          <div className="kpi-value">{formatMoney(costAtRisk)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Protected resources excluded</div>
          <div className="kpi-value">{report.reportMetadata.excludedProtectedResources}</div>
        </div>
      </div>
      {lines.length === 0 ? (
        <EvidenceState title="No stale-resource findings" detail="No stale or orphaned resources matched the current evidence rules." />
      ) : (
        <div className="stale-table-scroll">
          <table className="report-table stale-resource-table">
            <thead>
              <tr>
                <th>Category</th>
                <th>Resource</th>
                <th>Subscription</th>
                <th>Evidence</th>
                <th className="num">Monthly impact ({displayCurrency})</th>
              </tr>
            </thead>
            <tbody>
              {lines.map(({ category, line }) => (
                <tr key={`${category.category}-${line.resourceId}`}>
                  <td>
                    <strong>{category.displayName}</strong>
                    <small className={`impact-label ${category.impactType}`}>
                      {category.impactType === 'potential_savings'
                        ? 'Potential saving'
                        : category.impactType === 'cost_at_risk'
                          ? 'Cost at risk'
                          : 'Inventory review'}
                    </small>
                  </td>
                  <td>
                    <a className="resource-link" href={azurePortalResourceUrl(line.resourceId)} target="_blank" rel="noreferrer">
                      <span><strong>{line.resourceName}</strong><small>{line.resourceId}</small></span>
                      <ExternalLink size={14} aria-hidden="true" />
                    </a>
                  </td>
                  <td>
                    <span className="subscription-reference">
                      <strong>{line.subscriptionName}</strong>
                      <small title={line.subscriptionId}>{line.subscriptionId}</small>
                    </span>
                  </td>
                  <td>
                    <span className={`evidence-badge evidence-${line.evidenceType.replaceAll('_', '-')}`}>
                      {EVIDENCE_LABELS[line.evidenceType]}
                    </span>
                    <span className="evidence-detail">{line.detail}</span>
                  </td>
                  <td className="num">{line.monthlyCost === null ? 'Not billed' : formatMoney(line.monthlyCost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DomainTab({
  domain,
  narrativeByCategory,
  expandedCategory,
  formatMoney,
  displayCurrency,
  onOpenRemediation,
  metricCoverage,
  onToggleCategory,
}: {
  domain: DomainSummary;
  narrativeByCategory: Map<string, { priority: string; narrative: string }>;
  expandedCategory: string | null;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
  onOpenRemediation: (finding: string, plan: RemediationPlan | null) => void;
  metricCoverage?: MetricCoverageSummary[];
  onToggleCategory: (category: string) => void;
}) {
  const [selectedResourceId, setSelectedResourceId] = useState<string | null>(null);
  useEffect(() => setSelectedResourceId(null), [expandedCategory]);
  const inventoryDomain = domain.domain === 'sql' || domain.domain === 'ai';
  const inventoryCount = domain.categories
    .filter((category) => category.impactType === 'inventory')
    .reduce((sum, category) => sum + category.count, 0);
  return (
    <div className="panel">
      <h2 className="section-title">{DOMAIN_HEADINGS[domain.domain] ?? domain.domain} Domain at a Glance</h2>
      {metricCoverage && metricCoverage.length > 0 && (
        <div className="metric-coverage" aria-label="Closed-period network traffic coverage">
          <header>
            <span>Closed-period traffic evidence</span>
            <strong>{metricCoverage.reduce((sum, item) => sum + item.zeroTrafficCount, 0)} verified idle</strong>
          </header>
          <div>
            {metricCoverage.map((coverage) => (
              <span key={coverage.category} className={coverage.unavailableCount > 0 ? 'incomplete' : ''}>
                <b>{NETWORK_METRIC_LABELS[coverage.category] ?? coverage.category}</b>
                {coverage.completeCount}/{coverage.candidateCount} checked · {coverage.zeroTrafficCount} zero traffic
                {coverage.unavailableCount > 0 && <i>{coverage.unavailableCount} unavailable</i>}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">{domain.domain} spend / month</div>
          <div className="kpi-value">{formatMoney(domain.domainSpendMonth)}</div>
        </div>
        {inventoryDomain ? (
          <div className="kpi-card">
            <div className="kpi-label">Resources inventoried</div>
            <div className="kpi-value">{inventoryCount}</div>
          </div>
        ) : (
          <div className="kpi-card positive">
            <div className="kpi-label">Verified saving / month</div>
            <div className="kpi-value">{formatMoney(domain.verifiedSavingMonth)}</div>
          </div>
        )}
        {domain.monthlyCostAtRisk > 0 && (
          <div className="kpi-card risk">
            <div className="kpi-label">Billed cost at risk / month</div>
            <div className="kpi-value">{formatMoney(domain.monthlyCostAtRisk)}</div>
          </div>
        )}
        {!inventoryDomain && (
          <>
            <div className="kpi-card positive">
              <div className="kpi-label">Verified saving / year</div>
              <div className="kpi-value">{formatMoney(domain.verifiedSavingYear)}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">% of {domain.domain} spend</div>
              <div className="kpi-value">{percent(domain.pctOfDomainSpend)}</div>
            </div>
          </>
        )}
      </div>

      {domain.advisorRecommendations.length > 0 && (
        <section className="domain-advisor" aria-label={`Azure Advisor guidance for ${domain.domain}`}>
          <header>
            <span>Matched Azure Advisor guidance</span>
            <strong>{domain.advisorRecommendations.length}</strong>
          </header>
          <div>
            {domain.advisorRecommendations.map((recommendation) => (
              <article key={`${recommendation.resourceId}-${recommendation.recommendation}`}>
                <a href={azurePortalResourceUrl(recommendation.resourceId)} target="_blank" rel="noreferrer">
                  {recommendation.resourceName}<ExternalLink size={12} aria-hidden="true" />
                </a>
                <span className="subscription-reference">
                  <strong>{recommendation.subscriptionName}</strong>
                  <small title={recommendation.subscriptionId}>{recommendation.subscriptionId}</small>
                </span>
                <b>{recommendation.problem}</b>
                <p>{recommendation.recommendation}</p>
                <small>
                  {recommendation.estimatedSavings === null
                    ? 'Advisor estimate not reported'
                    : `${formatMoney(recommendation.estimatedSavings)} ${recommendation.savingsPeriod} Advisor estimate`}
                  {' · '}
                  {recommendation.billedCost === null
                    ? 'No matched closed-period resource cost'
                    : `${formatMoney(recommendation.billedCost)} closed-period billed cost`}
                </small>
              </article>
            ))}
          </div>
        </section>
      )}

      {domain.categories.length === 0 ? (
        <EvidenceState title="No findings in this scope" detail="No findings for this domain in the selected subscriptions." />
      ) : (
        <div className="domain-table-scroll">
          <table className="report-table domain-findings-table">
            <thead>
              <tr>
                <th>Finding</th>
                <th className="num">Resources</th>
                <th className="num">{inventoryDomain ? 'Monthly billed cost' : 'Monthly impact'}</th>
                <th className="num">{displayCurrency}/{inventoryDomain ? 'year run rate' : 'year'}</th>
                <th>Priority</th>
                <th>Narrative</th>
              </tr>
            </thead>
            <tbody>
              {domain.categories.map((cat) => {
                const p = narrativeByCategory.get(cat.category);
                const expanded = expandedCategory === cat.category;
                const selectedLine = cat.lines.find((line) => line.resourceId === selectedResourceId);
                return (
                  <Fragment key={cat.category}>
                    <tr id={`finding-${cat.category}`} className={`finding-summary-row ${expanded ? 'is-expanded' : ''}`}>
                      <td>
                        <button
                          className="finding-disclosure"
                          type="button"
                          aria-expanded={expanded}
                          aria-controls={`resources-${cat.category}`}
                          onClick={() => onToggleCategory(cat.category)}
                        >
                          <ChevronRight size={16} aria-hidden="true" />
                          <span>
                            <strong>{cat.displayName}</strong>
                            <small className={`impact-label ${cat.impactType}`}>
                              {cat.impactType === 'cost_at_risk'
                                ? 'Cost at risk'
                                : cat.impactType === 'inventory'
                                  ? 'Inventory review'
                                  : 'Potential saving'}
                            </small>
                          </span>
                        </button>
                      </td>
                      <td className="num">{cat.count}</td>
                      <td className="num">{formatMoney(cat.monthlyTotal)}</td>
                      <td className="num">{formatMoney(cat.annualTotal)}</td>
                      <td>{p?.priority ? <span className="pill">{p.priority}</span> : ''}</td>
                      <td>{p?.narrative ?? ''}</td>
                    </tr>
                    {expanded && (
                      <tr className="resource-detail-row">
                        <td colSpan={6}>
                          <div id={`resources-${cat.category}`} className="resource-detail-panel">
                            <div className="resource-detail-heading">
                              <span>Affected resources</span>
                              <strong>{cat.count}</strong>
                              <span className="resource-selection-summary">
                                {selectedLine ? selectedLine.resourceName : 'Select a resource to review'}
                              </span>
                              {selectedLine && (
                                <a
                                  className="resource-selection-action"
                                  href={azurePortalResourceUrl(selectedLine.resourceId)}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  Open selected <ExternalLink size={13} aria-hidden="true" />
                                </a>
                              )}
                              {cat.remediation && (
                                <button type="button" onClick={() => onOpenRemediation(cat.displayName, cat.remediation)}>
                                  <FileCode2 size={14} />
                                  {cat.remediation.mode === 'preview' ? 'Preview script' : 'Inspect dependencies'}
                                </button>
                              )}
                            </div>
                            <div className="resource-table-scroll">
                              <table className="resource-table">
                                <thead>
                                  <tr>
                                    <th className="resource-select-column"><span className="sr-only">Select</span></th>
                                    <th>Resource</th>
                                    <th>Evidence</th>
                                    <th>Subscription</th>
                                    <th className="num">Monthly cost</th>
                                    <th className="num">Confidence</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {cat.lines.map((line) => (
                                    <tr
                                      key={line.resourceId}
                                      className={selectedResourceId === line.resourceId ? 'is-selected' : ''}
                                      onClick={() => setSelectedResourceId(line.resourceId)}
                                    >
                                      <td className="resource-select-column">
                                        <input
                                          type="radio"
                                          name={`resource-option-${cat.category}`}
                                          checked={selectedResourceId === line.resourceId}
                                          onChange={() => setSelectedResourceId(line.resourceId)}
                                          aria-label={`Select ${line.resourceName}`}
                                        />
                                      </td>
                                      <td>
                                        <a
                                          className="resource-link"
                                          href={azurePortalResourceUrl(line.resourceId)}
                                          target="_blank"
                                          rel="noreferrer"
                                          title={`Open ${line.resourceName} in Azure Portal`}
                                        >
                                          <span><strong>{line.resourceName}</strong><small>{line.resourceId}</small></span>
                                          <ExternalLink size={14} aria-hidden="true" />
                                        </a>
                                      </td>
                                      <td>
                                        <span
                                          className={`evidence-badge evidence-${line.evidenceType.replaceAll('_', '-')}`}
                                          title={EVIDENCE_TITLES[line.evidenceType]}
                                        >
                                          {EVIDENCE_LABELS[line.evidenceType]}
                                        </span>
                                        <span className="evidence-detail">
                                          {line.detail || 'Resource matched the current inventory rule.'}
                                        </span>
                                        {(line.costEvidence ?? []).length > 0 && (
                                          <ul className="cost-evidence-list" aria-label="Closed-period billing evidence">
                                            {(line.costEvidence ?? []).map((evidence, index) => (
                                              <li key={`${evidence.meterId}-${evidence.productId}-${evidence.pricingModel}-${index}`}>
                                                <span>
                                                  <b>{evidence.meterName || evidence.productName || evidence.meterCategory || 'Billing meter'}</b>
                                                  <small>
                                                    {[
                                                      evidence.productName && evidence.productName !== evidence.meterName ? evidence.productName : '',
                                                      evidence.meterSubCategory || evidence.meterCategory,
                                                      evidence.pricingModel,
                                                      evidence.benefitName || evidence.reservationName,
                                                    ].filter(Boolean).join(' · ')}
                                                  </small>
                                                </span>
                                                <strong>{formatMoney(evidence.monthlyCost)}</strong>
                                              </li>
                                            ))}
                                          </ul>
                                        )}
                                        <FocusPricingEvidenceDetails
                                          items={line.focusPricingEvidence ?? []}
                                          displayCurrency={displayCurrency}
                                        />
                                      </td>
                                      <td>
                                        <span className="subscription-reference">
                                          <strong>{line.subscriptionName}</strong>
                                          <small title={line.subscriptionId}>{line.subscriptionId}</small>
                                        </span>
                                      </td>
                                      <td className="num">{line.monthlyCost === null ? 'Not billed' : formatMoney(line.monthlyCost)}</td>
                                      <td className="num">{Math.round(line.confidence * 100)}%</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AdvisorTab({ report, formatMoney }: { report: FullReport; formatMoney: MoneyFormatter }) {
  return (
    <div className="panel">
      <h2 className="section-title">Azure Advisor Reconciliation</h2>
      <p className="section-subtitle">Cross-references Advisor's own cost recommendations against actual billed cost.</p>
      <div className="measure-grid">
        {report.advisorReconciliation.measures.map((m) => (
          <div className="panel measure-card" key={m.measure}>
            <div className="kpi-label">{m.measure}</div>
            <div className="measure-value">{m.monetaryValue === null ? m.value : formatMoney(m.monetaryValue)}</div>
            <div className="measure-explanation">{m.explanation}</div>
          </div>
        ))}
      </div>
      {report.advisorReconciliation.recommendations.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table className="report-table advisor-recommendations-table">
            <thead>
              <tr>
                <th>Subscription</th>
                <th>Resource</th>
                <th>Recommendation</th>
                <th className="num">Advisor estimate</th>
                <th className="num">Matched billed cost</th>
              </tr>
            </thead>
            <tbody>
              {report.advisorReconciliation.recommendations.map((recommendation) => (
                <tr key={`${recommendation.resourceId}-${recommendation.recommendation}`}>
                  <td>
                    <span className="subscription-reference">
                      <strong>{recommendation.subscriptionName}</strong>
                      <small title={recommendation.subscriptionId}>{recommendation.subscriptionId}</small>
                    </span>
                  </td>
                  <td>
                    <a href={azurePortalResourceUrl(recommendation.resourceId)} target="_blank" rel="noreferrer">
                      {recommendation.resourceName} <ExternalLink size={12} aria-hidden="true" />
                    </a>
                  </td>
                  <td>{recommendation.recommendation}</td>
                  <td className="num">
                    {recommendation.estimatedSavings === null
                      ? 'Not reported'
                      : `${formatMoney(recommendation.estimatedSavings)} ${recommendation.savingsPeriod}`}
                  </td>
                  <td className="num">
                    {recommendation.billedCost === null ? 'Not matched' : formatMoney(recommendation.billedCost)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function GovernanceTab({ report }: { report: FullReport }) {
  const totalUntagged = report.governance.reduce((sum, r) => sum + r.untaggedResources, 0);
  return (
    <div className="panel">
      <h2 className="section-title">Governance, Tagging and Risk</h2>
      <p className="section-subtitle">Tagging maturity — {totalUntagged.toLocaleString()} untagged resources across the selected subscriptions.</p>
      {report.governance.length === 0 ? (
        <EvidenceState title="Governance evidence unavailable" detail="No data available." />
      ) : (
        <table className="report-table">
          <thead>
            <tr>
              <th>Subscription</th>
              <th className="num">Untagged resources</th>
              <th className="num">% of estate total</th>
            </tr>
          </thead>
          <tbody>
            {report.governance.map((row) => (
              <tr key={row.subscriptionId}>
                <td>{row.subscriptionName}</td>
                <td className="num">{row.untaggedResources.toLocaleString()}</td>
                <td className="num">{percent(row.pctOfEstate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function CostByTagsTab({
  report,
  formatMoney,
  displayCurrency,
  costWindow,
  costFilters,
  onCostFiltersChange,
  budgetState,
}: {
  report: FullReport;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
  costWindow: CostWindow;
  costFilters: CostFilter;
  onCostFiltersChange: (value: CostFilter) => void;
  budgetState?: BudgetState;
}) {
  const summary = report.tagCosts;
  const dimensions = summary?.dimensions ?? [];
  const [selectedKey, setSelectedKey] = useState<string>(dimensions[0]?.tagKey ?? '');
  const [selectedValue, setSelectedValue] = useState<string | null>(null);
  const [breakdown, setBreakdown] = useState<CostDimension>('service');
  const dimension = dimensions.find((item) => item.tagKey === selectedKey) ?? dimensions[0];
  const allRows = dimension?.rows ?? [];
  const activeValue = dimension?.tagKey === selectedKey && allRows.some((row) => row.value === selectedValue)
    ? selectedValue : null;
  const rows = activeValue === null ? allRows : allRows.filter((row) => row.value === activeValue);
  const maxCost = Math.max(...allRows.map((row) => row.monthlyCost), 1);
  /* Selecting a tag key and value here scopes the breakdowns below, so the two
     halves of this page answer the same question rather than sitting side by
     side unaware of each other. */
  const scopedFilters: CostFilter = {
    ...costFilters,
    ...(dimension?.tagKey ? { tagKey: dimension.tagKey } : {}),
    ...(activeValue === null ? {} : { tagValue: activeValue }),
  };
  /* Budgets are matched against the rows the selection actually covers, not
     against the tag string, so a budget scoped by resource group still shows
     up when that group is what carries the tag. */
  const taggedBudgets = useMemo(() => {
    if (!budgetState || report.costDetails?.status !== 'complete') return [];
    const rows = report.costDetails.rows.filter((row) => matchesCostFilter(row, scopedFilters));
    return relateBudgets(budgetState.budgets, rows, scopedFilters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [budgetState?.budgets, report.costDetails, JSON.stringify(scopedFilters)]);
  const scopeLabel = activeValue === null
    ? `all ${dimension?.tagKey ?? 'tag'} values`
    : `${dimension?.tagKey} = ${activeValue || '(empty)'}`;
  return (
    <div className="panel">
      <h2 className="section-title">Cost by Tags/Application</h2>
      <p className="section-subtitle">Grouped by any FOCUS resource tag found (inherited from the resource group when a resource has no tag of its own), with a next-month forecast based on overall spend trend.</p>
      {!summary?.available || dimensions.length === 0 ? (
        <EvidenceState title="Tag evidence unavailable" detail={summary?.status ?? 'No resource or resource-group tags were present.'} />
      ) : (
        <>
          <div className="tag-cost-controls">
            <label className="currency-control">
              <span>Tag key</span>
              <select aria-label="Tag key" title={dimension?.tagKey} value={dimension?.tagKey ?? ''} onChange={(event) => {
                setSelectedKey(event.target.value);
                setSelectedValue(null);
              }}>
                {dimensions.map((item) => (
                  <option value={item.tagKey} key={item.tagKey}>{item.tagKey}</option>
                ))}
              </select>
            </label>
            <label className="currency-control">
              <span>Tag value</span>
              <select
                aria-label="Tag value"
                title={activeValue === null ? 'All values' : activeValue || '(empty)'}
                value={activeValue === null ? '' : JSON.stringify(activeValue)}
                disabled={allRows.length === 0}
                onChange={(event) => {
                  setSelectedKey(dimension?.tagKey ?? '');
                  setSelectedValue(event.target.value === '' ? null : JSON.parse(event.target.value) as string);
                }}
              >
                <option value="">All values</option>
                {allRows.map((row) => (
                  <option value={JSON.stringify(row.value)} key={row.value}>{row.value || '(empty)'}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="tag-cost-table-scroll" role="region" aria-label="Tag cost breakdown" tabIndex={0}>
            <table className="report-table app-cost-table">
              <thead>
                <tr>
                  <th>{dimension?.tagKey}</th>
                  <th className="num">Monthly cost ({displayCurrency})</th>
                  <th className="num">% of total</th>
                  <th className="num">Forecast next month</th>
                  <th>Relative spend</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.value}>
                    <td>{row.value}</td>
                    <td className="num">{formatMoney(row.monthlyCost)}</td>
                    <td className="num">{percent(row.pctOfTotal)}</td>
                    <td className="num">{row.forecastNextMonth === null ? '—' : formatMoney(row.forecastNextMonth)}</td>
                    <td>
                      <span className="app-cost-bar-track">
                        <span className="app-cost-bar-fill" style={{ width: `${Math.min(100, (row.monthlyCost / maxCost) * 100)}%` }} />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {dimension && dimension.unallocatedCost > 0 && (
            <p className="section-subtitle">{formatMoney(dimension.unallocatedCost)}/mo has no {dimension.tagKey} tag value.</p>
          )}
        </>
      )}

      {/* The budget that governs this tag, if one does.

          Answered on this page rather than by sending the reader to the budget
          tab, because "is this application within budget" is the question the
          tag selection raises. A subscription-wide budget is reported as such:
          it bears on the tag but is not an allocation for it. */}
      {budgetState && (
        <section className="tag-cost-budgets" aria-label={`Budgets covering ${dimension?.tagKey ?? 'the selected tag'}`}>
          <h3 className="section-title">Budget for this selection</h3>
          {budgetState.loading ? (
            <p role="status">Checking subscription budgets...</p>
          ) : budgetState.error ? (
            <p role="alert">{budgetState.error}</p>
          ) : taggedBudgets.length === 0 ? (
            <EvidenceState
              title="No budget covers this selection"
              detail={`No Azure budget in the assessed subscriptions matches ${scopeLabel}. Create one on the Budgets page to track this spend against a limit.`}
            />
          ) : (
            <>
              <p className="section-subtitle">{taggedBudgets.length === 1 ? 'One budget bears' : `${taggedBudgets.length} budgets bear`} on {scopeLabel}.</p>
              {taggedBudgets.slice(0, 3).map(({ budget, relation }) => {
                const status = budgetThreshold(budget);
                const native = (value: number | null) => value === null ? 'Unavailable' : `${budget.currency} ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                return (
                  <article className="tag-budget-card" key={`${budget.subscriptionId}:${budget.name}`}>
                    <header className="cost-section-heading">
                      <h4>{budget.name}</h4>
                      <span className={`budget-status budget-${status.tone}`}>{status.label}</span>
                    </header>
                    <p className="billing-provenance">{relation} · {budget.timeGrain} · {native(budget.currentSpend)} of {native(budget.amount)} reported by Azure for the current cycle.</p>
                    <BudgetDailyChart budget={budget} details={report.costDetails} window={costWindow} formatMoney={formatMoney} />
                  </article>
                );
              })}
              {taggedBudgets.length > 3 && <p className="section-subtitle">{taggedBudgets.length - 3} further matching {taggedBudgets.length - 3 === 1 ? 'budget is' : 'budgets are'} listed on the Budgets page.</p>}
            </>
          )}
        </section>
      )}

      {/* Where the tagged money actually went. Scoped by the tag selection
          above when one is active, so this answers "what is this tag paying
          for" rather than repeating the estate totals. */}
      <section className="tag-cost-breakdowns" aria-label="Cost breakdown by dimension">
        <h3 className="section-title">Cost breakdown</h3>
        <p className="section-subtitle">
          {activeValue === null
            ? `Across the selected range${dimension?.tagKey ? `, all ${dimension.tagKey} values` : ''}.`
            : `Scoped to ${dimension?.tagKey} = ${activeValue || '(empty)'}.`}
        </p>
        <CostFilters details={report.costDetails} value={costFilters} onChange={onCostFiltersChange} />
        <GroupedCostBreakdown
          details={report.costDetails}
          window={costWindow}
          filters={scopedFilters}
          formatMoney={formatMoney}
          displayCurrency={displayCurrency}
          dimension={breakdown}
          onDimensionChange={setBreakdown}
          label="Tagged cost breakdown"
        />
      </section>
    </div>
  );
}

function BudgetsTab({ report, costWindow, formatMoney }: { report: FullReport; costWindow: CostWindow; formatMoney: MoneyFormatter }) {
  function defaultStartDate(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;
  }
  const emptyForm = (): BudgetWriteRequest => ({
    subscriptionId: report.subscriptionBreakdown[0]?.subscriptionId ?? '',
    name: '',
    amount: 0,
    timeGrain: 'Monthly',
    startDate: defaultStartDate(),
    alertThresholdPercent: null,
    alertEmail: '',
  });
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<BudgetWriteRequest>(emptyForm());
  const [editing, setEditing] = useState<{ subscriptionId: string; name: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [openBudget, setOpenBudget] = useState<string | null>(null);

  const subscriptionIds = report.subscriptionBreakdown.map((row) => row.subscriptionId);
  const subscriptionNames = new Map(report.subscriptionBreakdown.map((row) => [row.subscriptionId, row.subscriptionName]));

  useEffect(() => {
    const controller = new AbortController();
    if (subscriptionIds.length === 0) {
      setBudgets([]);
      setLoading(false);
      return undefined;
    }
    setLoading(true);
    listBudgets(subscriptionIds, controller.signal)
      .then(setBudgets)
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'Budgets are unavailable.');
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report.subscriptionBreakdown]);

  function formatNative(amount: number | null, currency: string): string {
    if (amount === null) return '—';
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
      maximumFractionDigits: 0,
    }).format(amount);
  }

  function edit(budget: Budget) {
    setEditing({ subscriptionId: budget.subscriptionId, name: budget.name });
    setForm({
      subscriptionId: budget.subscriptionId,
      name: budget.name,
      amount: budget.amount,
      timeGrain: (budget.timeGrain as BudgetTimeGrain) || 'Monthly',
      startDate: budget.periodStart ? budget.periodStart.slice(0, 10) : defaultStartDate(),
      alertThresholdPercent: null,
      alertEmail: '',
    });
  }

  function cancelEdit() {
    setEditing(null);
    setForm(emptyForm());
  }

  async function submit() {
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        const updated = await updateBudget(editing.subscriptionId, editing.name, form);
        setBudgets((current) => current.map((budget) => (
          budget.subscriptionId === editing.subscriptionId && budget.name === editing.name ? updated : budget
        )));
      } else {
        const created = await createBudget(form);
        setBudgets((current) => [...current, created]);
      }
      cancelEdit();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Budget save failed.');
    } finally {
      setSaving(false);
    }
  }

  async function remove(budget: Budget) {
    setError(null);
    try {
      await deleteBudget(budget.subscriptionId, budget.name);
      setBudgets((current) => current.filter((item) => (
        !(item.subscriptionId === budget.subscriptionId && item.name === budget.name)
      )));
      if (editing?.subscriptionId === budget.subscriptionId && editing?.name === budget.name) cancelEdit();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Budget delete failed.');
    }
  }

  return (
    <div className="panel">
      <div className="section-title-row">
        <h2 className="section-title">Budgets</h2>
        {/* live: fetched from Azure on every visit, unlike the rest of this report (snapshot-based) */}
        <span className="live-badge" title="Fetched live from Azure, not from the report snapshot">Live</span>
      </div>
      <p className="section-subtitle">Create, edit, and delete Azure Cost Management budgets directly for the selected subscriptions.</p>
      {error && <p className="workflow-error" role="alert">{error}</p>}
      <form
        className="budget-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label><span>Subscription</span>
        <select
          aria-label="Budget subscription"
          required
          disabled={!!editing}
          value={form.subscriptionId}
          onChange={(event) => setForm((current) => ({ ...current, subscriptionId: event.target.value }))}
        >
          <option value="">Select subscription</option>
          {report.subscriptionBreakdown.map((subscription) => (
            <option value={subscription.subscriptionId} key={subscription.subscriptionId}>{subscription.subscriptionName}</option>
          ))}
        </select>
        </label>
        <label><span>Budget name</span>
        <input
          required
          disabled={!!editing}
          placeholder="Budget name"
          aria-label="Budget name"
          value={form.name}
          onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
        />
        </label>
        <label><span>Amount</span>
        <input
          required
          type="number"
          min="0"
          step="0.01"
          placeholder="0.00"
          aria-label="Budget amount"
          value={form.amount || ''}
          onChange={(event) => setForm((current) => ({ ...current, amount: Number(event.target.value) }))}
        />
        </label>
        <label><span>Recurrence</span>
        <select
          aria-label="Budget recurrence"
          value={form.timeGrain}
          onChange={(event) => setForm((current) => ({ ...current, timeGrain: event.target.value as BudgetTimeGrain }))}
        >
          <option value="Monthly">Monthly</option>
          <option value="Quarterly">Quarterly</option>
          <option value="Annually">Annually</option>
        </select>
        </label>
        <label><span>Start date</span>
        <input
          required
          type="date"
          aria-label="Budget start date"
          value={form.startDate}
          onChange={(event) => setForm((current) => ({ ...current, startDate: event.target.value }))}
        />
        </label>
        <label><span>Alert threshold (%)</span>
        <input
          type="number"
          min="1"
          max="1000"
          placeholder="Optional"
          aria-label="Budget alert threshold percentage"
          value={form.alertThresholdPercent ?? ''}
          onChange={(event) => setForm((current) => ({
            ...current,
            alertThresholdPercent: event.target.value === '' ? null : Number(event.target.value),
          }))}
        />
        </label>
        <label><span>Alert email</span>
        <input
          type="email"
          placeholder="Optional"
          aria-label="Budget alert email"
          value={form.alertEmail ?? ''}
          onChange={(event) => setForm((current) => ({ ...current, alertEmail: event.target.value }))}
        />
        </label>
        <div className="budget-form-actions">
          <button type="submit" disabled={saving}>{saving ? <RefreshCw className="spin" size={16} aria-hidden="true" /> : editing ? <Check size={16} aria-hidden="true" /> : <Plus size={16} aria-hidden="true" />}{saving ? 'Saving...' : editing ? 'Save changes' : 'Add budget'}</button>
          {editing && <button type="button" onClick={cancelEdit}><X size={16} aria-hidden="true" />Cancel</button>}
        </div>
      </form>
      {loading ? (
        <EvidenceState loading title="Checking budgets" detail="Loading budgets..." />
      ) : budgets.length === 0 ? (
        <EvidenceState title="No budgets in this scope" detail="No budgets defined yet for the selected subscriptions." />
      ) : (
        <table className="report-table budget-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Subscription</th>
              <th className="num">Amount</th>
              <th className="num">Current spend</th>
              <th className="num">Forecast spend</th>
              <th>Projected outcome</th>
              <th>Time grain</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {budgets.map((budget) => {
              const pctUsed = budget.amount && budget.currentSpend !== null ? budget.currentSpend / budget.amount : 0;
              const status = pctUsed >= 1 ? 'over' : pctUsed >= 0.85 ? 'at_risk' : 'on_track';
              const forecastDelta = budget.forecastSpend !== null ? budget.forecastSpend - budget.amount : null;
              const key = `${budget.subscriptionId}-${budget.name}`;
              const drillable = report.costDetails?.status === 'complete';
              return (
                <Fragment key={key}>
                <tr>
                  <td>{drillable
                    ? <button type="button" className="finding-link" aria-expanded={openBudget === key} aria-label={`Daily spend for ${budget.name}`} onClick={() => setOpenBudget((value) => value === key ? null : key)}>{budget.name}</button>
                    : budget.name}</td>
                  <td>{subscriptionNames.get(budget.subscriptionId) ?? budget.subscriptionId}</td>
                  <td className="num">{formatNative(budget.amount, budget.currency)}</td>
                  <td className="num">
                    {formatNative(budget.currentSpend, budget.currency)}
                    <span className="budget-progress-track">
                      <span className={`budget-progress-fill budget-progress-${status}`} style={{ width: `${Math.min(100, pctUsed * 100)}%` }} />
                    </span>
                  </td>
                  <td className="num">{formatNative(budget.forecastSpend, budget.currency)}</td>
                  <td>
                    {forecastDelta === null ? (
                      <span className="budget-outcome budget-outcome-unknown">Forecast unavailable</span>
                    ) : forecastDelta > 0 ? (
                      <span className="budget-outcome budget-outcome-overrun" title="Projected end-of-period spend exceeds the budget">
                        <TrendingUp size={14} aria-hidden="true" /> Overrun {formatNative(forecastDelta, budget.currency)}
                      </span>
                    ) : (
                      <span className="budget-outcome budget-outcome-surplus" title="Projected end-of-period spend stays under the budget">
                        <TrendingDown size={14} aria-hidden="true" /> Surplus {formatNative(Math.abs(forecastDelta), budget.currency)}
                      </span>
                    )}
                  </td>
                  <td>{budget.timeGrain}</td>
                  <td className="budget-actions">
                    <button type="button" onClick={() => edit(budget)}>Edit</button>
                    <button type="button" onClick={() => void remove(budget)}>Delete</button>
                  </td>
                </tr>
                {drillable && openBudget === key && (
                  <tr><td colSpan={8}><BudgetDailyChart budget={budget} details={report.costDetails} window={costWindow} formatMoney={formatMoney} /></td></tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function ActionPlanTab({
  report,
  formatMoney,
  displayCurrency,
  snapshotId,
}: {
  report: Pick<FullReport, 'actionPlan'>;
  formatMoney: MoneyFormatter;
  displayCurrency: string;
  snapshotId: string | null;
}) {
  type ActionDraft = Pick<FinOpsActionState, 'status' | 'owner' | 'dueDate' | 'realizedSavingMonth' | 'note'> & { expectedVersion: number };
  const emptyDraft = (): ActionDraft => ({ status: 'open', owner: '', dueDate: null, realizedSavingMonth: null, note: '', expectedVersion: 0 });
  const toDraft = (state: FinOpsActionState): ActionDraft => ({ status: state.status, owner: state.owner, dueDate: state.dueDate, realizedSavingMonth: state.realizedSavingMonth, note: state.note, expectedVersion: state.version ?? 0 });
  const [drafts, setDrafts] = useState<Record<string, ActionDraft>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [loadedSnapshot, setLoadedSnapshot] = useState<string | null>(null);
  const [loadingActions, setLoadingActions] = useState(false);
  const [mustReload, setMustReload] = useState(false);
  const [dirtyIds, setDirtyIds] = useState<Set<string>>(new Set());
  const [reloadKey, setReloadKey] = useState(0);
  const generation = useRef(0);
  const pendingSave = useRef<number | null>(null);

  useEffect(() => {
    const currentGeneration = ++generation.current;
    const base = Object.fromEntries(report.actionPlan.filter((item) => item.actionId).map((item) => [item.actionId, emptyDraft()]));
    setDrafts(base);
    setWorkflowError(null);
    setLoadedSnapshot(null);
    setSaving(null);
    setSaved(null);
    setMustReload(false);
    setDirtyIds(new Set());
    pendingSave.current = null;
    setLoadingActions(Boolean(snapshotId));
    if (!snapshotId) return;
    const controller = new AbortController();
    getFinOpsActions(snapshotId, controller.signal)
      .then((states) => {
        if (controller.signal.aborted || generation.current !== currentGeneration) return;
        setDrafts({ ...base, ...Object.fromEntries(states.filter(state => state.actionId in base).map(state => [state.actionId, toDraft(state)])) });
        setLoadedSnapshot(snapshotId);
      })
      .catch((error) => {
        if (controller.signal.aborted || generation.current !== currentGeneration) return;
        setWorkflowError(error instanceof Error ? error.message : 'Action workflow is unavailable.');
      }).finally(() => {
        if (!controller.signal.aborted && generation.current === currentGeneration) setLoadingActions(false);
      });
    return () => { controller.abort(); generation.current += 1; };
  }, [report.actionPlan, snapshotId, reloadKey]);

  function updateDraft(actionId: string, update: Partial<ActionDraft>) {
    setDrafts((current) => ({ ...current, [actionId]: { ...(current[actionId] ?? emptyDraft()), ...update } }));
    setSaved(null);
    setDirtyIds(current => new Set(current).add(actionId));
  }

  async function save(actionId: string) {
    if (!snapshotId || loadedSnapshot !== snapshotId || loadingActions || mustReload || pendingSave.current !== null) return;
    const currentGeneration = generation.current;
    pendingSave.current = currentGeneration;
    setSaving(actionId);
    setWorkflowError(null);
    try {
      const state = await updateFinOpsAction(snapshotId, actionId, drafts[actionId] ?? emptyDraft());
      if (generation.current !== currentGeneration) return;
      setDrafts(current => ({ ...current, [actionId]: toDraft(state) }));
      setDirtyIds(current => { const next = new Set(current); next.delete(actionId); return next; });
      setSaved(actionId);
    } catch (error) {
      if (generation.current !== currentGeneration) return;
      setWorkflowError(error instanceof Error ? error.message : 'Action update failed. Reload saved actions before retrying.');
      setMustReload(true);
    } finally {
      if (pendingSave.current === currentGeneration) pendingSave.current = null;
      if (generation.current === currentGeneration) setSaving(null);
    }
  }

  function reloadActions() {
    if (dirtyIds.size && !window.confirm('Reloading replaces your unsaved action edits with the saved values. Continue?')) return;
    setReloadKey(current => current + 1);
  }

  return (
    <div className="panel">
      <h2 className="section-title">Consolidated Action Plan</h2>
      <p className="section-subtitle">Phased plan, highest monthly saving first. Workflow updates are audited against the persisted report scope.</p>
      {!snapshotId && report.actionPlan.length > 0 && <p className="workflow-notice">Run or reload a persisted report to track action status.</p>}
      {snapshotId && <button className="outline-command" type="button" onClick={reloadActions} disabled={loadingActions || saving !== null}><RefreshCw size={14} /> Reload saved actions</button>}
      {loadingActions && <p role="status">Loading saved actions...</p>}
      {workflowError && <p className="workflow-error" role="alert">{workflowError}</p>}
      {saved && <p role="status">Action saved.</p>}
      {report.actionPlan.length === 0 ? (
        <EvidenceState title="No tracked actions" detail="No actions for the selected subscriptions." />
      ) : (
        <div className="action-plan-scroll" role="region" aria-label="Action plan table" tabIndex={0}><table className="report-table action-plan-table finops-action-table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Subscriptions</th>
              <th className="num">Saving ({displayCurrency}/mo)</th>
              <th>Pre-requisite</th>
              <th>Status</th>
              <th>Owner</th>
              <th>Due</th>
              <th className="num">Realized ({displayCurrency}/mo)</th>
              <th>Note</th>
              <th><span className="sr-only">Save</span></th>
            </tr>
          </thead>
          <tbody>
            {report.actionPlan.map((item) => {
              const draft = drafts[item.actionId] ?? emptyDraft();
              const editable = Boolean(snapshotId && item.actionId && loadedSnapshot === snapshotId && !loadingActions && saving !== item.actionId);
              return <tr key={item.actionId || item.action}>
                <td>{item.action}</td>
                <td><SubscriptionReferences subscriptions={item.affectedSubscriptions} /></td>
                <td className="num">{formatMoney(item.savingMonth)}</td>
                <td>{item.prerequisite}</td>
                <td><select disabled={!editable} aria-label={`Status for ${item.action}`} value={draft.status} onChange={(event) => updateDraft(item.actionId, { status: event.target.value as ActionDraft['status'] })}><option value="open">Open</option><option value="in_progress">In progress</option><option value="completed">Completed</option><option value="dismissed">Dismissed</option></select></td>
                <td><input disabled={!editable} aria-label={`Owner for ${item.action}`} value={draft.owner} placeholder="Owner" onChange={(event) => updateDraft(item.actionId, { owner: event.target.value })} /></td>
                <td><input disabled={!editable} aria-label={`Due date for ${item.action}`} type="date" value={draft.dueDate ?? ''} onChange={(event) => updateDraft(item.actionId, { dueDate: event.target.value || null })} /></td>
                <td><input disabled={!editable} type="number" min="0" step="0.01" value={draft.realizedSavingMonth ?? ''} aria-label={`Realized saving for ${item.action}`} onChange={(event) => updateDraft(item.actionId, { realizedSavingMonth: event.target.value === '' ? null : Number(event.target.value) })} /></td>
                <td><input disabled={!editable} aria-label={`Note for ${item.action}`} value={draft.note} placeholder="Evidence note" onChange={(event) => updateDraft(item.actionId, { note: event.target.value })} /></td>
                <td><button type="button" disabled={!editable || saving !== null || mustReload} onClick={() => void save(item.actionId)}>{saving === item.actionId ? <RefreshCw className="spin" size={14} /> : saved === item.actionId ? <Check size={14} /> : <FileCode2 size={14} />}<span className="sr-only">Save {item.action}</span></button></td>
              </tr>;
            })}
          </tbody>
        </table></div>
      )}
    </div>
  );
}
