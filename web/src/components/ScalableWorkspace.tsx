import { Suspense, lazy, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react';
import {
  cancelAssessment, getAssessmentJob, getAssessmentRecords, getAssessmentSnapshot,
  getScalableLatestReport, getLatestAssessmentJob, getInteractiveAssessment, queueAssessment,
  type AssessmentJob, type AssessmentPage, type ScalableSnapshot,
} from '../api';
import './scalable-workspace.css';
import type { ReportSnapshot } from '../report/models';
import { CostRangeControls } from './CostRangeControls';
import { defaultCostWindow, monthCostWindow, monthToDateWindow, type CostWindow } from '../report/costDetails';

const ClassicReport = lazy(() => import('./ReportView').then((module) => ({ default: module.ReportView })));

const label = (value: string) => value.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\./g, ' / ');
const message = (error: unknown) => error instanceof Error ? error.message : 'Assessment request failed.';
const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function pause(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 2000);
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
}

function summaryValue(value: unknown, path: string, collections: ScalableSnapshot['collections']): ReactNode {
  const deferred = collections.find((item) => item.path === path);
  if (deferred) return <span>{deferred.total.toLocaleString()} records; open the paginated detail collection below.</span>;
  if (value === null || value === undefined) return <span>Unavailable</span>;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.length
    ? <ol>{value.map((item: unknown, index) => <li key={index}>{summaryValue(item, `${path}.${index}`, collections)}</li>)}</ol>
    : <span>No entries in this section.</span>;
  if (isObject(value)) return <dl>{Object.entries(value).map(([key, item]) =>
    <div key={key}><dt>{label(key)}</dt><dd>{summaryValue(item, `${path}.${key}`, collections)}</dd></div>)}</dl>;
  return <span role="alert">Unsupported report value.</span>;
}

function RecordBrowser({ snapshot, section }: { snapshot: ScalableSnapshot; section: string }) {
  const collections = snapshot.collections.filter((item) => item.path === section || item.path.startsWith(`${section}.`));
  const [selectedPath, setSelectedPath] = useState('');
  const path = collections.some((item) => item.path === selectedPath) ? selectedPath : collections[0]?.path ?? '';
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(-1);
  const [history, setHistory] = useState<number[]>([]);
  const [page, setPage] = useState<AssessmentPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search); setCursor(-1); setHistory([]); }, 350);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    setPage(null);
    setError(null);
    if (query.trim() && query.trim().length < 3) {
      setLoading(false);
      setError('Use at least three characters for indexed detail search.');
      return () => controller.abort();
    }
    setLoading(true);
    getAssessmentRecords(snapshot.snapshotId, path, cursor, query, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setPage(result); })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(message(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [snapshot.snapshotId, path, cursor, query]);

  if (!collections.length) return null;
  const columns = [...new Set((page?.items ?? []).flatMap((item) => isObject(item) ? Object.keys(item) : []))].slice(0, 6);
  const display = (value: unknown) => value === null || value === undefined ? 'Unavailable'
    : typeof value === 'object' ? 'Open record details' : String(value);
  return <section aria-label="Paginated assessment details">
    <h3>Complete detail collections</h3>
    <p>Collection totals cover the complete assessment, not just this page. Search filters resource names, types, groups, and categories.</p>
    <div className="scalable-controls">
      <label>Detail collection<select value={path} onChange={(event) => {
        setSelectedPath(event.target.value); setCursor(-1); setHistory([]);
      }}>{collections.map((item) => <option key={item.path} value={item.path}>{label(item.path)} ({item.total.toLocaleString()})</option>)}</select></label>
      <label>Search records (3+ characters)<input value={search} maxLength={100} onChange={(event) => setSearch(event.target.value)} /></label>
    </div>
    {loading && <p role="status">Loading a bounded detail page...</p>}
    {error && <p role="alert">{error}</p>}
    {page && <>
      <p>{page.items.length.toLocaleString()} rows on this page; {page.total.toLocaleString()} in the complete collection{page.filtered ? ' (search active; total is before filtering)' : ''}.</p>
      {!page.items.length && <p>No matching records.</p>}
      <div className="table-scroll"><table className="data-table"><thead><tr>
        {columns.map((column) => <th key={column}>{label(column)}</th>)}<th>Full record</th>
      </tr></thead><tbody>{page.items.map((item, index) => <tr key={index}>
        {columns.map((column) => <td key={column}>{display(isObject(item) ? item[column] : item)}</td>)}
        <td><details><summary>Open record details</summary><pre>{JSON.stringify(item, null, 2)}</pre></details></td>
      </tr>)}</tbody></table></div>
      <div className="scalable-controls">
        <button type="button" className="ghost-button" disabled={!history.length} onClick={() => {
          setCursor(history[history.length - 1]); setHistory((values) => values.slice(0, -1));
        }}>Previous page</button>
        <button type="button" className="ghost-button" disabled={page.nextCursor === null} onClick={() => {
          if (page.nextCursor !== null) { setHistory((values) => [...values, cursor]); setCursor(page.nextCursor); }
        }}>Next page</button>
      </div>
    </>}
  </section>;
}

