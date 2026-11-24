import { Component, Suspense, lazy, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from 'react';
import { BarChart3, LoaderCircle, Maximize2, MessageSquareText, Moon, RefreshCw, Rows3, Settings2, Sun } from 'lucide-react';
import { CostRangeControls } from './components/CostRangeControls';
import { monthCostWindow, type CostWindow } from './report/costDetails';
import { runCostAssessment } from './collectors/costAssessment';
import { getLatestReport, narrate, type CostAgentOutput } from './api';
import { apiFetch, initializeApiIdentity, IDENTITY_REQUIRED_EVENT, type VerifiedIdentity } from './apiIdentity';
import type { ReportSnapshot } from './report/models';
import { BRAND_NAME } from './brand';
import './manual-workspace.css';

const ReportView = lazy(() => import('./components/ReportView').then((module) => ({ default: module.ReportView })));
const ChatWindow = lazy(() => import('./components/ChatWindow').then((module) => ({ default: module.ChatWindow })));
const ManualAdmin = lazy(() => import('./components/ManualAdmin'));
type View = 'report' | 'chat' | 'admin';
type Subscription = { subscriptionId: string; displayName: string };

class ReportErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('Report view crashed', error, info); }
  render() {
    return this.state.error
      ? <section role="alert">This report could not be displayed. <button type="button" onClick={() => window.location.reload()}>Reload</button></section>
      : this.props.children;
  }
}

function Loading({ label }: { label: string }) {
  return <p className="loading-state" role="status"><LoaderCircle className="spin" size={18} aria-hidden="true" /> {label}</p>;
}

