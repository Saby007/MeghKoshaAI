import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, RefreshCw } from 'lucide-react';
import { downloadServiceRetirements, getServiceRetirements, type ServiceRetirementSummary } from '../api';
import type { FullReport } from '../report/models';

export function ServiceRetirements({ report, snapshotId }: { report: FullReport; snapshotId: string | null }) {
  const [subscriptionId, setSubscriptionId] = useState('');
  const [result, setResult] = useState<ServiceRetirementSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const [limit, setLimit] = useState(50);
  useEffect(() => { setResult(null); setError(null); setLoading(false); setLimit(50); return () => pending.current?.abort(); }, [subscriptionId, snapshotId]);

  async function check() {
    if (!snapshotId || loading) return;
    const controller = new AbortController();
    pending.current?.abort(); pending.current = controller;
    setResult(null); setError(null); setLoading(true);
    const timer = globalThis.setTimeout(() => {
      if (!controller.signal.aborted) { controller.abort(); setLoading(false); setError('Retirement lookup timed out. Retry the check.'); }
    }, 20000);
    try { const response = await getServiceRetirements(snapshotId, subscriptionId || undefined, controller.signal); if (!controller.signal.aborted) setResult(response); }
    catch (failure) { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'Retirement data is unavailable.'); }
    finally { globalThis.clearTimeout(timer); if (!controller.signal.aborted) setLoading(false); }
  }

  return <section className="service-retirements required-tag-costs" aria-label="Service retirements">
    <div className="cost-section-heading"><h2>Service retirements</h2><button type="button" className="ghost-button" disabled={!result || exporting} onClick={async () => {
      if (!snapshotId) return;
      setExporting(true); setError(null);
      try { await downloadServiceRetirements(snapshotId, subscriptionId || undefined); }
      catch (failure) { setError(failure instanceof Error ? failure.message : 'Retirement export unavailable.'); }
      finally { setExporting(false); }
    }}><Download size={16} aria-hidden="true" />Download retirements</button></div>
    <div className="billing-filters"><label className="billing-filter"><span>Subscription</span><select aria-label="Retirement subscription" value={subscriptionId} onChange={(event) => setSubscriptionId(event.target.value)}><option value="">All report subscriptions</option>{report.subscriptionBreakdown.map((item) => <option key={item.subscriptionId} value={item.subscriptionId}>{item.subscriptionName}</option>)}</select></label>
      <button type="button" className="ghost-button" disabled={!snapshotId || loading} onClick={() => void check()}><RefreshCw size={16} aria-hidden="true" />{loading ? 'Checking retirements...' : 'Check service retirements'}</button></div>
    {error && <p role="alert">{error}</p>}
    {!snapshotId && <p role="status">A saved report is required for retirement checks.</p>}
    {result && <>
      {result.sources.filter((source) => !source.available).map((source) => <p key={source.subscriptionId} role="status">{source.subscriptionId}: {source.message}</p>)}
      {result.notices.length ? <div className="billing-table-scroll" tabIndex={0} role="region" aria-label="Affected resources and retirement dates"><table className="data-table billing-table"><thead><tr><th>Retiring feature</th><th>Date</th><th>Affected resource</th><th>Matched cost ({result.currency})</th><th>Guidance</th></tr></thead><tbody>{result.notices.slice(0, limit).map((notice) => <tr key={notice.recommendationId}>
        <th>{notice.feature}</th><td>{notice.retirementDate || 'Not returned'}</td><td><a href={`https://portal.azure.com/#resource${encodeURI(notice.resourceId)}`} target="_blank" rel="noreferrer">{notice.resourceName} <ExternalLink size={13} aria-hidden="true" /></a><small>{notice.resourceType}</small></td><td>{notice.matchedCost === null ? 'Unavailable' : notice.matchedCost.toLocaleString(undefined, { maximumFractionDigits: 4 })}</td><td>{notice.guidance}</td>
      </tr>)}</tbody></table></div> : result.sources.every((source) => source.available) && <p role="status">No resource-specific retirements were returned by Azure Advisor for this scope.</p>}
      {result.notices.length > limit && <button type="button" className="ghost-button" onClick={() => setLimit((value) => value + 50)}>Show more retirements</button>}
      <p className="billing-provenance">Advisor checked {result.observedAt}. Matched cost: {result.costPeriod}, FOCUS EffectiveCost. Multiple notices can reference the same resource; costs are not additive.</p>
    </>}
    <p className="billing-provenance">Resource-specific Azure Advisor evidence, not an exhaustive retirement catalog. Upgrades without a retirement date/feature are not assumed to be retirements.</p>
  </section>;
}