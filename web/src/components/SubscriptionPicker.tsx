import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Check, CheckCheck, ChevronDown, LoaderCircle, Play, RefreshCw, Search, X } from 'lucide-react';

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
  error: string | null;
  onToggle: (subscriptionId: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
  onRun: () => void;
  /* Shown in the same row, between the subscription picker and the run controls. */
  children?: ReactNode;
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
  error,
  onToggle,
  onSelectAll,
  onClearAll,
  onRun,
  children,
}: SubscriptionPickerProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const selectRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (running) setMenuOpen(false);
  }, [running]);

  useEffect(() => {
    if (!menuOpen) {
      setFilter('');
      return;
    }
    const onPointerDown = (event: MouseEvent) => {
      if (selectRef.current && !selectRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [menuOpen]);

  const allSelected = subscriptions.length > 0 && selectedIds.size === subscriptions.length;
  const selectedNames = subscriptions.filter((item) => selectedIds.has(item.subscriptionId));

  const visibleSubscriptions = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return subscriptions;
    return subscriptions.filter((item) =>
      item.displayName.toLowerCase().includes(needle) || item.subscriptionId.toLowerCase().includes(needle));
  }, [subscriptions, filter]);

  const triggerLabel = selectedIds.size === 0
    ? 'Select subscriptions'
    : allSelected
      ? 'All subscriptions'
      : selectedNames.map((item) => item.displayName).join(', ');

  return (
    <section
      className={`scope-ribbon ${menuOpen ? 'has-select-open' : ''} ${running || loading ? 'is-running' : ''} ${error ? 'has-error' : ''}`}
      aria-busy={running || loading}
    >
      {(running || loading) && <div className="scope-progress" />}
      <div className="scope-ribbon-main">
        <span className="scope-panel-title">Azure subscriptions</span>

        {loading ? (
          <div className="subscription-skeleton" aria-label="Loading subscriptions">
            <i />
          </div>
        ) : subscriptions.length === 0 ? (
          <p className="empty-state" role="status">No accessible subscriptions returned.</p>
        ) : (
          <div className={`subscription-select ${menuOpen ? 'is-open' : ''}`} ref={selectRef}>
            <button
              type="button"
              className="subscription-select-trigger"
              disabled={running}
              aria-expanded={menuOpen}
              aria-haspopup="true"
              aria-controls="subscription-select-menu"
              onClick={() => setMenuOpen((current) => !current)}
            >
              <span className="subscription-select-value">{triggerLabel}</span>
              {scopeChanged && <i className="scope-modified">Modified</i>}
              <span className="subscription-select-count">{selectedIds.size} of {subscriptions.length}</span>
              <ChevronDown size={15} className="subscription-select-caret" />
            </button>

            <div id="subscription-select-menu" className="subscription-select-menu" hidden={!menuOpen}>
              <div className="subscription-select-actions">
                <button type="button" onClick={onSelectAll} disabled={running || allSelected}>
                  <CheckCheck size={14} /> Select all
                </button>
                <button type="button" onClick={onClearAll} disabled={running || selectedIds.size === 0}>
                  <X size={14} /> Clear all
                </button>
              </div>

              {subscriptions.length > 8 && (
                <div className="subscription-select-search">
                  <Search size={13} />
                  <input
                    type="search"
                    value={filter}
                    placeholder="Filter by name or ID"
                    aria-label="Filter subscriptions"
                    onChange={(event) => setFilter(event.target.value)}
                  />
                </div>
              )}

              {visibleSubscriptions.length === 0 ? (
                <p className="subscription-select-empty" role="status">No subscriptions match that filter.</p>
              ) : (
                <ul className="subscription-grid">
                  {visibleSubscriptions.map((subscription) => {
                    const selected = selectedIds.has(subscription.subscriptionId);
                    return (
                      <li key={subscription.subscriptionId} className={selected ? 'selected' : ''}>
                        <label>
                          <input
                            type="checkbox"
                            checked={selected}
                            disabled={running}
                            onChange={() => onToggle(subscription.subscriptionId)}
                          />
                          <span className="subscription-check">{selected && <Check size={12} />}</span>
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
        )}

        {children && <div className="scope-range">{children}</div>}

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
    </section>
  );
}
