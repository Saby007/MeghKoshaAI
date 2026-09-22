import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowRight, Download, TriangleAlert, X } from 'lucide-react';
import { downloadCostDetailReport, getResourceAvailability, type ResourceAvailability } from '../api';
import type { CostDetailSummary, FullReport } from '../report/models';
import { compareCostGroups, costCoverage, costWindowDates, dailySubscriptionCosts, presetCostWindow, previousCostWindow, type CostDimension, type CostFilter, type CostWindow } from '../report/costDetails';
import './cost-explorer.css';
import { BudgetContext, type BudgetState } from './BudgetContext';
import { DayAxis, dayAxis } from './TrendChart';

type Formatter = (value: number) => string;
export type SelectedDay = { date: string; previousDate: string; subscriptionId: string };
const money = (value: number | null, format: Formatter) => value === null ? 'Unavailable' : format(value);
const changeLabel = (value: number | null) => value === null ? 'N/A' : `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
const tone = (value: number | null) => value === null || value === 0 ? '' : value > 0 ? 'cost-increase' : 'cost-decrease';
const COLORS = ['var(--color-category-compute)', 'var(--color-metric-green)', 'var(--color-category-databases)', 'var(--color-category-ai)', 'var(--color-category-networking)'];

/* The chart is authored in user units and then stretched to whatever width the
   container happens to be. Because an SVG viewBox scales uniformly, that stretch
   magnifies the text inside it too: an 880-unit chart in a 1380px column renders
   at 1.57x, so its 13px labels arrive on screen at 20px - larger than body text
   and the same size as a section heading, which is why the chart read as huge.
   Tracking the container width in the viewBox keeps the scale at exactly 1, so
   chart type matches the rest of the interface. Falls back to the authored width
   where ResizeObserver is unavailable (jsdom), and never drops below the scroll
   container's min-width so narrow viewports still pan instead of cramming. */
const CHART_WIDTH_FALLBACK = 880;
const CHART_WIDTH_MIN = 720;
const CHART_HEIGHT = 300;

function useChartWidth(ref: { current: HTMLElement | null }) {
  const [width, setWidth] = useState(CHART_WIDTH_FALLBACK);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      const measured = Math.round(node.clientWidth);
      if (measured > 0) setWidth(Math.max(CHART_WIDTH_MIN, measured));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export function CostFilters({ details, value, onChange }: { details?: CostDetailSummary; value: CostFilter; onChange: (value: CostFilter) => void }) {
  const rows = details?.rows ?? [];
  const fields = [
    { key: 'subscriptionId', label: 'Subscription', options: [...new Map(rows.map((row) => [row.subscriptionId, row.subscriptionName])).entries()] },
    ...(['serviceName', 'resourceGroup', 'region'] as const).map((key) => ({ key, label: key === 'serviceName' ? 'Service' : key === 'resourceGroup' ? 'Resource group' : 'Region', options: [...new Set(rows.map((row) => row[key]))].sort().map((text) => [text, text]) })),
  ];
  const tagKeys = [...new Set(rows.flatMap((row) => Object.keys(row.tags)))].sort();
  const tagValues = [...new Set(rows.flatMap((row) => Object.entries(row.tags).filter(([key]) => key.toLowerCase() === value.tagKey?.toLowerCase()).map(([, text]) => text)))].sort();
  return <div className="billing-filters cost-detail-filters">
    {fields.map((field) => <label className="billing-filter" key={field.key}><span>{field.label}</span><select aria-label={`Cost ${field.label.toLowerCase()}`} value={value[field.key as keyof CostFilter] ?? ''} onChange={(event) => onChange({ ...value, [field.key]: event.target.value || undefined })}>
      <option value="">All</option>{field.options.map(([id, name]) => <option value={id} key={id}>{name}</option>)}
    </select></label>)}
    <label className="billing-filter"><span>Tag key</span><select aria-label="Cost tag key" value={value.tagKey ?? ''} onChange={(event) => onChange({ ...value, tagKey: event.target.value || undefined, tagValue: undefined })}><option value="">All tags</option>{tagKeys.map((key) => <option key={key}>{key}</option>)}</select></label>
    {value.tagKey && <label className="billing-filter"><span>Tag value</span><select aria-label="Cost tag value" value={value.tagValue === undefined ? '' : JSON.stringify(value.tagValue)} onChange={(event) => onChange({ ...value, tagValue: event.target.value === '' ? undefined : JSON.parse(event.target.value) })}><option value="">All values</option>{tagValues.map((text) => <option key={text} value={JSON.stringify(text)}>{text || '(Empty value)'}</option>)}</select></label>}
  </div>;
}

export function CostExportButton({ report, snapshotId, window, filters = {}, previous, selectedDates, label = 'Download cost report' }: { report: Pick<FullReport, 'costDetails' | 'reportMetadata'>; snapshotId?: string | null; window: CostWindow; filters?: CostFilter; previous?: CostWindow; selectedDates?: string[]; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const available = !!snapshotId && costCoverage(report.costDetails, window).complete;
  return <div className="cost-export-control"><button type="button" className="ghost-button" disabled={!available || busy} title={`Download filtered costs in source currency ${report.reportMetadata.currency}`} onClick={async () => {
    if (!snapshotId) return;
    setBusy(true); setError(null);
    try { await downloadCostDetailReport(snapshotId, window, filters, previous, selectedDates); }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'Cost export unavailable.'); }
    finally { setBusy(false); }
  }}><Download size={16} aria-hidden="true" />{busy ? 'Exporting...' : label}</button>{error && <p role="alert">{error}</p>}</div>;
}

function ResourceAvailabilityCell({ resourceId, resourceType, date, snapshotId }: { resourceId: string; resourceType: string; date: string | null; snapshotId?: string | null }) {
  const [result, setResult] = useState<ResourceAvailability | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => { setResult(null); setError(null); setBusy(false); return () => pending.current?.abort(); }, [resourceId, date, snapshotId]);
  if (resourceType.toLowerCase() !== 'microsoft.compute/virtualmachines' && !/\/providers\/microsoft\.compute\/virtualmachines\/[^/]+$/i.test(resourceId)) return <span title="No common platform uptime metric is available for this resource type">Not supported</span>;
  if (!date || !snapshotId) return <span>Not collected</span>;
  return <div className="resource-availability-cell">
    {result ? <span title={result.statusMessage}>{result.availableHours !== null ? `${result.availableHours.toFixed(2)} h observed` : result.observedAvailableHours !== null ? `${result.observedAvailableHours.toFixed(2)} h observed (partial)` : 'Unavailable'}<small>{result.coverageMinutes}/{result.expectedMinutes} minutes</small></span> : <span>{busy ? 'Checking...' : 'Not collected'}</span>}
    <button type="button" className="ghost-button" disabled={busy} title={`Check VM platform availability for ${date}`} aria-label={`Check VM availability for ${resourceId} on ${date}`} onClick={async () => {
      const controller = new AbortController(); pending.current?.abort(); pending.current = controller;
      setBusy(true); setError(null);
      const timer = globalThis.setTimeout(() => { controller.abort(); setBusy(false); setError('Availability lookup timed out.'); }, 12000);
      try { const response = await getResourceAvailability(snapshotId, resourceId, date, controller.signal); if (!controller.signal.aborted) setResult(response); }
      catch (failure) { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'Availability unavailable.'); }
      finally { globalThis.clearTimeout(timer); if (!controller.signal.aborted) setBusy(false); }
    }}><Activity size={16} aria-hidden="true" /></button>
    {error && <small role="alert">{error}</small>}
  </div>;
}

export function ResourceCostTable({ details, window, previous, filters = {}, formatMoney, snapshotId }: { details?: CostDetailSummary; window: CostWindow; previous?: CostWindow; filters?: CostFilter; formatMoney: Formatter; snapshotId?: string | null }) {
  const rows = compareCostGroups(details, window, filters, 'resource', previous);
  const [limit, setLimit] = useState(50);
  useEffect(() => setLimit(50), [window.startDate, window.endDate, JSON.stringify(filters)]);
  if (details?.status !== 'complete') return <p role="status">{details?.statusMessage ?? 'Resource costs are unavailable in this snapshot. Run a new report.'}</p>;
  return <>
    <div className="billing-table-scroll cost-detail-scroll" tabIndex={0} role="region" aria-label="Resource cost comparison">
      <table className="data-table billing-table cost-detail-table"><caption>Resource costs: {window.startDate} - {window.endDate}; previous: {(previous ?? previousCostWindow(window)).startDate} - {(previous ?? previousCostWindow(window)).endDate}</caption>
        <thead><tr><th>Resource / subscription</th><th>Owner tag</th><th>Selected cost</th><th>Previous cost</th><th>Change</th><th>Average / hour</th><th>Uptime</th></tr></thead>
        <tbody>{rows.slice(0, limit).map((row) => <tr key={row.id}>
          <th scope="row"><details><summary>{row.name}</summary><span className="cost-resource-id">{row.sources[0]?.resourceId || 'No resource ID in FOCUS'}</span>
            {row.sources.map((source) => <div key={source.detailId} className="cost-source-detail"><strong>{source.serviceName} / {source.resourceGroup} / {source.region}</strong><span>{Object.entries(source.tags).map(([key, value]) => `${key}: ${value}`).join('; ') || 'Untagged'}</span><small>{source.tagAttributionSource === 'exported_resource_tags' ? 'Exported resource tags' : 'Includes current resource-group tags'}</small></div>)}
          </details><small>{row.subscriptionName}</small></th>
          <td>{row.owners.join('; ') || 'Not recorded'}</td><td>{money(row.current, formatMoney)}</td><td>{money(row.previous, formatMoney)}</td>
          <td className={tone(row.delta)}>{money(row.delta, formatMoney)}<small>{changeLabel(row.percentage)}{row.previous === 0 ? ' (zero baseline)' : ''}</small></td>
          <td>{row.current === null ? 'Unavailable' : `${formatMoney(row.current / (costWindowDates(window).length * 24))}/hr`}</td><td><ResourceAvailabilityCell resourceId={row.sources[0].resourceId} resourceType={row.sources[0].resourceType} date={window.startDate === window.endDate ? window.startDate : null} snapshotId={snapshotId} /></td>
        </tr>)}</tbody>
      </table>
      {!rows.length && <p role="status">No resource charges match this window and filter.</p>}
    </div>
    {rows.length > limit && <button type="button" className="ghost-button" onClick={() => setLimit((count) => count + 50)}>Show more resources ({rows.length - limit})</button>}
    <p className="billing-provenance">FOCUS EffectiveCost. Hourly values are daily averages, not measured hourly billing or uptime. VM availability is an on-demand platform observation, not application health or billable runtime; gaps remain unknown. Owner values are tags, not verified approvals.</p>
  </>;
}

/* The daily figures behind the chart. Extracted so the page can place
   them where it wants - they are reference data, not part of reading
   the chart - while still driving the same day drilldown. */
export function DailySubscriptionValues({ details, window, filters = {}, formatMoney, onSelectDay }: { details?: CostDetailSummary; window: CostWindow; filters?: CostFilter; formatMoney: Formatter; onSelectDay?: (date: string, previousDate: string, subscriptionId: string) => void }) {
  const series = dailySubscriptionCosts(details, window, filters);
  if (!series.length) return null;
  return <details className="cost-daily-values"><summary>Daily subscription amounts</summary><div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Daily subscription comparison table"><table className="data-table billing-table"><thead><tr><th>Subscription</th><th>Date</th><th>Selected cost</th><th>Previous date</th><th>Previous cost</th></tr></thead><tbody>{series.flatMap((item) => item.days.map((day) => <tr key={`${item.subscriptionId}:${day.date}`}><th>{item.subscriptionName}</th><td>{onSelectDay ? <button type="button" className="finding-link" onClick={() => onSelectDay(day.date, day.previousDate, item.subscriptionId)}>{day.date}</button> : day.date}</td><td>{money(day.current, formatMoney)}</td><td>{day.previousDate}</td><td>{money(day.previous, formatMoney)}</td></tr>))}</tbody></table></div></details>;
}

export function CostComparisonChart({ details, window, filters = {}, formatMoney, onSelectDay, showDailyValues = true }: { details?: CostDetailSummary; window: CostWindow; filters?: CostFilter; formatMoney: Formatter; onSelectDay?: (date: string, previousDate: string, subscriptionId: string) => void; showDailyValues?: boolean }) {
  const series = dailySubscriptionCosts(details, window, filters);
  const dates = costWindowDates(window);
  const values = series.flatMap((item) => item.days.flatMap((day) => [day.current, day.previous].filter((amount): amount is number => amount !== null)));
  const maximum = values.reduce((result, value) => Math.max(result, value), 1);
  const minimum = values.reduce((result, value) => Math.min(result, value), 0);
  const chartRef = useRef<HTMLDivElement>(null);
  const measured = useChartWidth(chartRef);
  const axis = dayAxis(dates, measured, 82, 24, Math.max(1, dates.length - 1));
  const width = axis.width;
  const height = CHART_HEIGHT + axis.extraHeight;
  const xFor = (index: number) => 82 + index * (width - 106) / Math.max(1, dates.length - 1);
  const yFor = (value: number) => 254 - (value - minimum) / (maximum - minimum) * 224;
  function pathFor(points: { current: number | null; previous: number | null }[], field: 'current' | 'previous') {
    let connected = false;
    return points.map((point, index) => {
      const value = point[field];
      if (value === null) { connected = false; return ''; }
      const command = connected ? 'L' : 'M'; connected = true;
      return `${command}${xFor(index)},${yFor(value)}`;
    }).join(' ');
  }
  if (!series.length || !dates.length) return <p role="status">{details?.statusMessage ?? 'Daily subscription cost detail is unavailable in this snapshot.'}</p>;
  return <>
    <div className="cost-chart-legend">{series.map((item, index) => <span key={item.subscriptionId}><i style={{ background: COLORS[index % COLORS.length] }} />{item.subscriptionName}</span>)}<span>Solid: selected period</span><span>Dotted: preceding period</span></div>
    <div className="cost-chart-scroll" ref={chartRef} tabIndex={0} role="region" aria-label="Subscription cost comparison chart">
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: `${width}px`, minWidth: `${width}px` }} className="cost-comparison-chart" role="group" aria-label="Current and previous subscription cost">
        {[0, 1, 2, 3, 4].map((tick) => { const value = minimum + (maximum - minimum) * tick / 4; return <g key={tick}><line x1={82} x2={width - 24} y1={yFor(value)} y2={yFor(value)} className="cost-chart-grid" /><text x={72} y={yFor(value)} textAnchor="end" dominantBaseline="middle">{formatMoney(value)}</text></g>; })}
        {series.map((item, index) => <g key={item.subscriptionId} style={{ color: COLORS[index % COLORS.length] }}>
          <path d={pathFor(item.days, 'previous')} className="cost-chart-previous" /><path d={pathFor(item.days, 'current')} className="cost-chart-current" />
          {item.days.map((day, slot) => day.current === null ? null : <circle key={day.date} cx={xFor(slot)} cy={yFor(day.current)} r={4} fill="currentColor" role={onSelectDay ? 'button' : undefined} tabIndex={onSelectDay ? 0 : undefined} data-cost-date={day.date} aria-label={`${item.subscriptionName}, ${day.date}, ${formatMoney(day.current)}`} onClick={() => onSelectDay?.(day.date, day.previousDate, item.subscriptionId)} onKeyDown={(event) => { if (onSelectDay && ['Enter', ' '].includes(event.key)) { event.preventDefault(); onSelectDay(day.date, day.previousDate, item.subscriptionId); } }}><title>{day.date}: {formatMoney(day.current)}; {day.previousDate}: {money(day.previous, formatMoney)}</title></circle>)}
        </g>)}
        <DayAxis dates={dates} xFor={xFor} y={axis.rotated ? 270 : 284} rotated={axis.rotated} />
      </svg>
    </div>
    {showDailyValues && <DailySubscriptionValues details={details} window={window} filters={filters} formatMoney={formatMoney} onSelectDay={onSelectDay} />}
  </>;
}

export function CostWindowOverview({ report, snapshotId, window, onChange, formatMoney, onOpenAnomalies, budgetState, filters, onFiltersChange, showBudget = true, showDailyValues = true, showHeading = true, selectedDay: controlledDay, onSelectDay }: { report: FullReport; snapshotId: string | null; window: CostWindow; onChange: (value: CostWindow) => void; formatMoney: Formatter; onOpenAnomalies: () => void; budgetState?: BudgetState; filters: CostFilter; onFiltersChange: (value: CostFilter) => void; showBudget?: boolean; showDailyValues?: boolean; showHeading?: boolean; selectedDay?: SelectedDay | null; onSelectDay?: (value: SelectedDay | null) => void }) {
  /* The day drilldown is normally this component's own state. When the page
     places the daily figures elsewhere - they belong at the end of the report,
     not in the middle of the chart - that table still has to be able to open
     the same drilldown, so the selection can be lifted by the caller. */
  const [internalDay, setInternalDay] = useState<SelectedDay | null>(null);
  const selectedDay = controlledDay !== undefined ? controlledDay : internalDay;
  const setSelectedDay = onSelectDay ?? setInternalDay;
  useEffect(() => setSelectedDay(null), [window.startDate, window.endDate, JSON.stringify(filters)]);
  const coverage = costCoverage(report.costDetails, window);
  const previous = previousCostWindow(window);
  const previousCoverage = costCoverage(report.costDetails, previous);
  const groups = compareCostGroups(report.costDetails, window, filters);
  const total = coverage.complete ? groups.reduce((sum, row) => sum + (row.current ?? 0), 0) : null;
  const before = previousCoverage.complete ? groups.reduce((sum, row) => sum + (row.previous ?? 0), 0) : null;
  const spikes = groups.filter((row) => row.previous !== null && row.previous > 0 && row.percentage !== null && row.percentage > 30);
  return <section className="cost-window-overview" aria-label="Selected period cost overview">
    {showHeading
      ? <div className="cost-section-heading"><h2>Subscription cost comparison</h2><CostExportButton report={report} snapshotId={snapshotId} window={window} filters={filters} /></div>
      : null}
    <div className="billing-metrics cost-window-metrics">
      <div><span>Selected period</span><output>{money(total, formatMoney)}</output><small>{window.startDate} - {window.endDate} / {coverage.coveredDays} of {coverage.dates.length} days</small></div>
      <div><span>Previous period</span><output>{money(before, formatMoney)}</output><small>{previous.startDate} - {previous.endDate} / {previousCoverage.coveredDays} of {previousCoverage.dates.length} days</small></div>
      <button type="button" className="period-anomaly-link" onClick={onOpenAnomalies}><span><TriangleAlert size={16} aria-hidden="true" /> Period anomalies</span><strong>{coverage.complete && previousCoverage.complete ? spikes.length : 'Unavailable'}</strong><small>Resource cost spikes above 30% <ArrowRight size={14} aria-hidden="true" /></small></button>
    </div>
    <CostFilters details={report.costDetails} value={filters} onChange={onFiltersChange} />
    <CostComparisonChart details={report.costDetails} window={window} filters={filters} formatMoney={formatMoney} showDailyValues={showDailyValues} onSelectDay={(date, previousDate, subscriptionId) => setSelectedDay({ date, previousDate, subscriptionId })} />
      {showBudget && budgetState && <BudgetContext state={budgetState} details={report.costDetails} filters={filters} />}
    {selectedDay && <section className="cost-day-drilldown" aria-label="Selected day resource detail"><div className="cost-section-heading"><h3>{selectedDay.date} vs {selectedDay.previousDate}</h3><CostExportButton report={report} snapshotId={snapshotId} window={{ startDate: selectedDay.date, endDate: selectedDay.date }} previous={{ startDate: selectedDay.previousDate, endDate: selectedDay.previousDate }} filters={{ ...filters, subscriptionId: selectedDay.subscriptionId }} label="Download day detail" /><button type="button" className="ghost-button" aria-label="Close day detail" title="Close day detail" onClick={() => setSelectedDay(null)}><X size={16} /></button></div><ResourceCostTable details={report.costDetails} window={{ startDate: selectedDay.date, endDate: selectedDay.date }} previous={{ startDate: selectedDay.previousDate, endDate: selectedDay.previousDate }} filters={{ ...filters, subscriptionId: selectedDay.subscriptionId }} formatMoney={formatMoney} snapshotId={snapshotId} /></section>}
  </section>;
}

export function PeriodCostAnomalies({ report, window, onChange, formatMoney, filters, onFiltersChange, snapshotId }: { report: FullReport; window: CostWindow; onChange: (value: CostWindow) => void; formatMoney: Formatter; filters: CostFilter; onFiltersChange: (value: CostFilter) => void; snapshotId: string | null }) {
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const [dimension, setDimension] = useState<CostDimension>('resource');
  const [limit, setLimit] = useState(50);
  const coverage = costCoverage(report.costDetails, window);
  const previous = previousCostWindow(window);
  const available = coverage.complete && costCoverage(report.costDetails, previous).complete;
  const changes = compareCostGroups(report.costDetails, window, filters, dimension).sort((first, second) => Math.abs(second.delta ?? 0) - Math.abs(first.delta ?? 0));
  const spikes = changes.filter((row) => row.previous !== null && row.previous > 0 && row.percentage !== null && row.percentage > 30);
  const selectedGroup = changes.find((row) => row.id === selectedGroupId);
  const chartDetails = selectedGroup && report.costDetails ? { ...report.costDetails, rows: selectedGroup.sources } : report.costDetails;
  useEffect(() => { setSelectedGroupId(null); setLimit(50); }, [window.startDate, window.endDate, JSON.stringify(filters), dimension]);
  return <section className="period-cost-anomalies" aria-label="Selected period anomalies">
    <CostFilters details={report.costDetails} value={filters} onChange={onFiltersChange} />
    <div className="cost-section-heading"><label className="billing-filter"><span>Analyze by</span><select aria-label="Period anomaly dimension" value={dimension} onChange={(event) => setDimension(event.target.value as CostDimension)}><option value="resource">Resource</option><option value="resourceGroup">Resource group</option><option value="tag" disabled={!filters.tagKey}>Tag value</option><option value="service">Service</option></select></label><CostExportButton report={report} snapshotId={snapshotId} window={window} filters={filters} /></div>
    <h2>Period cost spikes <span className="cost-count">{available ? spikes.length : 'Unavailable'}</span></h2>
    <p className="billing-provenance">More than 30% above the preceding {coverage.dates.length}-day window. New spend with a zero baseline is not a percentage spike. Separate from the same-weekday statistical detector below.</p>
    {!available ? <p role="status">Complete resource cost evidence is required for both {window.startDate} - {window.endDate} and {previous.startDate} - {previous.endDate}.</p> : <>
      <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Period cost spikes"><table className="data-table billing-table"><thead><tr><th>Dimension</th><th>Subscription</th><th>Owner tag</th><th>Selected cost</th><th>Previous cost</th><th>Increase</th></tr></thead><tbody>{spikes.slice(0, limit).map((row) => <tr key={row.id}><th><button type="button" className="finding-link" onClick={() => setSelectedGroupId(row.id)}>{row.name}</button></th><td>{row.subscriptionName}</td><td>{row.owners.join('; ') || 'Not recorded'}</td><td>{money(row.current, formatMoney)}</td><td>{money(row.previous, formatMoney)}</td><td className="cost-increase">{changeLabel(row.percentage)}</td></tr>)}</tbody></table></div>
      {!spikes.length && <p role="status">No resource cost spikes exceeded 30% in this period.</p>}
      {spikes.length > limit && <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 50)}>Show more spikes</button>}
      {selectedGroup && <><p>{selectedGroup.name} rose {changeLabel(selectedGroup.percentage)} ({money(selectedGroup.delta, formatMoney)}) across {new Set(selectedGroup.sources.map((row) => row.resourceId || row.detailId)).size} resource(s) in the selected period.</p><button type="button" className="ghost-button" onClick={() => setSelectedGroupId(null)}>Show all resources</button></>}
      <CostComparisonChart details={chartDetails} window={window} filters={filters} formatMoney={formatMoney} />
      {selectedGroup && <ResourceCostTable details={chartDetails} window={window} filters={filters} formatMoney={formatMoney} snapshotId={snapshotId} />}
      <details><summary>All increases and decreases</summary><div className="billing-table-scroll" tabIndex={0} role="region" aria-label="All period cost changes"><table className="data-table billing-table"><thead><tr><th>Dimension</th><th>Subscription</th><th>Current</th><th>Previous</th><th>Change</th></tr></thead><tbody>{changes.slice(0, limit).map((row) => <tr key={row.id}><th>{row.name}</th><td>{row.subscriptionName}</td><td>{money(row.current, formatMoney)}</td><td>{money(row.previous, formatMoney)}</td><td className={tone(row.delta)}>{money(row.delta, formatMoney)} / {changeLabel(row.percentage)}</td></tr>)}</tbody></table></div>{changes.length > limit && <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 50)}>Show more changes</button>}</details>
    </>}
  </section>;
}

export function SubscriptionCostBreakdown({ report, snapshotId, window, onChange, formatMoney, filters, onFiltersChange }: { report: FullReport; snapshotId: string | null; window: CostWindow; onChange: (value: CostWindow) => void; formatMoney: Formatter; filters: CostFilter; onFiltersChange: (value: CostFilter) => void }) {
  const [dimension, setDimension] = useState<CostDimension>('resource');
  const [limit, setLimit] = useState(50);
  const rows = compareCostGroups(report.costDetails, window, filters, dimension);
  useEffect(() => setLimit(50), [dimension, JSON.stringify(filters), window.startDate, window.endDate]);
  return <section className="subscription-cost-breakdown" aria-label="Subscription cost breakdown">
    <div className="cost-section-heading"><h2>Cost by subscription</h2><CostExportButton report={report} snapshotId={snapshotId} window={window} filters={filters} /></div>
    <CostFilters details={report.costDetails} value={filters} onChange={onFiltersChange} />
    <label className="billing-filter"><span>Group by</span><select aria-label="Cost grouping" value={dimension} onChange={(event) => setDimension(event.target.value as CostDimension)}><option value="resource">Resource</option><option value="service">Service</option><option value="resourceType">Resource type</option><option value="region">Region</option><option value="resourceGroup">Resource group</option><option value="tag" disabled={!filters.tagKey}>Tag value</option></select></label>
    {dimension === 'resource' ? <ResourceCostTable details={report.costDetails} window={window} filters={filters} formatMoney={formatMoney} snapshotId={snapshotId} /> : report.costDetails?.status !== 'complete' ? <p role="status">Resource cost details are unavailable in this snapshot. Run a new report.</p> : <>
      <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Grouped subscription costs"><table className="data-table billing-table"><thead><tr><th>Subscription</th><th>{dimension === 'tag' ? filters.tagKey : 'Dimension'}</th><th>Selected cost</th><th>Previous cost</th><th>Change</th></tr></thead><tbody>{rows.slice(0, limit).map((row) => <tr key={row.id}><th>{row.subscriptionName}</th><td>{row.name}</td><td>{money(row.current, formatMoney)}</td><td>{money(row.previous, formatMoney)}</td><td className={tone(row.delta)}>{money(row.delta, formatMoney)}<small>{changeLabel(row.percentage)}</small></td></tr>)}</tbody></table></div>
      {rows.length > limit && <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 50)}>Show more groups</button>}
    </>}
  </section>;
}

export function RequiredTagCosts({ report, snapshotId, window, formatMoney, filters }: { report: FullReport; snapshotId: string | null; window: CostWindow; formatMoney: Formatter; filters: CostFilter }) {
  const [requiredKeys, setRequiredKeys] = useState<string[]>([]);
  const [newKey, setNewKey] = useState('');
  const keys = [...new Set(report.costDetails?.rows.flatMap((row) => Object.keys(row.tags)) ?? [])].sort();
  const selectedFilters = { ...filters, requiredTagKeys: JSON.stringify(requiredKeys) };
  const rows = compareCostGroups(report.costDetails, window, selectedFilters).filter((row) => row.sources.some((source) => Object.keys(source.dailyCosts).some((day) => day >= window.startDate && day <= window.endDate)));
  const amount = costCoverage(report.costDetails, window).complete ? rows.reduce((sum, row) => sum + (row.current ?? 0), 0) : null;
  return <section className="required-tag-costs" aria-label="Missing required tag costs">
    <div className="cost-section-heading"><h2>Missing-tag resource cost</h2><CostExportButton report={report} snapshotId={snapshotId} window={window} filters={selectedFilters} /></div>
    <div className="billing-filters"><label className="billing-filter"><span>Required tag key</span><input aria-label="Required tag key" value={newKey} maxLength={512} list="known-cost-tag-keys" onChange={(event) => setNewKey(event.target.value)} /><datalist id="known-cost-tag-keys">{keys.map((key) => <option key={key} value={key} />)}</datalist></label><button type="button" className="ghost-button" disabled={!newKey.trim() || requiredKeys.length >= 50} onClick={() => { setRequiredKeys((current) => [...new Set([...current, newKey.trim()])]); setNewKey(''); }}>Add required key</button></div>
    <div className="cost-chart-legend">{requiredKeys.map((key) => <span key={key}>{key}<button type="button" className="ghost-button" aria-label={`Remove required key ${key}`} title={`Remove ${key}`} onClick={() => setRequiredKeys((current) => current.filter((value) => value !== key))}><X size={14} /></button></span>)}</div>
    <p>{money(amount, formatMoney)} / {rows.length} billed resource{rows.length === 1 ? '' : 's'} / {window.startDate} - {window.endDate}</p>
    <p className="billing-provenance">{requiredKeys.length ? 'Missing any selected key or an empty value.' : 'Resources with no non-empty tags.'} Unattributed charges without resource IDs are excluded. Current resource-group fallback is identified in resource details.</p>
    <ResourceCostTable details={report.costDetails} window={window} filters={selectedFilters} formatMoney={formatMoney} snapshotId={snapshotId} />
  </section>;
}