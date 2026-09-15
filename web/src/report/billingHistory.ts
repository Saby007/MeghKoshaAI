import type { FullReport, TagDailyCostSeries } from './models';
import { matchesCostFilter, type CostFilter } from './costDetails';

export type BillingSource = Pick<FullReport, 'dailyCostTrend' | 'tagDailyCostTrend'> & Partial<Pick<FullReport, 'costDetails' | 'reportMetadata'>>;
export type BillingDayFilter = 'all' | 'weekdays' | 'weekends';
export type BillingTimeFilter = 'all' | 'business' | 'off-hours';
export type BillingDay = { date: string; hours: number; totalCost: number | null; averageHourlyCost: number | null };
export type BusinessCalendar = { start: string; end: string; timeZone: string };
export const DEFAULT_BUSINESS_CALENDAR: BusinessCalendar = { start: '09:00', end: '17:00', timeZone: 'UTC' };

const DAY_MS = 24 * 60 * 60 * 1000;
const timestamp = (date: string) => Date.parse(`${date}T00:00:00Z`);
const validDate = (date: string) => Number.isFinite(timestamp(date)) && new Date(timestamp(date)).toISOString().slice(0, 10) === date;
export const billingTagId = (series: TagDailyCostSeries) => JSON.stringify([series.tagKey, series.tagValue]);

export function validBusinessCalendar(calendar: BusinessCalendar): boolean {
  if (!calendar || !/^(?:[01]\d|2[0-3]):(?:00|15|30|45)$/.test(calendar.start) || !/^(?:[01]\d|2[0-3]):(?:00|15|30|45)$/.test(calendar.end) || calendar.start === calendar.end || typeof calendar.timeZone !== 'string' || !calendar.timeZone) return false;
  try { new Intl.DateTimeFormat('en-GB', { timeZone: calendar.timeZone }); return true; } catch { return false; }
}

