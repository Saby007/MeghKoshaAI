// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getCostAnomalies, getExchangeRates, getRateOptimization, getResourceAvailability, getServiceRetirements, listBudgets } from '../api';
import { anomalyFixture, detailReportFixture, pricingReportFixture, reportFixture, tagReportFixture } from '../report/testFixtures';
import { ReportView } from './ReportView';
import { BudgetContext, budgetFilterMatches, budgetThreshold } from './BudgetContext';

vi.mock('../api', async (importOriginal) => ({ ...await importOriginal<typeof import('../api')>(), getCostAnomalies: vi.fn(), getExchangeRates: vi.fn(), getRateOptimization: vi.fn(), getResourceAvailability: vi.fn(), getServiceRetirements: vi.fn(), listBudgets: vi.fn() }));
let container: HTMLDivElement;
let root: Root;
const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === label)!;
const tagSelect = (label: string) => container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
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

it('keeps the same visible category navigation across report tab switches', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  const navigation = container.querySelector('[aria-label="Spend by category"]')!;
  expect(navigation.closest('details')).toBeNull();
  await act(async () => button('History').click());
  expect(container.querySelector('[aria-label="Spend by category"]')).toBe(navigation);
  expect(navigation.closest('details')).toBeNull();
  await act(async () => navigation.querySelectorAll<HTMLButtonElement>('button')[1].click());
  expect(navigation.querySelector('[aria-pressed="true"]')?.textContent).toContain('Compute');
  expect(container.querySelector('#report-page-heading')?.textContent).toBe('Compute Optimization');
  expect(container.querySelectorAll('[aria-label="Spend by category"]')).toHaveLength(1);
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
  expect(container.textContent).toContain('USD -5');
  expect(container.textContent).toContain('Projected overrun: USD 40');
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
  expect(getResourceAvailability).not.toHaveBeenCalled();
  vi.mocked(getResourceAvailability).mockResolvedValue({ resourceId: detailReportFixture.costDetails!.rows[0].resourceId, date: '2026-09-07', status: 'partial', statusMessage: 'Missing minutes remain unknown', availableHours: null, observedAvailableHours: 12, coverageMinutes: 720, expectedMinutes: 1440, metric: 'VmAvailabilityMetric', observedAt: '2026-09-12T00:00:00Z' });
  await act(async () => drilldown.querySelector<HTMLButtonElement>('[aria-label^="Check VM availability"]')!.click());
  expect(drilldown.textContent).toContain('12.00 h observed (partial)');
  expect(drilldown.textContent).toContain('720/1440 minutes');
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
  expect(container.querySelector('[aria-label="Cost anomaly overview"]')?.textContent).toContain('Demo subscription');
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
  await act(async () => button('View all').click());
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

it('keeps the overview compact and mounts detailed visuals only on demand without dropping evidence', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  const details = container.querySelector<HTMLDetailsElement>('.cost-analysis-details')!;
  expect(details.open).toBe(false);
  expect(container.querySelector('.executive-visual-grid')).toBeNull();
  expect(container.querySelector('.cost-analysis-details .billing-filters')).toBeNull();
  expect(container.textContent).toContain('Assessed month');
  expect(container.textContent).not.toContain('Month to date');
  expect(container.querySelector('[aria-label="Spend by category"]')?.tagName).toBe('NAV');
  expect(container.querySelector('[aria-pressed="true"]')?.textContent).toContain('All spend');
  await act(async () => { details.open = true; details.dispatchEvent(new Event('toggle')); });
  const visuals = container.querySelector('.executive-visual-grid');
  expect(visuals).not.toBeNull();
  expect(container.textContent).toContain('No prioritised findings in this snapshot');
  expect(container.querySelector('[aria-label="Billing day filter"]')).not.toBeNull();
  await act(async () => { details.open = false; details.dispatchEvent(new Event('toggle')); });
  expect(container.querySelector('.executive-visual-grid')).toBe(visuals);
  expect(getCostAnomalies).toHaveBeenCalledOnce();
});

it('filters tag costs by the selected key and value without changing financial evidence', async () => {
  await act(async () => root.render(<ReportView report={tagReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags').click());
  expect(tagSelect('Tag value')).not.toBeNull();
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values', 'Platform', 'Sales']);
  const table = container.querySelector('.app-cost-table')!;
  expect(table.querySelectorAll('tbody tr')).toHaveLength(2);
  const originalSalesRow = table.querySelectorAll('tbody tr')[1];
  const originalEvidence = originalSalesRow.textContent;
  const originalBarWidth = originalSalesRow.querySelector<HTMLElement>('.app-cost-bar-fill')!.style.width;

  await chooseTagOption('Tag value', 'Sales');
  expect(table.querySelectorAll('tbody tr')).toHaveLength(1);
  expect(table.querySelector('tbody tr')!.textContent).toBe(originalEvidence);
  expect(table.querySelector<HTMLElement>('.app-cost-bar-fill')!.style.width).toBe(originalBarWidth);

  await chooseTagOption('Tag value', 'All values');
  expect(table.querySelectorAll('tbody tr')).toHaveLength(2);
  await chooseTagOption('Tag value', 'Sales');
  await chooseTagOption('Tag key', 'Environment');
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values', 'Production']);
  expect(tagSelect('Tag value').selectedOptions[0].textContent).toBe('All values');
  expect(table.querySelectorAll('tbody tr')).toHaveLength(1);
  expect(table.querySelector('tbody tr')!.textContent).toContain('Production');
  expect(table.querySelector('tbody tr')!.textContent).not.toContain('Sales');
});

it('falls back to all values after report changes and allows selecting values on the fallback key', async () => {
  await act(async () => root.render(<ReportView report={tagReportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags').click());
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
  expect(container.querySelectorAll('.app-cost-table tbody tr')).toHaveLength(2);
  await chooseTagOption('Tag value', 'Sales');
  expect(container.querySelectorAll('.app-cost-table tbody tr')).toHaveLength(1);
  expect(container.querySelector('.app-cost-table tbody')!.textContent).toContain('Sales');
});

it.each(['', 'All values', 'Team "A" & Operations'])('matches the literal tag value %j without treating it as All values', async (value) => {
  const team = tagReportFixture.tagCosts.dimensions[0];
  const report = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...team, rows: [{ ...team.rows[0], value }, team.rows[1]] }],
  } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags').click());
  const select = tagSelect('Tag value');
  expect(select.options[1].value).not.toBe(select.options[0].value);
  await act(async () => { select.value = select.options[1].value; select.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.querySelectorAll('.app-cost-table tbody tr')).toHaveLength(1);
  expect(container.querySelector('.app-cost-table tbody td')!.textContent).toBe(value);
});

it('preserves the missing-tag state and disables the value filter for a key without rows', async () => {
  await act(async () => root.render(<ReportView report={reportFixture} narration={null} snapshotId="visual-report-1" />));
  await act(async () => button('Cost by Tags').click());
  expect(tagSelect('Tag value')).toBeNull();
  expect(container.textContent).toContain(reportFixture.tagCosts!.status);
  const report = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts, dimensions: [{ ...tagReportFixture.tagCosts.dimensions[0], rows: [] }],
  } };
  await act(async () => root.render(<ReportView report={report} narration={null} snapshotId="visual-report-1" />));
  expect(tagSelect('Tag value').disabled).toBe(true);
  expect([...tagSelect('Tag value').options].map((option) => option.textContent)).toEqual(['All values']);
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
  await act(async () => button('Cost by Tags').click());
  expect(container.querySelector('.evidence-state')?.textContent).toContain('Tag evidence unavailable');
});