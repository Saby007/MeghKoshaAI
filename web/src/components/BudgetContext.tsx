import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { listBudgets } from '../api';
import type { Budget, CostDetailRow, CostDetailSummary, FullReport } from '../report/models';
import { costTagValue, matchesCostFilter, type CostFilter } from '../report/costDetails';

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

export function BudgetContext({ state, details, filters = {}, showHeading = true }: { state: BudgetState; details?: CostDetailSummary; filters?: CostFilter; showHeading?: boolean }) {
  const rows = details?.rows.filter((row) => matchesCostFilter(row, filters)) ?? [];
  const budgets = state.budgets.filter((budget) => !filters.subscriptionId || budget.subscriptionId.toLowerCase() === filters.subscriptionId.toLowerCase()).map((budget) => {
    const conditions = rows.filter((row) => row.subscriptionId.toLowerCase() === budget.subscriptionId.toLowerCase()).map((row) => budgetFilterMatches(budget.filter, row));
    const unfiltered = budget.filter !== undefined && Object.keys(budget.filter).length === 0;
    return { budget, relation: unfiltered ? 'Subscription-wide budget' : conditions.includes(true) ? 'Matching budget filter' : !conditions.length || conditions.includes(null) ? 'Filter applicability unverified' : 'unrelated' };
  }).filter((item) => item.relation !== 'unrelated');
  return <section className="cost-budget-context" aria-label="Applicable Azure budgets" aria-busy={state.loading}>
    {showHeading && <header className="cost-section-heading"><h3>Azure budget context</h3><button type="button" className="ghost-button" disabled={state.loading} onClick={state.refresh} aria-label="Refresh budget context" title="Refresh budget context"><RefreshCw size={16} /></button></header>}
    {state.loading ? <p role="status">Checking subscription budgets...</p> : state.error ? <p role="alert">{state.error}</p> : !budgets.length ? <p role="status">No matching subscription-scope budgets were returned.</p> : <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Azure budget status"><table className="data-table billing-table"><thead><tr><th>Budget / scope</th><th>Budget amount</th><th>Current spend</th><th>Budget remaining</th><th>Azure forecast</th><th>Status</th></tr></thead><tbody>{budgets.map(({ budget, relation }) => {
      const status = budgetThreshold(budget);
      const native = (value: number | null) => value === null ? 'Unavailable' : `${budget.currency} ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
      return <tr key={`${budget.subscriptionId}:${budget.name}`}><th>{budget.name}<small>{relation}</small><details><summary>Budget scope</summary><span>{budget.scope || budget.subscriptionId}</span><pre>{JSON.stringify(budget.filter ?? 'Filter metadata not returned', null, 2)}</pre><small>Active: {budget.periodStart} - {budget.periodEnd || 'Open-ended'} / {budget.timeGrain}</small><small>Checked: {budget.observedAt || 'Not reported'}</small></details></th><td>{native(budget.amount)}</td><td>{native(budget.currentSpend)}</td><td>{native(budget.currentSpend === null ? null : budget.amount - budget.currentSpend)}</td><td>{native(budget.forecastSpend)}{budget.forecastSpend !== null && <small>{budget.forecastSpend > budget.amount ? 'Projected overrun' : 'Projected within budget'}: {native(Math.abs(budget.amount - budget.forecastSpend))}</small>}</td><td><span className={`budget-status budget-${status.tone}`}>{status.label}</span></td></tr>;
    })}</tbody></table></div>}
    <p className="billing-provenance">Native budget current-cycle ActualCost, not the selected historical EffectiveCost window. Green: within budget; amber: up to 10% over; red: more than 10% over. A subscription-wide budget is not an application allocation or spending cap.</p>
  </section>;
}