export function businessHoursForDate(date: string, calendar: BusinessCalendar): number {
  if (!validDate(date) || !validBusinessCalendar(calendar)) throw new Error('Invalid business calendar');
  const clockMinutes = (value: string) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  const start = clockMinutes(calendar.start);
  const end = clockMinutes(calendar.end);
  const formatter = new Intl.DateTimeFormat('en-GB', { timeZone: calendar.timeZone, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  let hours = 0;
  for (let minute = 0; minute < 1440; minute += 15) {
    const parts = Object.fromEntries(formatter.formatToParts(timestamp(date) + minute * 60000).map((part) => [part.type, part.value]));
    const clock = Number(parts.hour) * 60 + Number(parts.minute);
    let weekday = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(parts.weekday);
    const inside = start < end ? clock >= start && clock < end : clock >= start || clock < end;
    if (start > end && clock < end) weekday = (weekday + 6) % 7;
    if (inside && weekday >= 1 && weekday <= 5) hours += .25;
  }
  return hours;
}

export function billingTags(source: BillingSource) {
  if (source.costDetails?.status === 'complete') {
    const tags = new Map<string, string>();
    for (const row of source.costDetails.rows) for (const [key, value] of Object.entries(row.tags)) tags.set(JSON.stringify([key, value]), `${key} / ${value}`);
    return [...tags].sort((first, second) => first[1].localeCompare(second[1])).map(([id, label]) => ({ id, label }));
  }
  if (source.tagDailyCostTrend.status === 'unavailable') return [];
  return source.tagDailyCostTrend.series.map((series) => ({
    id: billingTagId(series),
    label: series.tagKey ? `${series.tagKey} / ${series.tagValue}` : series.tagValue,
  }));
}

export function billingDates(source: BillingSource): string[] {
  if (source.costDetails?.status === 'complete') return source.costDetails.dates.filter(validDate).slice().sort();
  if (source.dailyCostTrend.status === 'unavailable') return [];
  return [...new Set(source.dailyCostTrend.days.filter((day) => validDate(day.date) && Number.isFinite(day.totalCost)).map((day) => day.date))].sort();
}

export function billingCostOnDate(source: BillingSource, date: string, tagId = ''): number | null {
  if (source.costDetails?.status === 'complete') {
    if (!validDate(date) || !source.costDetails.dates.includes(date)) return null;
    let filters: CostFilter = {};
    if (tagId) {
      if (!billingTags(source).some((tag) => tag.id === tagId)) return null;
      const [tagKey, tagValue] = JSON.parse(tagId);
      filters = { tagKey, tagValue };
    }
    return source.costDetails.rows.filter((row) => matchesCostFilter(row, filters)).reduce((sum, row) => sum + (row.dailyCosts[date] ?? 0), 0);
  }
  if (!validDate(date) || source.dailyCostTrend.status === 'unavailable') return null;
  const total = source.dailyCostTrend.days.find((day) => day.date === date)?.totalCost;
  if (total === undefined || !Number.isFinite(total)) return null;
  if (!tagId) return total;
  if (source.tagDailyCostTrend.status === 'unavailable') return null;
  const series = source.tagDailyCostTrend.series.find((item) => billingTagId(item) === tagId);
  if (!series) return null;
  const point = series.days.find((day) => day.date === date);
  if (point) return Number.isFinite(point.totalCost) ? point.totalCost : null;
  return source.tagDailyCostTrend.windowDates?.includes(date) ? 0 : null;
}

export function billingWindow(source: BillingSource, options: {
  rangeDays: number;
  dayFilter?: BillingDayFilter;
  timeFilter?: BillingTimeFilter;
  tagId?: string;
  startDate?: string;
  endDate?: string;
  businessCalendar?: BusinessCalendar;
}) {
  const dates = billingDates(source);
  const endDate = options.endDate ?? dates.at(-1) ?? null;
  const days: BillingDay[] = [];
  const calendar = options.businessCalendar ?? DEFAULT_BUSINESS_CALENDAR;
  const estimated = options.timeFilter === 'business' || options.timeFilter === 'off-hours';
  if (estimated && !validBusinessCalendar(calendar)) return { days, coveredDays: 0, coveredHours: 0, totalCost: null, estimated, averageHourlyCost: null, incomplete: true, calendarError: 'Use distinct quarter-hour start/end times and a valid IANA time zone.' };
  if (endDate && validDate(endDate) && dates.length) {
    const start = options.startDate ? timestamp(options.startDate) : Math.max(timestamp(dates[0]), timestamp(endDate) - (Math.max(1, options.rangeDays) - 1) * DAY_MS);
    if ((timestamp(endDate) - start) / DAY_MS >= 366) return { days, coveredDays: 0, coveredHours: 0, totalCost: null, estimated: false, averageHourlyCost: null, incomplete: true, calendarError: null };
    for (let instant = start; instant <= timestamp(endDate); instant += DAY_MS) {
      const weekday = new Date(instant).getUTCDay();
      const weekend = weekday === 0 || weekday === 6;
      if (options.dayFilter === 'weekends' && !weekend) continue;
      if (options.dayFilter === 'weekdays' && weekend) continue;
      const date = new Date(instant).toISOString().slice(0, 10);
      const businessHours = estimated ? businessHoursForDate(date, calendar) : 0;
      const hours = options.timeFilter === 'business' ? businessHours
        : options.timeFilter === 'off-hours' ? 24 - businessHours : 24;
      if (hours === 0) continue;
      const dailyCost = billingCostOnDate(source, date, options.tagId);
      const totalCost = dailyCost === null ? null : dailyCost * (hours / 24);
      days.push({ date, hours, totalCost, averageHourlyCost: dailyCost === null ? null : dailyCost / 24 });
    }
  }
  const covered = days.filter((day) => day.totalCost !== null);
  const totalCost = covered.length ? covered.reduce((sum, day) => sum + day.totalCost!, 0) : null;
  const coveredHours = covered.reduce((sum, day) => sum + day.hours, 0);
  return {
    days, coveredDays: covered.length, coveredHours, totalCost,
    estimated, calendarError: null,
    averageHourlyCost: totalCost === null ? null : totalCost / coveredHours,
    incomplete: covered.length !== days.length,
  };
}

export function compareBillingDates(source: BillingSource, baselineDate: string, comparisonDate: string, tagId = '') {
  const baseline = billingCostOnDate(source, baselineDate, tagId);
  const comparison = billingCostOnDate(source, comparisonDate, tagId);
  const delta = baseline === null || comparison === null ? null : comparison - baseline;
  return {
    baseline, comparison, delta,
    percentChange: delta === null || baseline === null || baseline === 0 ? null : delta / Math.abs(baseline) * 100,
  };
}