export default function ScalableWorkspace({
  subscriptionIds, staleDays, refreshToken, handledRefresh, onBusyChange, onInteractiveSnapshot, onSnapshotChange,
}: {
  subscriptionIds: string[]; staleDays: number; refreshToken: number;
  handledRefresh: MutableRefObject<number>; onBusyChange: (busy: boolean) => void;
  onInteractiveSnapshot?: (snapshot: ReportSnapshot | null) => void;
  onSnapshotChange?: (snapshot: ScalableSnapshot | null) => void;
}) {
  const scope = [...subscriptionIds].map((id) => id.toLowerCase()).sort().join(',');
  const [snapshot, setSnapshot] = useState<ScalableSnapshot | null>(null);
  const [job, setJob] = useState<AssessmentJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [section, setSection] = useState('executiveSummary');
  const [retry, setRetry] = useState(0);
  const [classic, setClassic] = useState<ReportSnapshot | null>(null);
  const [classicLoading, setClassicLoading] = useState(false);
  const [classicWindow, setClassicWindow] = useState<CostWindow>({ startDate: '', endDate: '' });
  const pendingClassic = useRef<AbortController | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const matchesScope = snapshot?.subscriptionIds.map((id) => id.toLowerCase()).sort().join(',') === scope
      && snapshot.staleDays === staleDays;
    onSnapshotChange?.(matchesScope ? snapshot : null);
  }, [scope, staleDays, snapshot, onSnapshotChange]);

  useEffect(() => {
    pendingClassic.current?.abort();
    setClassic(null); setClassicLoading(false);
    onInteractiveSnapshot?.(null);
    return () => pendingClassic.current?.abort();
  }, [scope, snapshot?.snapshotId, onInteractiveSnapshot]);

  useEffect(() => {
    const controller = new AbortController();
    const current = ++generation.current;
    const fresh = handledRefresh.current !== refreshToken;
    handledRefresh.current = refreshToken;
    setJob(null); setError(null);
    setLoading(!fresh && !!scope);
    onBusyChange(fresh);
    if (!fresh) setSnapshot(null);
    if (!scope) { onBusyChange(false); return () => controller.abort(); }
    async function run() {
      try {
        let currentJob: AssessmentJob | null;
        if (fresh) {
          currentJob = await queueAssessment(scope.split(','), staleDays, controller.signal);
        } else {
          const [saved, resumed] = await Promise.all([
            getScalableLatestReport(scope.split(','), staleDays, controller.signal),
            getLatestAssessmentJob(scope.split(','), staleDays, controller.signal),
          ]);
          if (controller.signal.aborted || generation.current !== current) return;
          setSnapshot(saved); setLoading(false);
          currentJob = resumed;
        }
        const deadline = Date.now() + 60 * 60 * 1000;
        while (currentJob && !controller.signal.aborted && generation.current === current) {
          setJob(currentJob);
          onBusyChange(['queued', 'running'].includes(currentJob.status));
          if (currentJob.status === 'succeeded') {
            if (!currentJob.snapshotId) throw new Error('Assessment completed without a published snapshot.');
            const result = await getAssessmentSnapshot(currentJob.snapshotId, controller.signal);
            if (!controller.signal.aborted && generation.current === current) setSnapshot(result);
            return;
          }
          if (currentJob.status === 'failed') throw new Error(currentJob.error || 'Assessment failed. Review worker status before retrying.');
          if (currentJob.status === 'cancelled') return;
          if (Date.now() > deadline) throw new Error('Stopped waiting after one hour. The durable job may still be running; reconnect to check it.');
          await pause(controller.signal);
          currentJob = await getAssessmentJob(currentJob.jobId, controller.signal);
        }
      } catch (failure) {
        if (!controller.signal.aborted && generation.current === current) setError(message(failure));
      } finally {
        if (!controller.signal.aborted && generation.current === current) { onBusyChange(false); setLoading(false); }
      }
    }
    void run();
    return () => { controller.abort(); onBusyChange(false); };
  }, [refreshToken, scope, staleDays, retry, handledRefresh, onBusyChange]);

  const currentSection = snapshot ? Object.entries(snapshot.report).find(([key]) => key === section)?.[1] : null;
  const currency = snapshot?.report.reportMetadata.currency;
  const money = currency ? new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 0 }) : null;
  async function openClassic() {
    if (!snapshot) return;
    pendingClassic.current?.abort();
    const controller = new AbortController();
    pendingClassic.current = controller;
    setClassicLoading(true); setError(null);
    try {
      const result = await getInteractiveAssessment(snapshot.snapshotId, controller.signal);
      if (controller.signal.aborted) return;
      const dates = result.report.costDetails?.dates ?? result.report.dailyCostTrend?.days.map((day) => day.date) ?? [];
      setClassicWindow(defaultCostWindow(result.report.costDetails, result.report.reportMetadata.periodStart, result.report.reportMetadata.periodEnd, dates));
      setClassic(result); onInteractiveSnapshot?.(result);
    } catch (failure) {
      if (!controller.signal.aborted) setError(message(failure));
    } finally {
      if (!controller.signal.aborted) setClassicLoading(false);
    }
  }
  return <section className="scalable-workspace" aria-label="Scalable assessment workspace">
    <header><h2>Assessment dashboard</h2><p>Private PostgreSQL serving mode. Complete summaries load first; large collections are fetched one bounded page at a time.</p></header>
    {!scope && <p>Select subscriptions to open an assessment.</p>}
    {loading && <p role="status">Opening the saved assessment summary...</p>}
    {error && <div role="alert"><p>{error}</p><button className="ghost-button" type="button" onClick={() => setRetry((value) => value + 1)}>Retry saved summary</button></div>}
    {job && <section aria-label="Background assessment status">
      <p role="status">Assessment {job.status}: {job.phase}. Attempt {job.attempts}.</p>
      {['queued', 'running'].includes(job.status) && <button className="ghost-button" type="button" onClick={() => {
        const current = generation.current;
        void cancelAssessment(job.jobId).then(() => {
          if (generation.current === current) setJob({ ...job, status: 'cancelled', phase: 'cancelled' });
        }).catch((failure: unknown) => { if (generation.current === current) setError(message(failure)); });
      }}>Cancel assessment</button>}
      {snapshot && ['queued', 'running'].includes(job.status) && <p>The last completed assessment remains available while the replacement runs.</p>}
    </section>}
    {!snapshot && !loading && scope && !error && <p>No completed PostgreSQL assessment is available for this exact scope. Use Run report to queue one; opening this page never starts an assessment.</p>}
    {snapshot && <>
      <p>{snapshot.report.reportMetadata.period} | {snapshot.subscriptionIds.length} subscriptions | {currency} | Saved {new Date(snapshot.createdAt).toLocaleString()}</p>
      <div className="scalable-controls">
        <button type="button" className="ghost-button" disabled={classicLoading} onClick={() => {
          if (classic) setClassic(null); else void openClassic();
        }}>{classicLoading ? 'Checking interactive report budget...' : classic ? 'Back to paginated explorer' : 'Open classic tabs'}</button>
        <span>Classic tabs require a complete snapshot within the interactive size budget. Chat uses bounded PostgreSQL evidence and remains available for large reports.</span>
      </div>
      {classic ? <section aria-label="Bounded classic report">
        <CostRangeControls
          dates={classic.report.costDetails?.dates ?? classic.report.dailyCostTrend?.days.map((day) => day.date) ?? []}
          value={classicWindow} onChange={setClassicWindow}
          month={monthCostWindow(classic.report.reportMetadata.periodStart, classic.report.reportMetadata.periodEnd)}
          monthToDate={monthToDateWindow(classic.report.costDetails)}
        />
        <p>Data remains tied to this completed snapshot. On-demand FOCUS reprocessing is worker-only in PostgreSQL mode.</p>
        <Suspense fallback={<p role="status">Opening classic tabs...</p>}>
          <ClassicReport report={classic.report} narration={null} snapshotId={classic.snapshotId}
            snapshotCreatedAt={classic.createdAt} costWindow={classicWindow} onCostWindowChange={setClassicWindow} />
        </Suspense>
      </section> : <>
      <div className="scalable-metrics">
        <section><h3>Assessed spend</h3><strong>{money?.format(snapshot.report.executiveSummary.currentMonthlySpend) ?? 'Currency unavailable'}</strong></section>
        <section><h3>Potential monthly savings</h3><strong>{money?.format(snapshot.report.executiveSummary.potentialSavingsMonth) ?? 'Currency unavailable'}</strong></section>
        <section><h3>Active resources</h3><strong>{snapshot.report.executiveSummary.activeResources.toLocaleString()}</strong></section>
        <section><h3>Completeness</h3><strong>{snapshot.report.completeness.status}</strong></section>
      </div>
      <label>Report section<select value={section} onChange={(event) => setSection(event.target.value)}>
        {Object.keys(snapshot.report).map((key) => <option key={key} value={key}>{label(key)}</option>)}
      </select></label>
      <section className="scalable-summary" aria-label="Complete assessment summary">
        <h3>{label(section)}</h3>{summaryValue(currentSection, section, snapshot.collections)}
      </section>
      <RecordBrowser key={`${snapshot.snapshotId}:${section}`} snapshot={snapshot} section={section} />
      </>}
    </>}
  </section>;
}
