import { useEffect, useMemo, useRef, useState } from 'react';
import type { CostDetailSummary } from '../report/models';
import { compareCostGroups, costWindowDates, matchesCostFilter, type CostDimension, type CostFilter, type CostWindow } from '../report/costDetails';

type Formatter = (value: number) => string;

const SERIES_COLORS = [
  'var(--color-category-compute)',
  'var(--color-metric-green)',
  'var(--color-category-databases)',
  'var(--color-category-ai)',
  'var(--color-category-networking)',
  'var(--color-category-other)',
];

const CHART_HEIGHT = 260;
const CHART_MIN_WIDTH = 640;

/* Charts are authored in user units and stretched to the container, and a
   viewBox scales uniformly - so a fixed viewBox magnifies the labels inside
   it. Tracking the measured width keeps the scale at exactly 1 and the axis
   type at the same size as the rest of the interface. */
function useChartWidth(ref: { current: HTMLElement | null }) {
  const [width, setWidth] = useState(880);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      const measured = Math.round(node.clientWidth);
      if (measured > 0) setWidth(Math.max(CHART_MIN_WIDTH, measured));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export type TrendSeries = { id: string; name: string; points: (number | null)[] };

/* A multi-series daily line chart over an arbitrary set of groups.

   Deliberately dumb about where its series come from, so the same chart draws
   the top services, the top resource groups or the top resources without
   three near-identical implementations. */
export function DailyTrendChart({
  dates,
  series,
  formatMoney,
  emptyMessage = 'No cost evidence matches the selected range and filters.',
  ariaLabel = 'Daily cost trend',
}: {
  dates: string[];
  series: TrendSeries[];
  formatMoney: Formatter;
  emptyMessage?: string;
  ariaLabel?: string;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const width = useChartWidth(frame);
  const height = CHART_HEIGHT;
  const padLeft = 78;
  const padRight = 20;
  const padTop = 16;
  const baseline = height - 40;

  const values = series.flatMap((item) => item.points.filter((value): value is number => value !== null));
  const maximum = values.reduce((peak, value) => Math.max(peak, value), 0) || 1;
  const xFor = (index: number) => padLeft + index * (width - padLeft - padRight) / Math.max(1, dates.length - 1);
  const yFor = (value: number) => baseline - (value / maximum) * (baseline - padTop);

  const paths = useMemo(() => series.map((item) => {
    let connected = false;
    const path = item.points.map((value, index) => {
      if (value === null) { connected = false; return ''; }
      const command = connected ? 'L' : 'M';
      connected = true;
      return `${command}${xFor(index).toFixed(2)},${yFor(value).toFixed(2)}`;
    }).join(' ');
    return { ...item, path };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [series, width, maximum, dates.length]);

  if (!dates.length || !series.length || !values.length) {
    return <p role="status" className="empty-state">{emptyMessage}</p>;
  }

  const ticks = [0, 1, 2, 3, 4].map((step) => maximum * step / 4);
  const labelStep = Math.max(1, Math.ceil(dates.length / 8));

  return (
    <div className="trend-chart">
      <div className="cost-chart-legend">
        {series.map((item, index) => (
          <span key={item.id}><i style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }} />{item.name}</span>
        ))}
      </div>
      <div className="cost-chart-scroll" ref={frame} tabIndex={0} role="region" aria-label={ariaLabel}>
        <svg viewBox={`0 0 ${width} ${height}`} className="cost-comparison-chart" role="img" aria-label={ariaLabel}>
          {ticks.map((value) => (
            <g key={value}>
              <line x1={padLeft} x2={width - padRight} y1={yFor(value)} y2={yFor(value)} className="cost-chart-grid" />
              <text x={padLeft - 10} y={yFor(value)} textAnchor="end" dominantBaseline="middle">{formatMoney(value)}</text>
            </g>
          ))}
          {paths.map((item, index) => (
            <path
              key={item.id}
              d={item.path}
              className="cost-chart-current"
              style={{ color: SERIES_COLORS[index % SERIES_COLORS.length] }}
            />
          ))}
          {dates.map((date, index) => (
            index % labelStep === 0 || index === dates.length - 1
              ? <text key={date} x={xFor(index)} y={height - 14} textAnchor="middle">{date.slice(5)}</text>
              : null
          ))}
        </svg>
      </div>
    </div>
  );
}

/* Turns the top groups on a dimension into daily series. Kept next to the
   chart because the "top N by total, then per-day" shape is the same every
   time it is used, and doing it in each caller invites three subtly
   different definitions of "top". */
export function useTopGroupSeries({
  details,
  window,
  filters,
  dimension,
  limit = 5,
}: {
  details?: CostDetailSummary;
  window: CostWindow;
  filters: CostFilter;
  dimension: CostDimension;
  limit?: number;
}): { dates: string[]; series: TrendSeries[] } {
  const filterKey = JSON.stringify(filters);
  return useMemo(() => {
    const dates = costWindowDates(window);
    if (!details || details.status !== 'complete' || !dates.length) return { dates, series: [] };
    const groups = compareCostGroups(details, window, filters, dimension).slice(0, limit);
    const series = groups.map((group) => {
      const rows = group.sources.filter((row) => matchesCostFilter(row, filters));
      return {
        id: group.id,
        name: group.name,
        points: dates.map((date) => {
          const covered = rows.filter((row) => row.dailyCosts[date] !== undefined);
          return covered.length ? covered.reduce((sum, row) => sum + row.dailyCosts[date], 0) : null;
        }),
      };
    });
    return { dates, series };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [details, window.startDate, window.endDate, filterKey, dimension, limit]);
}
