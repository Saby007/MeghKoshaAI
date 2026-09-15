import { Component, Suspense, lazy, useEffect, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { ArrowRight, BarChart3, CalendarClock, Check, LoaderCircle, Maximize2, MessageSquareText, Moon, RefreshCw, Rows3, ShieldCheck, Sun } from 'lucide-react';
import { SubscriptionPicker, type Subscription } from './components/SubscriptionPicker';
import { runCostAssessment, type StaleDays } from './collectors/costAssessment';
import { getLatestReport, narrate, type CostAgentOutput } from './api';
import { apiFetch, ApiIdentityRequiredError, initializeApiIdentity, redirectApiIdentity, signOutApiIdentity, IDENTITY_REQUIRED_EVENT, type VerifiedIdentity } from './apiIdentity';
import type { FullReport, ReportSnapshot } from './report/models';
import { BRAND_NAME } from './brand';

const ReportView = lazy(() => import('./components/ReportView').then((module) => ({ default: module.ReportView })));
const ChatWindow = lazy(() => import('./components/ChatWindow').then((module) => ({ default: module.ChatWindow })));
const ScheduleManager = lazy(() => import('./components/ScheduleManager').then((module) => ({ default: module.ScheduleManager })));

// Prevents a single bad field/render error anywhere in the (large) report tree from
// unmounting the whole app to a blank screen - shows a reload prompt instead. This
// covers cases like an open browser tab still running an older bundle against a
// freshly-deployed backend that renamed/removed a field it expects.
class ReportErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Report view crashed', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="report-crash-banner" role="alert">
          <strong>Something went wrong showing this report.</strong>
          <p>This usually clears up with a reload - the app may have updated while this page was open.</p>
          <button type="button" onClick={() => window.location.reload()}>Reload</button>
        </div>
      );
    }
    return this.props.children;
  }
}

type ClientPrincipal = VerifiedIdentity | null;
type AssessmentPhase = 'collecting' | 'narrating' | null;
type WorkspaceView = 'report' | 'chat' | 'schedules';
const staleDayOptions = [7, 14, 30, 60, 90, 180, 365];
const scopePreferenceKey = (identity: VerifiedIdentity) => `mkai-report-scope:${identity.tenantId}:${identity.userId}`;

function rememberScope(identity: VerifiedIdentity, subscriptionIds: string[], staleDays: StaleDays) {
  try {
    window.localStorage.setItem(scopePreferenceKey(identity), JSON.stringify({ subscriptionIds, staleDays }));
  } catch {}
}

function WorkspaceLoading({ label }: { label: string }) {
  return (
    <section className="workspace-skeleton" role="status" aria-busy="true" aria-live="polite">
      <span className="loading-state"><LoaderCircle className="spin" size={18} aria-hidden="true" /> Loading {label}...</span>
      <div className="workspace-skeleton-grid" aria-hidden="true"><i /><i /><i /></div>
    </section>
  );
}

function AssessmentProgress({ phase, updating }: { phase: Exclude<AssessmentPhase, null>; updating: boolean }) {
  const narrating = phase === 'narrating';
  return (
    <section className={`assessment-progress ${updating ? 'is-update' : ''}`} role="status" aria-live="polite">
      <span className="assessment-progress-icon" aria-hidden="true"><LoaderCircle className="spin" size={22} /></span>
      <span className="assessment-progress-copy">
        <small>{updating ? 'Updating assessment' : 'Running assessment'}</small>
        <strong>{narrating ? 'Generating report narrative' : 'Collecting Azure data'}</strong>
        <span>
          {narrating
            ? 'The verified report is ready. Please wait while the narrative is prepared.'
            : 'Please wait while cost, resource, Advisor, and governance evidence is reconciled.'}
        </span>
      </span>
      <ol className="assessment-progress-steps" aria-label="Assessment progress">
        <li className={narrating ? 'complete' : 'active'}><b>01</b> Collect</li>
        <li className={narrating ? 'active' : ''}><b>02</b> Narrate</li>
      </ol>
    </section>
  );
}

