import { describe, expect, it } from 'vitest';
import { buildTakeaways, untaggedShare } from './ExecutiveTakeaways';
import { reportFixture } from '../report/testFixtures';
import type { Budget, CostDetailRow, FullReport } from '../report/models';

const money = (value: number) => `$${value.toFixed(0)}`;
const window = { startDate: '2026-08-01', endDate: '2026-08-31' };
const budget = (name: string, amount: number, currentSpend: number): Budget => ({ subscriptionId: 'sub-1', name, category: 'Cost', amount, currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-01-01', periodEnd: '', currentSpend, forecastSpend: null });
const row = (tags: Record<string, string>, amount: number): CostDetailRow => ({ tags, dailyCosts: { '2026-08-05': amount } } as unknown as CostDetailRow);

describe('executive takeaways', () => {
  it('leads with the spend change and names the worst budget overrun', () => {
    const report: FullReport = { ...reportFixture, executiveSummary: { ...reportFixture.executiveSummary, spendChangePercentage: 0.076, currentMonthlySpend: 494 } };
    const items = buildTakeaways({ report, budgets: [budget('Small', 10, 11), budget('Big', 200, 800), budget('Fine', 30, 14)], anomalySignals: 0, window, formatMoney: money });
    expect(items[0].text).toBe('Spend rose 7.6% to $494 on the previous month');
    const budgets = items.find((item) => item.key === 'budgets')!;
    expect(budgets.text).toBe('2 of 3 budgets are over this cycle - Big over by 300%');
    expect(budgets.tone).toBe('risk');
  });

  it('says nothing it cannot support', () => {
    const report: FullReport = { ...reportFixture, executiveSummary: { ...reportFixture.executiveSummary, spendChangePercentage: null, potentialSavingsMonth: 0, idleReviewCandidates: 0 }, topServices: [], costDetails: undefined };
    const items = buildTakeaways({ report, budgets: [budget('Fine', 30, 14)], anomalySignals: null, window, formatMoney: money });
    expect(items).toEqual([]);
  });

  it('measures untagged spend over the selected window only', () => {
    const rows = [row({}, 87), row({ app: 'web' }, 13), { tags: {}, dailyCosts: { '2026-07-05': 1000 } } as unknown as CostDetailRow];
    expect(untaggedShare(rows, window)).toBeCloseTo(0.87);
    expect(untaggedShare([], window)).toBeNull();
  });
});
