import type { Budget, CostDetailRow, CostHierarchyItem } from './models';
import type { CostDimension, CostFilter, CostGroup, CostWindow } from './costDetails';
import { costWindowDates } from './costDetails';

export type CostQueryOptions = {
  limit?: number; sort?: 'cost' | 'change'; nonzero?: boolean; currentOnly?: boolean; spikesOnly?: boolean;
  untaggedOnly?: boolean;
};
export type CostMetrics = {
  total: number; currentTotal: number; previousTotal: number; largest: number;
  spikeCount: number; subscriptionCount: number;
  observedCurrentTotal: number;
};
export type CostGroupPage = CostMetrics & { items: CostGroup[]; nextOffset: number | null };
export type DailyCostSeries = {
  subscriptionId: string; subscriptionName: string;
  days: { date: string; previousDate: string; current: number | null; previous: number | null }[];
};
export type CostFacets = {
  subscriptionId: [string, string][]; serviceName: [string, string][];
  resourceGroup: [string, string][]; region: [string, string][];
  tagKey: string[]; tagValue: string[];
  tagPair: [string, string][];
};
export type BudgetDailyData = { dates: string[]; values: (number | null)[]; verified: boolean };
export type RelatedBudget = { budget: Budget; relation: string };
export type CostQueryBody = Record<string, unknown>;
export type CostTransport = <T>(body: CostQueryBody, signal: AbortSignal) => Promise<T>;
export type ReportCollection = { path: string; total: number; context?: Record<string, string> };
export type RecordQueryOptions = {
  includePaths?: string[]; search?: string; searchFields?: string[]; searchPaths?: string[];
  searchAliases?: Record<string, string[]>; equals?: Record<string, string>; oneOf?: Record<string, string[]>;
  hasArrays?: string[]; arrayMatches?: { path: string; field: string; value: string }[];
  sums?: Record<string, { field: string; equals?: Record<string, string>; operation?: 'sum' | 'max' }>;
  facets?: Record<string, string>; metadataOnly?: boolean; byPath?: boolean; facetsOnly?: boolean; facetOrder?: 'label' | 'source';
};
export type RecordProjection<T> = {
  items: { path: string; value: T; context: Record<string, string> }[];
  total: number; unfilteredTotal: number; sums: Record<string, number>;
  facets: Record<string, [string, string][]>; nextOffset: number | null;
  byPath?: Record<string, { total: number; sums: Record<string, number> }>;
};

export interface ReportDataReader extends CostDataReader {
  readonly collections: ReportCollection[];
  records<T>(paths: string[], query: RecordQueryOptions, limit: number): RecordProjection<T>;
  total(path: string): number | undefined;
  hierarchy(subscriptionId: string | null, resourceGroup: string | null): {
    items: { key: string; label: string; detail: string; spend: number; resource: CostHierarchyItem | null }[];
    total: number; sourceCount: number; attributedTotal: number; subscriptionName: string | null;
  };
}

export interface CostDataReader {
  groups(window: CostWindow, filters: CostFilter, dimension: CostDimension, previous?: CostWindow, options?: CostQueryOptions): CostGroupPage;
  metrics(window: CostWindow, filters: CostFilter, options?: CostQueryOptions): CostMetrics;
  daily(window: CostWindow, filters: CostFilter, observedOnly?: boolean): DailyCostSeries[];
  topSeries(window: CostWindow, filters: CostFilter, dimension: CostDimension, limit: number): { dates: string[]; series: { id: string; name: string; points: (number | null)[] }[] };
  facets(tagKey?: string, tagPairs?: boolean): CostFacets;
  budgetDaily(budget: Budget, window: CostWindow, filters: CostFilter): BudgetDailyData;
  relatedBudgets(budgets: Budget[], filters: CostFilter, requireEvidence?: boolean): RelatedBudget[];
  sources(window: CostWindow, filters: CostFilter, dimension: CostDimension, groupId: string, previous: CostWindow | undefined, limit: number): { items: Omit<CostDetailRow, 'dailyCosts'>[]; total: number };
  withFilters(filters: CostFilter): CostDataReader;
  forGroup(dimension: CostDimension, sourceId: string, tagKey?: string): CostDataReader;
  dispose(): void;
}

type CacheEntry =
  | { state: 'pending'; promise: Promise<void>; bytes: number }
  | { state: 'fulfilled'; value: unknown; bytes: number }
  | { state: 'rejected'; error: unknown; bytes: number };

class SnapshotQueries {
  private readonly entries = new Map<string, CacheEntry>();
  private readonly controller = new AbortController();
  private bytes = 0;
  private disposed = false;
  constructor(private readonly request: CostTransport) {}

