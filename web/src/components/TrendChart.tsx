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

/* Every day carries a label, so the axis - not the plot - decides how wide the
   chart has to be.

   Two constants set that. AXIS_LABEL_WIDTH is what "MM-DD" occupies at
   --font-size-xs with a gap either side; below it, horizontal labels collide,
   so they tilt. AXIS_SLOT_MIN is the pitch a tilted label needs: at -60 degrees
   consecutive labels clear each other once slot * sin(60) exceeds the line
   height, which is ~15px here. When even that does not fit in the measured
   box the chart grows past it and the surrounding container scrolls, because
   dropping a day to save horizontal space is the one thing that is not on
   offer. */
const AXIS_LABEL_WIDTH = 38;
const AXIS_SLOT_MIN = 18;
const AXIS_ROTATED_HEIGHT = 34;

/* Tilted labels descend below the axis, so the frame has to grow to hold them
   - otherwise they are simply clipped by the viewBox, which is the same as not
   drawing them. */
export function dayAxis(dates: string[], measured: number, padLeft: number, padRight: number, divisor: number) {
  const width = Math.max(measured, padLeft + padRight + dates.length * AXIS_SLOT_MIN);
  const pitch = (width - padLeft - padRight) / Math.max(1, divisor);
  const rotated = pitch < AXIS_LABEL_WIDTH;
  return { width, rotated, extraHeight: rotated ? AXIS_ROTATED_HEIGHT : 0 };
}

export function DayAxis({ dates, xFor, y, rotated }: {
  dates: string[]; xFor: (index: number) => number; y: number; rotated: boolean;
}) {
  return (
    <>
      {dates.map((date, index) => {
        const x = xFor(index);
        return rotated
          ? <text key={date} x={x} y={y} textAnchor="end" transform={`rotate(-60 ${x.toFixed(2)} ${y})`}>{date.slice(5)}</text>
          : <text key={date} x={x} y={y} textAnchor="middle">{date.slice(5)}</text>;
      })}
    </>
  );
}

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
  const measured = useChartWidth(frame);
  const padLeft = 78;
  const padRight = 20;
  const padTop = 16;
  const axis = dayAxis(dates, measured, padLeft, padRight, Math.max(1, dates.length - 1));
  const width = axis.width;
  const height = CHART_HEIGHT + axis.extraHeight;
  const baseline = CHART_HEIGHT - 40;

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

  return (
    <div className="trend-chart">
      <div className="cost-chart-legend">
        {series.map((item, index) => (
          <span key={item.id}><i style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }} />{item.name}</span>
        ))}
      </div>
      <div className="cost-chart-scroll" ref={frame} tabIndex={0} role="region" aria-label={ariaLabel}>
        <svg viewBox={`0 0 ${width} ${height}`} style={{ width: `${width}px`, minWidth: `${width}px` }} className="cost-comparison-chart" role="img" aria-label={ariaLabel}>
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
          <DayAxis dates={dates} xFor={xFor} y={baseline + (axis.rotated ? 16 : 26)} rotated={axis.rotated} />
        </svg>
      </div>
    </div>
  );
}

/* A single-series daily bar chart where every day is a control.

   Bars rather than a line because each day here is a thing you can open, and
   a line implies a continuous quantity you read between the points. The
   interactive layer is real HTML buttons positioned over the plot, not the
   <rect>s themselves: SVG elements are not HTMLElements, so they carry no
   native button semantics and cannot be clicked programmatically. The buttons
   also span the full column height, so the target is the day rather than the
   few pixels the bar happens to occupy on a quiet day.

   Positioning the overlay in pixels is only sound because useChartWidth keeps
   the viewBox at exactly 1:1 with the rendered box, so a user unit is a CSS
   pixel. */
