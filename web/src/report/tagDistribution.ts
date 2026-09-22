import type { TagDailyCostTrendSummary } from './models';
import type { CostWindow } from './costDetails';

export function tagDistribution(summary: TagDailyCostTrendSummary, rangeDays: number, window?: CostWindow) {
  const availableDates = summary.windowDates ?? [];
  const dates = window
    ? availableDates.filter((date) => date >= window.startDate && date <= window.endDate)
    : availableDates.slice(-rangeDays);
  const included = new Set(dates);
  const series = summary.distributionSeries ?? [];
  if (!dates.length || !series.length) return { available: false, signed: false, total: 0, days: 0, items: [] };
  const items = series.map(item => ({
    tagValue: item.tagValue,
    avgHourly: item.days.filter(day => included.has(day.date)).reduce((sum, day) => sum + day.totalCost, 0) / (24 * dates.length),
  })).filter(item => item.avgHourly !== 0).sort((first, second) => second.avgHourly - first.avgHourly);
  return { available: true, signed: items.some(item => item.avgHourly < 0), total: items.reduce((sum, item) => sum + item.avgHourly, 0), days: dates.length, items };
}