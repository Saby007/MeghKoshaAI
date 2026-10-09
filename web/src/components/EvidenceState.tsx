import { CircleAlert, Inbox, LoaderCircle } from 'lucide-react';

export function EvidenceState({ title, detail, loading = false, tone = 'neutral', action }: {
  title: string;
  detail: string;
  loading?: boolean;
  tone?: 'neutral' | 'error';
  action?: { label: string; onClick: () => void; disabled?: boolean };
}) {
  const Icon = loading ? LoaderCircle : tone === 'error' ? CircleAlert : Inbox;
  return (
    <div className={`evidence-state${tone === 'error' ? ' evidence-state-error' : ''}`} aria-busy={loading}>
      <span className="evidence-state-icon" aria-hidden="true"><Icon className={loading ? 'spin' : undefined} size={21} /></span>
      <div className="evidence-state-copy" role={tone === 'error' && !loading ? 'alert' : 'status'}>
        <h3>{title}</h3><p>{detail}</p>
        {loading && <div className="evidence-skeleton" aria-hidden="true"><i /><i /><i /></div>}
      </div>
      {action && <div className="evidence-state-actions"><button type="button" className="outline-command"
        disabled={loading || action.disabled} onClick={action.onClick}>{action.label}</button></div>}
    </div>
  );
}