export function DailyBarChart({
  dates,
  values,
  formatMoney,
  seriesName,
  selectedDate = null,
  onSelectDate,
  selectLabel = (date) => date,
  emptyMessage = 'No cost evidence matches the selected range and filters.',
  ariaLabel = 'Daily cost',
  reference = null,
}: {
  dates: string[];
  values: (number | null)[];
  formatMoney: Formatter;
  seriesName: string;
  selectedDate?: string | null;
  onSelectDate?: (date: string) => void;
  selectLabel?: (date: string, value: number | null) => string;
  emptyMessage?: string;
  ariaLabel?: string;
  reference?: { value: number; label: string } | null;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const measured = useChartWidth(frame);
  const padLeft = 78;
  const padRight = 20;
  const padTop = 16;
  const axis = dayAxis(dates, measured, padLeft, padRight, dates.length);
  const width = axis.width;
  const height = CHART_HEIGHT + axis.extraHeight;
  const baseline = CHART_HEIGHT - 40;

  const present = values.filter((value): value is number => value !== null);
  /* The reference line is part of the picture, so it has to fit inside the
     scale. Leaving it out of the maximum drew an allowance line pinned to the
     top of the frame whenever spend was below budget, which reads as "at
     budget" - the opposite of what it means. */
  const maximum = Math.max(...present, reference?.value ?? 0, 0) || 1;
  const slot = (width - padLeft - padRight) / Math.max(1, dates.length);
  const barWidth = Math.max(2, Math.min(slot * 0.68, 34));
  const xFor = (index: number) => padLeft + slot * (index + 0.5);
  const yFor = (value: number) => baseline - (value / maximum) * (baseline - padTop);

  if (!dates.length || !present.length) {
    return <p role="status" className="empty-state">{emptyMessage}</p>;
  }

  const ticks = [0, 1, 2, 3, 4].map((step) => maximum * step / 4);

  return (
    <div className="trend-chart">
      <div className="cost-chart-legend">
        <span><i style={{ background: SERIES_COLORS[0] }} />{seriesName}</span>
        {reference && <span><i className="cost-chart-reference-key" />{reference.label}</span>}
        {onSelectDate && <span>Select a day for its resource costs</span>}
      </div>
      <div className="cost-chart-scroll" ref={frame} tabIndex={0} role="region" aria-label={ariaLabel}>
        <div className="cost-bar-plot" style={{ width: `${width}px`, height: `${height}px` }}>
          <svg viewBox={`0 0 ${width} ${height}`} style={{ width: `${width}px`, minWidth: `${width}px` }} className="cost-comparison-chart" role="img" aria-label={ariaLabel}>
            {ticks.map((value) => (
              <g key={value}>
                <line x1={padLeft} x2={width - padRight} y1={yFor(value)} y2={yFor(value)} className="cost-chart-grid" />
                <text x={padLeft - 10} y={yFor(value)} textAnchor="end" dominantBaseline="middle">{formatMoney(value)}</text>
              </g>
            ))}
            {dates.map((date, index) => {
              const value = values[index];
              if (value === null || value === undefined) return null;
              const top = yFor(value);
              const selected = selectedDate === date;
              return (
                <rect
                  key={date}
                  x={xFor(index) - barWidth / 2}
                  y={top}
                  width={barWidth}
                  height={Math.max(1, baseline - top)}
                  rx={2}
                  className={`cost-bar${selected ? ' cost-bar-selected' : selectedDate ? ' cost-bar-dim' : ''}`}
                  data-cost-date={date}
                />
              );
            })}
            {reference && (
              <line
                x1={padLeft}
                x2={width - padRight}
                y1={yFor(reference.value)}
                y2={yFor(reference.value)}
                className="cost-chart-reference"
              />
            )}
            <DayAxis dates={dates} xFor={xFor} y={baseline + (axis.rotated ? 16 : 26)} rotated={axis.rotated} />
          </svg>
          {onSelectDate && (
            <div className="cost-bar-hits" aria-label={`${ariaLabel} by day`}>
              {dates.map((date, index) => {
                const value = values[index];
                const money = value === null || value === undefined ? 'no cost evidence' : formatMoney(value);
                return (
                  <button
                    key={date}
                    type="button"
                    className={`cost-bar-hit${selectedDate === date ? ' cost-bar-hit-selected' : ''}`}
                    style={{ left: `${xFor(index) - slot / 2}px`, width: `${slot}px`, top: `${padTop}px`, height: `${baseline - padTop}px` }}
                    aria-label={selectLabel(date, value ?? null)}
                    aria-pressed={selectedDate === date}
                    title={`${date}: ${money}`}
                    onClick={() => onSelectDate(date)}
                  />
                );
              })}
            </div>
          )}
        </div>
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
