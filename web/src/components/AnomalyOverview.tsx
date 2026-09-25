import { useEffect, useState } from 'react';
import { ArrowDownRight, ArrowRight, ArrowUpRight, ExternalLink, RefreshCw, TriangleAlert } from 'lucide-react';
import { getCostAnomalies } from '../api';
import type { AnomalySummary, FullReport } from '../report/models';

type AnomalyScope = Pick<FullReport, 'subscriptionBreakdown' | 'reportMetadata'>;
export type AnomalyState = { result: AnomalySummary | null; error: string | null; loading: boolean; refresh: () => void };

export function useAnomalySummary(report: AnomalyScope): AnomalyState {
  const [result, setResult] = useState<AnomalySummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const scopeKey = JSON.stringify([...new Set(report.subscriptionBreakdown.map((item) => item.subscriptionId.toLowerCase()))].sort());
  const currency = report.reportMetadata.currency.toUpperCase();
  const reportVersion = report.reportMetadata.generatedAt;
  useEffect(() => {
    const controller = new AbortController();
    const subscriptionIds: string[] = JSON.parse(scopeKey);
    setResult(null);
    setError(null);
    setLoading(true);
    if (!subscriptionIds.length) {
      setLoading(false);
      setError('No report subscriptions are available.');
      return () => controller.abort();
    }
    const timer = window.setTimeout(() => {
      controller.abort();
      setError('Anomaly history loading timed out. Retry detection.');
      setLoading(false);
    }, 60000);
    getCostAnomalies(subscriptionIds, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return;
        if (response.currency.toUpperCase() !== currency) throw new Error('Anomaly currency does not match the selected report.');
        setResult(response);
      })
      .catch((requestError) => {
        if (!controller.signal.aborted) setError(requestError instanceof Error ? requestError.message : 'Cost anomaly detection is unavailable.');
      })
      .finally(() => { window.clearTimeout(timer); if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [scopeKey, currency, reportVersion, refreshVersion]);
  return { result, error, loading, refresh: () => setRefreshVersion((version) => version + 1) };
}

export function AnomalyOverview({ state, onOpenDetails, formatMoney }: {
  state: AnomalyState; onOpenDetails: () => void; formatMoney: (value: number) => string;
}) {
  const { result, error, loading, refresh } = state;
  const ready = !loading && !error && result?.status === 'ready';
  const severityOrder = { High: 0, Medium: 1, Low: 2 };
  const signals = ready ? result.anomalies.slice().sort((first, second) => severityOrder[first.severity] - severityOrder[second.severity] || Math.abs(second.absoluteDelta) - Math.abs(first.absoluteDelta)).slice(0, 3) : [];
  const spikeImpact = ready ? result.anomalies.filter((signal) => signal.dimensionType === 'subscription').reduce((sum, signal) => sum + Math.max(0, signal.absoluteDelta), 0) : null;
  return (
    <section className="anomaly-overview" aria-label="Cost anomaly overview" aria-busy={loading}>
      <header>
        <h3><TriangleAlert size={17} aria-hidden="true" /> Cost anomalies</h3>
        <button type="button" className="ghost-button" onClick={onOpenDetails}>View all <ArrowRight size={14} aria-hidden="true" /></button>
      </header>
      <div className="anomaly-overview-content">
      {loading && <div className="anomaly-loading">
        <p role="status">Checking latest complete FOCUS history</p>
        <div className="ui-skeleton" aria-hidden="true" />
        <div className="ui-skeleton" aria-hidden="true" />
      </div>}
      {!loading && error && <div className="anomaly-overview-error"><p role="alert">{error}</p><button type="button" className="ghost-button" onClick={refresh} aria-label="Retry anomaly detection" title="Retry anomaly detection"><RefreshCw size={15} /></button></div>}
      {!loading && !error && result?.status === 'insufficient_history' && <p role="status">Insufficient complete history: {result.completeDays}/{result.requiredDays} days. {result.statusMessage}</p>}
      {ready && (
        <>
          <div className="anomaly-overview-metrics">
            <span><strong>{result.anomalies.length}</strong> signal{result.anomalies.length === 1 ? '' : 's'}</span>
            <span><strong>{result.anomalies.filter((signal) => signal.severity === 'High').length}</strong> high severity</span>
            <span>Subscription spike impact <strong>{formatMoney(spikeImpact!)}</strong></span>
          </div>
          {signals.length ? <ul className="anomaly-overview-signals">{signals.map((signal) => (
            <li key={signal.anomalyId}>
              <span className={`anomaly-severity ${signal.severity.toLowerCase()}`}>{signal.severity}</span>
              <button type="button" onClick={onOpenDetails} title={`${signal.dimensionName} · ${signal.dimensionType}`}><span>{signal.dimensionName}</span><small>{signal.date} · {signal.dimensionType.replace('_', ' ')}</small></button>
              <strong className={signal.absoluteDelta > 0 ? 'cost-increase' : 'cost-decrease'}>{signal.absoluteDelta > 0 ? '+' : ''}{formatMoney(signal.absoluteDelta)}</strong>
            </li>
          ))}</ul> : <p role="status">No anomalies crossed the configured thresholds.</p>}
          <small className="anomaly-overview-provenance">Latest complete history: {result.historyStart} - {result.historyEnd} · {result.algorithmVersion} · application detector</small>
        </>
      )}
      </div>
    </section>
  );
}

export function AICostAlerts({ state, formatMoney, displayCurrency }: {
  state: AnomalyState; formatMoney: (value: number) => string; displayCurrency: string;
}) {
  const { result, error, loading, refresh } = state;
  const [type, setType] = useState('all');
  const [visibleCount, setVisibleCount] = useState(12);
  const available = !loading && !error && result?.status === 'ready' && Array.isArray(result.aiAnomalies);
  const signals = available ? result.aiAnomalies!.filter((signal) => type === 'all' || signal.anomalyType === type) : [];
  useEffect(() => { setType('all'); setVisibleCount(12); }, [result]);
  return (
    <section className="ai-cost-alerts" aria-label="AI billing anomaly alerts" aria-busy={loading}>
      <header className="billing-heading">
        <div><TriangleAlert size={18} aria-hidden="true" /><h2>AI cost alerts</h2></div>
        <button type="button" className="ghost-button" aria-label="Refresh AI cost alerts" title="Refresh AI cost alerts" onClick={refresh} disabled={loading}><RefreshCw size={16} className={loading ? 'spin' : undefined} aria-hidden="true" /></button>
      </header>
      {loading && <p role="status">Checking AI billing history...</p>}
      {!loading && error && <p className="ai-alert-unavailable" role="alert">{error}</p>}
      {!loading && !error && result?.status === 'insufficient_history' && <p role="status">AI billing alerts need {result.requiredDays} complete history days; {result.completeDays} available. {result.statusMessage}</p>}
      {!loading && !error && result?.status === 'ready' && !Array.isArray(result.aiAnomalies) && <p role="status">AI billing alerts are unavailable in this response. Refresh after the API update.</p>}
      {available && <>
        <div className="billing-filters">
          <label className="billing-filter"><span>Signal type</span><select aria-label="AI anomaly type" value={type} onChange={(event) => { setType(event.target.value); setVisibleCount(12); }}><option value="all">All signals</option><option value="spike">Cost spikes</option><option value="drop">Cost drops</option><option value="new_resource">New cost</option></select></label>
          <span className="ai-alert-period">{result.historyStart} - {result.historyEnd} UTC · {displayCurrency}</span>
        </div>
        {signals.length === 0 ? <p role="status">{type === 'all' ? 'No AI billing anomalies crossed the detection thresholds.' : 'No AI signals match this filter.'}</p> : <div className="ai-alert-list">
          {signals.slice(0, visibleCount).map((signal) => {
            const drop = signal.anomalyType === 'drop';
            const Icon = drop ? ArrowDownRight : ArrowUpRight;
            const label = drop ? 'Cost drop' : signal.anomalyType === 'new_resource' ? 'New cost' : 'Cost spike';
            return <article key={signal.anomalyId} className={`ai-cost-signal ${drop ? 'is-drop' : 'is-spike'}`} aria-label={`${label}: ${signal.dimensionName}`}>
              <header><span className="ai-signal-direction"><Icon size={18} aria-hidden="true" />{label}</span><strong>{signal.dimensionName}</strong><span>{signal.severity} severity</span></header>
              <dl className="ai-signal-metrics">
                <div><dt>Actual cost</dt><dd>{formatMoney(signal.actualCost)}</dd></div>
                <div><dt>Expected cost</dt><dd>{formatMoney(signal.expectedCost)}</dd></div>
                <div><dt>Change</dt><dd className={`ai-signal-change ${signal.absoluteDelta === 0 ? '' : signal.absoluteDelta > 0 ? 'cost-increase' : 'cost-decrease'}`.trim()}>{signal.absoluteDelta > 0 ? '+' : ''}{formatMoney(signal.absoluteDelta)}<small>{signal.percentageDelta === null ? 'No prior baseline' : `${signal.percentageDelta > 0 ? '+' : ''}${(signal.percentageDelta * 100).toFixed(1)}%`}</small></dd></div>
              </dl>
              <details><summary>Detection details</summary>
                <dl className="ai-signal-details"><div><dt>Subscription</dt><dd>{signal.subscriptionName || signal.subscriptionId}</dd></div><div><dt>Dimension</dt><dd>{signal.dimensionType} · {signal.dimensionId}</dd></div><div><dt>Incident (UTC)</dt><dd>{signal.firstDetectedDate} - {signal.lastDetectedDate} · {signal.durationDays} day(s)</dd></div><div><dt>Representative date</dt><dd>{signal.date}</dd></div><div><dt>Expected range</dt><dd>{formatMoney(signal.expectedLower)} - {formatMoney(signal.expectedUpper)}</dd></div><div><dt>Baseline</dt><dd>{signal.baselineSamples} same-weekday samples</dd></div></dl>
                {signal.contributors.length > 0 && <ul className="ai-alert-contributors">{signal.contributors.map((contributor) => <li key={`${contributor.resourceId}:${contributor.name}`}><span>{contributor.name}</span><strong>{formatMoney(contributor.cost)}</strong></li>)}</ul>}
                <a href={signal.investigationUrl} target="_blank" rel="noreferrer">Investigate in Azure <ExternalLink size={13} aria-hidden="true" /></a>
              </details>
            </article>;
          })}
        </div>}
        {signals.length > visibleCount && <button type="button" className="ghost-button" onClick={() => setVisibleCount((count) => count + 12)}>Show more signals</button>}
        <p className="billing-provenance">FOCUS EffectiveCost · {result.algorithmVersion} · checked {result.generatedAt}. Service and resource signals can overlap and are not additive. A cost drop is not a verified saving.</p>
      </>}
    </section>
  );
}