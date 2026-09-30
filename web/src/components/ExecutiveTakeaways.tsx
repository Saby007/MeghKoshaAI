import { ArrowRight } from 'lucide-react';
import type { Budget, CostDetailRow, FullReport } from '../report/models';
import { costWindowDates, type CostWindow } from '../report/costDetails';
import { budgetThreshold } from './BudgetContext';

export type TakeawayTone = 'neutral' | 'good' | 'warning' | 'risk';
export type TakeawayTarget = 'History' | 'Stale Resources' | 'Cost by Tags/Application' | 'Budgets' | 'Cost Anomalies';
export type Takeaway = { key: string; tone: TakeawayTone; text: string; target?: TakeawayTarget };

type Formatter = (value: number) => string;

/* Share of spend in the window carried by rows with no tag values at all. */
export function untaggedShare(rows: CostDetailRow[], window: CostWindow): number | null {
  const dates = new Set(costWindowDates(window));
  if (!dates.size) return null;
  let total = 0;
  let untagged = 0;
  for (const row of rows) {
    let amount = 0;
    for (const [day, value] of Object.entries(row.dailyCosts)) if (dates.has(day)) amount += value;
    total += amount;
    if (!Object.values(row.tags).some((value) => value.trim())) untagged += amount;
  }
  return total > 0 ? untagged / total : null;
}

/* The headline statements for the executive summary.
   Every line is derived from a figure shown elsewhere on the page and is only
   emitted when it is true, so the strip cannot claim more than the report
   supports. Order is by what a reader should act on first: money already
   spent over plan, then things to review, then context. */
export function buildTakeaways({ report, budgets, anomalySignals, window, formatMoney }: {
  report: FullReport;
  budgets: Budget[];
  anomalySignals: number | null;
  window: CostWindow;
  formatMoney: Formatter;
}): Takeaway[] {
  const s = report.executiveSummary;
  const items: Takeaway[] = [];

  const over = budgets
    .map((budget) => ({ budget, status: budgetThreshold(budget) }))
    .filter(({ status }) => status.tone === 'over' || status.tone === 'warning');
  if (over.length) {
    const worst = over.slice().sort((a, b) => (b.budget.currentSpend ?? 0) / Math.max(b.budget.amount, 1) - (a.budget.currentSpend ?? 0) / Math.max(a.budget.amount, 1))[0];
    items.push({
      key: 'budgets',
      tone: over.some(({ status }) => status.tone === 'over') ? 'risk' : 'warning',
      text: `${over.length} of ${budgets.length} ${budgets.length === 1 ? 'budget is' : 'budgets are'} over this cycle - ${worst.budget.name} ${worst.status.label.toLowerCase()}`,
      target: 'Budgets',
    });
  }

  if (s.potentialSavingsMonth > 0) {
    items.push({ key: 'savings', tone: 'good', text: `${formatMoney(s.potentialSavingsMonth)} a month in verified savings available`, target: 'Stale Resources' });
  }

  const idle = s.idleReviewCandidates ?? 0;
  if (idle > 0) {
    items.push({ key: 'idle', tone: 'warning', text: `${idle} idle-resource ${idle === 1 ? 'candidate needs' : 'candidates need'} owner review`, target: 'Stale Resources' });
  }

  if (anomalySignals && anomalySignals > 0) {
    items.push({ key: 'anomalies', tone: 'warning', text: `${anomalySignals} cost ${anomalySignals === 1 ? 'anomaly' : 'anomalies'} detected`, target: 'Cost Anomalies' });
  }

  const untagged = report.costDetails?.status === 'complete' ? untaggedShare(report.costDetails.rows, window) : null;
  if (untagged !== null && untagged >= 0.2) {
    items.push({ key: 'untagged', tone: untagged >= 0.5 ? 'warning' : 'neutral', text: `${Math.round(untagged * 100)}% of spend carries no tags, so it cannot be attributed`, target: 'Cost by Tags/Application' });
  }

  const top = report.topServices[0];
  if (top && top.pctOfTotal >= 0.25) {
    items.push({ key: 'concentration', tone: 'neutral', text: `${top.displayName} is ${Math.round(top.pctOfTotal * 100)}% of spend (${formatMoney(top.monthlySpend)})` });
  }

  if (s.spendChangePercentage !== null) {
    const change = s.spendChangePercentage;
    items.unshift({
      key: 'spend',
      tone: 'neutral',
      text: change === 0
        ? `Spend held at ${formatMoney(s.currentMonthlySpend)}`
        : `Spend ${change > 0 ? 'rose' : 'fell'} ${Math.abs(change * 100).toFixed(1)}% to ${formatMoney(s.currentMonthlySpend)} on the previous month`,
      target: 'History',
    });
  }

  return items.slice(0, 5);
}

export function ExecutiveTakeaways({ items, onNavigate }: { items: Takeaway[]; onNavigate: (target: TakeawayTarget) => void }) {
  if (!items.length) return null;
  return (
    <ul className="executive-takeaways" aria-label="Key takeaways">
      {items.map((item) => (
        <li key={item.key} className={`takeaway takeaway-${item.tone}`}>
          {item.target ? (
            <button type="button" onClick={() => onNavigate(item.target!)}>
              <i aria-hidden="true" />
              <span>{item.text}</span>
              <ArrowRight size={13} aria-hidden="true" />
            </button>
          ) : (
            <div><i aria-hidden="true" /><span>{item.text}</span></div>
          )}
        </li>
      ))}
    </ul>
  );
}
