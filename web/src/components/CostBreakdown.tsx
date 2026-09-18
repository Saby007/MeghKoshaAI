import { useMemo, useState } from 'react';
import type { CostDetailSummary } from '../report/models';
import { compareCostGroups, costWindowDates, type CostDimension, type CostFilter, type CostWindow } from '../report/costDetails';
import './cost-breakdown.css';

type Formatter = (value: number) => string;

/* The dimensions a cost breakdown can be grouped by, in the order a reader is
   most likely to want them: what the money was spent on, where it lives, and
   finally the individual resource. `tag` is deliberately excluded - tag
   grouping needs a chosen key and has its own dedicated view. */
export const BREAKDOWN_DIMENSIONS: { id: CostDimension; label: string; noun: string }[] = [
  { id: 'service', label: 'Service', noun: 'service' },
  { id: 'resourceGroup', label: 'Resource group', noun: 'resource group' },
  { id: 'resource', label: 'Resource', noun: 'resource' },
];

const changeLabel = (value: number | null) => value === null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
const changeTone = (value: number | null) => value === null || value === 0 ? '' : value > 0 ? 'cost-increase' : 'cost-decrease';

/* A ranked cost breakdown over one dimension.

   Two things matter here beyond looks. First, `compareCostGroups` walks every
   detail row against every date in the window, so it is memoised on the exact
   inputs that change it - recomputing it on an unrelated re-render is the most
   expensive mistake this view can make. Second, the row list is capped and
   extended on request: a large estate can produce thousands of groups, and
   rendering them all costs far more than anyone reads. */
export function GroupedCostBreakdown({
  details,
  window,
  filters = {},
  formatMoney,
  displayCurrency,
  dimension,
  onDimensionChange,
  initialLimit = 12,
  label = 'Cost breakdown',
}: {
  details?: CostDetailSummary;
  window: CostWindow;
  filters?: CostFilter;
  formatMoney: Formatter;
  displayCurrency: string;
  dimension: CostDimension;
  onDimensionChange: (value: CostDimension) => void;
  initialLimit?: number;
  label?: string;
}) {
  const [limit, setLimit] = useState(initialLimit);
  const filterKey = JSON.stringify(filters);
  const groups = useMemo(
    () => compareCostGroups(details, window, filters, dimension),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [details, window.startDate, window.endDate, filterKey, dimension],
  );
  const dates = costWindowDates(window);
  const total = groups.reduce((sum, row) => sum + (row.current ?? 0), 0);
  const largest = groups.reduce((peak, row) => Math.max(peak, row.current ?? 0), 0);
  const noun = BREAKDOWN_DIMENSIONS.find((item) => item.id === dimension)?.noun ?? 'group';
  const visible = groups.slice(0, limit);

  return (
    <div className="cost-breakdown">
      <div className="cost-breakdown-controls">
        <div className="segmented" role="group" aria-label="Group cost by">
          {BREAKDOWN_DIMENSIONS.map((item) => (
            <button
              type="button"
              key={item.id}
              aria-pressed={dimension === item.id}
              className={dimension === item.id ? 'active' : ''}
              onClick={() => { onDimensionChange(item.id); setLimit(initialLimit); }}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="cost-breakdown-total">
          {groups.length.toLocaleString()} {groups.length === 1 ? noun : `${noun}s`} · {formatMoney(total)}
        </span>
      </div>

      {!details || details.status !== 'complete' ? (
        <p role="status" className="empty-state">{details?.statusMessage ?? 'Resource cost evidence is unavailable in this snapshot.'}</p>
      ) : !dates.length ? (
        <p role="status" className="empty-state">Select a valid date range of at most 366 days.</p>
      ) : !groups.length ? (
        <p role="status" className="empty-state">No {noun} charges match the selected range and filters.</p>
      ) : (
        <>
          <div className="billing-table-scroll" tabIndex={0} role="region" aria-label={label}>
            <table className="report-table app-cost-table">
              <thead>
                <tr>
                  <th scope="col">{BREAKDOWN_DIMENSIONS.find((item) => item.id === dimension)?.label}</th>
                  <th scope="col" className="num">Cost ({displayCurrency})</th>
                  <th scope="col" className="num">% of total</th>
                  <th scope="col" className="num">vs previous</th>
                  <th scope="col">Share</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => (
                  <tr key={row.id}>
                    <th scope="row">
                      {row.name}
                      {dimension === 'resource' && <small>{row.subscriptionName}</small>}
                    </th>
                    <td className="num">{row.current === null ? 'Unavailable' : formatMoney(row.current)}</td>
                    <td className="num">{total > 0 && row.current !== null ? `${(row.current / total * 100).toFixed(1)}%` : '—'}</td>
                    <td className={`num ${changeTone(row.percentage)}`}>{changeLabel(row.percentage)}</td>
                    <td>
                      <span className="app-cost-bar-track">
                        <span
                          className="app-cost-bar-fill"
                          style={{ width: `${largest > 0 ? Math.min(100, (row.current ?? 0) / largest * 100) : 0}%` }}
                        />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {groups.length > visible.length && (
            <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 25)}>
              Show more ({(groups.length - visible.length).toLocaleString()} remaining)
            </button>
          )}
        </>
      )}
    </div>
  );
}