export default function App() {
  const [identity, setIdentity] = useState<VerifiedIdentity | null>(null);
  const [startupError, setStartupError] = useState<string | null>(null);
  const [startup, setStartup] = useState(0);
  const [version, setVersion] = useState(0);
  const [view, setView] = useState<View>('report');
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [snapshot, setSnapshot] = useState<ReportSnapshot | null>(null);
  const [narration, setNarration] = useState<CostAgentOutput>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [costWindow, setCostWindow] = useState<CostWindow>({ startDate: '', endDate: '' });
  const [theme, setTheme] = useState(() => window.localStorage.getItem('mkai-theme') === 'light' ? 'light' : 'dark');
  const [density, setDensity] = useState(() => window.localStorage.getItem('mkai-density') === 'comfortable' ? 'comfortable' : 'compact');
  const generation = useRef(0);
  const pendingReaderDisposal = useRef<{ reader: NonNullable<ReportSnapshot['report']['remote']>; timer: number } | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.density = density;
    window.localStorage.setItem('mkai-theme', theme);
    window.localStorage.setItem('mkai-density', density);
  }, [theme, density]);

  useEffect(() => {
    let active = true;
    setStartupError(null);
    initializeApiIdentity().then((session) => { if (active) setIdentity(session); })
      .catch((failure: unknown) => { if (active) setStartupError(failure instanceof Error ? failure.message : 'Workspace initialization failed.'); });
    return () => { active = false; };
  }, [startup]);

  useEffect(() => {
    const expired = () => {
      generation.current += 1;
      setIdentity(null); setSnapshot(null); setSubscription(null);
      setStartupError('This browser workspace expired. Reopen the workspace and upload your report again.');
    };
    globalThis.window.addEventListener(IDENTITY_REQUIRED_EVENT, expired);
    return () => globalThis.window.removeEventListener(IDENTITY_REQUIRED_EVENT, expired);
  }, []);

  useEffect(() => {
    const reader = snapshot?.report.remote;
    if (!reader) return;
    if (pendingReaderDisposal.current?.reader === reader) {
      globalThis.window.clearTimeout(pendingReaderDisposal.current.timer);
      pendingReaderDisposal.current = null;
    }
    return () => {
      const timer = globalThis.window.setTimeout(() => reader.dispose(), 0);
      pendingReaderDisposal.current = { reader, timer };
    };
  }, [snapshot?.report.remote]);

  useEffect(() => {
    if (!identity) return;
    const controller = new AbortController();
    const current = ++generation.current;
    setLoading(true); setError(null);
    async function load() {
      const response = await apiFetch('/api/subscriptions', { signal: controller.signal });
      if (!response.ok) throw new Error('The configured subscription could not be loaded.');
      const subscriptions: Subscription[] = await response.json();
      if (subscriptions.length > 1) throw new Error('This workspace must contain only one target subscription.');
      const selected = subscriptions[0] ?? null;
      const saved = selected ? await getLatestReport(controller.signal, [selected.subscriptionId], 90) : null;
      if (controller.signal.aborted || current !== generation.current) return;
      setSubscription(selected); setSnapshot(saved);
      setNarration(null);
      if (saved) {
        const dates = saved.report.costDetails?.dates ?? [];
        setCostWindow({ startDate: dates[0] ?? saved.report.reportMetadata.periodStart,
          endDate: dates.at(-1) ?? saved.report.reportMetadata.periodEnd });
        if (identity?.features?.aiNarration) {
          try {
            const result = await narrate(saved.subscriptionIds, saved.report);
            if (!controller.signal.aborted && current === generation.current) setNarration(result);
          } catch (failure) {
            if (!controller.signal.aborted && current === generation.current) {
              setError(`Dashboard ready; optional narration failed: ${failure instanceof Error ? failure.message : 'Retry the report.'}`);
            }
          }
        }
      }
    }
    load().catch((failure: unknown) => {
      if (!controller.signal.aborted && current === generation.current) setError(failure instanceof Error ? failure.message : 'Report loading failed.');
    }).finally(() => { if (!controller.signal.aborted && current === generation.current) setLoading(false); });
    return () => controller.abort();
  }, [identity, version]);

  async function rebuild() {
    if (!subscription || running) return;
    const current = ++generation.current;
    setRunning(true); setError(null);
    try {
      await runCostAssessment([subscription.subscriptionId], 90);
      if (current === generation.current) setVersion((value) => value + 1);
    } catch (failure) {
      if (current === generation.current) setError(failure instanceof Error ? failure.message : 'Dashboard generation failed.');
    } finally { if (current === generation.current) setRunning(false); }
  }

  if (startupError) return <main className="center-screen"><p role="alert">{startupError}</p>
    <button type="button" onClick={() => setStartup((value) => value + 1)}>Reopen workspace</button></main>;
  if (!identity) return <main className="center-screen"><Loading label="Opening your FOCUS workspace..." /></main>;
  const report = snapshot?.report;
  const dates = report?.costDetails?.dates ?? [];
  return <div className="app-shell manual-workspace">
    <a className="skip-link" href="#workspace-main">Skip to workspace</a>
    <header className="app-header">
      <span className="brand-lockup"><img className="brand-mark" src="/brand-mark.svg" alt="" width={28} height={28} /><span className="brand-product">{BRAND_NAME}</span></span>
      <nav className="workspace-nav" aria-label="Workspace">
        <button type="button" className={view === 'report' ? 'active' : ''} onClick={() => setView('report')}><BarChart3 size={15} /> Report</button>
        <button type="button" className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')}><MessageSquareText size={15} /> Chat</button>
        <button type="button" className={view === 'admin' ? 'active' : ''} onClick={() => setView('admin')}><Settings2 size={15} /> Admin</button>
      </nav>
      <span className="account-chip">
        <button type="button" className="theme-toggle" aria-label="Toggle display density" onClick={() => setDensity((value) => value === 'compact' ? 'comfortable' : 'compact')}>{density === 'compact' ? <Maximize2 size={15} /> : <Rows3 size={15} />}</button>
        <button type="button" className="theme-toggle" aria-label="Toggle light and dark theme" onClick={() => setTheme((value) => value === 'light' ? 'dark' : 'light')}>{theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}</button>
        <span className="account-name">No-login FOCUS workspace</span>
      </span>
    </header>
    <main id="workspace-main" tabIndex={-1}>
      <div className="view-panel" hidden={view !== 'report'}>
        <section className="manual-scope">
          <div><strong>{subscription?.displayName ?? 'No target subscription configured'}</strong><small>{subscription?.subscriptionId ?? 'Start in Admin: target subscription, FOCUS report, optional Reader credentials.'}</small></div>
          <button type="button" className="ghost-button" disabled={!snapshot || running || loading} onClick={() => void rebuild()}><RefreshCw size={15} /> {running ? 'Processing...' : 'Rebuild from uploaded report'}</button>
          <button type="button" className="dark-button" onClick={() => setView('admin')}>Upload / replace report</button>
          {report && <CostRangeControls dates={dates} value={costWindow} onChange={setCostWindow}
            month={monthCostWindow(report.reportMetadata.periodStart, report.reportMetadata.periodEnd)} />}
        </section>
        {loading && <Loading label="Opening saved dashboard..." />}
        {running && <Loading label="Processing the uploaded report. The previous complete dashboard stays available." />}
        {error && <section role="alert">{error} <button type="button" onClick={() => setVersion((value) => value + 1)}>Retry</button></section>}
        {!report && !loading && <section className="report-awaiting">
          <h1>Build a dashboard from your FOCUS report</h1>
          <p>Set one target subscription in Admin, then upload a FOCUS CSV or gzip report for any duration. A replacement report overrides the previous active report.</p>
          <p>Service-principal credentials are optional. Without them, financial views work and live Azure inventory, Advisor and metric sections stay unavailable.</p>
          <button type="button" className="dark-button" onClick={() => setView('admin')}>Open Admin and upload</button>
        </section>}
        {report && <ReportErrorBoundary><Suspense fallback={<Loading label="Loading report views..." />}>
          <ReportView key={snapshot.snapshotId} report={report} narration={narration} snapshotId={snapshot.snapshotId}
            snapshotCreatedAt={snapshot.createdAt} costWindow={costWindow} onCostWindowChange={setCostWindow} />
        </Suspense></ReportErrorBoundary>}
      </div>
      <div className="view-panel" hidden={view !== 'admin'}><Suspense fallback={<Loading label="Loading Admin..." />}>
        <ManualAdmin onConfigurationChanged={() => { setSnapshot(null); setVersion((value) => value + 1); }}
          onReady={() => { setVersion((value) => value + 1); setView('report'); }} />
      </Suspense></div>
      {view === 'chat' && <Suspense fallback={<Loading label="Loading Chat..." />}>
        <ChatWindow snapshotId={snapshot?.snapshotId ?? null} period={report?.reportMetadata.period ?? null}
          currency={report?.reportMetadata.currency ?? null} subscriptionCount={snapshot ? 1 : 0} onOpenReport={() => setView('report')} />
      </Suspense>}
    </main>
  </div>;
}