function BrandLockup({ onLight = false }: { onLight?: boolean }) {
  return (
    <span className={`brand-lockup ${onLight ? 'on-light' : ''}`} aria-label={BRAND_NAME}>
      <span className="brand-dollar-mark" aria-hidden="true">
        <i>$</i>
        <i>$</i>
        <i>$</i>
        <i>$</i>
      </span>
      <span className="brand-divider" aria-hidden="true" />
      <span className="brand-product">{BRAND_NAME}</span>
    </span>
  );
}

type Theme = 'dark' | 'light';
const THEME_STORAGE_KEY = 'mkai-theme';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === 'light' ? 'light' : 'dark';
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  return [theme, () => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))];
}

function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const label = `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`;
  return (
    <button type="button" className="theme-toggle" onClick={onToggle} title={label} aria-label={label}>
      {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
    </button>
  );
}

type SignInStep = 'email' | 'redirecting';

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

function SignInScreen({ theme, onToggleTheme, notice }: { theme: Theme; onToggleTheme: () => void; notice?: string | null }) {
  const [step, setStep] = useState<SignInStep>('email');
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function handleContinue(event: React.FormEvent) {
    event.preventDefault();
    if (!isValidEmail(email)) {
      setError('Enter a valid email address to continue.');
      return;
    }
    setError(null);
    setStep('redirecting');
    try {
      await redirectApiIdentity(email.trim());
    } catch (failure) {
      setStep('email');
      setError(failure instanceof Error ? failure.message : 'Microsoft sign-in could not start. Retry the connection.');
    }
  }

  return (
    <main className="signin-screen signin-production" aria-labelledby="signin-heading">
      <ThemeToggle theme={theme} onToggle={onToggleTheme} />
      <section className="signin-showcase">
        <BrandLockup />
        <p className="signin-tagline">See. Assess. Save.</p>
        <p className="signin-headline">Total clarity for your <span>Azure spend</span>.</p>
        <ul className="signin-feature-list">
          <li><BarChart3 size={18} aria-hidden="true" /><div><strong>Azure cost assessment</strong><small>Costs, anomalies, and evidence-backed opportunities.</small></div></li>
          <li><ShieldCheck size={18} aria-hidden="true" /><div><strong>Access scoped to you</strong><small>Subscriptions in your organization's tenant, verified against your Azure roles.</small></div></li>
          <li><CalendarClock size={18} aria-hidden="true" /><div><strong>Explicit control changes</strong><small>Resource assessment is read-only. Budget and export changes require authorized operators.</small></div></li>
        </ul>
      </section>
      <section className="signin-panel">
        <div className="signin-card-v2">
          <h1 id="signin-heading">Sign in</h1>
          {notice && <p role="alert" className="signin-error">{notice}</p>}
          {step === 'email' && (
            <form onSubmit={handleContinue} noValidate>
              <label className="signin-field">
                <span>Email address</span>
                <input
                  type="email"
                  autoComplete="username"
                  autoFocus
                  required
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value);
                    if (error) setError(null);
                  }}
                  placeholder="you@company.com"
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? 'signin-error' : undefined}
                />
              </label>
              {error && <p id="signin-error" className="signin-error" role="alert">{error}</p>}
              <button type="submit" className="dark-button signin-continue">Continue <ArrowRight size={16} aria-hidden="true" /></button>
            </form>
          )}
          {step !== 'email' && (
            <div className="signin-detecting" role="status" aria-live="polite">
              <LoaderCircle className="spin" size={20} aria-hidden="true" />
              <p>Redirecting to Microsoft Entra ID...</p>
            </div>
          )}
          <p className="signin-card-footnote">Microsoft Entra ID · Your organization's access policy</p>
        </div>
      </section>
    </main>
  );
}

