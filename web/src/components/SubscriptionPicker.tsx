import { useEffect, useState } from 'react';
import { Check, CheckCheck, ChevronDown, LoaderCircle, Play, RefreshCw, X } from 'lucide-react';
import type { StaleDays } from '../collectors/costAssessment';

export type Subscription = { subscriptionId: string; displayName: string; state?: string };

type SubscriptionPickerProps = {
  subscriptions: Subscription[];
  selectedIds: Set<string>;
  loading: boolean;
  running: boolean;
  runningLabel: string;
  hasReport: boolean;
  periodLabel: string | null;
  scopeChanged: boolean;
  staleDays: StaleDays;
  error: string | null;
  onToggle: (subscriptionId: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
  onStaleDaysChange: (days: StaleDays) => void;
  onRun: () => void;
};

export function SubscriptionPicker({
  subscriptions,
  selectedIds,
  loading,
  running,
  runningLabel,
  hasReport,
  periodLabel,
  scopeChanged,
  staleDays,
  error,
  onToggle,
  onSelectAll,
  onClearAll,
  onStaleDaysChange,
  onRun,
}: SubscriptionPickerProps) {
  const [expanded, setExpanded] = useState(!hasReport);

  useEffect(() => {
    if (hasReport && !scopeChanged) setExpanded(false);
  }, [hasReport, scopeChanged]);

  const allSelected = subscriptions.length > 0 && selectedIds.size === subscriptions.length;
  const selectedNames = subscriptions.filter((item) => selectedIds.has(item.subscriptionId));
  const scopeSummary = selectedNames.length === 0
    ? 'No subscriptions selected'
    : allSelected
      ? 'All available subscriptions'
      : selectedNames.slice(0, 2).map((item) => item.displayName).join(' · ');

  return (
    <section
      className={`scope-ribbon ${expanded ? 'is-open' : ''} ${running || loading ? 'is-running' : ''} ${error ? 'has-error' : ''}`}
      aria-busy={running || loading}
    >
      {(running || loading) && <div className="scope-progress" />}
      <div className="scope-ribbon-main">
        <button
          type="button"
          className="scope-disclosure"
          onClick={() => setExpanded((current) => !current)}
          aria-expanded={expanded}
          aria-controls="subscription-scope-panel"
        >
          <span className="scope-icon"><ChevronDown size={18} /></span>
          <span className="scope-copy" aria-live="polite">
            <span className="scope-label">Report scope</span>
            <span className="scope-value">
              {loading ? 'Discovering subscriptions' : `${selectedIds.size} of ${subscriptions.length} selected`}
              {scopeChanged && <i>Modified</i>}
            </span>
            <span className="scope-summary">{loading ? 'Connecting to Azure Resource Manager' : scopeSummary}</span>
          </span>
        </button>

        <div className="scope-run-area">
          {periodLabel && <span className="scope-period">{periodLabel}</span>}
          <span className="scope-status">
            <i />
            {error ? 'Retry required' : loading ? 'Discovering subscriptions' : running ? runningLabel : hasReport && !scopeChanged ? 'Report current' : 'Ready to run'}
          </span>
          <button
            type="button"
            className="scope-run-button"
            disabled={selectedIds.size === 0 || running || loading}
            onClick={onRun}
          >
            {running || loading ? <LoaderCircle className="spin" size={17} /> : hasReport ? <RefreshCw size={17} /> : <Play size={17} />}
            {running || loading ? 'Please wait' : hasReport ? 'Update report' : 'Run report'}
          </button>
        </div>
      </div>

      {error && <p className="scope-error" role="alert">{error}</p>}
      <div id="subscription-scope-panel" className="scope-panel" hidden={!expanded}>
        <div className="scope-panel-inner">
          <div className="scope-panel-header">
            <span>Azure subscriptions</span>
            <div className="scope-panel-controls">
              <label className="scope-stale-control">
                <span>Stale threshold</span>
                <select
                  value={staleDays}
                  disabled={running}
                  onChange={(event) => onStaleDaysChange(Number(event.target.value) as StaleDays)}
                >
                  {[7, 14, 30, 60, 90, 180, 365].map((days) => (
                    <option value={days} key={days}>{days} days</option>
                  ))}
                </select>
              </label>
              <div className="scope-bulk-actions">
                <button type="button" onClick={onSelectAll} disabled={loading || running || allSelected}>
                  <CheckCheck size={15} /> Select all
                </button>
                <button type="button" onClick={onClearAll} disabled={loading || running || selectedIds.size === 0}>
                  <X size={15} /> Clear all
                </button>
              </div>
            </div>
          </div>

          {loading ? (
            <div className="subscription-skeleton" aria-label="Loading subscriptions">
              {[0, 1, 2, 3, 4].map((item) => <i key={item} style={{ animationDelay: `${item * 90}ms` }} />)}
            </div>
          ) : subscriptions.length === 0 ? (
            <p className="empty-state" role="status">No accessible subscriptions returned.</p>
          ) : (
            <ul className="subscription-grid">
              {subscriptions.map((subscription, index) => {
                const selected = selectedIds.has(subscription.subscriptionId);
                return (
                  <li key={subscription.subscriptionId} className={selected ? 'selected' : ''} style={{ animationDelay: `${index * 45}ms` }}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={running}
                        onChange={() => onToggle(subscription.subscriptionId)}
                      />
                      <span className="subscription-check">{selected && <Check size={14} />}</span>
                      <span>
                        <strong>{subscription.displayName}</strong>
                        <small>{subscription.subscriptionId}</small>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}

        </div>
      </div>
    </section>
  );
}
