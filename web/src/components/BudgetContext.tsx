import { Fragment, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { listBudgets } from '../api';
import type { Budget, CostDetailRow, CostDetailSummary, FullReport } from '../report/models';
import { costTagValue, costWindowDates, matchesCostFilter, type CostFilter, type CostWindow } from '../report/costDetails';
import { DailyBarChart } from './TrendChart';

export type BudgetState = { budgets: Budget[]; loading: boolean; error: string | null; refresh: () => void };

export function budgetThreshold(budget: Budget) {
  if (budget.category !== 'Cost' || budget.currentSpend === null || !Number.isFinite(budget.currentSpend) || !Number.isFinite(budget.amount) || budget.amount < 0) return { tone: 'unknown', label: 'Unavailable' };
  if (budget.currentSpend <= budget.amount) return { tone: 'within', label: 'Within budget' };
  return budget.currentSpend <= budget.amount * 1.1 ? { tone: 'warning', label: 'Up to 10% over budget' } : { tone: 'over', label: 'More than 10% over budget' };
}

export function budgetFilterMatches(expression: unknown, row: CostDetailRow): boolean | null {
  if (!expression || typeof expression !== 'object' || Array.isArray(expression)) return null;
  const filter = expression as Record<string, unknown>;
  if (!Object.keys(filter).length) return true;
  const keys = Object.keys(filter);
  if (keys.length !== 1) return null;
  if (keys[0] === 'and' || keys[0] === 'or') {
    const conditions = filter[keys[0]];
    if (!Array.isArray(conditions) || !conditions.length) return null;
    const results = conditions.map((condition) => budgetFilterMatches(condition, row));
    if (keys[0] === 'and') return results.includes(false) ? false : results.includes(null) ? null : true;
    return results.includes(true) ? true : results.includes(null) ? null : false;
  }
  const kind = keys[0];
  const condition = filter[kind];
  if (!['dimensions', 'tags'].includes(kind) || !condition || typeof condition !== 'object') return null;
  const { name, operator, values } = condition as Record<string, unknown>;
  if (typeof name !== 'string' || operator !== 'In' || !Array.isArray(values) || !values.every((value) => typeof value === 'string')) return null;
  const dimensions: Record<string, string> = { resourceid: row.resourceId, resourcegroupname: row.resourceGroup, resourcegroup: row.resourceGroup, servicename: row.serviceName, resourcelocation: row.region, subscriptionid: row.subscriptionId };
  if (kind === 'tags') { const value = costTagValue(row, name); return value === undefined ? false : values.includes(value); }
  const value = dimensions[name.toLowerCase()];
  return value === undefined ? null : values.some((expected) => expected.toLowerCase() === value.toLowerCase());
}

export function useBudgetSummary(report: Pick<FullReport, 'subscriptionBreakdown' | 'reportMetadata'>): BudgetState {
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const scope = JSON.stringify(report.subscriptionBreakdown.map((row) => row.subscriptionId).sort());
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setBudgets([]);
    const timer = window.setTimeout(() => { controller.abort(); setLoading(false); setError('Azure budget lookup timed out. Retry the budget check.'); }, 15000);
    listBudgets(JSON.parse(scope), controller.signal).then((result) => { if (!controller.signal.aborted) setBudgets(result); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'Budgets are unavailable.'); })
      .finally(() => { window.clearTimeout(timer); if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [scope, report.reportMetadata.generatedAt, version]);
  return { budgets, loading, error, refresh: () => setVersion((value) => value + 1) };
}

/* Rows the budget actually governs, within the assessed evidence.

   Reuses budgetFilterMatches so "what this budget covers" has one definition
   across the app. `verified` is false when Azure returned a filter shape we
   cannot evaluate, or returned no filter metadata at all - in that case the
   rows are the whole subscription, which is a guess, and the caller has to say
   so rather than presenting the total as the budget's spend. */
export function budgetScopedRows(budget: Budget, details?: CostDetailSummary): { rows: CostDetailRow[]; verified: boolean } {
  if (!details || details.status !== 'complete') return { rows: [], verified: false };
  const inSubscription = details.rows.filter((row) => row.subscriptionId.toLowerCase() === budget.subscriptionId.toLowerCase());
  const unfiltered = budget.filter !== undefined && Object.keys(budget.filter).length === 0;
  if (unfiltered) return { rows: inSubscription, verified: true };
  const decided = inSubscription.map((row) => ({ row, match: budgetFilterMatches(budget.filter, row) }));
  if (decided.some((item) => item.match === null) || budget.filter === undefined) {
    return { rows: inSubscription, verified: false };
  }
  return { rows: decided.filter((item) => item.match === true).map((item) => item.row), verified: true };
}

/* An even daily share of the budget, for the reference line.

   Azure does not publish a daily target, so this is arithmetic on the amount,
   not a figure Azure reported - and it is only meaningful where the period
   length is knowable. Monthly is the common case; for other grains this
   returns null and the chart simply draws no line rather than inventing one. */
export function budgetDailyAllowance(budget: Budget, window: CostWindow): number | null {
  if (budget.timeGrain !== 'Monthly' || !Number.isFinite(budget.amount) || budget.amount <= 0) return null;
  const anchor = window.endDate || window.startDate;
  const [year, month] = anchor.split('-').map(Number);
  if (!Number.isFinite(year) || !Number.isFinite(month)) return null;
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return days > 0 ? budget.amount / days : null;
}

/* What a budget spent per day across the selected period.

   The series is FOCUS EffectiveCost from the assessed window, scoped to the
   budget's own filter. It is NOT the ActualCost figure behind currentSpend, so
   the two will not tie out and the caller must not present them as the same
   number. */
export function BudgetDailyChart({
  budget,
  details,
  window,
  formatMoney,
}: {
  budget: Budget;
  details?: CostDetailSummary;
  window: CostWindow;
  formatMoney: (value: number) => string;
}) {
  const { rows, verified } = budgetScopedRows(budget, details);
  const dates = costWindowDates(window);
  const values = dates.map((date) => {
    const covered = rows.filter((row) => row.dailyCosts[date] !== undefined);
    return covered.length ? covered.reduce((sum, row) => sum + row.dailyCosts[date], 0) : null;
  });
  const observed = values.filter((value): value is number => value !== null);
  const total = observed.reduce((sum, value) => sum + value, 0);
  const allowance = budgetDailyAllowance(budget, window);
  const over = allowance === null ? 0 : observed.filter((value) => value > allowance).length;
  return (
    <section className="budget-daily-chart" aria-label={`Daily spend for ${budget.name}`}>
      <DailyBarChart
        dates={dates}
        values={values}
        seriesName={`${budget.name} daily cost`}
        formatMoney={formatMoney}
        ariaLabel={`Daily cost for ${budget.name}`}
        emptyMessage="No daily cost evidence covers this budget in the selected period."
        reference={allowance === null ? null : { value: allowance, label: `Even daily share of ${budget.currency} ${budget.amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` }}
      />
      {observed.length > 0 && (
        <p className="billing-provenance">
          {formatMoney(total)} across {observed.length} covered {observed.length === 1 ? 'day' : 'days'}
          {allowance === null
            ? `. ${budget.timeGrain} budgets have no single daily share, so no allowance line is drawn.`
            : `, against an even daily share of ${formatMoney(allowance)}. ${over} ${over === 1 ? 'day is' : 'days are'} above that share.`}
          {' '}Selected-window EffectiveCost scoped to this budget&apos;s filter, not the current-cycle ActualCost behind its reported spend, so these totals are not expected to match.
          {!verified && ' Azure did not return an evaluable filter for this budget, so this is the whole subscription and may be wider than the budget really covers.'}
        </p>
      )}
    </section>
  );
}

/* Which budgets bear on a set of cost rows, and how confidently.

   Exported because the tag page asks the same question the budget table does -
   "does a budget cover this?" - and two implementations would drift into two
   different answers. */
export function relateBudgets(budgets: Budget[], rows: CostDetailRow[], filters: CostFilter = {}) {
  return budgets
    .filter((budget) => !filters.subscriptionId || budget.subscriptionId.toLowerCase() === filters.subscriptionId.toLowerCase())
    .map((budget) => {
      const conditions = rows.filter((row) => row.subscriptionId.toLowerCase() === budget.subscriptionId.toLowerCase()).map((row) => budgetFilterMatches(budget.filter, row));
      const unfiltered = budget.filter !== undefined && Object.keys(budget.filter).length === 0;
      return { budget, relation: unfiltered ? 'Subscription-wide budget' : conditions.includes(true) ? 'Matching budget filter' : !conditions.length || conditions.includes(null) ? 'Filter applicability unverified' : 'unrelated' };
    })
    .filter((item) => item.relation !== 'unrelated');
}

export function BudgetContext({ state, details, filters = {}, showHeading = true, window, formatMoney }: { state: BudgetState; details?: CostDetailSummary; filters?: CostFilter; showHeading?: boolean; window?: CostWindow; formatMoney?: (value: number) => string }) {
  const [openBudget, setOpenBudget] = useState<string | null>(null);
  const rows = details?.rows.filter((row) => matchesCostFilter(row, filters)) ?? [];
  const budgets = relateBudgets(state.budgets, rows, filters);
  return <section className="cost-budget-context" aria-label="Applicable Azure budgets" aria-busy={state.loading}>
    {showHeading && <header className="cost-section-heading"><h3>Azure budget context</h3><button type="button" className="ghost-button" disabled={state.loading} onClick={state.refresh} aria-label="Refresh budget context" title="Refresh budget context"><RefreshCw size={16} /></button></header>}
    {state.loading ? <p role="status">Checking subscription budgets...</p> : state.error ? <p role="alert">{state.error}</p> : !budgets.length ? <p role="status">No matching subscription-scope budgets were returned.</p> : <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Azure budget status"><table className="data-table billing-table"><thead><tr><th>Budget / scope</th><th>Budget amount</th><th>Current spend</th><th>Budget remaining</th><th>Azure forecast</th><th>Status</th></tr></thead><tbody>{budgets.map(({ budget, relation }) => {
      const status = budgetThreshold(budget);
      const key = `${budget.subscriptionId}:${budget.name}`;
      /* The drilldown needs a period to chart and a formatter to label it, so
         callers that do not supply them keep the plain table. */
      const drillable = Boolean(window && formatMoney && details?.status === 'complete');
      const native = (value: number | null) => value === null ? 'Unavailable' : `${budget.currency} ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
      return <Fragment key={`${budget.subscriptionId}:${budget.name}`}><tr><th>{drillable
        ? <button type="button" className="finding-link" aria-expanded={openBudget === key} aria-label={`Daily spend for ${budget.name}`} onClick={() => setOpenBudget((value) => value === key ? null : key)}>{budget.name}</button>
        : budget.name}<small>{relation}</small><details><summary>Budget scope</summary><span>{budget.scope || budget.subscriptionId}</span><pre>{JSON.stringify(budget.filter ?? 'Filter metadata not returned', null, 2)}</pre><small>Active: {budget.periodStart} - {budget.periodEnd || 'Open-ended'} / {budget.timeGrain}</small><small>Checked: {budget.observedAt || 'Not reported'}</small></details></th><td>{native(budget.amount)}</td><td>{native(budget.currentSpend)}</td><td>{native(budget.currentSpend === null ? null : budget.amount - budget.currentSpend)}</td><td>{native(budget.forecastSpend)}{budget.forecastSpend !== null && <small>{budget.forecastSpend > budget.amount ? 'Projected overrun' : 'Projected within budget'}: {native(Math.abs(budget.amount - budget.forecastSpend))}</small>}</td><td><span className={`budget-status budget-${status.tone}`}>{status.label}</span></td></tr>
      {drillable && openBudget === key && window && formatMoney && <tr><td colSpan={6}><BudgetDailyChart budget={budget} details={details} window={window} formatMoney={formatMoney} /></td></tr>}
      </Fragment>;
    })}</tbody></table></div>}
    <p className="billing-provenance">Native budget current-cycle ActualCost, not the selected historical EffectiveCost window. Green: within budget; amber: up to 10% over; red: more than 10% over. A subscription-wide budget is not an application allocation or spending cap.</p>
  </section>;
}