export default function App() {
  const [theme, toggleTheme] = useTheme();
  const [density, setDensity] = useState<'compact' | 'comfortable'>(() => {
    try { return window.localStorage.getItem('mkai-density') === 'comfortable' ? 'comfortable' : 'compact'; } catch { return 'compact'; }
  });
  useEffect(() => {
    document.documentElement.dataset.density = density;
    try { window.localStorage.setItem('mkai-density', density); } catch {}
  }, [density]);
  const [principal, setPrincipal] = useState<ClientPrincipal>(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authVersion, setAuthVersion] = useState(0);
  const [verifiedIdentity, setVerifiedIdentity] = useState<VerifiedIdentity | null>(null);
  const [identityConnecting, setIdentityConnecting] = useState(false);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [report, setReport] = useState<FullReport | null>(null);
  const [loadedSnapshot, setLoadedSnapshot] = useState<ReportSnapshot | null>(null);
  const [narration, setNarration] = useState<CostAgentOutput>(null);
  const [running, setRunning] = useState(false);
  const [assessmentPhase, setAssessmentPhase] = useState<AssessmentPhase>(null);
  const [error, setError] = useState<string | null>(null);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [staleDays, setStaleDays] = useState<StaleDays>(90);
  const [loadingSubscriptions, setLoadingSubscriptions] = useState(false);
  const [loadingSnapshot, setLoadingSnapshot] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [snapshotMissing, setSnapshotMissing] = useState(false);
  const [autoOpenSaved, setAutoOpenSaved] = useState(() => {
    try { return window.localStorage.getItem('mkai-open-saved-report') !== 'false'; } catch { return true; }
  });
  const autoOpenSavedRef = useRef(autoOpenSaved);
  const [startupVersion, setStartupVersion] = useState(0);
  const pendingStartup = useRef<AbortController | null>(null);
  const workGeneration = useRef(0);
  const [view, setView] = useState<WorkspaceView>('report');
  const [visitedViews, setVisitedViews] = useState<Set<WorkspaceView>>(() => new Set(['report']));

  useEffect(() => {
    const currentAsset = Array.from(document.scripts)
      .map((script) => script.src)
      .find((source) => /\/assets\/index-[^/]+\.js$/.test(source));
    if (!currentAsset) return;

    async function reloadIfUpdated() {
      try {
        const response = await fetch(`/?asset-check=${Date.now()}`, { cache: 'no-store' });
        const html = await response.text();
        const nextAsset = html.match(/src="(\/assets\/index-[^"]+\.js)"/)?.[1];
        if (nextAsset && new URL(nextAsset, window.location.origin).href !== currentAsset) {
          window.location.reload();
        }
      } catch {
        // A transient version check must not interrupt the current workspace.
      }
    }

    function checkOnFocus() {
      if (document.visibilityState === 'visible') void reloadIfUpdated();
    }

    document.addEventListener('visibilitychange', checkOnFocus);
    const interval = window.setInterval(() => void reloadIfUpdated(), 5 * 60 * 1000);
    return () => {
      document.removeEventListener('visibilitychange', checkOnFocus);
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setCheckingAuth(true);
    setAuthError(null);
    const timer = window.setTimeout(() => {
      controller.abort();
      setAuthError('Sign-in status could not be loaded. Retry the connection.');
      setCheckingAuth(false);
    }, 15000);
    initializeApiIdentity('')
      .then((identity) => {
        if (controller.signal.aborted) return;
        setPrincipal(identity);
        setVerifiedIdentity(identity);
        setIdentityError(null);
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        setPrincipal(null);
        setVerifiedIdentity(null);
        if (!(failure instanceof ApiIdentityRequiredError)) setAuthError(failure instanceof Error ? failure.message : 'Sign-in status is unavailable.');
      })
      .finally(() => { window.clearTimeout(timer); if (!controller.signal.aborted) setCheckingAuth(false); });
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [authVersion]);

  useEffect(() => {
    const requireConnection = () => {
      workGeneration.current += 1;
      pendingStartup.current?.abort();
      setPrincipal(null);
      setVerifiedIdentity(null);
      setSubscriptions([]);
      setSelectedIds(new Set());
      setVisitedViews(new Set(['report']));
      setView('report');
      setStaleDays(90);
      setReport(null);
      setLoadedSnapshot(null);
      setNarration(null);
      setAssessmentPhase(null);
      setLoadingSnapshot(false);
      setLoadingSubscriptions(false);
      setRunning(false);
      setIdentityError('Your API session needs verification. Reconnect your Microsoft account.');
    };
    window.addEventListener(IDENTITY_REQUIRED_EVENT, requireConnection);
    return () => window.removeEventListener(IDENTITY_REQUIRED_EVENT, requireConnection);
  }, []);

  useEffect(() => {
    if (!verifiedIdentity || identityError) return;
    const controller = new AbortController();
    pendingStartup.current = controller;
    workGeneration.current += 1;
    let stage: 'subscriptions' | 'snapshot' = 'subscriptions';
    setLoadingSubscriptions(true);
    setLoadingSnapshot(false);
    setSnapshotError(null);
    setSnapshotMissing(false);
    const timer = window.setTimeout(() => {
      if (controller.signal.aborted) return;
      controller.abort();
      setLoadingSubscriptions(false);
      setLoadingSnapshot(false);
      if (stage === 'subscriptions') setError('Subscription loading timed out. Retry the saved report lookup.');
      else setSnapshotError('Saved report loading timed out. Retry or run a new report.');
    }, 25000);
    apiFetch('/api/subscriptions', { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`Subscription loading failed: ${res.status}`);
        return res.json();
      })
      .then(async (items: Subscription[]) => {
        if (controller.signal.aborted) return;
        const availableIds = items.map((item) => item.subscriptionId.toLowerCase());
        let subscriptionIds = availableIds;
        let threshold: StaleDays = 90;
        try {
          const preference = startupVersion > 0
            ? { subscriptionIds: [...selectedIds], staleDays }
            : JSON.parse(window.localStorage.getItem(scopePreferenceKey(verifiedIdentity)) ?? 'null');
          if (preference && Array.isArray(preference.subscriptionIds)) {
            const remembered = new Set(preference.subscriptionIds.filter((value: unknown) => typeof value === 'string').map((value: string) => value.toLowerCase()));
            const allowed = availableIds.filter((value) => remembered.has(value));
            if (allowed.length || startupVersion > 0) subscriptionIds = allowed;
            if (staleDayOptions.includes(preference.staleDays)) threshold = preference.staleDays;
          }
        } catch {}
        setSubscriptions(items);
        setSelectedIds(new Set(subscriptionIds));
        setStaleDays(threshold);
        setLoadingSubscriptions(false);
        setError(null);
        if (!subscriptionIds.length) { setSnapshotMissing(true); return; }
        if (!autoOpenSavedRef.current && startupVersion === 0) return;
        stage = 'snapshot';
        setLoadingSnapshot(true);
        const saved = await getLatestReport(controller.signal, subscriptionIds, threshold);
        if (controller.signal.aborted) return;
        if (!saved) { setSnapshotMissing(true); return; }
        const expected = new Set(subscriptionIds.map((value) => value.toLowerCase()));
        if (saved.staleDays !== threshold || saved.report.reportMetadata.staleDays !== threshold
          || new Set(saved.subscriptionIds.map((value) => value.toLowerCase())).size !== expected.size
          || saved.subscriptionIds.length !== expected.size
          || saved.subscriptionIds.some((value) => !expected.has(value.toLowerCase()))
          || saved.report.subscriptionBreakdown.length !== expected.size
          || saved.report.subscriptionBreakdown.some((item) => !expected.has(item.subscriptionId.toLowerCase()))) {
          throw new Error('The saved report does not match the authorized scope. Run a new report.');
        }
        setReport(saved.report);
        setLoadedSnapshot(saved);
        setStaleDays(saved.staleDays);
        setNarration(null);
        rememberScope(verifiedIdentity, subscriptionIds, threshold);
      })
      .catch((loadError) => {
        if (controller.signal.aborted) return;
        if (stage === 'subscriptions') setError(loadError instanceof Error ? loadError.message : 'Unable to load subscriptions.');
        else setSnapshotError(loadError instanceof Error ? loadError.message : 'Saved report unavailable.');
      })
      .finally(() => {
        window.clearTimeout(timer);
        if (!controller.signal.aborted) { setLoadingSubscriptions(false); setLoadingSnapshot(false); }
        if (pendingStartup.current === controller) pendingStartup.current = null;
      });
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [verifiedIdentity?.userId, verifiedIdentity?.tenantId, identityError, startupVersion]);

  if (checkingAuth) {
    return (
      <div className="center-screen">
        <p className="loading-state">Loading...</p>
      </div>
    );
  }

  if (authError) {
    return <main className="center-screen"><p role="alert">{authError}</p><button type="button" className="ghost-button" onClick={() => setAuthVersion((value) => value + 1)}><RefreshCw size={16} aria-hidden="true" /> Retry sign-in check</button></main>;
  }

  if (!principal) {
    return <SignInScreen theme={theme} onToggleTheme={toggleTheme} notice={identityError} />;
  }

  async function handleConnect() {
    if (identityConnecting || !principal) return;
    setIdentityConnecting(true);
    setIdentityError(null);
    try {
      let identity: VerifiedIdentity;
      try {
        identity = await initializeApiIdentity(principal.userDetails);
      } catch (failure) {
        if (!(failure instanceof ApiIdentityRequiredError)) throw failure;
        await redirectApiIdentity(principal.userDetails);
        return;
      }
      if (verifiedIdentity && (identity.userId !== verifiedIdentity.userId || identity.tenantId !== verifiedIdentity.tenantId)) {
        window.location.reload();
        return;
      }
      setVerifiedIdentity(identity);
    } catch (connectionError) {
      setIdentityError(connectionError instanceof Error ? connectionError.message : 'Microsoft account connection failed.');
    } finally {
      setIdentityConnecting(false);
    }
  }

  async function handleRun() {
    if (running || loadingSubscriptions || selectedIds.size === 0 || !verifiedIdentity || identityError) return;
    pendingStartup.current?.abort();
    setLoadingSnapshot(false);
    setSnapshotError(null);
    setSnapshotMissing(false);
    const generation = ++workGeneration.current;
    setError(null);
    setRunning(true);
    setAssessmentPhase('collecting');
    try {
      const subscriptionIds = [...selectedIds];
      const result = await runCostAssessment(subscriptionIds, staleDays);
      if (generation !== workGeneration.current) return;
      setReport(result);
      setLoadedSnapshot(null);
      rememberScope(verifiedIdentity, subscriptionIds.map((value) => value.toLowerCase()), staleDays);
      try {
        const persisted = await getLatestReport(undefined, subscriptionIds, staleDays);
        if (generation !== workGeneration.current) return;
        if (
          persisted
          && persisted.subscriptionIds.length === subscriptionIds.length
          && subscriptionIds.every((subscriptionId) => persisted.subscriptionIds.includes(subscriptionId.toLowerCase()))
          && persisted.staleDays === staleDays
        ) {
          setLoadedSnapshot(persisted);
          setReport(persisted.report);
        } else {
          setError('Report generated, but the saved copy could not be confirmed. "Open saved report" and Chat may be unavailable until you run it again.');
        }
      } catch (snapshotError) {
        if (generation !== workGeneration.current) return;
        setError(snapshotError instanceof Error
          ? `Report generated, but persisted exports are unavailable: ${snapshotError.message}`
          : 'Report generated, but persisted exports are unavailable.');
      }
      setNarration(null);
      if (verifiedIdentity.features?.aiNarration !== true) return;
      setAssessmentPhase('narrating');
      try {
        const narrative = await narrate(subscriptionIds, result);
        if (generation !== workGeneration.current) return;
        setNarration(narrative);
      } catch (narrationError) {
        if (generation !== workGeneration.current) return;
        setError(narrationError instanceof Error
          ? `Report generated, but narration failed: ${narrationError.message}`
          : 'Report generated, but narration is unavailable.');
      }
    } catch (runError) {
      if (generation !== workGeneration.current) return;
      setError(runError instanceof Error ? runError.message : 'Assessment failed. Please retry.');
    } finally {
      if (generation === workGeneration.current) { setAssessmentPhase(null); setRunning(false); }
    }
  }

  function toggleSubscription(subscriptionId: string) {
    pendingStartup.current?.abort();
    setLoadingSnapshot(false);
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(subscriptionId)) next.delete(subscriptionId);
      else next.add(subscriptionId);
      return next;
    });
  }

  function changeView(nextView: WorkspaceView) {
    setView(nextView);
    setVisitedViews((current) => current.has(nextView) ? current : new Set([...current, nextView]));
  }

  const reportIds = new Set(report?.subscriptionBreakdown.map((row) => row.subscriptionId) ?? []);
  const scopeChanged = report !== null && (
    selectedIds.size !== reportIds.size
    || [...selectedIds].some((id) => !reportIds.has(id))
    || staleDays !== report.reportMetadata.staleDays
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace-main">Skip to workspace</a>
      <header className="app-header">
        <BrandLockup />
        <nav className="workspace-nav" aria-label="Workspace">
          <button className={view === 'report' ? 'active' : ''} aria-current={view === 'report' ? 'page' : undefined} type="button" onClick={() => changeView('report')}>
            <BarChart3 size={15} /> Report
          </button>
          <button className={view === 'chat' ? 'active' : ''} aria-current={view === 'chat' ? 'page' : undefined} type="button" onClick={() => changeView('chat')}>
            <MessageSquareText size={15} /> Chat
          </button>
          <button className={view === 'schedules' ? 'active' : ''} aria-current={view === 'schedules' ? 'page' : undefined} type="button" onClick={() => changeView('schedules')}>
            <CalendarClock size={15} /> Schedules
          </button>
        </nav>
        <span className="account-chip">
          <button type="button" className="theme-toggle" title={`Switch to ${density === 'compact' ? 'comfortable' : 'compact'} layout`} aria-label={`Switch to ${density === 'compact' ? 'comfortable' : 'compact'} layout`} onClick={() => setDensity((value) => value === 'compact' ? 'comfortable' : 'compact')}>
            {density === 'compact' ? <Maximize2 size={15} aria-hidden="true" /> : <Rows3 size={15} aria-hidden="true" />}
          </button>
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <span className="account-name">{verifiedIdentity?.userDetails ?? principal.userDetails}</span>
          <button type="button" className="ghost-button" onClick={() => {
            window.dispatchEvent(new Event(IDENTITY_REQUIRED_EVENT));
            void signOutApiIdentity().catch(() => setAuthError('Sign-out failed. Please retry the connection.'));
          }}>
            Sign out
          </button>
        </span>
      </header>
      {(!verifiedIdentity || identityError) && (
        <section className="api-identity-status" aria-label="Microsoft API connection">
          <ShieldCheck size={18} aria-hidden="true" />
          <span role={identityError ? 'alert' : 'status'}>{identityConnecting ? 'Verifying Microsoft account' : identityError || 'Connect your Microsoft account to verify access.'}</span>
          <button type="button" className="ghost-button" disabled={identityConnecting} onClick={() => void handleConnect()}>
            {identityConnecting ? <LoaderCircle className="spin" size={15} aria-hidden="true" /> : <ShieldCheck size={15} aria-hidden="true" />}
            Connect Microsoft account
          </button>
        </section>
      )}
      {verifiedIdentity && (
      <main id="workspace-main" tabIndex={-1}>
        <div className="view-panel" hidden={view !== 'report'}>
          <SubscriptionPicker
            subscriptions={subscriptions}
            selectedIds={selectedIds}
            loading={loadingSubscriptions}
            running={running}
            runningLabel={assessmentPhase === 'narrating' ? 'Generating narrative' : 'Collecting Azure data'}
            hasReport={report !== null}
            periodLabel={report ? `${report.reportMetadata.periodStart} \u2013 ${report.reportMetadata.periodEnd}` : null}
            scopeChanged={scopeChanged}
            staleDays={staleDays}
            error={error}
            onToggle={toggleSubscription}
            onSelectAll={() => { pendingStartup.current?.abort(); setLoadingSnapshot(false); setSelectedIds(new Set(subscriptions.map((item) => item.subscriptionId))); }}
            onClearAll={() => { pendingStartup.current?.abort(); setLoadingSnapshot(false); setSelectedIds(new Set()); }}
            onStaleDaysChange={(value) => { pendingStartup.current?.abort(); setLoadingSnapshot(false); setStaleDays(value); }}
            onRun={handleRun}
          />
          <div className="saved-report-controls">
            <label><input type="checkbox" checked={autoOpenSaved} onChange={(event) => {
              const enabled = event.target.checked;
              setAutoOpenSaved(enabled); autoOpenSavedRef.current = enabled;
              try { window.localStorage.setItem('mkai-open-saved-report', String(enabled)); } catch {}
              if (!enabled && loadingSnapshot) { pendingStartup.current?.abort(); setLoadingSnapshot(false); }
            }} /> Open saved report automatically</label>
            <button type="button" className="ghost-button" disabled={loadingSubscriptions || loadingSnapshot || running || !!identityError} onClick={() => setStartupVersion((value) => value + 1)}><RefreshCw size={15} aria-hidden="true" /> Open saved report</button>
          </div>
          {loadingSnapshot && <section className="saved-report-status"><span role="status"><LoaderCircle className="spin" size={18} aria-hidden="true" /> Opening saved report...</span><button type="button" className="ghost-button" onClick={() => { pendingStartup.current?.abort(); setLoadingSnapshot(false); }}>Skip saved report</button></section>}
          {(snapshotError || (error && !report)) && <section className="saved-report-status">
            {snapshotError && <p role="alert">{snapshotError}</p>}
            <button type="button" className="ghost-button" onClick={() => setStartupVersion((value) => value + 1)} disabled={loadingSubscriptions || loadingSnapshot || running}><RefreshCw size={16} aria-hidden="true" /> Retry saved report</button>
          </section>}
          {running && assessmentPhase && <AssessmentProgress phase={assessmentPhase} updating={report !== null} />}
          {!report && !running && !loadingSubscriptions && !loadingSnapshot && !error && !snapshotError && (
            <section className="report-awaiting" aria-labelledby="report-ready-heading">
              <BarChart3 size={28} aria-hidden="true" />
              <h2 id="report-ready-heading">{selectedIds.size ? snapshotMissing ? 'No saved report for this scope' : 'Report workspace ready' : 'No subscriptions selected'}</h2>
              <p>{selectedIds.size} subscription{selectedIds.size === 1 ? '' : 's'} selected</p>
              <ol className="landing-steps" aria-label="Getting started">
                <li className={selectedIds.size > 0 ? 'is-done' : 'is-current'}>
                  <span className="landing-step-index">{selectedIds.size > 0 ? <Check size={14} aria-hidden="true" /> : '1'}</span>
                  <span className="landing-step-copy"><strong>Select subscriptions</strong><small>Choose which Azure subscriptions to include, above.</small></span>
                </li>
                <li className={selectedIds.size > 0 ? 'is-current' : 'is-pending'}>
                  <span className="landing-step-index">2</span>
                  <span className="landing-step-copy"><strong>Run your report</strong><small>Collects cost, savings and compliance findings for the selected scope.</small></span>
                </li>
                <li className="is-pending">
                  <span className="landing-step-index">3</span>
                  <span className="landing-step-copy"><strong>Explore Chat and Schedules</strong><small>Ask questions about the report or set up automatic six-month FOCUS exports.</small></span>
                </li>
              </ol>
              {selectedIds.size > 0 && <button type="button" className="dark-button" disabled={!!identityError} onClick={() => void handleRun()}><BarChart3 size={16} aria-hidden="true" /> Run first report</button>}
            </section>
          )}
          {report && (
            <ReportErrorBoundary>
              <Suspense fallback={<WorkspaceLoading label="report" />}>
                <ReportView report={report} narration={narration} snapshotId={loadedSnapshot?.snapshotId ?? null} snapshotCreatedAt={loadedSnapshot?.createdAt} />
              </Suspense>
            </ReportErrorBoundary>
          )}
        </div>
        {visitedViews.has('chat') && (
          <div className="view-panel" hidden={view !== 'chat'}>
            <Suspense fallback={<WorkspaceLoading label="chat" />}>
              <ChatWindow
                snapshotId={loadedSnapshot?.snapshotId ?? null}
                period={report?.reportMetadata.period ?? null}
                currency={report?.reportMetadata.currency ?? null}
                subscriptionCount={loadedSnapshot?.subscriptionIds.length ?? 0}
                onOpenReport={() => changeView('report')}
              />
            </Suspense>
          </div>
        )}
        {visitedViews.has('schedules') && (
          <div className="view-panel" hidden={view !== 'schedules'}>
            <Suspense fallback={<WorkspaceLoading label="schedules" />}><ScheduleManager /></Suspense>
          </div>
        )}
      </main>
      )}
    </div>
  );
}
