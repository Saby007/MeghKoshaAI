// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createBudget, updateBudget, getCostAnomalies, getExchangeRates, getRateOptimization, getResourceAvailability, getServiceRetirements, listBudgets } from '../api';
import { anomalyFixture, detailReportFixture, pricingReportFixture, reportFixture, resourceReportFixture, tagReportFixture } from '../report/testFixtures';
import { ReportView } from './ReportView';
import type { Budget, FullReport } from '../report/models';
import { compareCostGroups, dailySubscriptionCosts } from '../report/costDetails';
import { CostComparisonChart } from './CostExplorer';
import { BudgetContext, BudgetExpiry, budgetCycle, budgetFilterMatches, budgetThreshold } from './BudgetContext';

vi.mock('../api', async (importOriginal) => ({ ...await importOriginal<typeof import('../api')>(), createBudget: vi.fn(), updateBudget: vi.fn(), getCostAnomalies: vi.fn(), getExchangeRates: vi.fn(), getRateOptimization: vi.fn(), getResourceAvailability: vi.fn(), getServiceRetirements: vi.fn(), listBudgets: vi.fn() }));
let container: HTMLDivElement;
let root: Root;
const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === label)!;
const tagSelect = (label: string) => container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
/* Rows for actual tag values. The table also carries a trailing row for spend the key
   does not cover, which is a summary of the selection rather than a member of it, so
   counting it here would make "how many values match" depend on whether the estate
   happens to be fully tagged. */
const tagValueRows = (scope: ParentNode = container) =>
  scope.querySelectorAll('.app-cost-table tbody tr:not(.tag-cost-unallocated)');
const chooseTagOption = async (label: string, text: string) => {
  const select = tagSelect(label);
  const option = [...select.options].find((item) => item.textContent === text)!;
  await act(async () => { select.value = option.value; select.dispatchEvent(new Event('change', { bubbles: true })); });
};
beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.mocked(getCostAnomalies).mockResolvedValue(anomalyFixture);
  vi.mocked(listBudgets).mockResolvedValue([]);
  vi.mocked(getExchangeRates).mockRejectedValue(new Error('Synthetic offline fixture'));
  vi.mocked(getRateOptimization).mockRejectedValue(new Error('Synthetic purchase recommendations unavailable'));
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('filters managed budgets by expiry and forecast while preserving search and edit actions', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-09T05:00:00Z'));
  try {
    const base = { subscriptionId: 'sub-1', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-01-01', currentSpend: 20, forecastSpend: 30 };
    vi.mocked(listBudgets).mockResolvedValue([
      { ...base, name: 'Soon', periodEnd: '2026-11-08' },
      { ...base, name: 'Past', periodEnd: '2026-10-08' },
      { ...base, name: 'Open', periodEnd: '' },
      { ...base, name: 'Forecast', periodEnd: '2036-10-01', forecastSpend: 120 },
      { ...base, name: 'Over', periodEnd: '2027-01-01', currentSpend: 110 },
      { ...base, name: 'Invalid', periodEnd: 'invalid-date' },
    ]);
    await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="budget-polish" />));
    await act(async () => button('Budgets').click());
    const table = () => container.querySelector('[aria-label="Managed budgets"]');
    const names = () => [...(table()?.querySelectorAll('tbody tr') ?? [])].map((row) => row.firstElementChild?.textContent);
    const filter = container.querySelector<HTMLSelectElement>('[aria-label="Budget attention filter"]')!;
    const choose = async (value: string) => act(async () => { filter.value = value; filter.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(names()).toHaveLength(6);
    expect(table()?.querySelector('thead')?.textContent).toContain('End date');
    await choose('expiring');
    expect(names()).toEqual(['Soon']);
    await choose('expired');
    expect(names()).toEqual(['Past']);
    await choose('forecast');
    expect(names()).toEqual(['Forecast']);
    await choose('over');
    expect(names()).toEqual(['Over']);
    await choose('open');
    expect(names()).toEqual(['Open']);
    await choose('all');
    const search = container.querySelector<HTMLInputElement>('[aria-label="Search managed budgets"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'soon');
      search.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(names()).toEqual(['Soon']);
    await act(async () => button('Edit').click());
    expect(container.querySelector<HTMLInputElement>('[aria-label="Budget name"]')?.value).toBe('Soon');
    await choose('expired');
    expect(table()).toBeNull();
    expect(container.textContent).toContain('No budgets match these filters');
    await act(async () => button('Clear budget filters').click());
    expect(names()).toHaveLength(6);
  } finally {
    vi.useRealTimers();
  }
});

it('shows scope and snapshot context separately from live data on every report page', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="scope-polish" costWindow={{ startDate: '2026-09-01', endDate: '2026-09-07' }} />));
  const summary = container.querySelector('[aria-label="Report scope summary"]')!;
  expect(summary.textContent).toContain('1 subscription');
  expect(summary.textContent).toContain('UTC');
  expect(summary.textContent).toContain('Snapshot:');
  await act(async () => button('Budgets').click());
  expect(container.querySelector('[aria-label="Report scope summary"]')).toBe(summary);
  expect(container.querySelector('.live-badge')).not.toBeNull();
});