  read<T>(body: CostQueryBody): T {
    if (this.disposed) throw new Error('The saved report has been closed. Reopen it before loading more data.');
    const key = JSON.stringify(body);
    const entry = this.entries.get(key);
    if (entry) {
      this.entries.delete(key);
      this.entries.set(key, entry);
      if (entry.state === 'fulfilled') return entry.value as T;
      if (entry.state === 'rejected') throw entry.error;
      throw entry.promise;
    }
    const overhead = key.length * 2 + 512;
    const promise = this.request<T>(body, this.controller.signal).then(
      value => {
        if (this.disposed) return;
        const bytes = overhead + new TextEncoder().encode(JSON.stringify(value)).length;
        this.bytes += bytes - overhead;
        this.entries.delete(key);
        this.entries.set(key, { state: 'fulfilled', value, bytes });
        for (const [oldKey, old] of this.entries) {
          if (this.bytes <= 64 * 1024 * 1024) break;
          if (oldKey !== key && old.state !== 'pending') {
            this.bytes -= old.bytes;
            this.entries.delete(oldKey);
          }
        }
      },
      error => { if (!this.disposed) this.entries.set(key, { state: 'rejected', error, bytes: overhead }); },
    );
    this.bytes += overhead;
    this.entries.set(key, { state: 'pending', promise, bytes: overhead });
    throw promise;
  }

  dispose() {
    this.disposed = true;
    this.controller.abort();
    this.entries.clear();
    this.bytes = 0;
  }
}

class SqlCostReader implements ReportDataReader {
  constructor(
    private readonly queries: SnapshotQueries,
    private readonly constraints: CostFilter[] = [],
    private readonly selection: { dimension: CostDimension; sourceId: string; tagKey?: string }[] = [],
    readonly collections: ReportCollection[] = [],
  ) {}

  private body(body: CostQueryBody): CostQueryBody {
    return { constraints: this.constraints, groupConstraints: this.selection, ...body };
  }

  groups(window: CostWindow, filters: CostFilter, dimension: CostDimension, previous?: CostWindow, options: CostQueryOptions = {}): CostGroupPage {
    const requested = options.limit ?? 100;
    let offset = 0;
    let first: CostGroupPage | null = null;
    const items: CostGroup[] = [];
    while (items.length < requested) {
      const page = this.queries.read<CostGroupPage>(this.body({
        kind: 'groups', window, filters, dimension, previous,
        ...options, offset, limit: 50,
      }));
      first ??= page;
      items.push(...page.items);
      if (page.nextOffset === null) break;
      if (!Number.isInteger(page.nextOffset) || page.nextOffset <= offset || !page.items.length) throw new Error('Saved cost pagination did not advance.');
      offset = page.nextOffset;
    }
    if (!first) throw new Error('Saved cost query did not return a page.');
    const visible = items.slice(0, requested);
    return { ...first, items: visible, nextOffset: visible.length < first.total ? visible.length : null };
  }

  metrics(window: CostWindow, filters: CostFilter, options: CostQueryOptions = {}): CostMetrics {
    return this.queries.read<CostMetrics>(this.body({ kind: 'metrics', window, filters, ...options }));
  }

  daily(window: CostWindow, filters: CostFilter, observedOnly = false): DailyCostSeries[] {
    return this.queries.read<{ series: DailyCostSeries[] }>(
      this.body({ kind: 'daily', window, filters, observedOnly }),
    ).series;
  }

  topSeries(window: CostWindow, filters: CostFilter, dimension: CostDimension, limit: number) {
    const groups = this.groups(window, filters, dimension, undefined, { limit }).items;
    const dates = costWindowDates(window);
    const pending: Promise<unknown>[] = [];
    const series = groups.flatMap(group => {
      try {
        const daily = this.forGroup(dimension, group.id, filters.tagKey).daily(window, filters, true);
        return [{ id: group.id, name: group.name, points: dates.map((_, index) => {
          const observed = daily.map(item => item.days[index]?.current).filter((value): value is number => value !== null && value !== undefined);
          return observed.length ? observed.reduce((sum, value) => sum + value, 0) : null;
        }) }];
      } catch (error) {
        if (!(error instanceof Promise)) throw error;
        pending.push(error);
        return [];
      }
    });
    if (pending.length) throw Promise.all(pending);
    return { dates, series };
  }

  facets(tagKey = '', tagPairs = false): CostFacets {
    const result: CostFacets = { subscriptionId: [], serviceName: [], resourceGroup: [], region: [], tagKey: [], tagValue: [], tagPair: [] };
    let offset = 0;
    while (true) {
      const page = this.queries.read<{ items: { field: keyof CostFacets; value: string; label: string }[]; nextOffset: number | null }>(
        this.body({ kind: 'facets', tagKey, tagPairs, offset, limit: 100 }),
      );
      for (const item of page.items) {
        if (item.field === 'tagKey' || item.field === 'tagValue') result[item.field].push(item.value);
        else result[item.field].push([item.value, item.label]);
      }
      if (page.nextOffset === null) return result;
      if (!Number.isInteger(page.nextOffset) || page.nextOffset <= offset || !page.items.length) throw new Error('Saved filter pagination did not advance.');
      offset = page.nextOffset;
    }
  }

