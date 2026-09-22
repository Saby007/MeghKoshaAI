import { expect, it } from 'vitest';
import { tagDistribution } from './tagDistribution';
import type { TagDailyCostTrendSummary } from './models';

const summary: TagDailyCostTrendSummary = {
  status: 'complete', statusMessage: '', tagKey: '', availableTagValues: [], series: [],
  windowDates: ['2026-07-01', '2026-07-02'],
  distributionSeries: [{ tagKey: 'Tag set', tagValue: 'Team: A; App: B', totalCost: 1000,
    days: [{ date: '2026-07-01', totalCost: 24, averageHourlyCost: 1 }, { date: '2026-07-02', totalCost: 48, averageHourlyCost: 2 }] }],
};

it('uses selected dates instead of cached totals or per-resource active days', () => {
  expect(tagDistribution(summary, 1).total).toBe(2);
  expect(tagDistribution(summary, 2).total).toBe(1.5);
  expect(tagDistribution(summary, 90, { startDate: '2026-07-01', endDate: '2026-07-01' }).total).toBe(1);
});
it('does not use overlapping legacy tag series for an additive distribution', () => {
  expect(tagDistribution({ ...summary, distributionSeries: undefined }, 7).available).toBe(false);
});
it('preserves signed credits instead of dropping them from totals', () => {
  const result = tagDistribution({ ...summary, distributionSeries: [...summary.distributionSeries!, {
    tagKey: 'Tag set', tagValue: 'Credit', totalCost: -24, days: [{ date: '2026-07-02', totalCost: -24, averageHourlyCost: -1 }],
  }] }, 1);
  expect(result.total).toBe(1);
  expect(result.signed).toBe(true);
});