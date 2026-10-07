import { Fragment, useEffect, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';
import { listBudgets } from '../api';
import type { Budget, CostDetailRow, CostDetailSummary, FullReport } from '../report/models';
import { costTagValue, costWindowDates, matchesCostFilter, sameTagKey, type CostFilter, type CostWindow } from '../report/costDetails';
import { DailyBarChart } from './TrendChart';

export type BudgetState = { budgets: Budget[]; loading: boolean; error: string | null; refresh: () => void };

export function BudgetExpiry({ periodEnd, highlightNearExpiry = false }: Pick<Budget, 'periodEnd'> & { highlightNearExpiry?: boolean }) {
  if (!periodEnd) return <span className="budget-expiry">Open-ended</span>;
  const date = periodEnd.slice(0, 10);
  const parsed = new Date(`${date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) {
    return <span className="budget-expiry">Unavailable</span>;
  }
  const now = new Date();
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const daysLeft = Math.round((parsed.getTime() - today) / 86400000);
  const warning = highlightNearExpiry && daysLeft <= 30;
  const label = daysLeft < 0 ? 'Expired' : daysLeft === 0 ? 'Expires today' : `${daysLeft} ${daysLeft === 1 ? 'day' : 'days'} left`;
  return <span className={`budget-expiry${warning ? ' budget-expiry-warning' : ''}`}>
    <time dateTime={date}>{parsed.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })}</time>
    {warning && <> · {label}</>}
  </span>;
}

export function budgetThreshold(budget: Budget) {
  if (budget.category !== 'Cost' || budget.currentSpend === null || !Number.isFinite(budget.currentSpend) || !Number.isFinite(budget.amount) || budget.amount < 0) return { tone: 'unknown', label: 'Unavailable' };
  if (budget.currentSpend <= budget.amount) return { tone: 'within', label: 'Within budget' };
  /* The status states the overrun rather than naming the band it falls in:
     "Over by 12%" says more, in fewer words, than "More than 10% over budget". */
  const over = budget.amount > 0 ? Math.round((budget.currentSpend / budget.amount - 1) * 100) : null;
  const label = over === null ? 'Over budget' : `Over by ${Math.max(over, 1)}%`;
  return budget.currentSpend <= budget.amount * 1.1 ? { tone: 'warning', label } : { tone: 'over', label };
}

const CYCLE_MONTHS: Record<string, number> = { Monthly: 1, Quarterly: 3, Annually: 12 };

/* The budget cycle that "current spend" belongs to. Azure resets a budget on
   its own cadence from its start date, so a budget's current spend is not the
   report's month: checked on 30 Sep, a monthly budget reports September to
   date while the report covers August. Without the cycle beside the figure,
   $836.52 of budget spend read as more than the whole $494 month. */
export function budgetCycle(budget: Budget, asOf: string): { start: string; end: string } | null {
  const months = CYCLE_MONTHS[budget.timeGrain];
  const origin = /^\d{4}-\d{2}/.test(budget.periodStart) ? budget.periodStart : '';
  const today = /^\d{4}-\d{2}/.test(asOf) ? asOf : '';
  if (!months || !origin || !today) return null;
  const [originYear, originMonth] = origin.split('-').map(Number);
  const [year, month] = today.split('-').map(Number);
  const elapsed = (year - originYear) * 12 + (month - originMonth);
  if (elapsed < 0) return null;
  const startIndex = originYear * 12 + (originMonth - 1) + Math.floor(elapsed / months) * months;
  const endIndex = startIndex + months - 1;
  const iso = (index: number, day: number) => `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const lastDay = new Date(Date.UTC(Math.floor(endIndex / 12), (endIndex % 12) + 1, 0)).getUTCDate();
  return { start: iso(startIndex, 1), end: iso(endIndex, lastDay) };
}

function cycleLabel(cycle: { start: string; end: string }): string {
  const format = (value: string, withYear: boolean) => new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', year: withYear ? 'numeric' : undefined, timeZone: 'UTC' });
  if (cycle.start.slice(0, 7) === cycle.end.slice(0, 7)) return format(cycle.start, true);
  return `${format(cycle.start, cycle.start.slice(0, 4) !== cycle.end.slice(0, 4))} – ${format(cycle.end, true)}`;
}

/* Budget figures are in the budget's own currency, which the report's display
   currency does not convert, so they keep their own formatter - but with the
   same symbol-and-two-decimals shape as every other figure on the page
   rather than "USD 750". */
function budgetMoney(currency: string): (value: number) => string {
  try {
    const formatter = new Intl.NumberFormat(undefined, { style: 'currency', currency, currencyDisplay: 'narrowSymbol', minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (value) => formatter.format(value);
  } catch {
    return (value) => `${currency} ${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
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

/* Whether a budget's own filter selects a tag key and value.

   Asked of the budget's filter, never of the resources a selection covers. Testing
   rows instead made a `Contact` budget read as "scoped to Action = Do Not Delete",
   because the same resources happened to carry both tags - so every tag on the page
   listed the same budgets, which is the opposite of what the page claims to show.

   An `and` qualifies when any branch names the tag: the budget is then scoped to this
   application, possibly narrower. An `or` qualifies only when every branch names it,
   since otherwise the budget also covers spend outside the selection. */
export function budgetTargetsTag(expression: unknown, key: string, value: string): boolean {
  if (!expression || typeof expression !== 'object' || Array.isArray(expression)) return false;
  const filter = expression as Record<string, unknown>;
  const kinds = Object.keys(filter);
  if (kinds.length !== 1) return false;
  const [kind] = kinds;
  const body = filter[kind];
  if (kind === 'and' || kind === 'or') {
    if (!Array.isArray(body) || !body.length) return false;
    const results = body.map((condition) => budgetTargetsTag(condition, key, value));
    return kind === 'and' ? results.includes(true) : results.every(Boolean);
  }
  if (kind !== 'tags' || !body || typeof body !== 'object') return false;
  const { name, operator, values } = body as Record<string, unknown>;
  return typeof name === 'string'
    && sameTagKey(name, key)
    && operator === 'In'
    && Array.isArray(values)
    && values.includes(value);
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
   so rather than presenting the total as the budget's spend.

   `filters` narrows the evidence further to whatever selection the caller is
   reporting against - a tag or application, say. Without it a page that asks
   "how is this application tracking against its budget" charts the budget's
   entire subscription instead, which answers a different question. */
export function budgetScopedRows(budget: Budget, details?: CostDetailSummary, filters: CostFilter = {}): { rows: CostDetailRow[]; verified: boolean } {
  if (!details || details.status !== 'complete') return { rows: [], verified: false };
  const inSubscription = details.rows.filter((row) => row.subscriptionId.toLowerCase() === budget.subscriptionId.toLowerCase() && matchesCostFilter(row, filters));
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
/* The daily series behind a budget, scoped to its own filter and any caller
   selection, with the even daily share and how many days exceed it. Shared by
   the budget chart and by pages that show one budget's figures beside their own
   graph instead of drawing a second one. */
export function budgetDailySummary(budget: Budget, details: CostDetailSummary | undefined, window: CostWindow, filters: CostFilter = {}) {
  const { rows, verified } = budgetScopedRows(budget, details, filters);
  const dates = costWindowDates(window);
  const values = dates.map((date) => {
    const covered = rows.filter((row) => row.dailyCosts[date] !== undefined);
    return covered.length ? covered.reduce((sum, row) => sum + row.dailyCosts[date], 0) : null;
  });
  const observed = values.filter((value): value is number => value !== null);
  const total = observed.reduce((sum, value) => sum + value, 0);
  const allowance = budgetDailyAllowance(budget, window);
  const over = allowance === null ? 0 : observed.filter((value) => value > allowance).length;
  return { dates, values, observedDays: observed.length, total, allowance, over, verified };
}

export function budgetSummaryText(budget: Budget, summary: ReturnType<typeof budgetDailySummary>, formatMoney: (value: number) => string, scopeLabel?: string) {
  return `${formatMoney(summary.total)} across ${summary.observedDays} covered ${summary.observedDays === 1 ? 'day' : 'days'}${scopeLabel ? ` for ${scopeLabel}` : ''}`
    + (summary.allowance === null
      ? `. ${budget.timeGrain} budgets have no single daily share, so no allowance line is drawn.`
      : `, against an even daily share of ${formatMoney(summary.allowance)}. ${summary.over} ${summary.over === 1 ? 'day is' : 'days are'} above that share.`)
    + " Selected-window EffectiveCost scoped to this budget's filter, not the current-cycle ActualCost behind its reported spend, so these totals are not expected to match."
    + (summary.verified ? '' : ' Azure did not return an evaluable filter for this budget, so this is the whole subscription and may be wider than the budget really covers.');
}

export function BudgetDailyChart({
  budget,
  details,
  window,
  formatMoney,
  filters = {},
  scopeLabel,
  selectedDate = null,
  onSelectDate,
}: {
  budget: Budget;
  details?: CostDetailSummary;
  window: CostWindow;
  formatMoney: (value: number) => string;
  filters?: CostFilter;
  scopeLabel?: string;
  selectedDate?: string | null;
  onSelectDate?: (date: string) => void;
}) {
  const summary = budgetDailySummary(budget, details, window, filters);
  const { dates, values, allowance } = summary;
  return (
    <section className="budget-daily-chart" aria-label={`Daily spend for ${budget.name}`}>
      <DailyBarChart
        dates={dates}
        values={values}
        seriesName={`${budget.name} daily cost`}
        formatMoney={formatMoney}
        ariaLabel={`Daily cost for ${budget.name}`}
        emptyMessage={scopeLabel
          ? `No daily cost evidence covers ${scopeLabel} under this budget in the selected period.`
          : 'No daily cost evidence covers this budget in the selected period.'}
        reference={allowance === null ? null : { value: allowance, label: `Even daily share of ${budget.currency} ${budget.amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` }}
        selectedDate={selectedDate}
        onSelectDate={onSelectDate}
        selectLabel={(date) => `Resource costs for ${date}`}
      />
      {summary.observedDays > 0 && <p className="billing-provenance">{budgetSummaryText(budget, summary, formatMoney, scopeLabel)}</p>}
    </section>
  );
}

/* Which budgets bear on a set of cost rows, and how confidently.

   Exported because the tag page asks the same question the budget table does -
   "does a budget cover this?" - and two implementations would drift into two
   different answers.

   `requireEvidence` separates "there is no cost evidence to judge with" from
   "there is evidence and none of it falls in this budget's subscription".
   Callers reporting against a deliberately narrowed selection set it, so a
   budget covering a subscription the selection never touches is reported as
   unrelated instead of being claimed as covering it on no evidence at all. */
export function relateBudgets(budgets: Budget[], rows: CostDetailRow[], filters: CostFilter = {}, requireEvidence = false) {
  return budgets
    .filter((budget) => !filters.subscriptionId || budget.subscriptionId.toLowerCase() === filters.subscriptionId.toLowerCase())
    .map((budget) => {
      const inScope = rows.filter((row) => row.subscriptionId.toLowerCase() === budget.subscriptionId.toLowerCase());
      if (requireEvidence && !inScope.length) return { budget, relation: 'unrelated' };
      const conditions = inScope.map((row) => budgetFilterMatches(budget.filter, row));
      const unfiltered = budget.filter !== undefined && Object.keys(budget.filter).length === 0;
      return { budget, relation: unfiltered ? 'Subscription-wide budget' : conditions.includes(true) ? 'Matching budget filter' : !conditions.length || conditions.includes(null) ? 'Filter applicability unverified' : 'unrelated' };
    })
    .filter((item) => item.relation !== 'unrelated');
}

export function BudgetContext({ state, details, filters = {}, showHeading = true, window, formatMoney }: { state: BudgetState; details?: CostDetailSummary; filters?: CostFilter; showHeading?: boolean; window?: CostWindow; formatMoney?: (value: number) => string }) {
  const [openBudget, setOpenBudget] = useState<string | null>(null);
  const [budgetQuery, setBudgetQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'attention' | 'within' | 'unknown'>('all');
  const rows = details?.rows.filter((row) => matchesCostFilter(row, filters)) ?? [];
  const related = relateBudgets(state.budgets, rows, filters);
  const query = budgetQuery.trim().toLocaleLowerCase();
  /* Over budget now, or forecast by Azure to finish the cycle over it. */
  const needsAttention = (budget: Budget) => ['warning', 'over'].includes(budgetThreshold(budget).tone)
    || (budget.forecastSpend !== null && Number.isFinite(budget.amount) && budget.forecastSpend > budget.amount);
  const budgets = related.filter(({ budget, relation }) => {
    if (query && ![budget.name, budget.scope, budget.subscriptionId, relation].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)) return false;
    if (statusFilter === 'attention') return needsAttention(budget);
    if (statusFilter === 'within') return budgetThreshold(budget).tone === 'within' && !needsAttention(budget);
    if (statusFilter === 'unknown') return budgetThreshold(budget).tone === 'unknown';
    return true;
  });
  const attentionCount = related.filter(({ budget }) => needsAttention(budget)).length;
  return <section className="cost-budget-context" aria-label="Applicable Azure budgets" aria-busy={state.loading}>
    {showHeading && <header className="cost-section-heading"><h3>Azure budget context</h3><button type="button" className="ghost-button" disabled={state.loading} onClick={state.refresh} aria-label="Refresh budget context" title="Refresh budget context"><RefreshCw size={16} /></button></header>}
    {!state.loading && !state.error && related.length > 0 && <div className="table-toolbar" role="search" aria-label="Filter budgets">
      <label className="table-search"><Search size={15} aria-hidden="true" /><input type="search" aria-label="Search budgets" placeholder="Search budgets" value={budgetQuery} onChange={(event) => setBudgetQuery(event.target.value)} /></label>
      <div className="segmented" role="group" aria-label="Budget status">
        {([['all', 'All'], ['attention', `Needs attention${attentionCount ? ` (${attentionCount})` : ''}`], ['within', 'On track'], ['unknown', 'Unavailable']] as const).map(([value, label]) => <button key={value} type="button" className={statusFilter === value ? 'active' : ''} aria-pressed={statusFilter === value} onClick={() => setStatusFilter(value)}>{label}</button>)}
      </div>
      <span className="table-count" role="status">{budgets.length} of {related.length} budgets</span>
    </div>}
    {state.loading ? <p role="status">Checking subscription budgets...</p> : state.error ? <p role="alert">{state.error}</p> : !related.length ? <p role="status">No matching subscription-scope budgets were returned.</p> : !budgets.length ? <p role="status">No budgets match this search or status.</p> : <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Azure budget status"><table className="data-table billing-table budget-table"><thead><tr><th>Budget / scope</th><th>Budget</th><th>Spend this cycle</th><th>Remaining</th><th>Azure forecast</th><th>Status</th></tr></thead><tbody>{budgets.map(({ budget, relation }) => {
      const status = budgetThreshold(budget);
      const key = `${budget.subscriptionId}:${budget.name}`;
      /* The drilldown needs a period to chart and a formatter to label it, so
         callers that do not supply them keep the plain table. */
      const drillable = Boolean(window && formatMoney && details?.status === 'complete');
      const money = budgetMoney(budget.currency);
      const native = (value: number | null) => value === null ? 'Unavailable' : money(value);
      const cycle = budgetCycle(budget, budget.observedAt || new Date().toISOString());
      const remaining = budget.currentSpend === null ? null : budget.amount - budget.currentSpend;
      return <Fragment key={`${budget.subscriptionId}:${budget.name}`}><tr><th>{drillable
        ? <button type="button" className="finding-link" aria-expanded={openBudget === key} aria-label={`Daily spend for ${budget.name}`} onClick={() => setOpenBudget((value) => value === key ? null : key)}>{budget.name}</button>
        : budget.name}<small>{relation}</small><details><summary>Scope</summary><span>{budget.scope || budget.subscriptionId}</span><pre>{JSON.stringify(budget.filter ?? 'Filter metadata not returned', null, 2)}</pre><small>Active: {budget.periodStart} - {budget.periodEnd || 'Open-ended'} / {budget.timeGrain}</small><small>Checked: {budget.observedAt || 'Not reported'}</small></details></th>
        <td>{native(budget.amount)}<small>{budget.timeGrain}</small><small>Expiry: <BudgetExpiry periodEnd={budget.periodEnd} /></small></td>
        <td>{native(budget.currentSpend)}{cycle && <small>{cycleLabel(cycle)} to date</small>}</td>
        <td className={remaining !== null && remaining < 0 ? 'cost-increase' : ''}>{remaining === null ? 'Unavailable' : remaining < 0 ? `${money(-remaining)} over` : `${money(remaining)} left`}</td>
        <td>{native(budget.forecastSpend)}{budget.forecastSpend !== null && <small>{budget.forecastSpend > budget.amount ? `Overrun ${money(budget.forecastSpend - budget.amount)}` : 'Within budget'}</small>}</td>
        <td><span className={`budget-status budget-${status.tone}`}>{status.label}</span>{status.tone === 'within' && budget.forecastSpend !== null && budget.forecastSpend > budget.amount && <span className="budget-status budget-forecast">Forecast over</span>}</td></tr>
      {drillable && openBudget === key && window && formatMoney && <tr><td colSpan={6}><BudgetDailyChart budget={budget} details={details} window={window} formatMoney={formatMoney} /></td></tr>}
      </Fragment>;
    })}</tbody></table></div>}
    <p className="billing-provenance">Azure budget ActualCost for each budget's current cycle, which can differ from the report month. Amber: up to 10% over; red: more than 10% over. A subscription-wide budget is not an application allocation or spending cap.</p>
  </section>;
}
