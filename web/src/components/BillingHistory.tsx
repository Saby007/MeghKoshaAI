import { useEffect, useMemo, useState } from 'react';
import { ArrowLeftRight, CalendarDays, Clock3 } from 'lucide-react';
import { billingDates, billingTags, billingWindow, compareBillingDates, DEFAULT_BUSINESS_CALENDAR, validBusinessCalendar, type BusinessCalendar, type BillingDayFilter, type BillingTimeFilter, type BillingSource } from '../report/billingHistory';
import { CostExportButton, CostFilters, ResourceCostTable } from './CostExplorer';
import { GroupedCostBreakdown } from './CostBreakdown';
import { DailyBarChart } from './TrendChart';
import { compareCostGroups, matchesCostFilter, type CostDimension, type CostFilter, type CostWindow } from '../report/costDetails';
import type { CostDetailSummary } from '../report/models';
import { BudgetContext, type BudgetState } from './BudgetContext';

type Formatter = (value: number) => string;
const dateLabel = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
const moneyOrUnavailable = (value: number | null, format: Formatter) => value === null ? 'Unavailable' : format(value);

function TagFilter({ report, value, onChange }: { report: BillingSource; value: string; onChange: (value: string) => void }) {
  const options = useMemo(() => billingTags(report), [report]);
  return (
    <label className="billing-filter billing-tag-filter">
      <span>Tag</span>
      <select aria-label="Billing tag filter" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">All charges</option>
        {options.map((option) => <option value={option.id} key={option.id}>{option.label}</option>)}
      </select>
    </label>
  );
}

/* The ways History can split cost. Tags are deliberately absent: History answers
   "what changed between two days", and tag attribution is incomplete and
   non-additive, so a tag split cannot be read as a breakdown of the day's bill. */
const HISTORY_DIMENSIONS: { id: CostDimension; label: string; noun: string }[] = [
  { id: 'subscription', label: 'Subscription', noun: 'subscription' },
  { id: 'service', label: 'Service', noun: 'service' },
  { id: 'resourceGroup', label: 'Resource group', noun: 'resource group' },
  { id: 'resource', label: 'Resource', noun: 'resource' },
];
const HISTORY_PAGE = 15;
/* History covers the whole assessed scope. It has no filters of its own, so it
   does not inherit the page-wide ones either - a filter set on another page would
   otherwise narrow these totals with nothing on screen to say so. */
const ALL: CostFilter = {};

