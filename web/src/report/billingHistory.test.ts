import { expect, it } from 'vitest';
import { billingCostOnDate, billingDates, billingTagId, billingTags, billingWindow, businessHoursForDate, compareBillingDates, type BillingSource } from './billingHistory';
import { compareCostGroups, costCoverage, costWindowDates, dailySubscriptionCosts, previousCostWindow } from './costDetails';
import type { CostDetailSummary } from './models';

const point = (date: string, totalCost: number) => ({ date, totalCost, averageHourlyCost: 999 });
const details: CostDetailSummary = {
  status: 'complete', statusMessage: '', costBasis: 'EffectiveCost', granularity: 'daily',
  dates: ['2026-09-04', '2026-09-05', '2026-09-06', '2026-09-07'],
  rows: [{ detailId: 'one', subscriptionId: 'sub-1', subscriptionName: 'One', resourceId: '/vm', resourceName: 'vm', resourceType: 'vm', resourceGroup: 'group', serviceName: 'Compute', region: 'centralindia', tags: { Application: 'A', Owner: 'Team' }, tagAttributionSource: 'exported_resource_tags', dailyCosts: { '2026-09-04': 24, '2026-09-05': 24, '2026-09-06': 48, '2026-09-07': -2 } }],
};

it('compares exact preceding calendar windows without doubling charges for two tags', () => {
  const window = { startDate: '2026-09-06', endDate: '2026-09-07' };
  expect(previousCostWindow(window)).toEqual({ startDate: '2026-09-04', endDate: '2026-09-05' });
  const rows = compareCostGroups(details, window, { tagKey: 'application', tagValue: 'A' });
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ current: 46, previous: 48, delta: -2, owners: ['Team'] });
  expect(rows[0].percentage).toBeCloseTo(-4.1666667);
  expect(dailySubscriptionCosts(details, window)[0].days[1]).toMatchObject({ date: '2026-09-07', previousDate: '2026-09-05', current: -2, previous: 24 });
});

it('distinguishes incomplete previous history, verified zero, and invalid custom ranges', () => {
  const window = { startDate: '2026-09-04', endDate: '2026-09-05' };
  expect(compareCostGroups(details, window)[0].previous).toBeNull();
  expect(costCoverage(details, previousCostWindow(window)).complete).toBe(false);
  expect(costWindowDates({ startDate: '2026-02-31', endDate: '2026-09-05' })).toEqual([]);
  const zero = { ...details, rows: [{ ...details.rows[0], dailyCosts: { '2026-09-05': 4 } }] };
  expect(dailySubscriptionCosts(zero, window)[0].days[0].current).toBe(0);
  expect(compareCostGroups(details, window, { subscriptionId: 'other' })).toEqual([]);
  expect(compareCostGroups(undefined, window)).toEqual([]);
});

it('filters billed resources missing any configured required tag without counting unattributed charges', () => {
  const window = { startDate: '2026-09-04', endDate: '2026-09-05' };
  expect(compareCostGroups(details, window, { requiredTagKeys: '["owner"]' })).toEqual([]);
  expect(compareCostGroups(details, window, { requiredTagKeys: '["owner","CostCentre"]' })[0].current).toBe(48);
  const unattributed = { ...details, rows: [{ ...details.rows[0], resourceId: '', tags: {} }] };
  expect(compareCostGroups(unattributed, window, { requiredTagKeys: '[]' })).toEqual([]);
});

const source: BillingSource = {
  dailyCostTrend: { status: 'complete', statusMessage: '', days: [
    point('2026-09-04', 240), point('2026-09-05', 24), point('2026-09-06', 48), point('2026-09-07', 120),
  ] },
  tagDailyCostTrend: {
    status: 'complete', statusMessage: '', tagKey: '', availableTagValues: ['A', 'Unavailable tag'],
    windowDates: ['2026-09-04', '2026-09-05', '2026-09-06', '2026-09-07'],
    series: [
      { tagKey: 'Team', tagValue: 'A', totalCost: 999, days: [point('2026-09-05', 24)] },
      { tagKey: 'Application', tagValue: 'A', totalCost: 999, days: [point('2026-09-06', 48)] },
    ],
  },
};

it('uses UTC weekend dates and only weekend hours in the denominator', () => {
  const result = billingWindow(source, { rangeDays: 7, dayFilter: 'weekends' });
  expect(result.days.map((day) => day.date)).toEqual(['2026-09-05', '2026-09-06']);
  expect(result.totalCost).toBe(72);
  expect(result.averageHourlyCost).toBe(1.5);
  expect(result.days.map((day) => day.averageHourlyCost)).toEqual([1, 2]);
});

it('uses the configured local business calendar across UTC day boundaries and rejects invalid zones', () => {
  expect(businessHoursForDate('2026-09-06', { start: '00:00', end: '08:00', timeZone: 'Asia/Tokyo' })).toBe(8);
  expect(businessHoursForDate('2026-09-07', { start: '09:00', end: '17:00', timeZone: 'Asia/Kolkata' })).toBe(8);
  const calendar = { start: '22:00', end: '06:00', timeZone: 'UTC' };
  const business = billingWindow(source, { rangeDays: 7, timeFilter: 'business', businessCalendar: calendar });
  const off = billingWindow(source, { rangeDays: 7, timeFilter: 'off-hours', businessCalendar: calendar });
  expect(business.totalCost! + off.totalCost!).toBe(billingWindow(source, { rangeDays: 7 }).totalCost);
  expect(billingWindow(source, { rangeDays: 7, timeFilter: 'business', businessCalendar: { ...calendar, timeZone: 'Invalid/Zone' } }).totalCost).toBeNull();
});