it('loads and submits the budget end date when editing, rejecting dates before the start', async () => {
  const budget = { subscriptionId: 'sub-1', name: 'Finance', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '2027-09-01T00:00:00Z', currentSpend: 10, forecastSpend: 20 };
  vi.mocked(listBudgets).mockResolvedValue([budget]);
  vi.mocked(updateBudget).mockResolvedValue({ ...budget, periodEnd: '2028-09-01' });
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="form-report" />));
  await act(async () => button('Budgets').click());
  await act(async () => button('Edit').click());
  const end = container.querySelector<HTMLInputElement>('[aria-label="Budget end date"]')!;
  expect(end.value).toBe('2027-09-01');
  const changeEnd = async (value: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(end, value);
    end.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await changeEnd('2026-08-01');
  await act(async () => container.querySelector('form.budget-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(updateBudget).not.toHaveBeenCalled();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('end date must be after');
  await changeEnd('2028-09-01');
  await act(async () => container.querySelector('form.budget-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(updateBudget).toHaveBeenCalledWith('sub-1', 'Finance', expect.objectContaining({ endDate: '2028-09-01' }));
});

it('submits the chosen expiry date when adding a budget', async () => {
  const created = { subscriptionId: 'sub-1', name: 'New-budget', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '2027-09-01', currentSpend: 0, forecastSpend: null };
  vi.mocked(createBudget).mockResolvedValue(created);
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="form-report" />));
  await act(async () => button('Budgets').click());
  for (const [label, value] of [['Budget name', 'New-budget'], ['Budget amount', '100'], ['Budget start date', '2026-09-01'], ['Budget end date', '2027-09-01']]) {
    const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }
  await act(async () => container.querySelector('form.budget-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(createBudget).toHaveBeenCalledWith(expect.objectContaining({ name: 'New-budget', endDate: '2027-09-01' }));
  expect(container.querySelector<HTMLInputElement>('[aria-label="Budget end date"]')?.value).toBe('');
});

it('reports an ignored end date and switches to editing rather than offering to recreate the saved budget', async () => {
  const saved = { subscriptionId: 'sub-1', name: 'Demo', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-10-01', periodEnd: '2036-10-01T00:00:00Z', currentSpend: null, forecastSpend: null };
  vi.mocked(createBudget).mockResolvedValue(saved);
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="ignored-end-date" />));
  await act(async () => button('Budgets').click());
  for (const [label, value] of [['Budget name', 'Demo'], ['Budget amount', '100'], ['Budget start date', '2026-10-01'], ['Budget end date', '2026-12-01']]) {
    const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }
  await act(async () => container.querySelector('form.budget-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('was saved, but the requested end date 2026-12-01 was not applied');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('2036-10-01');
  expect(container.querySelector<HTMLInputElement>('[aria-label="Budget end date"]')?.value).toBe('2026-12-01');
  expect(button('Save changes')).toBeDefined();
  expect(button('Add budget')).toBeUndefined();
  expect(container.querySelector<HTMLInputElement>('[aria-label="Budget name"]')?.disabled).toBe(true);
});

it('shows the actual budget expiry date without timezone shifts or invented dates', async () => {
  await act(async () => root.render(<BudgetExpiry periodEnd="2027-03-31T00:00:00Z" />));
  expect(container.querySelector('time')?.dateTime).toBe('2027-03-31');
  expect(container.textContent).toContain('2027');
  await act(async () => root.render(<BudgetExpiry periodEnd="" />));
  expect(container.textContent).toBe('Open-ended');
  await act(async () => root.render(<BudgetExpiry periodEnd="2027-02-30" />));
  expect(container.textContent).toBe('Unavailable');
});

it('flags the inclusive 30-day expiry window on the Budgets tab, including today and expired budgets', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-02T10:59:59Z'));
  try {
    const budget = { subscriptionId: 'sub-1', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-01-01', currentSpend: 10, forecastSpend: 20 };
    vi.mocked(listBudgets).mockResolvedValue([
      { ...budget, name: 'Thirty days', periodEnd: '2026-11-01T00:00:00Z' },
      { ...budget, name: 'Thirty-one days', periodEnd: '2026-11-02' },
      { ...budget, name: 'Today', periodEnd: '2026-10-02' },
      { ...budget, name: 'Past', periodEnd: '2026-10-01' },
      { ...budget, name: 'No end', periodEnd: '' },
    ]);
    await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="expiry-report" />));
    expect(container.querySelector('.budget-expiry-warning')).toBeNull();
    await act(async () => button('Budgets').click());
    const rows = [...container.querySelectorAll('.budget-table tbody tr')];
    const expiry = (name: string) => rows.find((row) => row.firstElementChild?.textContent === name)?.querySelector('.budget-expiry');
    expect(expiry('Thirty days')?.classList.contains('budget-expiry-warning')).toBe(true);
    expect(expiry('Thirty days')?.textContent).toContain('30 days left');
    expect(expiry('Thirty-one days')?.classList.contains('budget-expiry-warning')).toBe(false);
    expect(expiry('Today')?.textContent).toContain('Expires today');
    expect(expiry('Past')?.textContent).toContain('Expired');
    expect(expiry('No end')?.textContent).toBe('Open-ended');
    expect(expiry('No end')?.classList.contains('budget-expiry-warning')).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

it('shows point data on hover and keyboard focus while preserving day drilldown', async () => {
  const costWindow = { startDate: '2026-09-01', endDate: '2026-09-07' };
  const onSelect = vi.fn();
  const format = (value: number) => `$${value.toFixed(2)}`;
  await act(async () => root.render(<CostComparisonChart details={detailReportFixture.costDetails} window={costWindow} formatMoney={format} onSelectDay={onSelect} showDailyValues={false} chartHeight={160} />));
  const series = dailySubscriptionCosts(detailReportFixture.costDetails, costWindow)[0];
  const day = series.days[0];
  const dot = container.querySelector<SVGCircleElement>(`[data-cost-date="${day.date}"]`)!;
  await act(async () => dot.dispatchEvent(new Event('pointerover', { bubbles: true })));
  const tooltip = container.querySelector('[role="tooltip"]')!;
  expect(tooltip.textContent).toContain(series.subscriptionName);
  expect(tooltip.textContent).toContain(day.date);
  expect(tooltip.textContent).toContain(day.previousDate);
  expect(tooltip.textContent).toContain(format(day.current!));
  expect(tooltip.textContent).toContain(format(day.previous!));
  expect(dot.getAttribute('aria-describedby')).toBe(tooltip.id);
  await act(async () => dot.dispatchEvent(new Event('pointerout', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).toBeNull();
  await act(async () => dot.dispatchEvent(new FocusEvent('focusin', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).not.toBeNull();
  await act(async () => dot.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).toBeNull();
  await act(async () => dot.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  expect(onSelect).toHaveBeenCalledWith(day.date, day.previousDate, series.subscriptionId);
  await act(async () => dot.dispatchEvent(new FocusEvent('focusin', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).not.toBeNull();
  await act(async () => root.render(<CostComparisonChart details={detailReportFixture.costDetails} window={{ startDate: '2026-09-02', endDate: '2026-09-08' }} formatMoney={format} onSelectDay={onSelect} showDailyValues={false} chartHeight={160} />));
  expect(container.querySelector('[role="tooltip"]')).toBeNull();
});

it('moves comparison focus within a subscription without selecting a day', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<CostComparisonChart details={detailReportFixture.costDetails}
    window={{ startDate: '2026-09-01', endDate: '2026-09-07' }} formatMoney={(value) => `$${value.toFixed(2)}`} onSelectDay={onSelect} showDailyValues={false} />));
  const dots = container.querySelector('circle')!.parentElement!.querySelectorAll<SVGCircleElement>('circle');
  await act(async () => dots[0].focus());
  await act(async () => dots[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
  expect(document.activeElement).toBe(dots[1]);
  await act(async () => dots[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })));
  expect(document.activeElement).toBe(dots[dots.length - 1]);
  expect(onSelect).not.toHaveBeenCalled();
  await act(async () => dots[dots.length - 1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  expect(onSelect).toHaveBeenCalledTimes(1);
});

it('does not invent previous cost or change in a point tooltip with missing history', async () => {
  const costWindow = { startDate: '2026-09-01', endDate: '2026-09-07' };
  const original = detailReportFixture.costDetails!;
  const previousDate = dailySubscriptionCosts(original, costWindow)[0].days[0].previousDate;
  const details = {
    ...original,
    dates: original.dates.filter((date) => date !== previousDate),
    rows: original.rows.map((row) => ({ ...row, dailyCosts: Object.fromEntries(Object.entries(row.dailyCosts).filter(([date]) => date !== previousDate)) })),
  };
  await act(async () => root.render(<CostComparisonChart details={details} window={costWindow} formatMoney={(value) => `$${value.toFixed(2)}`} showDailyValues={false} />));
  await act(async () => container.querySelector('[data-cost-date]')!.dispatchEvent(new FocusEvent('focusin', { bubbles: true })));
  expect([...container.querySelectorAll('[role="tooltip"] dd')].map((cell) => cell.textContent).slice(1)).toEqual(['Unavailable', 'Unavailable']);
});

it('combines the financial overview into exactly five cards and updates period amounts with filters', async () => {
  const costWindow = { startDate: '2026-09-01', endDate: '2026-09-07' };
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" costWindow={costWindow} />));
  const metrics = container.querySelector('.executive-financial-metrics')!;
  expect(metrics.children).toHaveLength(5);
  expect([...metrics.children].map((card) => card.querySelector('span, .kpi-label')?.textContent?.trim())).toEqual([
    'Selected period spend', 'Previous period', 'Estimated wastage', 'Potential savings', 'Cost spikes',
  ]);
  expect(container.querySelector('[data-dashboard-section="cost-comparison"] .cost-window-metrics')).toBeNull();
  expect(metrics.querySelector('.assessed-month-context')?.textContent).toContain('Assessed month:');
  const expected = (filters = {}) => compareCostGroups(detailReportFixture.costDetails, costWindow, filters).reduce((sum, row) => sum + (row.current ?? 0), 0);
  const selectedAmount = () => Number(metrics.querySelector('output')!.textContent!.replace(/[^\d.-]/g, ''));
  expect(selectedAmount()).toBeCloseTo(expected(), 2);
  const service = container.querySelector<HTMLSelectElement>('[aria-label="Cost service"]')!;
  await act(async () => { service.value = 'Virtual Machines'; service.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(selectedAmount()).toBeCloseTo(expected({ serviceName: 'Virtual Machines' }), 2);
  expect(metrics.children).toHaveLength(5);
});

it('keeps unavailable period evidence explicit in the merged financial cards', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  expect(container.querySelector('.executive-financial-metrics output')?.textContent).toBe('Unavailable');
  expect(container.querySelector('.executive-financial-metrics .period-anomaly-link strong')?.textContent).toBe('Unavailable');
});

it('draws the comparison chart shorter on the executive summary than where it is the whole subject', async () => {
  const costWindow = { startDate: '2026-09-01', endDate: '2026-09-07' };
  const chartHeight = () => Number(container.querySelector('.cost-comparison-chart')!.getAttribute('viewBox')!.split(' ')[3]);
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" costWindow={costWindow} onCostWindowChange={() => {}} />));
  const executive = chartHeight();
  // The same chart on the anomalies tab, where it is the subject, keeps its full height.
  await act(async () => button('Cost Anomalies').click());
  const full = chartHeight();
  expect(full).toBeGreaterThanOrEqual(300);
  expect(executive).toBeLessThan(full - 80);
});
it('puts the cost comparison above the spend distribution on the executive summary', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  const order = [...container.querySelectorAll('[data-dashboard-section]')].map((section) => section.getAttribute('data-dashboard-section'));
  expect(order.indexOf('cost-comparison')).toBeGreaterThan(-1);
  expect(order.indexOf('cost-comparison')).toBeLessThan(order.indexOf('spend-distribution-overview'));
  expect(order.indexOf('cost-overview')).toBeLessThan(order.indexOf('cost-comparison'));
});
it('presents signals, services and prioritised findings as front-page sections without disclosures', async () => {
  const report = {
    ...reportFixture,
    operationalSignals: [{ key: 'checks', label: 'Checks', value: '0', detail: 'No issues found', tone: 'positive' }],
    topServices: [{ rank: 1, serviceName: 'Microsoft.Compute', displayName: 'Compute', monthlySpend: 2200, pctOfTotal: 0.7 }],
  };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  for (const id of ['operational-signals', 'top-services', 'prioritised-findings']) {
    const section = container.querySelector(`[data-dashboard-section="${id}"]`)!;
    expect(section.tagName).toBe('SECTION');
    expect(section.closest('.executive-report')).not.toBeNull();
    expect(section.closest('.executive-evidence-grid')).not.toBeNull();
    expect(section.querySelector('summary')).toBeNull();
    expect(section.querySelector('h2')).not.toBeNull();
  }
  expect(container.querySelector('.anomaly-overview')).toBeNull();
  expect(container.textContent).not.toContain('Spend by resource type');
  expect(container.querySelector('.executive-donut-panel')).toBeNull();
  expect(container.textContent).not.toContain('Report details');
  expect(container.querySelector('.report-evidence-details')).toBeNull();
  expect(container.querySelector('.executive-evidence-grid')?.classList.contains('has-findings')).toBe(report.prioritizedFindings.length > 0);
});
it('keeps populated findings and multi-subscription evidence in the organised landing page', async () => {
  const report: FullReport = {
    ...reportFixture,
    subscriptionBreakdown: [
      ...reportFixture.subscriptionBreakdown,
      { ...reportFixture.subscriptionBreakdown[0], subscriptionId: 'sub-2', subscriptionName: 'Second subscription' },
    ],
    prioritizedFindings: [{
      rank: 1, category: 'unattached_disks', finding: 'Review unattached disks',
      evidence: 'Verified billed cost', impactType: 'potential_savings',
      monthlySaving: 20, monthlyCostAtRisk: null, severity: 'Medium',
    }],
  };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  expect(container.querySelector('.executive-evidence-grid.has-findings')).not.toBeNull();
  expect(container.querySelector('[data-dashboard-section="prioritised-findings"] .executive-findings-table')?.textContent).toContain('Review unattached disks');
  expect(container.querySelector('[data-dashboard-section="subscriptions"]')?.textContent).toContain('Second subscription');
  expect(container.querySelector('[data-dashboard-section="top-services"]')).toBeNull();
  expect(container.querySelector('[data-dashboard-section="operational-signals"]')).toBeNull();
});
it('uses one persistent left navigation across report pages', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  const navigation = container.querySelector('nav[aria-label="Report navigation"]')!;
  expect(navigation.classList.contains('report-sidenav')).toBe(true);
  expect(container.querySelector('.report-topnav, .category-pill-row, [aria-label="Report areas"]')).toBeNull();
  await act(async () => button('History').click());
  expect(container.querySelector('nav[aria-label="Report navigation"]')).toBe(navigation);
  expect(navigation.querySelector('[data-current="true"] .report-sidenav-group-toggle')?.textContent?.trim()).toBe('Cost Management');
  await act(async () => button('Compute Optimization').click());
  expect(container.querySelector('#report-page-heading')?.textContent).toBe('Compute Optimization');
  expect(container.querySelectorAll('nav[aria-label="Report navigation"]')).toHaveLength(1);
});

it('navigates actionable cost overview cards and leaves informational cards static', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  expect(container.querySelector('[aria-label="Open cost history"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Open stale and orphaned resources"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Open savings roadmap"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Open confirmed idle resources"]')).not.toBeNull();
  const labels = [...container.querySelectorAll('.kpi-label')];
  expect(labels.find((label) => label.textContent === 'Other billed resources')?.closest('button')).toBeNull();
  expect(labels.find((label) => label.textContent === 'Advisor score')?.closest('button')).toBeNull();
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Open savings roadmap"]')!.click());
  expect(container.querySelector('#report-page-heading')?.textContent).toBe('Savings Roadmap');
});

it('does not present zero waste or zero confirmed idle resources as risk cards', async () => {
  const report = { ...reportFixture, executiveSummary: { ...reportFixture.executiveSummary, estimatedWastageMonth: 0 } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  for (const label of ['Open stale and orphaned resources', 'Open confirmed idle resources']) {
    expect(container.querySelector(`[aria-label="${label}"]`)?.classList.contains('risk')).toBe(false);
    expect(container.querySelector(`[aria-label="${label}"]`)?.classList.contains('neutral')).toBe(true);
  }
  expect(container.querySelector('[aria-label="Open confirmed idle resources"]')?.textContent).toContain('3 candidates require evidence or owner review');
});

it('searches and filters budgets, and flags forecast overruns that are still within budget', async () => {
  const base = { subscriptionId: 'sub-1', category: 'Cost', currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '', filter: {} } as const;
  const budgets = [
    { ...base, name: 'Platform', amount: 100, currentSpend: 20, forecastSpend: 140 },
    { ...base, name: 'Finance', amount: 100, currentSpend: 115, forecastSpend: 150 },
    { ...base, name: 'Sandbox', amount: 100, currentSpend: 10, forecastSpend: 40 },
    { ...base, name: 'Legacy', amount: 100, currentSpend: null, forecastSpend: null },
  ];
  await act(async () => root.render(<BudgetContext details={detailReportFixture.costDetails} state={{ budgets, loading: false, error: null, refresh: vi.fn() }} />));
  const names = () => [...container.querySelectorAll('.budget-table tbody th')].map((cell) => cell.firstChild?.textContent);
  expect(names()).toEqual(['Platform', 'Finance', 'Sandbox', 'Legacy']);
  expect(container.querySelector('.table-count')?.textContent).toBe('4 of 4 budgets');
  const platform = [...container.querySelectorAll('.budget-table tbody tr')].find((row) => row.textContent?.includes('Platform'))!;
  expect([...platform.querySelectorAll('.budget-status')].map((badge) => badge.textContent)).toEqual(['Within budget', 'Forecast over']);
  await act(async () => button('Needs attention (2)').click());
  expect(names()).toEqual(['Platform', 'Finance']);
  await act(async () => button('On track').click());
  expect(names()).toEqual(['Sandbox']);
  await act(async () => button('Unavailable').click());
  expect(names()).toEqual(['Legacy']);
  await act(async () => button('All').click());
  const search = container.querySelector<HTMLInputElement>('[aria-label="Search budgets"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'fin');
    search.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(names()).toEqual(['Finance']);
  expect(container.querySelector('.table-count')?.textContent).toBe('1 of 4 budgets');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'nothing');
    search.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(container.textContent).toContain('No budgets match this search or status.');
});

it('retries a failed budget read without claiming the scope is empty or losing a form draft', async () => {
  vi.mocked(listBudgets).mockRejectedValue(new Error('Synthetic lookup denied.'));
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Budgets').click());
  expect(container.textContent).toContain('Budget lookup unavailable');
  expect(container.textContent).not.toContain('No budgets in this scope');
  const name = container.querySelector<HTMLInputElement>('[aria-label="Budget name"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(name, 'Keep this draft');
    name.dispatchEvent(new Event('input', { bubbles: true }));
  });
  vi.mocked(listBudgets).mockResolvedValue([financeBudget]);
  await act(async () => button('Retry budget lookup').click());
  expect(container.textContent).not.toContain('Budget lookup unavailable');
  expect(container.querySelector('.budget-management-scroll')?.textContent).toContain('Finance app budget');
  expect(name.value).toBe('Keep this draft');
  expect(createBudget).not.toHaveBeenCalled();
  expect(updateBudget).not.toHaveBeenCalled();
});

it('bounds budget loading and ignores a late response after timeout', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  let finish!: (budgets: Budget[]) => void;
  const pending = new Promise<Budget[]>(resolve => { finish = resolve; });
  vi.mocked(listBudgets).mockReturnValue(pending);
  try {
    await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
    await act(async () => button('Budgets').click());
    expect(container.textContent).toContain('Checking budgets');
    await act(async () => vi.advanceTimersByTime(15000));
    expect(container.textContent).toContain('Azure budget lookup timed out.');
    expect(container.textContent).not.toContain('No budgets in this scope');
    await act(async () => finish([financeBudget]));
    expect(container.querySelector('.budget-management-scroll')).toBeNull();
    expect(container.textContent).toContain('Budget lookup unavailable');
  } finally {
    vi.useRealTimers();
  }
});

it('keeps budget write errors visible when only the list is refreshed', async () => {
  vi.mocked(createBudget).mockRejectedValue(new Error('Synthetic save failed.'));
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Budgets').click());
  await act(async () => container.querySelector('.budget-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(container.textContent).toContain('Synthetic save failed.');
  await act(async () => button('Refresh budgets').click());
  expect(container.textContent).toContain('Synthetic save failed.');
  expect(createBudget).toHaveBeenCalledOnce();
});

it('starts an empty-scope budget by focusing the form rather than creating anything', async () => {
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Budgets').click());
  await act(async () => button('Start a budget').click());
  await act(async () => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
  expect(document.activeElement?.getAttribute('aria-label')).toBe('Budget name');
  expect(createBudget).not.toHaveBeenCalled();
});

it('labels managed-budget card fields and retains a single accessible daily-spend control', async () => {
  vi.mocked(listBudgets).mockResolvedValue([financeBudget]);
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Budgets').click());
  const table = container.querySelector('.budget-management-scroll .mobile-card-table')!;
  expect(table.getAttribute('role')).toBe('table');
  const headers = [...table.querySelectorAll('thead th')].map(header => header.textContent);
  expect([...table.querySelectorAll('tbody td[data-label]')].map(cell => cell.getAttribute('data-label'))).toEqual(headers);
  const daily = table.querySelector<HTMLButtonElement>('[aria-label="Daily spend for Finance app budget"]')!;
  expect(table.querySelectorAll('[aria-label="Edit budget Finance app budget"]')).toHaveLength(1);
  await act(async () => daily.click());
  expect(table.querySelector('.mobile-card-detail')?.getAttribute('role')).toBe('row');
  expect(table.querySelector('.mobile-card-detail td')?.getAttribute('role')).toBe('cell');
  expect(createBudget).not.toHaveBeenCalled();
  expect(updateBudget).not.toHaveBeenCalled();
});

it('offers a read-only budget context retry even when the section heading is hidden', async () => {
  const refresh = vi.fn();
  await act(async () => root.render(<BudgetContext state={{ budgets: [], loading: false, error: 'Synthetic budget lookup failed.', refresh }} showHeading={false} />));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Synthetic budget lookup failed.');
  expect(container.textContent).not.toContain('No matching subscription-scope budgets were returned.');
  await act(async () => button('Retry subscription budgets').click());
  expect(refresh).toHaveBeenCalledOnce();
  expect(createBudget).not.toHaveBeenCalled();
});

it('resets only local budget-context filters from its no-match state', async () => {
  const refresh = vi.fn();
  await act(async () => root.render(<BudgetContext state={{ budgets: [financeBudget], loading: false, error: null, refresh }} />));
  const input = container.querySelector<HTMLInputElement>('[aria-label="Search budgets"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'no match');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => button('Reset budget context filters').click());
  expect(input.value).toBe('');
  expect(container.querySelector('.budget-table')?.textContent).toContain(financeBudget.name);
  expect(refresh).not.toHaveBeenCalled();
});

it('retries the selected recommendation scenario without changing scope or creating budgets', async () => {
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Rate Optimization').click());
  expect(container.textContent).toContain('Recommendations unavailable');
  const before = vi.mocked(getRateOptimization).mock.calls.length;
  await act(async () => button('Retry recommendation lookup').click());
  expect(getRateOptimization).toHaveBeenCalledTimes(before + 1);
  const calls = vi.mocked(getRateOptimization).mock.calls;
  expect(calls[calls.length - 1].slice(0, 4)).toEqual(calls[calls.length - 2].slice(0, 4));
  expect(createBudget).not.toHaveBeenCalled();
});

it('keeps budget matching, forecast variance and status thresholds explicit', async () => {
  const budget = { subscriptionId: 'sub-1', name: 'Finance', category: 'Cost', amount: 100, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '', currentSpend: 105, forecastSpend: 140 };
  expect(budgetThreshold({ ...budget, currentSpend: 100 }).tone).toBe('within');
  expect(budgetThreshold({ ...budget, currentSpend: 110 }).tone).toBe('warning');
  expect(budgetThreshold({ ...budget, currentSpend: 111 }).tone).toBe('over');
  expect(budgetThreshold({ ...budget, currentSpend: null }).tone).toBe('unknown');
  const row = detailReportFixture.costDetails!.rows[0];
  expect(budgetFilterMatches({ tags: { name: 'Application', operator: 'In', values: ['Finance'] } }, row)).toBe(true);
  expect(budgetFilterMatches({ tags: { name: 'application', operator: 'In', values: ['Other'] } }, row)).toBe(false);
  expect(budgetFilterMatches({ dimensions: { name: 'UnknownDimension', operator: 'In', values: ['x'] } }, row)).toBeNull();
  expect(budgetFilterMatches(undefined, row)).toBeNull();
  await act(async () => root.render(<BudgetContext details={detailReportFixture.costDetails} state={{ budgets: [budget], loading: false, error: null, refresh: vi.fn() }} />));
  expect(container.textContent).toContain('Filter applicability unverified');
  expect(container.textContent).toContain('$5.00 over');
  expect(container.textContent).toContain('Overrun $40.00');
  expect(container.textContent).toContain('Expiry: Open-ended');
  expect(budgetThreshold(budget).label).toBe('Over by 5%');
  // A monthly budget checked on 30 Sep reports September to date, not the report month.
  expect(budgetCycle({ ...budget, periodStart: '2026-01-01' }, '2026-09-30T10:00:00Z')).toEqual({ start: '2026-09-01', end: '2026-09-30' });
  expect(budgetCycle({ ...budget, timeGrain: 'Quarterly', periodStart: '2026-01-01' }, '2026-08-15')).toEqual({ start: '2026-07-01', end: '2026-09-30' });
  expect(budgetCycle({ ...budget, timeGrain: 'Annually', periodStart: '2025-04-01' }, '2026-02-10')).toEqual({ start: '2025-04-01', end: '2026-03-31' });
  expect(budgetCycle({ ...budget, periodStart: '2027-01-01' }, '2026-09-30')).toBeNull();
});

it('drills from a selected period into day resources, preserves the range for anomalies, and replaces savings breakdown', async () => {
  // The cost window is now owned by App and presented once in the saved-report
  // strip, so the range arrives as a prop rather than being set from a control
  // inside the report. The behaviour under test is unchanged: the same window
  // drives the period drilldown and the anomalies tab.
  const costWindow = { startDate: '2026-09-01', endDate: '2026-09-07' };
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" costWindow={costWindow} onCostWindowChange={() => {}} />));
  expect(container.querySelector('.cost-chart-previous')?.getAttribute('d')).toContain('M');
  await act(async () => container.querySelector<SVGElement>('[data-cost-date="2026-09-07"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  const drilldown = container.querySelector('[aria-label="Selected day resource detail"]')!;
  expect(drilldown.textContent).toContain('2026-09-07 vs 2026-08-31');
  expect(drilldown.textContent).toContain('finance-vm');
  expect(drilldown.textContent).toContain('Finance team');
  expect(drilldown.querySelector('.cost-change-cell.cost-increase')).not.toBeNull();
  expect(getResourceAvailability).not.toHaveBeenCalled();
  vi.mocked(getResourceAvailability).mockResolvedValue({ resourceId: detailReportFixture.costDetails!.rows[0].resourceId, date: '2026-09-07', status: 'partial', statusMessage: 'Missing minutes remain unknown', availableHours: null, observedAvailableHours: 12, coverageMinutes: 720, expectedMinutes: 1440, metric: 'VmAvailabilityMetric', observedAt: '2026-09-12T00:00:00Z' });
  await act(async () => drilldown.querySelector<HTMLButtonElement>('[aria-label^="Check VM availability"]')!.click());
  expect(drilldown.textContent).toContain('12.00 h observed (partial)');
  expect(drilldown.textContent).toContain('720/1440 minutes');
  // A non-zero spike count is red, not the amber used for a mere warning.
  expect(container.querySelector('.period-anomaly-link strong')?.className).toBe('metric-count-risk');
  expect(container.querySelector('.period-anomaly-link strong')?.className).not.toContain('warn');
  await act(async () => container.querySelector<HTMLButtonElement>('.period-anomaly-link')!.click());
  expect(container.querySelector('[aria-label="Selected period anomalies"]')?.textContent).toContain('finance-vm');
  // The 7-day window supplied above is what the anomalies tab compares against.
  expect(container.querySelector('[aria-label="Selected period anomalies"]')?.textContent).toContain('preceding 7-day window');
  await act(async () => button('Subscription Breakdown').click());
  expect(container.textContent).not.toContain('Savings by Subscription');
  const grouping = container.querySelector<HTMLSelectElement>('[aria-label="Cost grouping"]')!;
  await act(async () => { grouping.value = 'service'; grouping.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.querySelector('[aria-label="Grouped subscription costs"]')?.textContent).toContain('Virtual Machines');
});

it('opens billing views from the full report and reuses main-screen anomaly results', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  expect(container.querySelector('[aria-label="Cost anomaly overview"]')).toBeNull();
  await act(async () => button('History').click());
  expect(container.querySelector('[aria-label="Billing history comparison"]')).not.toBeNull();
  expect(container.querySelector('#report-page-heading')?.textContent).toBe('History');
  expect(container.querySelector<HTMLDetailsElement>('.report-context-details')?.open).toBe(false);
  expect(container.querySelector('.report-context-details')?.textContent).toContain('Assessed month');
  expect(container.querySelectorAll('.report-context-details')).toHaveLength(1);
  expect(container.querySelector('.report-context-details')?.textContent).toContain('Saved snapshot');
  expect(container.querySelector('.report-context-details')?.textContent).toContain('EffectiveCost');
  expect(container.querySelector('.currency-provenance')?.closest('.report-context-details')).not.toBeNull();
  await act(async () => button('Cost by Hour').click());
  expect(container.querySelector('[aria-label="Billing day filter"]')).not.toBeNull();
  await act(async () => button('Executive Summary').click());
  await act(async () => button('Cost Anomalies').click());
  expect(container.textContent).toContain('Detected signals');
  expect(getCostAnomalies).toHaveBeenCalledOnce();
});

it('checks service retirements only on request and distinguishes failed sources from an empty result', async () => {
  vi.mocked(getServiceRetirements).mockResolvedValue({ snapshotId: 'visual-report-1', observedAt: '2026-09-12T00:00:00Z', costPeriod: '2026-08', currency: 'USD', notices: [], sources: [{ subscriptionId: 'sub-1', available: false, message: 'Advisor is unavailable.' }] });
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Governance & Risk').click());
  expect(getServiceRetirements).not.toHaveBeenCalled();
  await act(async () => button('Check service retirements').click());
  expect(getServiceRetirements).toHaveBeenCalledWith('visual-report-1', undefined, expect.any(AbortSignal));
  expect(container.querySelector('[aria-label="Service retirements"]')?.textContent).toContain('Advisor is unavailable');
  expect(container.textContent).not.toContain('No resource-specific retirements were returned');
});

it('inspects monthly totals with cents, zero and credits without inventing missing categories', async () => {
  const report: FullReport = { ...reportFixture, spendHistory: { ...reportFixture.spendHistory, months: [
    { month: '2026-06', total: 1200.12, categorySpend: { Compute: 1200, Other: 0.12 }, subscriptionSpend: {}, subscriptionCategorySpend: {} },
    { month: '2026-07', total: 0, categorySpend: { Storage: 0 }, subscriptionSpend: {}, subscriptionCategorySpend: {} },
    { month: '2026-08', total: -9.25, categorySpend: { Storage: -9.25 }, subscriptionSpend: {}, subscriptionCategorySpend: {} },
  ] } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  const panel = container.querySelector('.executive-history-panel')!;
  const months = panel.querySelectorAll<HTMLButtonElement>('.monthly-column');
  await act(async () => months[0].focus());
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$1,200.12');
  expect([...panel.querySelectorAll('.chart-inspection dt')].map(item => item.textContent)).toEqual(['Reported total', 'Compute', 'Other']);
  await act(async () => months[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
  expect(document.activeElement).toBe(months[1]);
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$0.00');
  await act(async () => months[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })));
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('-$9.25');
  expect(panel.querySelector('.monthly-credit')).not.toBeNull();
  expect(panel.querySelectorAll('.monthly-zero')).toHaveLength(3);
  await act(async () => months[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(panel.querySelector('.chart-inspection')).toBeNull();
});

it('keeps a zero-only history at the baseline and distinguishes credit-only attribution from missing data', async () => {
  const base = detailReportFixture.costDetails!.rows[0];
  const report: FullReport = { ...reportFixture,
    spendHistory: { ...reportFixture.spendHistory, status: 'partial', months: [
      { month: '2026-08', total: 0, categorySpend: { Compute: 0 }, subscriptionSpend: {}, subscriptionCategorySpend: {} },
    ] },
    costHierarchy: [{ ...base, monthlySpend: -10, pctOfTotal: 0 }],
  };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  expect(container.querySelector<HTMLElement>('.monthly-stack')!.style.top).toBe('100%');
  expect(container.querySelector<HTMLElement>('.monthly-stack')!.style.height).toBe('0%');
  await act(async () => container.querySelector<HTMLButtonElement>('.monthly-column')!.focus());
  expect(container.querySelector('.executive-history-panel .chart-inspection small')?.textContent).toContain('partial coverage');
  expect(container.querySelector('.executive-treemap-panel')?.textContent).toContain('zero and credit entries are not tiled');
  expect(container.querySelector('.executive-treemap-panel')?.textContent).not.toContain('Resource-level cost attribution is not available.');
});

it('retains sub-dollar hierarchy entries and preserves subscription-to-resource navigation', async () => {
  const base = detailReportFixture.costDetails!.rows[0];
  const report = { ...reportFixture, costHierarchy: [
    { ...base, monthlySpend: 100, pctOfTotal: 0.9995 },
    { ...base, subscriptionId: 'tiny', subscriptionName: 'Tiny subscription', resourceGroup: 'tiny-group',
      resourceId: '/subscriptions/tiny/resourceGroups/tiny-group/providers/Microsoft.Compute/disks/tiny',
      resourceName: 'Tiny resource', monthlySpend: 0.05, pctOfTotal: 0.0005 },
  ] };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  const panel = container.querySelector('.executive-treemap-panel')!;
  const tiny = [...panel.querySelectorAll<HTMLButtonElement>('.treemap-cell')].find(node => node.textContent?.includes('Tiny subscription'))!;
  await act(async () => tiny.focus());
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$0.05');
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('<0.1%');
  await act(async () => tiny.click());
  const group = panel.querySelector<HTMLButtonElement>('.treemap-cell')!;
  expect(group.textContent).toContain('tiny-group');
  await act(async () => group.click());
  const resource = panel.querySelector<HTMLAnchorElement>('a.treemap-cell')!;
  expect(resource.textContent).toContain('Tiny resource');
  expect(resource.href).toContain('portal.azure.com');
  expect(resource.target).toBe('_blank');
});

it('uses every attributed positive entry as the share denominator when showing the top 24 tiles', async () => {
  const base = detailReportFixture.costDetails!.rows[0];
  const costHierarchy = Array.from({ length: 25 }, (_, index) => ({
    ...base, subscriptionId: `sub-${index}`, subscriptionName: `Subscription ${index}`, monthlySpend: 100, pctOfTotal: 0.04,
  }));
  await act(async () => root.render(<ReportView report={{ ...reportFixture, costHierarchy }} narration={null} snapshotId="visual-report-1" />));
  const panel = container.querySelector('.executive-treemap-panel')!;
  expect(panel.querySelectorAll('.treemap-cell')).toHaveLength(24);
  expect(panel.textContent).toContain('24 of 25 entries');
  await act(async () => panel.querySelector<HTMLButtonElement>('.treemap-cell')!.focus());
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('4.0%');
});

it('keeps all regional entries readable, including unmapped locations and tiny reported shares', async () => {
  const regions = ['eastus', 'eastus2', 'westus', 'westus2', 'centralus', 'centralindia', 'southindia', 'westeurope',
    'northeurope', 'japaneast', 'australiaeast', 'global', 'custom-location'];
  const regionSpend = regions.map((region, index) => ({ region, monthlySpend: index === 0 ? 0.05 : 100.12, pctOfTotal: index === 0 ? 0.00001 : 0.08 }));
  await act(async () => root.render(<ReportView report={{ ...reportFixture, regionSpend }} narration={null} snapshotId="visual-report-1" />));
  const panel = container.querySelector('.executive-region-panel')!;
  expect(panel.querySelectorAll('.region-ranking button')).toHaveLength(13);
  const dots = panel.querySelectorAll<SVGCircleElement>('circle');
  await act(async () => dots[0].focus());
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$0.05');
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('<0.1%');
  const firstRow = panel.querySelector<HTMLButtonElement>('.region-ranking button')!;
  await act(async () => firstRow.focus());
  await act(async () => dots[0].dispatchEvent(new Event('pointerout', { bubbles: true })));
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$0.05');
  await act(async () => dots[0].focus());
  await act(async () => dots[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
  expect(document.activeElement).toBe(dots[1]);
  const custom = [...panel.querySelectorAll<HTMLButtonElement>('.region-ranking button')].find(row => row.textContent?.includes('custom-location'))!;
  await act(async () => custom.click());
  expect(panel.querySelector('.chart-inspection')?.parentElement).toBe(panel);
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('Global or unmapped; included in ranking');
  expect(panel.querySelector('.chart-inspection')?.textContent).toContain('$100.12');
});

it('displays sub-cent distribution values without turning them into zero', async () => {
  const regionSpend = [
    { region: 'global', monthlySpend: 0.00001, pctOfTotal: 0.00001 },
    { region: 'custom-location', monthlySpend: 0.000000001, pctOfTotal: 0.000000001 },
  ];
  await act(async () => root.render(<ReportView report={{ ...reportFixture, regionSpend }} narration={null} snapshotId="visual-report-1" />));
  const panel = container.querySelector('.executive-region-panel')!;
  const rows = panel.querySelectorAll<HTMLButtonElement>('.region-ranking button');
  expect(rows[0].textContent).toContain('$0.00001');
  await act(async () => rows[1].focus());
  expect(panel.querySelector('.chart-inspection dd')?.textContent).toBe('Spend < $0.00000001');
});

it('shows trend, subscriptions and regions together with hierarchy drilldown and no repeated overview content', async () => {
  const report = { ...reportFixture, costHierarchy: detailReportFixture.costDetails!.rows.map((row) => ({ ...row, monthlySpend: 100, pctOfTotal: 0.5 })) };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  const distribution = container.querySelector('.executive-distribution')!;
  const trend = distribution.querySelector('.executive-history-panel')!;
  const footprint = distribution.querySelector('.executive-visual-grid')!;
  expect(trend).not.toBeNull();
  expect(footprint.querySelector('.cost-treemap')).not.toBeNull();
  expect(footprint.querySelector('.region-map')).not.toBeNull();
  expect(distribution.querySelector('[hidden]')).toBeNull();
  expect(distribution.querySelector('[aria-label="Spend distribution view"]')).toBeNull();
  expect(container.querySelector('.cost-analysis-details')).toBeNull();
  expect(container.querySelector('[data-dashboard-section="prioritised-findings"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Time range"]')).toBeNull();
  expect(container.querySelector('.report-context-details .summary-strip')).toBeNull();
  expect([...container.querySelectorAll('.executive-takeaways .takeaway')].some((item) => item.textContent?.startsWith('Spend '))).toBe(false);
  expect(container.querySelector('[data-dashboard-section="spend-distribution-overview"] .dashboard-section-figure')).toBeNull();
  expect(container.textContent).not.toContain('Month to date');
  expect(container.querySelector('nav[aria-label="Report navigation"] [aria-current="page"]')?.textContent?.trim()).toBe('Executive Summary');
  expect(container.textContent).toContain('No prioritised findings in this snapshot');
  const node = footprint.querySelector<HTMLButtonElement>('.treemap-cell')!;
  await act(async () => node.click());
  const level = footprint.querySelector('.treemap-breadcrumb')!.textContent;
  expect(level).not.toBe('Subscriptions');
  expect(distribution.querySelector('.executive-history-panel')).toBe(trend);
  expect(distribution.querySelector('[hidden]')).toBeNull();
  expect(getCostAnomalies).toHaveBeenCalledOnce();
});

it('keeps the daily ledger expanded with subscription IDs and reveals comparison drilldown from a day', async () => {
  await act(async () => root.render(<ReportView report={detailReportFixture} narration={null} snapshotId="visual-report-1" costWindow={{ startDate: '2026-09-01', endDate: '2026-09-07' }} />));
  const ledger = container.querySelector<HTMLDetailsElement>('.cost-daily-values')!;
  expect(ledger.open).toBe(true);
  expect(ledger.querySelector('.daily-cost-group-name strong')?.textContent).toBe('Demo subscription');
  expect(ledger.querySelector('.daily-cost-group-name small')?.textContent).toBe('Subscription ID: sub-1');
  const heading = container.querySelector<HTMLHeadingElement>('#dashboard-section-cost-comparison')!;
  heading.scrollIntoView = vi.fn();
  const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 0; });
  try {
    await act(async () => ledger.querySelector<HTMLButtonElement>('.finding-link')!.click());
    expect(container.querySelector('[aria-label="Selected day resource detail"]')?.textContent).toContain('finance-vm');
    expect(heading.scrollIntoView).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(heading);
  } finally {
    frame.mockRestore();
  }
});

it('shows no graph or budget for a key alone and charts the chosen value over the report window', async () => {
  const window = { startDate: '2026-09-01', endDate: '2026-09-07' };
  await act(async () => root.render(<ReportView report={applicationTagReport} narration={null} snapshotId="visual-report-1" costWindow={window} onCostWindowChange={vi.fn()} />));
  await act(async () => button('Cost by Tags/Application').click());

  // Filters lead the page; the old tag-set pie and all-values trend are gone.
  expect(container.querySelector('.executive-donut-panel')).toBeNull();
  expect(container.querySelector('[aria-label="Daily cost trend"]')).toBeNull();
  const panel = container.querySelector('.panel')!;
  const controls = panel.querySelector('.tag-cost-controls')!;
  expect(panel.querySelector('.tag-selection')).toBeNull();
  expect(panel.querySelector('.tag-selection-hint')!.textContent).toContain('Choose a value for application above');
  expect(controls.compareDocumentPosition(panel.querySelector('.tag-selection-hint')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  await chooseTagOption('Tag value', 'Finance');
  const selection = panel.querySelector('.tag-selection')!;
  expect(selection.querySelector('h3')!.textContent).toBe('application = Finance');
  // Finance alone across the seven days, not the subscription.
  expect(selection.textContent).toContain('$1,632.00 in the selected period');
  expect(selection.querySelector('[aria-label="Daily cost for application = Finance"]')).not.toBeNull();
  expect(panel.querySelector('.tag-selection-hint')).toBeNull();

  await chooseTagOption('Tag value', 'All values');
  expect(panel.querySelector('.tag-selection')).toBeNull();

  await act(async () => button('Cost by Hour').click());
  expect(container.querySelector('[aria-label="Cost by hour"]')?.textContent).toContain('2026-09-01 - 2026-09-07');
});

it('ranks tag keys and states what one explains from its rows, not from a stored zero', async () => {
  // A snapshot written before the API carried the figure holds 0, not a missing value.
  const staleSnapshot = {
    ...tagReportFixture,
    tagCosts: {
      ...tagReportFixture.tagCosts,
      dimensions: tagReportFixture.tagCosts.dimensions.map((item) => ({ ...item, allocatedCost: 0, coverage: 0 })),
    },
  };
  await act(async () => root.render(<ReportView report={staleSnapshot} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags/Application').click());
  expect([...tagSelect('Tag key').options].map((option) => option.textContent)).toEqual(['Environment', 'Team']);
  const explains = container.querySelector('.tag-cost-coverage')!.textContent!;
  expect(explains).toContain('Environment explains $250.00 of $250.00');
  expect(explains).not.toContain('%');
});

it('filters tag costs by the selected key and value without changing financial evidence', async () => {
  await act(async () => root.render(<ReportView report={tagReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags/Application').click());
  // Keys open most-explanatory first: Environment (250) before Team (200).
  expect([...tagSelect('Tag key').options].map((option) => option.value)).toEqual(['Environment', 'Team']);
  await chooseTagOption('Tag key', 'Team');
  expect(tagSelect('Tag value')).not.toBeNull();
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values', 'Platform', 'Sales']);
  const table = container.querySelector('.app-cost-table')!;
  expect(tagValueRows(table)).toHaveLength(2);
  const originalSalesRow = tagValueRows(table)[1];
  const originalEvidence = originalSalesRow.textContent;
  const originalBarWidth = originalSalesRow.querySelector<HTMLElement>('.app-cost-bar-fill')!.style.width;

  await chooseTagOption('Tag value', 'Sales');
  expect(tagValueRows(table)).toHaveLength(1);
  expect(table.querySelector('tbody tr')!.textContent).toBe(originalEvidence);
  expect(table.querySelector<HTMLElement>('.app-cost-bar-fill')!.style.width).toBe(originalBarWidth);

  await chooseTagOption('Tag value', 'All values');
  expect(tagValueRows(table)).toHaveLength(2);
  await chooseTagOption('Tag value', 'Sales');
  await chooseTagOption('Tag key', 'Environment');
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values', 'Production']);
  expect(tagSelect('Tag value').selectedOptions[0].textContent).toBe('All values');
  expect(tagValueRows(table)).toHaveLength(1);
  expect(table.querySelector('tbody tr')!.textContent).toContain('Production');
  expect(table.querySelector('tbody tr')!.textContent).not.toContain('Sales');
});

it('falls back to all values after report changes and allows selecting values on the fallback key', async () => {
  await act(async () => root.render(<ReportView report={tagReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags/Application').click());
  await chooseTagOption('Tag key', 'Team');
  await chooseTagOption('Tag value', 'Sales');
  const team = tagReportFixture.tagCosts.dimensions[0];
  const refreshedReport = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...team, rows: [team.rows[0]] }],
  } };
  await act(async () => root.render(<ReportView report={refreshedReport} narration={null} snapshotId="visual-report-1" />));
  expect(tagSelect('Tag value').selectedOptions[0].textContent).toBe('All values');
  expect(container.querySelector('.app-cost-table tbody')!.textContent).toContain('Platform');

  const changedKeyReport = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...team, tagKey: 'Owner' }],
  } };
  await act(async () => root.render(<ReportView report={changedKeyReport} narration={null} snapshotId="visual-report-1" />));
  expect(tagSelect('Tag key').value).toBe('Owner');
  expect(tagSelect('Tag value').selectedOptions[0].textContent).toBe('All values');
  expect(tagValueRows()).toHaveLength(2);
  await chooseTagOption('Tag value', 'Sales');
  expect(tagValueRows()).toHaveLength(1);
  expect(container.querySelector('.app-cost-table tbody')!.textContent).toContain('Sales');
});

it.each(['', 'All values', 'Team "A" & Operations'])('matches the literal tag value %j without treating it as All values', async (value) => {
  const team = tagReportFixture.tagCosts.dimensions[0];
  const report = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...team, rows: [{ ...team.rows[0], value }, team.rows[1]] }],
  } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags/Application').click());
  const select = tagSelect('Tag value');
  expect(select.options[1].value).not.toBe(select.options[0].value);
  await act(async () => { select.value = select.options[1].value; select.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(tagValueRows()).toHaveLength(1);
  /* The empty value is labelled, not rendered as an empty cell: the picker has always
     shown it as "(empty)", and a blank cell beside a cost reads as a failure to render
     rather than as a resource tagged with nothing. The label is the picker's. */
  expect(container.querySelector('.app-cost-table tbody td')!.textContent).toBe(value || '(empty)');
});

it('preserves the missing-tag state and disables the value filter for a key without rows', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags/Application').click());
  expect(tagSelect('Tag value')).toBeNull();
  expect(container.textContent).toContain(reportFixture.tagCosts!.status);
  const report = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...tagReportFixture.tagCosts.dimensions[0], rows: [] }],
  } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  expect(tagSelect('Tag value').disabled).toBe(true);
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values']);
});

/* The tag page reports budgets "for this selection", so the evidence behind
   that claim has to be the selection - not the budget's whole subscription. */
const applicationTagReport = {
  ...detailReportFixture,
  tagCosts: {
    available: true, status: 'Available', totalSpend: 200, growthRate: null,
    dimensions: [{ tagKey: 'application', unallocatedCost: 0, rows: [
      { value: 'Finance', monthlyCost: 120, pctOfTotal: 0.6, forecastNextMonth: null },
      { value: 'Platform', monthlyCost: 80, pctOfTotal: 0.4, forecastNextMonth: null },
    ] }],
  },
};
const subscriptionWideBudget = { subscriptionId: 'sub-1', name: 'Subscription budget', category: 'Cost', amount: 3000, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '', currentSpend: 400, forecastSpend: 900, filter: {} };
const financeBudget = { subscriptionId: 'sub-1', name: 'Finance app budget', category: 'Cost', amount: 2000, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '', currentSpend: 300, forecastSpend: 700, filter: { tags: { name: 'application', operator: 'In', values: ['Finance'] } } };
const foreignSubscriptionBudget = { subscriptionId: 'sub-2', name: 'Unrelated subscription budget', category: 'Cost', amount: 500, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '', currentSpend: 100, forecastSpend: 200, filter: {} };
const renderTagBudgets = async (budgets: unknown[]) => {
  vi.mocked(listBudgets).mockResolvedValue(budgets as never);
  await act(async () => root.render(<ReportView report={applicationTagReport} narration={null} snapshotId="visual-report-1" costWindow={{ startDate: '2026-09-01', endDate: '2026-09-07' }} onCostWindowChange={() => {}} />));
  await act(async () => button('Cost by Tags/Application').click());
};

const dayBar = (date: string) => container.querySelector<HTMLButtonElement>(`.tag-selection .cost-bar-hit[title^="${date}"]`);
const dayContributors = () => container.querySelector('.tag-day-drilldown');

it('makes the application graph clickable and lists that day\'s contributors, with no budget needed', async () => {
  await renderTagBudgets([]);
  // A key alone has no graph, so nothing to click yet.
  expect(container.querySelector('.tag-selection')).toBeNull();

  await chooseTagOption('Tag value', 'Finance');
  expect(container.querySelector('.tag-cost-budgets')).toBeNull();
  expect(dayContributors()).toBeNull();

  await act(async () => dayBar('2026-09-07')!.click());
  const contributors = dayContributors()!;
  expect(contributors.getAttribute('aria-label')).toBe('Contributors on 2026-09-07 for application = Finance');
  // The resources behind that day, inside the selected application only.
  expect(contributors.textContent).toContain('finance-vm');
  expect(contributors.textContent).not.toContain('shared-disk');
  expect(dayBar('2026-09-07')!.getAttribute('aria-pressed')).toBe('true');

  // The same day again, or Close, puts it away.
  await act(async () => dayBar('2026-09-07')!.click());
  expect(dayContributors()).toBeNull();
  await act(async () => dayBar('2026-09-06')!.click());
  expect(dayContributors()!.getAttribute('aria-label')).toContain('2026-09-06');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close day contributors"]')!.click());
  expect(dayContributors()).toBeNull();
});

it('drops the chosen day when the application changes, so one application\'s day is never shown for another', async () => {
  await renderTagBudgets([]);
  await chooseTagOption('Tag value', 'Finance');
  await act(async () => dayBar('2026-09-07')!.click());
  expect(dayContributors()).not.toBeNull();
  await chooseTagOption('Tag value', 'Platform');
  expect(dayContributors()).toBeNull();
  await act(async () => dayBar('2026-09-07')!.click());
  expect(dayContributors()!.textContent).toContain('shared-disk');
  expect(dayContributors()!.textContent).not.toContain('finance-vm');
});
it('lists only budgets whose own filter names the chosen key and value', async () => {
  const contactBudget = { ...financeBudget, name: 'Contact budget', filter: { tags: { name: 'contact', operator: 'In', values: ['Finance'] } } };
  await renderTagBudgets([financeBudget, subscriptionWideBudget, foreignSubscriptionBudget, contactBudget]);
  // A key alone shows no budget at all.
  expect(container.querySelector('.tag-cost-budgets')).toBeNull();

  await chooseTagOption('Tag value', 'Finance');
  const budgets = container.querySelector('.tag-cost-budgets')!;
  expect(budgets.textContent).toContain('Budget for application = Finance');
  expect(budgets.textContent).toContain('Finance app budget');
  expect(budgets.textContent).toContain('Expiry: Open-ended');
  // Same-named budgets in different subscriptions are told apart by subscription.
  expect(budgets.textContent).toContain('Demo subscription');
  // Budgets that merely contain the spend are not this application's budget.
  expect(budgets.textContent).not.toContain('Subscription budget');
  expect(budgets.textContent).not.toContain('Unrelated subscription budget');
  // Same value, different key: not a match.
  expect(budgets.textContent).not.toContain('Contact budget');
  expect(budgets.querySelector('.wider-budgets')).toBeNull();

  // No budget names Platform, so none is shown - not a stand-in.
  await chooseTagOption('Tag value', 'Platform');
  expect(container.querySelector('.tag-selection')).not.toBeNull();
  expect(container.querySelector('.tag-cost-budgets')).toBeNull();
});

it('shows one application graph when a budget tracks it, with the budget share on that graph', async () => {
  await renderTagBudgets([financeBudget]);
  await chooseTagOption('Tag value', 'Finance');
  const selection = container.querySelector('.tag-selection')!;
  // One chart only: the budget does not add a second graph.
  expect(selection.querySelectorAll('svg')).toHaveLength(1);
  expect(selection.querySelector('.budget-daily-chart')).toBeNull();
  expect(selection.querySelector('.cost-chart-reference')).not.toBeNull();
  expect(selection.textContent).toContain('Finance app budget: even daily share of USD');
  // The budget's figures stay, as text on its card.
  const card = selection.querySelector('.tag-budget-card')!;
  expect(card.textContent).toContain('Finance app budget');
  expect(card.textContent).toContain('covered days for application = Finance');
  // Day drilldown comes from the one graph and stays inside the application.
  await act(async () => dayBar('2026-09-03')!.click());
  expect(dayContributors()!.textContent).toContain('finance-vm');
  expect(dayContributors()!.textContent).not.toContain('shared-disk');
});

it('draws no budget line when several budgets track the application, but keeps one graph', async () => {
  const second = { ...financeBudget, name: 'Finance stretch budget', amount: 2000 };
  await renderTagBudgets([financeBudget, second]);
  await chooseTagOption('Tag value', 'Finance');
  const selection = container.querySelector('.tag-selection')!;
  expect(selection.querySelectorAll('svg')).toHaveLength(1);
  expect(selection.querySelector('.cost-chart-reference')).toBeNull();
  expect(selection.querySelectorAll('.tag-budget-card')).toHaveLength(2);
  expect(selection.textContent).toContain('2 budgets for application = Finance');
  expect(selection.querySelector('.chart-context-note')?.textContent).toContain('No single budget line is shown');
});

it('matches a budget on a hyphenated tag key shown in display form', async () => {
  const report = { ...applicationTagReport, costDetails: { ...applicationTagReport.costDetails!, rows: applicationTagReport.costDetails!.rows.map((row) => ({ ...row, tags: { 'azd-env-name': row.tags.application } })) },
    tagCosts: { ...applicationTagReport.tagCosts, dimensions: [{ ...applicationTagReport.tagCosts.dimensions[0], tagKey: 'Azd Env Name' }] } };
  vi.mocked(listBudgets).mockResolvedValue([{ ...financeBudget, filter: { tags: { name: 'azd-env-name', operator: 'In', values: ['Finance'] } } }] as never);
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" costWindow={{ startDate: '2026-09-01', endDate: '2026-09-07' }} onCostWindowChange={() => {}} />));
  await act(async () => button('Cost by Tags/Application').click());
  await chooseTagOption('Tag value', 'Finance');
  // "Azd Env Name" in the report is `azd-env-name` on the rows and in the budget.
  expect(container.querySelector('.tag-selection')!.textContent).toContain('$1,632.00 in the selected period');
  expect(container.querySelector('.tag-cost-budgets')!.textContent).toContain('Finance app budget');
});

it('ignores a tag value inherited from a different tag key when scoping the selection', async () => {
  await renderTagBudgets([financeBudget]);
  await chooseTagOption('Cost tag key', 'owner');
  await chooseTagOption('Cost tag value', 'Finance team');
  await chooseTagOption('Tag value', 'Finance');
  // "owner = Finance team" must not be tested against the application key, which
  // matches nothing and previously emptied the evidence for the page.
  const selection = container.querySelector('.tag-selection')!;
  expect(selection.textContent).toContain('$1,632.00 in the selected period');
  expect(selection.textContent).not.toContain('No daily cost evidence');
});

it.each([true, false])('shows the same realized RI and Savings Plan evidence in EA Pricing and Rate Optimization when pricing is available: %s', async (available) => {
  const report = { ...pricingReportFixture, pricingSummary: { ...pricingReportFixture.pricingSummary, available } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('EA Pricing').click());
  const section = container.querySelector('[aria-label="Realized commitment activity"]')!;
  expect(section).not.toBeNull();
  expect(section.textContent).toContain('Reservations (RI) and Savings Plans');
  const rows = [...section.querySelectorAll('tbody tr')].map((row) => row.textContent);
  expect(rows[0]).toContain('Reservations (RI)$240$100$40');
  expect(rows[1]).toContain('Savings Plans$120$50$10');
  expect(rows[0]).toContain('4 FOCUS rows');
  expect(rows[1]).toContain('3 FOCUS rows');
  expect(section.textContent).toContain('2026-08');
  expect(section.textContent).toContain('Source currency USD');
  expect(section.textContent).toContain('unused commitment cost is never counted as savings');
  expect(getRateOptimization).not.toHaveBeenCalled();
  await act(async () => button('Rate Optimization').click());
  expect([...container.querySelectorAll('.commitment-table tbody tr')].map((row) => row.textContent)).toEqual(rows);
  expect(container.textContent).toContain('Synthetic purchase recommendations unavailable');
  expect(getRateOptimization).toHaveBeenCalledOnce();
});

it('keeps missing commitment activity explicit in EA Pricing rather than inventing benefits', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('EA Pricing').click());
  const section = container.querySelector('[aria-label="Realized commitment activity"]')!;
  expect(section.textContent).toContain('No realized commitment activity');
  expect(section.textContent).toContain(reportFixture.commitmentSummary.status);
  expect(section.querySelector('table')).toBeNull();
  expect(getRateOptimization).not.toHaveBeenCalled();
});

it('distinguishes missing evidence from empty findings and does not draw empty storage charts', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Storage Optimization').click());
  expect(container.querySelector('.storage-tier-comparison')).toBeNull();
  expect(container.querySelector('.storage-tier-analysis .evidence-state')?.textContent).toContain('No storage accounts');
  expect(container.textContent).toContain('No findings in this scope');
  await act(async () => button('Governance & Risk').click());
  expect(container.querySelector('.evidence-state')?.textContent).toContain('Governance evidence unavailable');
  expect(container.querySelector('.evidence-state')?.getAttribute('aria-busy')).toBe('false');
  await act(async () => button('Cost by Tags/Application').click());
  expect(container.querySelector('.evidence-state')?.textContent).toContain('Tag evidence unavailable');
});

it('filters stale resources by search, subscription, category, impact and evidence', async () => {
  const disk = resourceReportFixture.tierACategories[0];
  const stoppedVm = {
    ...disk,
    category: 'stopped_vms', displayName: 'Stopped virtual machines', impactType: 'cost_at_risk' as const,
    count: 1, monthlyTotal: 80, annualTotal: 960,
    lines: [{ ...disk.lines[0], category: 'stopped_vms', resourceId: '/subscriptions/sub-2/resourceGroups/operations/providers/Microsoft.Compute/virtualMachines/operations-vm', resourceName: 'operations-vm', subscriptionId: 'sub-2', subscriptionName: 'Operations subscription', monthlyCost: 80, evidenceType: 'metrics_verified_idle' as const, detail: 'Stopped VM with retained billed resources.' }],
  };
  const snapshot = {
    ...disk,
    category: 'old_snapshots', displayName: 'Old snapshots', impactType: 'inventory' as const,
    count: 1, monthlyTotal: 0, annualTotal: 0,
    lines: [{ ...disk.lines[0], category: 'old_snapshots', resourceId: '/subscriptions/sub-3/resourceGroups/archive/providers/Microsoft.Compute/snapshots/archive-snapshot', resourceName: 'archive-snapshot', subscriptionId: 'sub-3', subscriptionName: 'Archive subscription', monthlyCost: null, evidenceType: 'inventory_candidate' as const, detail: 'Snapshot exceeded the inventory age threshold.' }],
  };
  const report = { ...resourceReportFixture, tierACategories: [disk, stoppedVm, snapshot] };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Stale Resources').click());
  const rows = () => container.querySelectorAll('.stale-resource-table tbody tr');
  const select = (label: string) => container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
  const choose = async (label: string, value: string) => {
    const control = select(label);
    await act(async () => { control.value = value; control.dispatchEvent(new Event('change', { bubbles: true })); });
  };
  const clear = async () => { await act(async () => button('Clear filters').click()); };
  expect(rows()).toHaveLength(3);
  expect(container.querySelector('.billing-provenance[role="status"]')?.textContent).toContain('Showing 3 of 3');

  const search = container.querySelector<HTMLInputElement>('[aria-label="Find stale resource"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'snapshot');
    search.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain('archive-snapshot');
  await clear();

  await choose('Stale resource subscription', 'sub-2');
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain('operations-vm');
  expect([...container.querySelectorAll('.stale-kpi-grid .kpi-card')].find((card) => card.textContent?.includes('Billed cost at risk'))?.textContent).toContain('80');
  await clear();

  await choose('Stale resource category', 'old_snapshots');
  expect(rows()[0].textContent).toContain('Old snapshots');
  await clear();
  await choose('Stale resource impact', 'cost_at_risk');
  expect(rows()[0].textContent).toContain('operations-vm');
  await clear();
  await choose('Stale resource evidence', 'inventory_candidate');
  expect(rows()[0].textContent).toContain('archive-snapshot');

  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'no matching resource');
    search.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(container.querySelector('.stale-resource-table')).toBeNull();
  expect(container.textContent).toContain('No resources match these filters');
  await clear();
  expect(rows()).toHaveLength(3);
});