import { useEffect, useRef, useState } from 'react';
import {
  CalendarClock,
  Check,
  ChevronDown,
  ChevronUp,
  History,
  Pause,
  Play,
  RefreshCw,
  Settings2,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import {
  ApiRequestError,
  configureCostExport,
  createCostSchedule,
  deleteCostSchedule,
  getCostExportConfiguration,
  listCostSchedules,
  listScheduleRuns,
  runAllCostSchedules,
  runCostSchedule,
  setCostScheduleState,
  SCHEDULE_WINDOW_MONTHS,
  DAILY_TIME_PATTERN,
  DEFAULT_DAILY_TIME,
  type CostSchedule,
  type FocusExportConfiguration,
  type ScheduleDailyPull,
  type ScheduleDailyRun,
  type ScheduleMonthStatus,
  type ScheduleRun,
  type ScheduleWindowMonths,
} from '../api';
import { redirectApiIdentity } from '../apiIdentity';

function formatDate(value: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Timestamp unavailable';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(new Date(value));
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return 'Duration unavailable';
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function defaultScheduleValue(): string {
  const now = new Date();
  let year = now.getUTCFullYear();
  let month = now.getUTCMonth();
  if (new Date(Date.UTC(year, month, 5, 3)) <= now) {
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  return new Date(Date.UTC(year, month, 5, 3)).toISOString().slice(0, 16);
}

const utcSchedule = (value: string) => `${value}:00Z`;
const windowOf = (schedule: CostSchedule): ScheduleWindowMonths => schedule.windowMonths ?? 6;
// Configure creates whichever of the closed-month and daily exports is missing.
const canConfigureAny = (configuration: FocusExportConfiguration): boolean => configuration.canConfigure || configuration.daily?.canConfigure === true;

function runLabel(run: ScheduleRun | null): string {
  if (!run) return 'Awaiting first export';
  if (run.status === 'queued') return `${run.period || 'Export'} queued`;
  if (run.status === 'running') return `${run.period} in progress`;
  if (run.status === 'failed') return `${run.period} failed`;
  if (run.status === 'succeeded') return `${run.period} exported`;
  return 'Execution status unknown';
}

const monthStatusLabel: Record<ScheduleMonthStatus, string> = {
  pending: 'Pending',
  submitting: 'Sending',
  queued: 'Queued',
  running: 'Active',
  succeeded: 'Done',
  failed: 'Failed',
};

function formatDay(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
    .format(new Date(`${value}T00:00:00Z`));
}

function nextPull(daily: ScheduleDailyPull): string {
  if (!daily.nextRunAt || !Number.isFinite(Date.parse(daily.nextRunAt))) return '';
  // A pull already due starts on the worker's next 5-minute run.
  return Date.parse(daily.nextRunAt) <= Date.now() ? ' \u00b7 next pull starting shortly' : ` \u00b7 next pull ${formatDate(daily.nextRunAt)}`;
}

function dailyLabel(daily: ScheduleDailyPull): string {
  if (!daily.enabled) return 'Daily pull off';
  if (daily.status === 'unavailable') return 'Daily export not configured';
  const through = daily.dataThrough ? ` \u00b7 data through ${formatDay(daily.dataThrough)}` : '';
  if (daily.status === 'failed') return `Daily pull failed${through}${nextPull(daily)}`;
  if (daily.status === 'running') return `Daily pull in progress${through}`;
  if (daily.dataThrough) return `Daily data through ${formatDay(daily.dataThrough)}${nextPull(daily)}`;
  return `Daily pull at ${daily.timeUtc} UTC${nextPull(daily)}`;
}

function DailyStatus({ daily }: { daily?: ScheduleDailyPull | null }) {
  if (!daily) return <small className="daily-status unavailable">Daily status unavailable</small>;
  return <div className="schedule-daily-evidence">
    <small className={`daily-status ${daily.enabled ? daily.status : 'off'}`}>{dailyLabel(daily)}</small>
    {daily.enabled && <dl className="schedule-pull-times">
      <div><dt>Pull time</dt><dd>{daily.timeUtc} UTC</dd></div>
      <div><dt>{daily.completedAt ? 'Last completed' : 'Last started'}</dt><dd>{daily.completedAt || daily.lastRunAt ? formatDate(daily.completedAt || daily.lastRunAt) : 'No execution recorded'}</dd></div>
    </dl>}
    {daily.enabled && daily.error && <small className="schedule-execution-error">{daily.error}</small>}
  </div>;
}

type ScheduleFilter = 'all' | 'attention' | 'active' | 'paused' | 'not_scheduled';

function needsAttention(schedule: CostSchedule): boolean {
  return schedule.state === 'unknown' || schedule.state === 'not_scheduled'
    || (!!schedule.availability && schedule.availability !== 'available')
    || schedule.latestRun?.status === 'failed' || schedule.latestRun?.status === 'unknown'
    || !schedule.daily || (schedule.daily.enabled && (schedule.daily.status === 'failed' || schedule.daily.status === 'unavailable'));
}

function dailyPeriodLabel(run: ScheduleDailyRun): string {
  const start = new Date(`${run.start}T00:00:00Z`);
  const end = new Date(`${run.end}T00:00:00Z`);
  const month = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' }).format(start);
  // A whole month is last month's re-pull on days 1-5, which picks up charges Azure posts after month end.
  if (new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate() + 1)).getUTCDate() === 1) return `${month} late charges`;
  return end.getUTCDate() === 1 ? `1 ${month}` : `1\u2013${end.getUTCDate()} ${month}`;
}

function DailyProgress({ daily, mobile = false }: { daily?: ScheduleDailyPull | null; mobile?: boolean }) {
  if (!daily?.enabled || !daily.runs?.length) return null;
  return (
    <ol className={`month-progress daily-progress ${mobile ? 'mobile-month-progress' : 'desktop-month-progress'}`} aria-label="Daily pull status">
      {daily.runs.map((run) => {
        const label = dailyPeriodLabel(run);
        return (
          <li className={run.status} key={run.start} aria-label={`Daily ${label} ${monthStatusLabel[run.status]}`}>
            <span>{label}</span><strong>{monthStatusLabel[run.status]}</strong>
          </li>
        );
      })}
    </ol>
  );
}

function MonthProgress({ run, mobile = false }: { run: ScheduleRun | null; mobile?: boolean }) {
  if (!run?.months?.length) return null;
  return (
    <ol className={`month-progress ${mobile ? 'mobile-month-progress' : 'desktop-month-progress'}`} aria-label={`${run.period} monthly export status`}>
      {run.months.map((month) => {
        const label = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' })
          .format(new Date(`${month.period}-01T00:00:00Z`));
        return (
          <li className={month.status} key={month.period} aria-label={`${label} ${monthStatusLabel[month.status]}`}>
            <span>{label}</span><strong>{monthStatusLabel[month.status]}</strong>
          </li>
        );
      })}
    </ol>
  );
}

export function ScheduleManager() {
  const [schedules, setSchedules] = useState<CostSchedule[]>([]);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ScheduleFilter>('all');
  const [scheduleEditingId, setScheduleEditingId] = useState<string | null>(null);
  const [scheduleValue, setScheduleValue] = useState(defaultScheduleValue);
  const [windowValue, setWindowValue] = useState<ScheduleWindowMonths>(6);
  const [dailyEnabled, setDailyEnabled] = useState(true);
  const [dailyTime, setDailyTime] = useState(DEFAULT_DAILY_TIME);
  const [exportEditingId, setExportEditingId] = useState<string | null>(null);
  const [exportConfiguration, setExportConfiguration] = useState<FocusExportConfiguration | null>(null);
  const [exportLoading, setExportLoading] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportRetryAfter, setExportRetryAfter] = useState(0);
  const [allowDestinationRole, setAllowDestinationRole] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [runningAll, setRunningAll] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [runs, setRuns] = useState<Record<string, ScheduleRun[]>>({});
  const [historyLoading, setHistoryLoading] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);
  const pendingRefresh = useRef<AbortController | null>(null);
  const pendingHistory = useRef<AbortController | null>(null);
  const pendingExport = useRef<AbortController | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh() {
    pendingRefresh.current?.abort();
    const controller = new AbortController();
    pendingRefresh.current = controller;
    setLoading(true);
    try {
      const nextSchedules = await listCostSchedules(controller.signal);
      if (pendingRefresh.current !== controller) return;
      setSchedules(nextSchedules);
      setLoadError(null);
      setLoadedAt(new Date().toISOString());
      if (exportEditingId && !nextSchedules.some(item => item.subscriptionId === exportEditingId && item.readAccess === true && item.costAccess === true)) closeExportConfiguration();
      return nextSchedules;
    } catch (refreshError) {
      if (pendingRefresh.current !== controller || controller.signal.aborted) return;
      setLoadError(refreshError instanceof Error ? refreshError : new Error('FOCUS exports could not be loaded.'));
      setSchedules([]);
      setRuns({});
      setExpandedId(null);
      setScheduleEditingId(null);
      setDeletingId(null);
      closeExportConfiguration();
      pendingHistory.current?.abort();
      pendingHistory.current = null;
    } finally {
      if (pendingRefresh.current === controller) {
        pendingRefresh.current = null;
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    void refresh();
    return () => {
      pendingRefresh.current?.abort();
      pendingRefresh.current = null;
      pendingHistory.current?.abort();
      pendingHistory.current = null;
      pendingExport.current?.abort();
      pendingExport.current = null;
    };
  }, []);

  useEffect(() => {
    // GET /api/schedules only checks cached ARM permissions (no live Cost Management query),
    // so periodic polling here is safe and does not risk the per-action throttling documented
    // for pause/resume/run/create/configure.
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      if (pendingRefresh.current || pendingExport.current || busyId || runningAll || deletingId) return;
      void refresh();
    }, 15000);
    return () => window.clearInterval(interval);
  }, [busyId, runningAll, deletingId]);

  useEffect(() => {
    if (exportRetryAfter <= 0) return;
    const interval = window.setInterval(() => setExportRetryAfter(seconds => Math.max(0, seconds - 1)), 1000);
    return () => window.clearInterval(interval);
  }, [exportRetryAfter > 0]);

  function closeExportConfiguration() {
    pendingExport.current?.abort();
    pendingExport.current = null;
    setExportEditingId(null);
    setExportConfiguration(null);
    setExportLoading(false);
    setExportError(null);
    setExportRetryAfter(0);
    setAllowDestinationRole(false);
  }

  async function openExportConfiguration(schedule: CostSchedule) {
    if (loading || loadError || busyId || exportRetryAfter > 0 || schedule.readAccess !== true || schedule.costAccess !== true) return;
    pendingExport.current?.abort();
    const controller = new AbortController();
    pendingExport.current = controller;
    setExportEditingId(schedule.subscriptionId);
    setScheduleEditingId(null);
    setExportConfiguration(null);
    setAllowDestinationRole(false);
    setExportError(null);
    setExportLoading(true);
    try {
      const details = await getCostExportConfiguration(schedule.subscriptionId, controller.signal);
      if (pendingExport.current !== controller) return;
      setExportConfiguration(details);
      if (details.state === 'configured') await refresh();
    } catch (setupError) {
      if (pendingExport.current !== controller || controller.signal.aborted) return;
      setExportError(setupError instanceof Error ? setupError.message : 'Export configuration is unavailable.');
      setExportRetryAfter(setupError instanceof ApiRequestError ? setupError.retryAfterSeconds || 0 : 0);
    } finally {
      if (pendingExport.current === controller) {
        pendingExport.current = null;
        setExportLoading(false);
      }
    }
  }

  async function configureExport(schedule: CostSchedule) {
    if (loading || loadError || busyId || exportRetryAfter > 0 || !allowDestinationRole || exportEditingId !== schedule.subscriptionId
      || exportConfiguration?.subscriptionId !== schedule.subscriptionId || !exportConfiguration.canConfigure
      || schedule.readAccess !== true || schedule.costAccess !== true) return;
    const controller = new AbortController();
    pendingExport.current = controller;
    setBusyId(schedule.subscriptionId);
    setExportError(null);
    try {
      await configureCostExport(schedule.subscriptionId, true, controller.signal);
      if (pendingExport.current !== controller || controller.signal.aborted) return;
      pendingExport.current = null;
      closeExportConfiguration();
      setNotice(`${schedule.displayName}: FOCUS export configured.`);
      const updated = await refresh();
      const current = updated?.find(item => item.subscriptionId === schedule.subscriptionId);
      if (current && current.state !== 'unknown' && current.readAccess === true && current.costAccess === true
        && (!current.availability || current.availability === 'available')) editSchedule(current);
    } catch (setupError) {
      if (controller.signal.aborted) return;
      setExportConfiguration(null);
      setAllowDestinationRole(false);
      setExportError(setupError instanceof Error ? setupError.message : 'Export configuration could not be confirmed. Refresh its status.');
      setExportRetryAfter(setupError instanceof ApiRequestError ? setupError.retryAfterSeconds || 0 : 0);
    } finally {
      if (pendingExport.current === controller) pendingExport.current = null;
      setBusyId(null);
    }
  }

  async function changeState(schedule: CostSchedule) {
    if (schedule.state !== 'active' && schedule.state !== 'paused') return;
    const nextState = schedule.state === 'active' ? 'paused' : 'active';
    setBusyId(schedule.subscriptionId);
    setError(null);
    try {
      await setCostScheduleState(schedule.subscriptionId, nextState);
      setNotice(`${schedule.displayName} ${nextState === 'active' ? 'resumed' : 'paused'}.`);
      await refresh();
    } catch (stateError) {
      setError(stateError instanceof Error ? stateError.message : 'Schedule update failed.');
    } finally {
      setBusyId(null);
    }
  }

  async function runNow(schedule: CostSchedule) {
    setBusyId(schedule.subscriptionId);
    setError(null);
    try {
      await runCostSchedule(schedule.subscriptionId);
      setNotice(`${schedule.displayName}: export requested \u2014 pulling and overwriting the last ${windowOf(schedule)} months.`);
      await refresh();
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Export could not be started.');
    } finally {
      setBusyId(null);
    }
  }

  async function runAll() {
    const subscriptionIds = schedules
      .filter((schedule) => schedule.readAccess === true && schedule.costAccess === true
        && (schedule.state === 'active' || schedule.state === 'paused' || schedule.state === 'not_scheduled'
          || (schedule.state === 'unknown' && schedule.availability === 'export_unavailable')))
      .map((schedule) => schedule.subscriptionId);
    if (!subscriptionIds.length || subscriptionIds.length !== schedules.length) return;
    setRunningAll(true);
    setError(null);
    try {
      const results = await runAllCostSchedules(subscriptionIds);
      const queued = results.filter((result) => result.status !== 'failed').length;
      const failures = results.filter((result) => result.status === 'failed');
      await refresh();
      setNotice(`${queued} refresh${queued === 1 ? '' : 'es'} requested${failures.length ? `; ${failures.length} failed` : ''}.`);
      if (failures.length) setError(failures.map((result) => result.error).filter(Boolean).join(' · '));
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Exports could not be started.');
    } finally {
      setRunningAll(false);
    }
  }

  function editSchedule(schedule: CostSchedule) {
    closeExportConfiguration();
    setScheduleEditingId(schedule.subscriptionId);
    setScheduleValue(schedule.scheduleStartAt?.slice(0, 16) ?? defaultScheduleValue());
    setWindowValue(windowOf(schedule));
    setDailyEnabled(schedule.daily?.enabled ?? true);
    setDailyTime(schedule.daily?.timeUtc ?? DEFAULT_DAILY_TIME);
  }

  async function saveSchedule(schedule: CostSchedule) {
    if (loading || loadError || schedule.state === 'unknown'
      || schedule.readAccess !== true || schedule.costAccess !== true
      || (schedule.availability && schedule.availability !== 'available')) return;
    if (!DAILY_TIME_PATTERN.test(dailyTime)) {
      setError('Enter the daily pull time as HH:MM in UTC.');
      return;
    }
    const daily = { enabled: dailyEnabled, timeUtc: dailyTime };
    setBusyId(schedule.subscriptionId);
    setError(null);
    try {
      if (schedule.state === 'not_scheduled') {
        await createCostSchedule(schedule.subscriptionId, utcSchedule(scheduleValue), windowValue, daily);
      } else {
        await setCostScheduleState(schedule.subscriptionId, schedule.state, utcSchedule(scheduleValue), windowValue, daily);
      }
      setNotice(`${schedule.displayName} monthly UTC schedule updated with ${windowValue} months of history${dailyEnabled ? ` and a daily pull at ${dailyTime} UTC` : ' and no daily pull'}.`);
      setScheduleEditingId(null);
      await refresh();
    } catch (scheduleError) {
      setError(scheduleError instanceof Error ? scheduleError.message : 'Schedule could not be updated.');
      if (scheduleError instanceof ApiRequestError && scheduleError.status === 403) await refresh();
    } finally {
      setBusyId(null);
    }
  }

  async function remove(schedule: CostSchedule) {
    setBusyId(schedule.subscriptionId);
    setError(null);
    try {
      await deleteCostSchedule(schedule.subscriptionId);
      setDeletingId(null);
      setExpandedId((current) => current === schedule.subscriptionId ? null : current);
      setNotice(`${schedule.displayName} schedule removed. Existing export configuration and access were not changed.`);
      await refresh();
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : 'Export deletion failed.');
    } finally {
      setBusyId(null);
    }
  }

  async function toggleHistory(schedule: CostSchedule) {
    pendingHistory.current?.abort();
    pendingHistory.current = null;
    if (expandedId === schedule.subscriptionId) {
      setExpandedId(null);
      return;
    }
    const controller = new AbortController();
    pendingHistory.current = controller;
    setExpandedId(schedule.subscriptionId);
    setHistoryLoading(schedule.subscriptionId);
    setHistoryError(null);
    try {
      const history = await listScheduleRuns(schedule.subscriptionId, controller.signal);
      if (pendingHistory.current !== controller) return;
      setRuns((current) => ({ ...current, [schedule.subscriptionId]: history }));
    } catch (historyError) {
      if (pendingHistory.current !== controller || controller.signal.aborted) return;
      setHistoryError(historyError instanceof Error ? historyError.message : 'Execution history could not be loaded.');
    } finally {
      if (pendingHistory.current === controller) {
        pendingHistory.current = null;
        setHistoryLoading(null);
      }
    }
  }

  const scheduledCount = schedules.filter((schedule) => schedule.state === 'active' || schedule.state === 'paused').length;
  const countsUnavailable = !!loadError || schedules.some((schedule) => schedule.state === 'unknown');
  const unavailableCount = schedules.filter((schedule) => schedule.availability && schedule.availability !== 'available').length;
  const runAllCount = schedules.filter((schedule) => schedule.readAccess === true && schedule.costAccess === true
    && (schedule.state === 'active' || schedule.state === 'paused' || schedule.state === 'not_scheduled'
      || (schedule.state === 'unknown' && schedule.availability === 'export_unavailable'))).length;
  // "Run all" acts on every listed subscription. Offering it while any row cannot be run
  // would silently act on a subset while presenting it as the whole estate, so it stays
  // disabled unless every listed subscription is runnable (or bootstrappable via export).
  const canRunAll = runAllCount > 0 && runAllCount === schedules.length;
  const loadStatus = loadError instanceof ApiRequestError && loadError.status === 401 ? 'Sign-in required'
    : loadError instanceof ApiRequestError && loadError.status === 403 ? 'Access not verified'
    : 'Subscription status unavailable';
  const attentionCount = schedules.filter(needsAttention).length;
  const filterOptions: { value: ScheduleFilter; label: string; count: number }[] = [
    { value: 'all', label: 'All subscriptions', count: schedules.length },
    { value: 'attention', label: 'Needs attention', count: attentionCount },
    ...(['active', 'paused', 'not_scheduled'] as const).map(value => ({
      value, label: value === 'not_scheduled' ? 'Not scheduled' : value === 'active' ? 'Active' : 'Paused',
      count: schedules.filter(schedule => schedule.state === value).length,
    })),
  ];
  const search = query.trim().toLowerCase();
  const visibleSchedules = schedules.filter(schedule =>
    (filter === 'all' || (filter === 'attention' ? needsAttention(schedule) : schedule.state === filter))
    && `${schedule.displayName} ${schedule.subscriptionId}`.toLowerCase().includes(search));
  const clearFilters = () => { setQuery(''); setFilter('all'); };

  return (
    <section className="operations-view" aria-label="Export schedules">
      <section className="operations-heading">
        <span className="operations-kicker"><CalendarClock size={14} /> 3-6 closed months plus a daily pull through yesterday</span>
        <h1>FOCUS export schedules</h1>
        <div className="operations-metrics" aria-label="Schedule summary">
          <span><strong data-unavailable={countsUnavailable}>{loading ? '...' : countsUnavailable ? 'Unavailable' : scheduledCount}</strong> scheduled</span>
          <span><strong data-unavailable={countsUnavailable}>{loading ? '...' : countsUnavailable ? 'Unavailable' : schedules.filter((schedule) => schedule.state === 'active').length}</strong> active</span>
          {unavailableCount > 0 && <span><strong>{unavailableCount}</strong> status unavailable</span>}
          <span><strong>{loading ? '...' : loadError ? 'Unavailable' : attentionCount}</strong> needs attention</span>
          <span><strong>UTC</strong> schedule time</span>
        </div>
      </section>

      {notice && <div className="operations-notice" role="status"><Check size={15} />{notice}</div>}
      {error && <div className="operations-error" role="alert"><X size={15} />{error}</div>}
      {loadError && <div className="operations-error" role="alert"><X size={15} /><span><strong>{loadStatus}.</strong> {loadError.message}</span>{loadError instanceof ApiRequestError && loadError.status === 401 && <button type="button" className="outline-command" onClick={() => void redirectApiIdentity('').catch(() => setError('Sign-in could not start.'))}>Sign in</button>}</div>}

      <section className="schedule-section" aria-labelledby="schedule-list-title">
        <header className="schedule-section-header">
          <div><span>Completed-month refresh</span><h2 id="schedule-list-title">FOCUS schedules</h2></div>
          <div className="schedule-header-actions">
            <span role="status">{loading ? 'Refreshing...' : loadError ? 'Refresh failed' : loadedAt ? `Checked ${formatDate(loadedAt)}` : ''}</span>
            <button className="outline-command" type="button" onClick={() => void runAll()} disabled={runningAll || loading || !!loadError || !canRunAll} title="Runs every listed subscription, regardless of search or filters">{runningAll ? <RefreshCw className="spin" size={15} /> : <Play size={15} />} Run all</button>
            <button className="icon-command" type="button" onClick={() => void refresh()} disabled={loading} title="Refresh schedules" aria-label="Refresh schedules"><RefreshCw className={loading ? 'spin' : ''} size={17} /></button>
          </div>
        </header>

        <p className="schedule-evidence-note">Monthly and daily pulls are separate. “Data through” is the latest reported UTC coverage, not live spend freshness. Run timestamps use your local timezone; schedule settings use UTC.</p>
        {schedules.length > 0 && <div className="schedule-list-toolbar">
          <label className="schedule-search"><Search size={16} aria-hidden="true" /><input type="search" aria-label="Search schedules" placeholder="Search subscription name or ID" value={query} onChange={event => setQuery(event.target.value)} /></label>
          <select aria-label="Filter schedules" value={filter} onChange={event => setFilter(event.target.value as ScheduleFilter)}>
            {filterOptions.map(option => <option key={option.value} value={option.value}>{option.label} ({option.count})</option>)}
          </select>
          {(query || filter !== 'all') && <button type="button" className="outline-command" onClick={clearFilters}>Clear filters</button>}
          <span role="status">{visibleSchedules.length} of {schedules.length} subscriptions</span>
          {(query || filter !== 'all') && <small>Run all still includes every subscription.</small>}
        </div>}
        {filter === 'attention' && <p className="schedule-evidence-note">Includes failed or unknown executions, unavailable status, missing daily evidence and subscriptions without a monthly schedule. Paused schedules and daily pulls switched off are not failures.</p>}
        <div className="schedule-table-wrap mobile-card-scroll" aria-busy={loading} tabIndex={0} role="region" aria-label="FOCUS export schedules">
          <table className="schedule-table mobile-card-table" role="table">
            <thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">Subscription</th><th role="columnheader" scope="col">State</th><th role="columnheader" scope="col">Next monthly run</th><th role="columnheader" scope="col">Monthly / daily evidence</th><th role="columnheader" scope="col" aria-label="Actions" /></tr></thead>
            <tbody role="rowgroup">
              {loading && schedules.length === 0 && <tr role="row"><td role="cell" colSpan={5} className="schedule-empty">Loading FOCUS exports...</td></tr>}
              {!loading && schedules.length === 0 && <tr role="row"><td role="cell" colSpan={5} className="schedule-empty">{loadError ? loadStatus : 'No accessible subscriptions returned'}</td></tr>}
              {!loading && schedules.length > 0 && visibleSchedules.length === 0 && <tr role="row"><td role="cell" colSpan={5} className="schedule-empty">No subscriptions match these filters. <button type="button" className="outline-command" onClick={clearFilters}>Show all subscriptions</button></td></tr>}
              {visibleSchedules.map((schedule) => {
                const isBusy = loading || !!loadError || busyId === schedule.subscriptionId || schedule.state === 'unknown'
                  || schedule.readAccess !== true || schedule.costAccess !== true
                  || (!!schedule.availability && schedule.availability !== 'available');
                // Export must remain clickable even when nothing has been configured yet or the
                // last automatic check failed: it is the only action allowed to create/write the
                // export, so it cannot depend on state that only exists once one already does.
                const canExport = !loading && !loadError && busyId === null && schedule.readAccess === true && schedule.costAccess === true
                  && (!schedule.availability || schedule.availability === 'available' || schedule.availability === 'export_unavailable');
                const isExpanded = expandedId === schedule.subscriptionId;
                return [
                  <tr key={schedule.subscriptionId} role="row">
                    <td role="cell" data-label="Subscription"><strong>{schedule.displayName}</strong><code>{schedule.subscriptionId}</code><MonthProgress run={schedule.latestRun} mobile /><DailyProgress daily={schedule.daily} mobile /></td>
                    <td role="cell" data-label="State"><span className={`schedule-state ${schedule.state}`}><i />{schedule.state === 'unknown' ? 'Unknown' : schedule.state.replace('_', ' ')}</span></td>
                    <td role="cell" data-label="Next monthly run">{schedule.state === 'unknown' ? 'Unavailable' : schedule.nextRunAt ? formatDate(schedule.nextRunAt) : schedule.state === 'paused' ? 'Paused' : schedule.state === 'active' ? 'Unavailable' : 'Not scheduled'}</td>
                    <td role="cell" data-label="Monthly / daily evidence">{schedule.availability && schedule.availability !== 'available' ? <><span>{schedule.availability === 'access_unavailable' ? 'Access unavailable' : schedule.availability === 'configuration_unavailable' ? 'Scheduler setup incomplete' : schedule.availability === 'history_unavailable' ? 'Execution history unavailable' : 'Export status unavailable'}</span><small>{schedule.statusMessage}</small></> : <><span className={`run-state ${schedule.latestRun?.status ?? 'pending'}`}>{schedule.state === 'not_scheduled' ? 'Export configured' : runLabel(schedule.latestRun)}</span><small>{schedule.latestRun ? `${formatDate(schedule.latestRun.completedAt || schedule.latestRun.startedAt)} · ${formatDuration(schedule.latestRun.durationSeconds)}` : schedule.state === 'not_scheduled' ? '' : 'No native execution record'}</small>{schedule.latestRun?.error && <small className="schedule-execution-error">{schedule.latestRun.error}</small>}<MonthProgress run={schedule.latestRun} /><DailyProgress daily={schedule.daily} /><DailyStatus daily={schedule.daily} /></>}</td>
                    <td role="cell" data-label="Actions"><div className="schedule-actions">
                      <button type="button" data-action="Setup" onClick={() => void openExportConfiguration(schedule)} disabled={loading || !!loadError || busyId !== null || exportRetryAfter > 0 || schedule.readAccess !== true || schedule.costAccess !== true} title="Configure FOCUS export" aria-label={`Configure export for ${schedule.displayName}`} aria-expanded={exportEditingId === schedule.subscriptionId}><Settings2 size={16} /></button>
                      <button type="button" data-action="History" onClick={() => void toggleHistory(schedule)} disabled={loading || !!loadError || schedule.readAccess !== true || schedule.costAccess !== true || schedule.state === 'not_scheduled' || schedule.state === 'unknown'} title="Execution history" aria-expanded={isExpanded} aria-label={`Execution history for ${schedule.displayName}`}><History size={16} />{isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}</button>
                      <button type="button" data-action="Run now" onClick={() => void runNow(schedule)} disabled={!canExport} title={`Export: create if needed, pull and overwrite the last ${windowOf(schedule)} months`} aria-label={`Export ${schedule.displayName} now`}><Play size={16} /></button>
                      <button type="button" data-action="Schedule" onClick={() => editSchedule(schedule)} disabled={isBusy} title={schedule.state === 'not_scheduled' ? 'Schedule export' : 'Reschedule export'} aria-label={`${schedule.state === 'not_scheduled' ? 'Schedule' : 'Reschedule'} ${schedule.displayName}`}><CalendarClock size={16} /></button>
                      <button type="button" data-action={schedule.state === 'active' ? 'Pause' : 'Resume'} onClick={() => void changeState(schedule)} disabled={isBusy || schedule.state === 'not_scheduled'} title={schedule.state === 'active' ? 'Pause schedule' : 'Resume schedule'} aria-label={`${schedule.state === 'active' ? 'Pause' : 'Resume'} ${schedule.displayName}`}>{schedule.state === 'active' ? <Pause size={16} /> : <Play size={16} />}</button>
                      {deletingId === schedule.subscriptionId ? <><button className="cancel-delete" type="button" data-action="Keep" onClick={() => setDeletingId(null)} title="Cancel delete" aria-label="Cancel delete"><X size={16} /></button><button className="confirm-delete" type="button" data-action="Confirm" onClick={() => void remove(schedule)} disabled={isBusy} title="Confirm delete" aria-label={`Confirm delete ${schedule.displayName}`}><Check size={16} /></button></> : <button type="button" data-action="Delete" onClick={() => setDeletingId(schedule.subscriptionId)} disabled={isBusy || schedule.state === 'not_scheduled'} title="Delete export" aria-label={`Delete ${schedule.displayName}`}><Trash2 size={16} /></button>}
                    </div></td>
                  </tr>,
                  exportEditingId === schedule.subscriptionId ? <tr role="row" className="history-row schedule-editor-row" key={`${schedule.subscriptionId}-export`}><td role="cell" colSpan={5}><div className="schedule-editor export-config-editor" role="region" aria-label={`Export configuration for ${schedule.displayName}`}>
                    <strong>FOCUS export configuration</strong>
                    {exportLoading && <span role="status">Checking export configuration...</span>}
                    {exportError && <div className="operations-error" role="alert">{exportError}{exportRetryAfter > 0 && ` (retry available in ${exportRetryAfter}s)`}</div>}
                    {exportConfiguration && <>
                      <dl className="export-configuration-details">
                        <div><dt>Export</dt><dd>{exportConfiguration.exportName}</dd></div>
                        <div><dt>Configuration</dt><dd>{exportConfiguration.state === 'configured' ? 'Configured' : 'Not created'}</dd></div>
                        <div><dt>Storage account</dt><dd><code>{exportConfiguration.storageResourceId}</code></dd></div>
                        <div><dt>Container and prefix</dt><dd><code>{exportConfiguration.container}/{exportConfiguration.rootFolderPath}</code></dd></div>
                        {exportConfiguration.daily && <div><dt>Daily export</dt><dd>{exportConfiguration.daily.exportName}{' · '}{exportConfiguration.daily.state === 'configured' ? 'Configured' : 'Not created'}</dd></div>}
                        {exportConfiguration.daily && <div><dt>Daily prefix</dt><dd><code>{exportConfiguration.container}/{exportConfiguration.daily.rootFolderPath}</code></dd></div>}
                        <div><dt>Dataset</dt><dd>FOCUS {exportConfiguration.dataVersion} / {exportConfiguration.format} / {windowOf(schedule)} completed months</dd></div>
                        <div><dt>Native Azure schedule</dt><dd>{exportConfiguration.nativeSchedule}</dd></div>
                      </dl>
                      {canConfigureAny(exportConfiguration) && <label className="export-role-confirmation"><input type="checkbox" checked={allowDestinationRole} onChange={event => setAllowDestinationRole(event.target.checked)} disabled={busyId !== null} /><span>Allow Storage Blob Data Contributor for the export identity on this container</span></label>}
                    </>}
                    <div className="export-config-actions">
                      {exportConfiguration && canConfigureAny(exportConfiguration) && <button className="primary-command" type="button" onClick={() => void configureExport(schedule)} disabled={busyId !== null || !allowDestinationRole || loading || !!loadError || exportRetryAfter > 0}>{busyId === schedule.subscriptionId ? <RefreshCw className="spin" size={15} /> : <Settings2 size={15} />} Configure export</button>}
                      {exportConfiguration?.state === 'configured' && <button className="primary-command" type="button" onClick={() => editSchedule(schedule)} disabled={isBusy}><CalendarClock size={15} /> {schedule.state === 'not_scheduled' ? 'Schedule export' : 'Edit schedule'}</button>}
                      <button className="outline-command" type="button" onClick={() => void openExportConfiguration(schedule)} disabled={busyId !== null || exportLoading || loading || exportRetryAfter > 0}>{exportRetryAfter > 0 ? `Retry in ${exportRetryAfter}s` : <><RefreshCw size={15} /> Refresh status</>}</button>
                      <button className="outline-command" type="button" onClick={closeExportConfiguration} disabled={busyId !== null}><X size={15} /> Close</button>
                    </div>
                  </div></td></tr> : null,
                  scheduleEditingId === schedule.subscriptionId ? <tr role="row" className="history-row schedule-editor-row" key={`${schedule.subscriptionId}-editor`}><td role="cell" colSpan={5}><div className="schedule-editor"><label><span>First monthly run (UTC)</span><input type="datetime-local" value={scheduleValue} onChange={(event) => setScheduleValue(event.target.value)} /></label><label className="schedule-window-field"><span>Months of history</span><select value={windowValue} onChange={(event) => setWindowValue(Number(event.target.value) as ScheduleWindowMonths)}>{SCHEDULE_WINDOW_MONTHS.map((months) => <option key={months} value={months}>{months} months</option>)}</select></label><label className="schedule-daily-toggle"><input type="checkbox" checked={dailyEnabled} onChange={(event) => setDailyEnabled(event.target.checked)} /><span>Daily pull through yesterday</span></label><label className="schedule-window-field"><span>Daily pull (UTC)</span><input type="time" value={dailyTime} onChange={(event) => setDailyTime(event.target.value)} disabled={!dailyEnabled} /></label><button className="primary-command" type="button" onClick={() => void saveSchedule(schedule)} disabled={isBusy}><Check size={15} /> Save schedule</button><button className="outline-command" type="button" onClick={() => setScheduleEditingId(null)}><X size={15} /> Cancel</button></div></td></tr> : null,
                  isExpanded ? <tr role="row" className="history-row" key={`${schedule.subscriptionId}-history`}><td role="cell" colSpan={5}><div className="run-history">{historyLoading === schedule.subscriptionId ? <span role="status">Loading execution history...</span> : historyError ? <span role="alert">{historyError}</span> : <>{(runs[schedule.subscriptionId] ?? []).length === 0 && <span>No native execution history</span>}{(runs[schedule.subscriptionId] ?? []).map((run) => <div key={run.runId}><span className={`run-status-dot ${run.status}`} /><strong>{run.period || 'Custom'}</strong><span>{run.status}</span><time>{formatDate(run.completedAt || run.startedAt)}</time><time>{formatDuration(run.durationSeconds)}</time>{run.error && <small>{run.error}</small>}</div>)}</>}</div></td></tr> : null,
                ];
              })}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}