it('filters a single tag independently without summing overlapping tag dimensions', () => {
  const result = billingWindow(source, { rangeDays: 7, dayFilter: 'weekends', tagId: billingTagId(source.tagDailyCostTrend.series[0]) });
  expect(result.totalCost).toBe(24);
  expect(result.averageHourlyCost).toBe(0.5);
  expect(result.coveredDays).toBe(2);
  expect(billingTags(source).map((tag) => tag.label)).toEqual(['Team / A', 'Application / A']);
});

it('never fills missing legacy tag dates with zero without explicit coverage', () => {
  const legacy = { ...source, tagDailyCostTrend: { ...source.tagDailyCostTrend, windowDates: undefined } };
  const result = billingWindow(legacy, { rangeDays: 7, dayFilter: 'weekends', tagId: billingTagId(source.tagDailyCostTrend.series[0]) });
  expect(result.days[1].totalCost).toBeNull();
  expect(result.incomplete).toBe(true);
  expect(result.coveredDays).toBe(1);
});

it('uses a calendar window and leaves missing billing days unavailable', () => {
  const sparse = { ...source, dailyCostTrend: { ...source.dailyCostTrend, days: [point('2026-09-04', 240), point('2026-09-07', 24)] } };
  const result = billingWindow(sparse, { rangeDays: 2 });
  expect(result.days.map((day) => day.date)).toEqual(['2026-09-06', '2026-09-07']);
  expect(result.days[0].totalCost).toBeNull();
  expect(result.totalCost).toBe(24);
  expect(result.incomplete).toBe(true);
  expect(compareBillingDates(sparse, '2026-09-05', '2026-09-07').delta).toBeNull();
});

it('keeps credits and does not invent percentage changes from a zero baseline', () => {
  const credits = { ...source, dailyCostTrend: { ...source.dailyCostTrend, days: [point('2026-09-05', 0), point('2026-09-06', -24)] } };
  expect(compareBillingDates(credits, '2026-09-05', '2026-09-06')).toEqual({ baseline: 0, comparison: -24, delta: -24, percentChange: null });
  expect(billingWindow(credits, { rangeDays: 7 }).averageHourlyCost).toBe(-0.5);
});

it('compares the exact selected dates in either direction', () => {
  expect(compareBillingDates(source, '2026-09-05', '2026-09-06')).toEqual({ baseline: 24, comparison: 48, delta: 24, percentChange: 100 });
  expect(compareBillingDates(source, '2026-09-06', '2026-09-05').delta).toBe(-24);
});

it('rejects unavailable sources, missing tags and invalid dates', () => {
  const unavailable = { ...source, dailyCostTrend: { ...source.dailyCostTrend, status: 'unavailable' as const } };
  expect(billingWindow(unavailable, { rangeDays: 30 }).totalCost).toBeNull();
  expect(billingCostOnDate(source, '2026-09-05', 'missing')).toBeNull();
  expect(billingCostOnDate(source, '2026-02-31')).toBeNull();
  expect(billingDates(unavailable)).toEqual([]);
});

it('selects weekdays and handles a range containing no weekend days', () => {
  expect(billingWindow(source, { rangeDays: 7, dayFilter: 'weekdays' }).totalCost).toBe(360);
  expect(billingWindow(source, { rangeDays: 1, dayFilter: 'weekends' }).averageHourlyCost).toBeNull();
});

it('estimates business hours and off-hours from daily totals without inventing hourly measurements', () => {
  const all = billingWindow(source, { rangeDays: 7 });
  const business = billingWindow(source, { rangeDays: 7, timeFilter: 'business' });
  const offHours = billingWindow(source, { rangeDays: 7, timeFilter: 'off-hours' });
  expect(all.estimated).toBe(false);
  expect(all.coveredHours).toBe(96);
  expect(business.estimated).toBe(true);
  expect(business.days.map((day) => [day.date, day.hours, day.totalCost])).toEqual([
    ['2026-09-04', 8, 80], ['2026-09-07', 8, 40],
  ]);
  expect(business.totalCost).toBe(120);
  expect(business.coveredHours).toBe(16);
  expect(business.averageHourlyCost).toBe(7.5);
  expect(offHours.estimated).toBe(true);
  expect(offHours.totalCost).toBe(312);
  expect(offHours.coveredHours).toBe(80);
  expect(business.totalCost! + offHours.totalCost!).toBe(all.totalCost);
  expect(business.coveredHours + offHours.coveredHours).toBe(all.coveredHours);
});

it('intersects time windows with UTC day and tag selections', () => {
  const weekends = billingWindow(source, { rangeDays: 7, dayFilter: 'weekends', timeFilter: 'business' });
  expect(weekends.days).toEqual([]);
  expect(weekends.totalCost).toBeNull();
  expect(weekends.coveredHours).toBe(0);
  const offHours = billingWindow(source, {
    rangeDays: 7, dayFilter: 'weekends', timeFilter: 'off-hours', tagId: billingTagId(source.tagDailyCostTrend.series[0]),
  });
  expect(offHours.totalCost).toBe(24);
  expect(offHours.coveredHours).toBe(48);
  expect(offHours.averageHourlyCost).toBe(0.5);
});

it('keeps credits and missing-day coverage when estimating a business-hours window', () => {
  const sparse = { ...source, dailyCostTrend: { ...source.dailyCostTrend, days: [point('2026-09-04', -24), point('2026-09-08', 48)] } };
  const result = billingWindow(sparse, { rangeDays: 7, timeFilter: 'business' });
  expect(result.days.map((day) => day.totalCost)).toEqual([-8, null, 16]);
  expect(result.totalCost).toBe(8);
  expect(result.coveredDays).toBe(2);
  expect(result.coveredHours).toBe(16);
  expect(result.averageHourlyCost).toBe(0.5);
  expect(result.incomplete).toBe(true);
});