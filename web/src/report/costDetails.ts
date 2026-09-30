import type { CostDetailRow, CostDetailSummary } from './models';

export type CostWindow = { startDate: string; endDate: string };
export type CostDimension = 'resource' | 'subscription' | 'service' | 'resourceType' | 'region' | 'resourceGroup' | 'tag';
export type CostFilter = { subscriptionId?: string; resourceId?: string; serviceName?: string; resourceGroup?: string; region?: string; tagKey?: string; tagValue?: string; requiredTagKeys?: string };
const DAY_MS = 86400000;

export function validCostDate(value: string): boolean {
  const instant = Date.parse(`${value}T00:00:00Z`);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(instant) && new Date(instant).toISOString().slice(0, 10) === value;
}

export function shiftCostDate(value: string, days: number): string {
  return validCostDate(value) ? new Date(Date.parse(`${value}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10) : '';
}

export function costWindowDates(window: CostWindow): string[] {
  if (!validCostDate(window.startDate) || !validCostDate(window.endDate)) return [];
  const count = (Date.parse(window.endDate) - Date.parse(window.startDate)) / DAY_MS + 1;
  if (count < 1 || count > 366) return [];
  return Array.from({ length: count }, (_, index) => shiftCostDate(window.startDate, index));
}

export function presetCostWindow(dates: string[], days: number): CostWindow {
  const endDate = dates.filter(validCostDate).sort().at(-1) ?? '';
  return { startDate: shiftCostDate(endDate, 1 - days), endDate };
}

/* The report's headline figure is a calendar month. Defaulting the cost
   window to a rolling 30 days put $487.02 (Aug 2-31) beside $494
   (Aug 1-31) on the same page, so the default is the assessed month. */
export function monthCostWindow(periodStart: string, periodEnd: string): CostWindow | null {
  return validCostDate(periodStart) && validCostDate(periodEnd) && periodStart <= periodEnd
    ? { startDate: periodStart, endDate: periodEnd }
    : null;
}

function lastDayOfMonth(year: number, monthIndex: number): number {
  return new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
}

/* A window that starts on the 1st and ends on the month's last day. */
export function isCalendarMonth(window: CostWindow): boolean {
  if (!validCostDate(window.startDate) || !validCostDate(window.endDate)) return false;
  const [year, month, day] = window.startDate.split('-').map(Number);
  return day === 1
    && window.endDate === `${window.startDate.slice(0, 8)}${String(lastDayOfMonth(year, month - 1)).padStart(2, '0')}`;
}

export function previousCostWindow(window: CostWindow): CostWindow {
  /* A calendar month compares with the previous calendar month, so days pair
     by day of month (Aug 2 with Jul 2). Shifting by day count instead paired
     Aug 2 with Jul 3, and for a 30-day month would have borrowed a day from
     the month before that. */
  if (isCalendarMonth(window)) {
    const [year, month] = window.startDate.split('-').map(Number);
    const previousYear = month === 1 ? year - 1 : year;
    const previousMonth = month === 1 ? 12 : month - 1;
    const prefix = `${previousYear}-${String(previousMonth).padStart(2, '0')}`;
    return { startDate: `${prefix}-01`, endDate: `${prefix}-${String(lastDayOfMonth(previousYear, previousMonth - 1)).padStart(2, '0')}` };
  }
  const days = costWindowDates(window).length;
  return days ? { startDate: shiftCostDate(window.startDate, -days), endDate: shiftCostDate(window.startDate, -1) } : { startDate: '', endDate: '' };
}

/* The date in the previous window that a given day is compared with. For a
   calendar month that is the same day of the previous month; a day with no
   counterpart (the 31st against a 30-day month) has none, rather than being
   paired with the 30th a second time and counted twice in period totals. */
export function comparisonDate(window: CostWindow, index: number): string {
  const previous = previousCostWindow(window);
  if (isCalendarMonth(window)) return costWindowDates(previous)[index] ?? '';
  return shiftCostDate(previous.startDate, index);
}

export function costTagValue(row: CostDetailRow, key: string): string | undefined {
  return Object.entries(row.tags).find(([name]) => name.toLowerCase() === key.toLowerCase())?.[1];
}

export function matchesCostFilter(row: CostDetailRow, filter: CostFilter): boolean {
  if (filter.requiredTagKeys !== undefined) {
    try {
      const keys: string[] = JSON.parse(filter.requiredTagKeys);
      if (!Array.isArray(keys) || !keys.every((key) => typeof key === 'string')) return false;
      const missing = keys.length ? keys.some((key) => !costTagValue(row, key)?.trim()) : !Object.values(row.tags).some((value) => value.trim());
      if (!row.resourceId || !missing) return false;
    } catch { return false; }
  }
  return (!filter.subscriptionId || row.subscriptionId.toLowerCase() === filter.subscriptionId.toLowerCase())
    && (!filter.resourceId || row.resourceId.toLowerCase() === filter.resourceId.toLowerCase())
    && (!filter.serviceName || row.serviceName === filter.serviceName)
    && (!filter.resourceGroup || row.resourceGroup.toLowerCase() === filter.resourceGroup.toLowerCase())
    && (!filter.region || row.region.toLowerCase() === filter.region.toLowerCase())
    && (filter.tagValue === undefined || !filter.tagKey || costTagValue(row, filter.tagKey) === filter.tagValue);
}

export function costCoverage(details: CostDetailSummary | undefined, window: CostWindow) {
  const dates = costWindowDates(window);
  const available = new Set(details?.status === 'complete' ? details.dates : []);
  const coveredDays = dates.filter((day) => available.has(day)).length;
  return { dates, coveredDays, complete: dates.length > 0 && coveredDays === dates.length };
}

export function costChange(current: number | null, previous: number | null) {
  const delta = current === null || previous === null ? null : current - previous;
  return { delta, percentage: delta === null || previous === null || previous === 0 ? null : delta / Math.abs(previous) * 100 };
}

export function compareCostGroups(details: CostDetailSummary | undefined, window: CostWindow, filter: CostFilter = {}, dimension: CostDimension = 'resource', previous = previousCostWindow(window)) {
  const currentCoverage = costCoverage(details, window);
  const previousCoverage = costCoverage(details, previous);
  const currentDates = new Set(currentCoverage.dates);
  const previousDates = new Set(previousCoverage.dates);
  const groups = new Map<string, { id: string; name: string; subscriptionId: string; subscriptionName: string; current: number; previous: number; sources: CostDetailRow[] }>();
  if (details?.status === 'complete') for (const row of details.rows) {
    if (!matchesCostFilter(row, filter)) continue;
    if (!Object.keys(row.dailyCosts).some((day) => currentDates.has(day) || previousDates.has(day))) continue;
    const value = dimension === 'resource' ? row.resourceId || JSON.stringify([row.resourceName, row.serviceName, row.resourceGroup])
      : dimension === 'subscription' ? row.subscriptionId : dimension === 'service' ? row.serviceName : dimension === 'resourceType' ? row.resourceType
        : dimension === 'region' ? row.region : dimension === 'resourceGroup' ? row.resourceGroup : costTagValue(row, filter.tagKey ?? '') ?? null;
    const name = dimension === 'resource' ? row.resourceName || 'Unattributed charge'
      : dimension === 'subscription' ? row.subscriptionName : value ?? 'Untagged';
    const key = JSON.stringify([row.subscriptionId, value]);
    const group = groups.get(key) ?? { id: key, name, subscriptionId: row.subscriptionId, subscriptionName: row.subscriptionName, current: 0, previous: 0, sources: [] };
    group.sources.push(row);
    for (const [day, amount] of Object.entries(row.dailyCosts)) {
      if (currentDates.has(day)) group.current += amount;
      if (previousDates.has(day)) group.previous += amount;
    }
    groups.set(key, group);
  }
  return [...groups.values()].map((group) => {
    const current = currentCoverage.complete ? group.current : null;
    const previous = previousCoverage.complete ? group.previous : null;
    const owners = group.sources.flatMap((row) => Object.entries(row.tags)
      .filter(([key]) => /^(owner|applicationowner|technicalowner|businessowner)$/i.test(key))
      .map(([, value]) => value));
    return { ...group, current, previous, ...costChange(current, previous), owners: [...new Set(owners)] };
  }).sort((first, second) => Math.abs(second.current ?? second.previous ?? 0) - Math.abs(first.current ?? first.previous ?? 0) || first.id.localeCompare(second.id));
}

export function dailySubscriptionCosts(details: CostDetailSummary | undefined, window: CostWindow, filter: CostFilter = {}) {
  const previous = previousCostWindow(window);
  const rows = details?.status === 'complete' ? details.rows.filter((row) => matchesCostFilter(row, filter)) : [];
  const subscriptions = [...new Map(rows.map((row) => [row.subscriptionId, row.subscriptionName])).entries()];
  const dates = costWindowDates(window);
  const available = new Set(details?.status === 'complete' ? details.dates : []);
  return subscriptions.map(([subscriptionId, subscriptionName]) => {
    const totals = new Map<string, number>();
    for (const row of rows) if (row.subscriptionId === subscriptionId) {
      for (const [day, amount] of Object.entries(row.dailyCosts)) totals.set(day, (totals.get(day) ?? 0) + amount);
    }
    return { subscriptionId, subscriptionName, days: dates.map((date, index) => {
      const previousDate = comparisonDate(window, index);
      return { date, previousDate, current: available.has(date) ? totals.get(date) ?? 0 : null, previous: previousDate && available.has(previousDate) ? totals.get(previousDate) ?? 0 : null };
    }) };
  });
}