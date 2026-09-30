import { describe, expect, it } from 'vitest';
import { compareCostGroups, comparisonDate, isCalendarMonth, monthCostWindow, previousCostWindow } from './costDetails';
import type { CostDetailRow, CostDetailSummary } from './models';

describe('calendar-month cost windows', () => {
  it('recognises whole calendar months only', () => {
    expect(isCalendarMonth({ startDate: '2026-08-01', endDate: '2026-08-31' })).toBe(true);
    expect(isCalendarMonth({ startDate: '2026-02-01', endDate: '2026-02-28' })).toBe(true);
    expect(isCalendarMonth({ startDate: '2026-08-02', endDate: '2026-08-31' })).toBe(false);
    expect(isCalendarMonth({ startDate: '2026-08-01', endDate: '2026-08-30' })).toBe(false);
  });

  it('compares a month with the previous calendar month, across a year boundary', () => {
    expect(previousCostWindow({ startDate: '2026-09-01', endDate: '2026-09-30' })).toEqual({ startDate: '2026-08-01', endDate: '2026-08-31' });
    expect(previousCostWindow({ startDate: '2026-01-01', endDate: '2026-01-31' })).toEqual({ startDate: '2025-12-01', endDate: '2025-12-31' });
  });

  it('pairs days by day of month and leaves days without a counterpart unpaired', () => {
    const august = { startDate: '2026-08-01', endDate: '2026-08-31' };
    expect(comparisonDate(august, 1)).toBe('2026-07-02');
    const october = { startDate: '2026-10-01', endDate: '2026-10-31' };
    expect(comparisonDate(october, 29)).toBe('2026-09-30');
    expect(comparisonDate(october, 30)).toBe('');
  });

  it('keeps day-count shifting for windows that are not a calendar month', () => {
    const window = { startDate: '2026-08-02', endDate: '2026-08-31' };
    expect(previousCostWindow(window)).toEqual({ startDate: '2026-07-03', endDate: '2026-08-01' });
    expect(comparisonDate(window, 0)).toBe('2026-07-03');
  });

  it('builds the assessed month window from report metadata, rejecting invalid input', () => {
    expect(monthCostWindow('2026-08-01', '2026-08-31')).toEqual({ startDate: '2026-08-01', endDate: '2026-08-31' });
    expect(monthCostWindow('', '2026-08-31')).toBeNull();
    expect(monthCostWindow('2026-08-31', '2026-08-01')).toBeNull();
  });
});

describe('grouping cost rows', () => {
  const row = (id: string, group: string, cost: number): CostDetailRow => ({
    detailId: id, subscriptionId: 'sub-1', subscriptionName: 'Sub', resourceId: `/subscriptions/sub-1/resourceGroups/${group}/providers/x/y/${id}`,
    resourceName: id, resourceType: 'x/y', resourceGroup: group, serviceName: 'Svc', region: 'eastus', tags: {}, tagAttributionSource: '',
    dailyCosts: { '2026-08-05': cost },
  });
  const details: CostDetailSummary = { status: 'complete', statusMessage: '', costBasis: '', granularity: 'daily', dates: ['2026-08-05'],
    rows: [row('a', 'SRE', 12), row('b', 'sre', 1), row('c', 'tub', 4)] };

  it('treats resource group names that differ only in case as one group', () => {
    const groups = compareCostGroups(details, { startDate: '2026-08-05', endDate: '2026-08-05' }, {}, 'resourceGroup');
    expect(groups.map((group) => [group.name, group.current])).toEqual([['SRE', 13], ['tub', 4]]);
  });
});