  budgetDaily(budget: Budget, window: CostWindow, filters: CostFilter): BudgetDailyData {
    return this.queries.read<BudgetDailyData>(this.body({
      kind: 'budget', window, filters,
      budget: { subscriptionId: budget.subscriptionId, name: budget.name, filter: budget.filter },
    }));
  }

  relatedBudgets(budgets: Budget[], filters: CostFilter, requireEvidence = false): RelatedBudget[] {
    const related = new Map<string, string>();
    for (let offset = 0; offset < budgets.length; offset += 20) {
      const result = this.queries.read<{ relations: { subscriptionId: string; name: string; relation: string }[] }>(this.body({
        kind: 'budgetRelations', filters, requireEvidence,
        budgets: budgets.slice(offset, offset + 20).map(budget => ({
          subscriptionId: budget.subscriptionId, name: budget.name, filter: budget.filter,
        })),
      }));
      for (const item of result.relations) related.set(`${item.subscriptionId.toLowerCase()}\0${item.name}`, item.relation);
    }
    return budgets.flatMap(budget => {
      const relation = related.get(`${budget.subscriptionId.toLowerCase()}\0${budget.name}`);
      return relation ? [{ budget, relation }] : [];
    });
  }

  sources(window: CostWindow, filters: CostFilter, dimension: CostDimension, sourceId: string, previous: CostWindow | undefined, limit: number) {
    const items: Omit<CostDetailRow, 'dailyCosts'>[] = [];
    let offset = 0;
    let total = 0;
    while (items.length < limit) {
      const page = this.queries.read<{ items: Omit<CostDetailRow, 'dailyCosts'>[]; total: number; nextOffset: number | null }>(
        this.body({ kind: 'sources', window, filters, dimension, sourceId, previous, offset, limit: 50 }),
      );
      total = page.total;
      items.push(...page.items);
      if (page.nextOffset === null) break;
      if (!Number.isInteger(page.nextOffset) || page.nextOffset <= offset || !page.items.length) throw new Error('Saved source pagination did not advance.');
      offset = page.nextOffset;
    }
    return { items: items.slice(0, limit), total };
  }

  withFilters(filters: CostFilter): CostDataReader {
    return new SqlCostReader(this.queries, [...this.constraints, filters], this.selection);
  }

  forGroup(dimension: CostDimension, sourceId: string, tagKey?: string): CostDataReader {
    return new SqlCostReader(this.queries, this.constraints, [...this.selection, { dimension, sourceId, tagKey }], this.collections);
  }

  records<T>(paths: string[], query: RecordQueryOptions, limit: number): RecordProjection<T> {
    if (!paths.length) throw new Error('A declared report collection is required.');
    if (query.metadataOnly) return this.queries.read<RecordProjection<T>>({ kind: 'records', paths, ...query, limit: 1 });
    const items: RecordProjection<T>['items'] = [];
    let first: RecordProjection<T> | null = null;
    let offset = 0;
    while (items.length < limit) {
      const page = this.queries.read<RecordProjection<T>>({ kind: 'records', paths, ...query, offset, limit: query.facetsOnly ? 5000 : 50 });
      first ??= page;
      items.push(...page.items);
      if (page.nextOffset === null) break;
      if (!Number.isInteger(page.nextOffset) || page.nextOffset <= offset || !page.items.length) throw new Error('Saved report pagination did not advance.');
      offset = page.nextOffset;
    }
    if (!first) throw new Error('Saved report query did not return a page.');
    const visible = items.slice(0, limit);
    return { ...first, items: visible, nextOffset: visible.length < first.total ? visible.length : null };
  }

  total(path: string) { return this.collections.find(item => item.path === path)?.total; }
  hierarchy(subscriptionId: string | null, resourceGroup: string | null) {
    return this.queries.read<ReturnType<ReportDataReader['hierarchy']>>({ kind: 'hierarchy', subscriptionId, resourceGroup });
  }
  dispose() { this.queries.dispose(); }
  toJSON() { return undefined; }
}

export function createCostDataReader(request: CostTransport): CostDataReader {
  return new SqlCostReader(new SnapshotQueries(request));
}

export function createReportDataReader(collections: ReportCollection[], request: CostTransport): ReportDataReader {
  return new SqlCostReader(new SnapshotQueries(request), [], [], collections);
}
