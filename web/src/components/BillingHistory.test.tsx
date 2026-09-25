// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { BillingHistoryTab, HourlyCostPanel } from './BillingHistory';
import { billingTagId, type BillingSource } from '../report/billingHistory';
import * as billingEvidence from '../report/billingHistory';
import { detailReportFixture } from '../report/testFixtures';

const report: BillingSource = {
  dailyCostTrend: { status: 'complete', statusMessage: 'Verified export dates', days: [
    { date: '2026-09-04', totalCost: 0, averageHourlyCost: 0 },
    { date: '2026-09-05', totalCost: 24, averageHourlyCost: 1 },
    { date: '2026-09-06', totalCost: 48, averageHourlyCost: 2 },
    { date: '2026-09-07', totalCost: 120, averageHourlyCost: 5 },
  ] },
  tagDailyCostTrend: { status: 'complete', statusMessage: '', tagKey: '', availableTagValues: ['A'],
    windowDates: ['2026-09-04', '2026-09-05', '2026-09-06', '2026-09-07'],
    series: [{ tagKey: 'Team', tagValue: 'A', totalCost: 999, days: [{ date: '2026-09-05', totalCost: 24, averageHourlyCost: 1 }] }],
  },
};
const format = (value: number) => `$${value.toFixed(2)}`;
let container: HTMLDivElement;
let root: Root;
const field = (label: string) => container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
const output = (label: string) => container.querySelector(`output[aria-label="${label}"]`)!.textContent;