export function BillingHistoryTab({ report, formatMoney, details, displayCurrency = '' }: {
  report: BillingSource; formatMoney: Formatter;
  details?: CostDetailSummary;
  displayCurrency?: string;
}) {
  const dates = useMemo(() => billingDates(report), [report]);
  const [baselineDate, setBaselineDate] = useState(() => dates.at(-2) ?? dates.at(-1) ?? '');
  const [comparisonDate, setComparisonDate] = useState(() => dates.at(-1) ?? '');
  const [dimension, setDimension] = useState<CostDimension>('subscription');
  const [limit, setLimit] = useState(HISTORY_PAGE);
  useEffect(() => {
    const nextDates = billingDates(report);
    setBaselineDate(nextDates.at(-2) ?? nextDates.at(-1) ?? '');
    setComparisonDate(nextDates.at(-1) ?? '');
  }, [report]);
  useEffect(() => setLimit(HISTORY_PAGE), [dimension, baselineDate, comparisonDate]);
  const detailed = details?.status === 'complete';
  const available = useMemo(() => new Set(detailed ? details!.dates : []), [details, detailed]);

  /* Day totals from the same rows the table below splits, so the headline and
     the table always add up to the same figure. Without resource detail the
     report's daily trend still answers the unfiltered total. */
  const dayTotal = (date: string): number | null => {
    if (!date) return null;
    if (detailed) {
      if (!available.has(date)) return null;
      return details!.rows.reduce((sum, row) => sum + (row.dailyCosts[date] ?? 0), 0);
    }
    return compareBillingDates(report, date, date, '').baseline;
  };
  const baseline = dayTotal(baselineDate);
  const comparison = dayTotal(comparisonDate);
  const delta = baseline === null || comparison === null ? null : comparison - baseline;
  const percentChange = delta === null || !baseline ? null : (delta / Math.abs(baseline)) * 100;

  /* One row per group, the two days side by side, largest movement first - the
     point of comparing two days is to find what moved. */
  const groups = useMemo(() => {
    if (!detailed || !baselineDate || !comparisonDate) return [];
    return compareCostGroups(details, { startDate: comparisonDate, endDate: comparisonDate }, ALL, dimension, { startDate: baselineDate, endDate: baselineDate })
      .filter((group) => (group.current ?? 0) !== 0 || (group.previous ?? 0) !== 0)
      .sort((left, right) => Math.abs(right.delta ?? 0) - Math.abs(left.delta ?? 0) || Math.abs(right.current ?? 0) - Math.abs(left.current ?? 0));
  }, [details, detailed, baselineDate, comparisonDate, dimension]);
  const visible = groups.slice(0, limit);
  /* The subscription under each name tells apart same-named services or groups
     in different subscriptions; with one subscription in view it is just noise. */
  const multipleSubscriptions = new Set(groups.map((group) => group.subscriptionId)).size > 1;
  const noun = HISTORY_DIMENSIONS.find((item) => item.id === dimension)!.noun;
  const heading = HISTORY_DIMENSIONS.find((item) => item.id === dimension)!.label;
  const change = (value: number | null) => value === null ? 'Unavailable' : `${value > 0 ? '+' : ''}${formatMoney(value)}`;
  const tone = (value: number | null) => value === null || value === 0 ? '' : value > 0 ? 'cost-increase' : 'cost-decrease';
  const percent = (value: number | null, base: number | null) => value === null ? base === 0 ? 'New' : '—' : `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;

  return (
    <section className="billing-history" aria-label="Billing history comparison">
      <header className="billing-heading">
        <div><CalendarDays size={18} aria-hidden="true" /><h2>History</h2></div>
        <span>Daily billed cost · UTC</span>
      </header>

      <div className="history-controls">
        <div className="segmented history-breakdown" role="group" aria-label="History breakdown">
          {HISTORY_DIMENSIONS.map((item) => (
            <button
              type="button"
              key={item.id}
              aria-pressed={dimension === item.id}
              className={dimension === item.id ? 'active' : ''}
              disabled={!detailed}
              onClick={() => setDimension(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <section className="history-compare" aria-label="Compare two days">
        <header className="cost-section-heading"><h3>Compare two days</h3></header>
        <div className="billing-filters">
          <label className="billing-filter"><span>Baseline</span><input type="date" aria-label="Baseline billing date" min={dates[0]} max={dates.at(-1)} value={baselineDate} onChange={(event) => setBaselineDate(event.target.value)} disabled={!dates.length} /></label>
          <button type="button" className="billing-swap ghost-button" aria-label="Swap billing dates" title="Swap billing dates" disabled={!dates.length} onClick={() => { setBaselineDate(comparisonDate); setComparisonDate(baselineDate); }}><ArrowLeftRight size={16} /></button>
          <label className="billing-filter"><span>Compare with</span><input type="date" aria-label="Comparison billing date" min={dates[0]} max={dates.at(-1)} value={comparisonDate} onChange={(event) => setComparisonDate(event.target.value)} disabled={!dates.length} /></label>
        </div>
        <div className="billing-metrics">
          <div><span>Baseline cost</span><output aria-label="Baseline cost">{moneyOrUnavailable(baseline, formatMoney)}</output><small>{baselineDate ? dateLabel(baselineDate) : 'No billing date'}</small></div>
          <div><span>Comparison cost</span><output aria-label="Comparison cost">{moneyOrUnavailable(comparison, formatMoney)}</output><small>{comparisonDate ? dateLabel(comparisonDate) : 'No billing date'}</small></div>
          <div><span>Change</span><output aria-label="Billing cost change" className={tone(delta)}>{change(delta)}</output><small>{percentChange === null ? baseline === 0 ? 'N/A (zero baseline)' : 'Percentage unavailable' : `${percentChange > 0 ? '+' : ''}${percentChange.toFixed(1)}%`}</small></div>
        </div>
        {!dates.length || baseline === null || comparison === null ? <p className="billing-coverage" role="status">Billing evidence is unavailable for one or both selected dates.</p> : null}

        {!detailed ? (
          <p className="billing-provenance">{details?.statusMessage ?? `Resource cost detail is not in this snapshot, so the day cannot be broken down by ${noun}.`}</p>
        ) : groups.length === 0 ? (
          baseline !== null && comparison !== null && <p className="billing-provenance" role="status">No {noun} charges match these dates and filters.</p>
        ) : (
          <>
            <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Two-day cost comparison">
              <table className="report-table app-cost-table history-compare-table">
                <thead>
                  <tr>
                    <th scope="col">{heading}</th>
                    <th scope="col" className="num">{baselineDate} ({displayCurrency})</th>
                    <th scope="col" className="num">{comparisonDate} ({displayCurrency})</th>
                    <th scope="col" className="num">Change</th>
                    <th scope="col" className="num">%</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((group) => (
                    <tr key={group.id}>
                      <th scope="row">{group.name}{dimension !== 'subscription' && multipleSubscriptions && <small>{group.subscriptionName}</small>}</th>
                      <td className="num">{moneyOrUnavailable(group.previous, formatMoney)}</td>
                      <td className="num">{moneyOrUnavailable(group.current, formatMoney)}</td>
                      <td className={`num ${tone(group.delta)}`}>{change(group.delta)}</td>
                      <td className={`num ${tone(group.delta)}`}>{percent(group.percentage, group.previous)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="history-compare-foot">
              <span>{groups.length.toLocaleString()} {groups.length === 1 ? noun : `${noun}s`} with cost on either day · largest change first</span>
              {groups.length > visible.length && (
                <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 25)}>
                  Show more ({(groups.length - visible.length).toLocaleString()} remaining)
                </button>
              )}
            </div>
          </>
        )}
      </section>
      <p className="billing-provenance">{report.dailyCostTrend.statusMessage}</p>
    </section>
  );
}

export function HourlyCostPanel({ report, formatMoney, formatHourlyMoney, rangeDays, embedded = false, snapshotId, budgetState, costWindow, onWindowChange, costFilters, onFiltersChange, displayCurrency = '' }: {
  report: BillingSource; formatMoney: Formatter; formatHourlyMoney: Formatter; rangeDays?: number; embedded?: boolean;
  snapshotId?: string | null; budgetState?: BudgetState; costWindow?: CostWindow; onWindowChange?: (value: CostWindow) => void;
  costFilters?: CostFilter; onFiltersChange?: (value: CostFilter) => void; displayCurrency?: string;
}) {
  const [selectedRange, setSelectedRange] = useState(30);
  const [dayFilter, setDayFilter] = useState<BillingDayFilter>('all');
  const [timeFilter, setTimeFilter] = useState<BillingTimeFilter>('all');
  const [breakdown, setBreakdown] = useState<CostDimension>('resource');
  const [businessCalendar, setBusinessCalendar] = useState<BusinessCalendar>(() => {
    try { const saved = JSON.parse(localStorage.getItem('mkai-business-calendar') ?? 'null'); return validBusinessCalendar(saved) ? saved : DEFAULT_BUSINESS_CALENDAR; } catch { return DEFAULT_BUSINESS_CALENDAR; }
  });
  useEffect(() => { if (validBusinessCalendar(businessCalendar)) { try { localStorage.setItem('mkai-business-calendar', JSON.stringify(businessCalendar)); } catch {} } }, [businessCalendar]);
  const [tagId, setTagId] = useState('');
  const [localFilters, setLocalFilters] = useState<CostFilter>({});
  const filters = costFilters ?? localFilters;
  const setFilters = onFiltersChange ?? setLocalFilters;
  const [expandedDate, setExpandedDate] = useState<string | null>(null);
  const dates = useMemo(() => billingDates(report), [report]);
  const [baselineDate, setBaselineDate] = useState(() => dates.at(-2) ?? dates.at(-1) ?? '');
  const [comparisonDate, setComparisonDate] = useState(() => dates.at(-1) ?? '');
  useEffect(() => { setTagId(''); setLocalFilters({}); setExpandedDate(null); setBaselineDate(dates.at(-2) ?? dates.at(-1) ?? ''); setComparisonDate(dates.at(-1) ?? ''); }, [report]);
  const filteredReport = useMemo(() => report.costDetails?.status === 'complete'
    ? { ...report, costDetails: { ...report.costDetails, rows: report.costDetails.rows.filter((row) => matchesCostFilter(row, filters)) } } : report, [report, filters]);
  const result = useMemo(() => billingWindow(filteredReport, { rangeDays: rangeDays ?? selectedRange, dayFilter, timeFilter, tagId, startDate: costWindow?.startDate, endDate: costWindow?.endDate, businessCalendar }), [filteredReport, rangeDays, selectedRange, dayFilter, timeFilter, tagId, costWindow?.startDate, costWindow?.endDate, businessCalendar]);
  const comparison = compareBillingDates(filteredReport, baselineDate, comparisonDate, tagId);
  const firstDate = result.days[0]?.date;
  const lastDate = result.days.at(-1)?.date;
  return (
    <section className={`hourly-cost-panel${embedded ? ' executive-visual' : ''}`} aria-label="Cost by hour">
      <header className="billing-heading">
        <div><Clock3 size={18} aria-hidden="true" /><h2>Cost by Hour</h2></div>
        <span>{result.estimated ? 'Estimated intraday allocation' : 'Derived daily average · billed cost / 24'}</span>
      </header>
      <div className="billing-filters">
        {rangeDays === undefined && !costWindow && <label className="billing-filter billing-window-filter"><span>Window</span><select aria-label="Hourly cost window" value={selectedRange} onChange={(event) => setSelectedRange(Number(event.target.value))}>{[7, 30, 60, 90].map((days) => <option key={days} value={days}>Last {days} export-calendar days</option>)}</select></label>}
        <label className="billing-filter"><span>Days (UTC)</span><select aria-label="Billing day filter" value={dayFilter} onChange={(event) => setDayFilter(event.target.value as BillingDayFilter)}><option value="all">All days</option><option value="weekdays">Weekdays only</option><option value="weekends">Weekends only</option></select></label>
        <label className="billing-filter"><span>Hours (UTC)</span><select aria-label="Billing time filter" value={timeFilter} onChange={(event) => setTimeFilter(event.target.value as BillingTimeFilter)}><option value="all">All hours</option><option value="business">Business hours</option><option value="off-hours">Off-hours</option></select></label>
        {report.costDetails?.status !== 'complete' && <TagFilter report={report} value={tagId} onChange={setTagId} />}
      </div>
      {report.costDetails?.status === 'complete' && <CostFilters details={report.costDetails} value={filters} onChange={setFilters} />}
      {timeFilter !== 'all' && <div className="billing-filters" role="group" aria-label="Business calendar">
        <label className="billing-filter"><span>Business start</span><input type="time" step={900} aria-label="Business hours start" value={businessCalendar.start} onChange={(event) => setBusinessCalendar({ ...businessCalendar, start: event.target.value })} /></label>
        <label className="billing-filter"><span>Business end</span><input type="time" step={900} aria-label="Business hours end" value={businessCalendar.end} onChange={(event) => setBusinessCalendar({ ...businessCalendar, end: event.target.value })} /></label>
        <label className="billing-filter"><span>IANA time zone</span><input type="text" aria-label="Business hours time zone" value={businessCalendar.timeZone} onChange={(event) => setBusinessCalendar({ ...businessCalendar, timeZone: event.target.value })} /></label>
      </div>}
      {result.calendarError && <p role="alert">{result.calendarError}</p>}
      <div className="billing-metrics">
        <div><span>{result.estimated ? result.incomplete ? 'Covered estimated cost' : 'Estimated window cost' : result.incomplete ? 'Covered billed cost' : 'Selected days cost'}</span><output aria-label={result.estimated ? 'Filtered estimated cost' : 'Filtered billed cost'}>{moneyOrUnavailable(result.totalCost, formatMoney)}</output><small>{firstDate && lastDate ? `${firstDate} - ${lastDate}` : 'No matching billing dates'}</small></div>
        <div><span>{result.estimated ? 'Estimated average hourly cost' : 'Average hourly cost'}</span><output aria-label="Filtered average hourly cost">{result.averageHourlyCost === null ? 'Unavailable' : `${formatHourlyMoney(result.averageHourlyCost)}/hr`}</output><small>Across covered {dayFilter === 'weekends' ? 'weekend ' : dayFilter === 'weekdays' ? 'weekday ' : ''}hours</small></div>
        <div><span>Coverage</span><output aria-label="Billing date coverage">{result.coveredDays} / {result.days.length}</output><small>UTC billing days · {result.coveredHours} selected hours</small></div>
      </div>
      {result.estimated && <p className="billing-coverage">Estimate assumes uniform spend across each UTC billing day, not measured hourly usage. Business hours: Mon-Fri {businessCalendar.start}-{businessCalendar.end} {businessCalendar.timeZone}. Off-hours: the remaining hours. Downloads retain full-day source costs.</p>}
      {result.incomplete && <p className="billing-coverage" role="status">Incomplete coverage. Unavailable dates are excluded from both cost and hours.</p>}
      {report.reportMetadata && firstDate && lastDate && <CostExportButton report={{ costDetails: report.costDetails, reportMetadata: report.reportMetadata }} snapshotId={snapshotId} window={{ startDate: firstDate, endDate: lastDate }} filters={filters} selectedDates={result.days.filter((day) => day.totalCost !== null).map((day) => day.date)} label="Download selected full-day costs" />}
      {result.days.length === 0 ? <p className="billing-coverage" role="status">No billing dates match these filters.</p> : (
        <>
          {/* The source is daily, so there is no measured intraday curve to
              draw. What genuinely varies - and what every filter above moves -
              is the hourly run-rate from day to day, so that is what this
              charts. Drawing a flat 24-hour profile would imply a precision
              the evidence does not have.

              Each bar is the day's control: the table that used to carry the
              drilldown has gone, so selecting a bar is now how you reach a
              day's resource costs. */}
          <section className="hourly-rate-chart" aria-label="Hourly cost rate over time">
            <h3>{result.estimated ? 'Estimated hourly rate' : 'Average hourly rate'}</h3>
            <DailyBarChart
              dates={result.days.map((day) => day.date)}
              values={result.days.map((day) => day.averageHourlyCost)}
              seriesName={result.estimated ? 'Estimated cost per hour' : 'Billed cost per hour'}
              formatMoney={formatHourlyMoney}
              ariaLabel="Hourly cost rate by day"
              emptyMessage="No hourly rate can be derived for the selected days."
              selectedDate={expandedDate}
              onSelectDate={report.costDetails?.status === 'complete'
                ? (date) => setExpandedDate((value) => value === date ? null : date)
                : undefined}
              selectLabel={(date) => `Resource costs for ${date}`}
            />
          </section>
          {expandedDate && report.costDetails?.status === 'complete' && (() => {
            const day = result.days.find((item) => item.date === expandedDate);
            return (
              <section className="billing-day-drilldown" aria-label={`Resource costs on ${expandedDate}`}>
                <header className="cost-section-heading">
                  <h3>{dateLabel(expandedDate)}</h3>
                  <button type="button" className="ghost-button" onClick={() => setExpandedDate(null)} aria-label="Close day details">Close</button>
                </header>
                {day && <p className="billing-provenance">
                  {day.hours} selected hours · {result.estimated ? 'estimated' : 'billed'} {moneyOrUnavailable(day.totalCost, formatMoney)} · {day.averageHourlyCost === null ? 'Unavailable' : `${formatHourlyMoney(day.averageHourlyCost)}/hr`}.
                  {' '}{baselineDate === expandedDate
                    ? 'The comparison baseline below is this same day, so the change column reads zero; pick another baseline under Full-day resource comparison to compare.'
                    : `Previous cost below is ${dateLabel(baselineDate)}.`}
                </p>}
                <ResourceCostTable
                  details={report.costDetails}
                  window={{ startDate: expandedDate, endDate: expandedDate }}
                  previous={{ startDate: baselineDate, endDate: baselineDate }}
                  filters={filters}
                  formatMoney={formatHourlyMoney}
                  snapshotId={snapshotId}
                />
              </section>
            );
          })()}
        </>
      )}
      {!embedded && report.costDetails?.status === 'complete' && firstDate && lastDate && (
        <section className="hourly-resource-breakdown" aria-label="Resource cost breakdown for the selected days">
          <h3>Cost by resource</h3>
          <p className="billing-provenance">Across the days selected above, following the same filters.</p>
          <GroupedCostBreakdown
            details={report.costDetails}
            window={{ startDate: firstDate, endDate: lastDate }}
            filters={filters}
            formatMoney={formatMoney}
            displayCurrency={displayCurrency}
            dimension={breakdown}
            onDimensionChange={setBreakdown}
            initialLimit={10}
            label="Hourly panel cost breakdown"
          />
        </section>
      )}
      {!embedded && report.costDetails?.status === 'complete' && <section className="hourly-resource-comparison" aria-label="Hourly resource date comparison">
        <h3>Full-day resource comparison</h3>
        <div className="billing-filters"><label className="billing-filter"><span>Baseline date</span><input type="date" aria-label="Hourly baseline date" value={baselineDate} min={dates[0]} max={dates.at(-1)} onChange={(event) => setBaselineDate(event.target.value)} /></label><label className="billing-filter"><span>Comparison date</span><input type="date" aria-label="Hourly comparison date" value={comparisonDate} min={dates[0]} max={dates.at(-1)} onChange={(event) => setComparisonDate(event.target.value)} /></label><button type="button" className="ghost-button" title="Swap resource comparison dates" aria-label="Swap resource comparison dates" onClick={() => { setBaselineDate(comparisonDate); setComparisonDate(baselineDate); }}><ArrowLeftRight size={16} /></button></div>
        <p>{baselineDate}: {moneyOrUnavailable(comparison.baseline, formatMoney)} / {comparisonDate}: {moneyOrUnavailable(comparison.comparison, formatMoney)}</p>
        <ResourceCostTable details={report.costDetails} window={{ startDate: comparisonDate, endDate: comparisonDate }} previous={{ startDate: baselineDate, endDate: baselineDate }} filters={filters} formatMoney={formatHourlyMoney} snapshotId={snapshotId} />
        {report.reportMetadata && <CostExportButton report={{ costDetails: report.costDetails, reportMetadata: report.reportMetadata }} snapshotId={snapshotId} window={{ startDate: comparisonDate, endDate: comparisonDate }} previous={{ startDate: baselineDate, endDate: baselineDate }} filters={filters} />}
      </section>}
      {budgetState && <BudgetContext state={budgetState} details={report.costDetails} filters={filters} window={firstDate && lastDate ? { startDate: firstDate, endDate: lastDate } : undefined} formatMoney={formatMoney} />}
      <p className="billing-provenance">{report.dailyCostTrend.statusMessage}{tagId ? ` ${report.tagDailyCostTrend.statusMessage}` : ''}</p>
    </section>
  );
}