async function change(label: string, value: string) {
  await act(async () => {
    const element = field(label);
    Object.getOwnPropertyDescriptor(element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); });

it('compares two selected dates, supports swapping and keeps zero-baseline percentages honest', async () => {
  await act(async () => root.render(<BillingHistoryTab report={report} formatMoney={format} />));
  expect(output('Billing cost change')).toBe('+$72.00');
  expect(container.querySelector('[aria-label="Billing cost change"]')?.classList.contains('cost-increase')).toBe(true);
  await change('Baseline billing date', '2026-09-04');
  expect(output('Baseline cost')).toBe('$0.00');
  expect(container.textContent).toContain('N/A (zero baseline)');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Swap billing dates"]')!.click());
  expect(output('Billing cost change')).toBe('$-120.00');
});

it('expands daily resource costs and compares the same raw tag across two dates', async () => {
  await act(async () => root.render(<HourlyCostPanel report={detailReportFixture} formatMoney={format} formatHourlyMoney={format} />));
  await change('Cost tag key', 'application');
  await change('Cost tag value', JSON.stringify('Finance'));
  await change('Hourly baseline date', '2026-09-06');
  await change('Hourly comparison date', '2026-09-07');
  const comparison = container.querySelector('[aria-label="Hourly resource date comparison"]')!;
  expect(comparison.textContent).toContain('finance-vm');
  expect(comparison.textContent).not.toContain('shared-disk');
  expect(comparison.textContent).toContain('$288.00');
  expect(comparison.textContent).toContain('$96.00');
  expect(comparison.textContent).toContain('200.0%');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Resource costs for 2026-09-07"]')!.click());
  expect(container.querySelectorAll('[aria-label="Resource cost comparison"]')).toHaveLength(2);
  expect(container.textContent).toContain('Not collected');
});

it('changes hourly totals and the denominator together for weekend and tag filters', async () => {
  await act(async () => root.render(<HourlyCostPanel report={report} formatMoney={format} formatHourlyMoney={format} />));
  await change('Billing day filter', 'weekends');
  expect(output('Filtered billed cost')).toBe('$72.00');
  expect(output('Filtered average hourly cost')).toBe('$1.50/hr');
  expect(output('Billing date coverage')).toBe('2 / 2');
  await change('Billing tag filter', billingTagId(report.tagDailyCostTrend.series[0]));
  expect(output('Filtered billed cost')).toBe('$24.00');
  expect(output('Filtered average hourly cost')).toBe('$0.50/hr');
  expect(container.textContent).toContain('billed cost / 24');
});

it('shows unavailable comparisons and incomplete hourly coverage instead of zero', async () => {
  const sparse = { ...report, dailyCostTrend: { ...report.dailyCostTrend, days: report.dailyCostTrend.days.filter((day) => day.date !== '2026-09-06') } };
  await act(async () => root.render(<BillingHistoryTab report={sparse} formatMoney={format} />));
  await change('Baseline billing date', '2026-09-06');
  expect(output('Baseline cost')).toBe('Unavailable');
  expect(output('Billing cost change')).toBe('Unavailable');
  await act(async () => root.render(<HourlyCostPanel report={sparse} formatMoney={format} formatHourlyMoney={format} />));
  await change('Billing day filter', 'weekends');
  expect(output('Billing date coverage')).toBe('1 / 2');
  expect(container.textContent).toContain('Incomplete coverage');
});

it('resets selected dates and tags when the report scope changes', async () => {
  await act(async () => root.render(<BillingHistoryTab report={report} formatMoney={format} />));
  await change('Billing tag filter', billingTagId(report.tagDailyCostTrend.series[0]));
  const next = { ...report, dailyCostTrend: { ...report.dailyCostTrend, days: [{ date: '2026-08-01', totalCost: 72, averageHourlyCost: 3 }] } };
  await act(async () => root.render(<BillingHistoryTab report={next} formatMoney={format} />));
  expect(field('Baseline billing date').value).toBe('2026-08-01');
  expect(field('Billing tag filter').value).toBe('');
  expect(output('Baseline cost')).toBe('$72.00');
});

it('reuses derived hourly evidence on presentation rerenders but recomputes when filters change', async () => {
  const derive = vi.spyOn(billingEvidence, 'billingWindow');
  await act(async () => root.render(<HourlyCostPanel report={report} formatMoney={format} formatHourlyMoney={format} />));
  expect(derive).toHaveBeenCalledOnce();
  await act(async () => root.render(<HourlyCostPanel report={report} formatMoney={(value) => format(value)} formatHourlyMoney={format} />));
  expect(derive).toHaveBeenCalledOnce();
  await change('Billing day filter', 'weekends');
  expect(derive).toHaveBeenCalledTimes(2);
  expect(output('Filtered billed cost')).toBe('$72.00');
});

it('labels business-hours costs as estimates and restores actual totals for all hours', async () => {
  await act(async () => root.render(<HourlyCostPanel report={report} formatMoney={format} formatHourlyMoney={format} />));
  expect(container.querySelector('.billing-metrics output')?.getAttribute('aria-label')).toBe('Filtered billed cost');
  expect(output('Filtered billed cost')).toBe('$192.00');
  await change('Billing time filter', 'business');
  expect(output('Filtered estimated cost')).toBe('$40.00');
  expect(output('Filtered average hourly cost')).toBe('$2.50/hr');
  expect(output('Billing date coverage')).toBe('2 / 2');
  expect(container.textContent).toContain('16 selected hours');
  expect(container.textContent).toContain('Mon-Fri 09:00-17:00 UTC');
  expect(container.textContent).toContain('not measured hourly usage');
  expect(container.querySelector('[aria-label="Filtered billed cost"]')).toBeNull();
  await change('Billing time filter', 'off-hours');
  expect(output('Filtered estimated cost')).toBe('$152.00');
  expect(container.textContent).toContain('80 selected hours');
  await change('Billing time filter', 'all');
  expect(output('Filtered billed cost')).toBe('$192.00');
  expect(container.querySelector('[aria-label="Filtered estimated cost"]')).toBeNull();
});

it('shows an unavailable result when business hours and weekend days do not overlap', async () => {
  await act(async () => root.render(<HourlyCostPanel report={report} formatMoney={format} formatHourlyMoney={format} />));
  await change('Billing day filter', 'weekends');
  await change('Billing time filter', 'business');
  expect(output('Filtered estimated cost')).toBe('Unavailable');
  expect(output('Filtered average hourly cost')).toBe('Unavailable');
  expect(output('Billing date coverage')).toBe('0 / 0');
  expect(container.textContent).toContain('No billing dates match these filters.');
  expect(container.querySelector('.billing-table')).toBeNull();
});