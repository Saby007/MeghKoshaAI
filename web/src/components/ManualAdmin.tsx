import { useEffect, useRef, useState, type FormEvent } from 'react';
import { LoaderCircle, RefreshCw, Upload } from 'lucide-react';
import {
  completeUpload, discardUpload, getConnection, getUpload, getUploads, matchingFiles,
  retryUpload, saveConnection, uploadFiles, type TargetConnection, type UploadStatus,
} from '../manualApi';

const message = (failure: unknown) => failure instanceof Error ? failure.message : 'The workspace request failed.';

export default function ManualAdmin({ onConfigurationChanged, onReady }: {
  onConfigurationChanged: () => void; onReady: () => void;
}) {
  const [connection, setConnection] = useState<TargetConnection | null>(null);
  const [subscription, setSubscription] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [tenant, setTenant] = useState('');
  const [client, setClient] = useState('');
  const [secret, setSecret] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [uploads, setUploads] = useState<UploadStatus[]>([]);
  const [active, setActive] = useState<UploadStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [bytes, setBytes] = useState({ done: 0, total: 0 });
  const [resume, setResume] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const transfer = useRef<AbortController | null>(null);
  const ready = useRef(onReady);
  ready.current = onReady;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    Promise.all([getConnection(controller.signal), getUploads(controller.signal)]).then(([configuration, history]) => {
      if (controller.signal.aborted) return;
      setConnection(configuration.connection);
      setSubscription(configuration.connection?.subscriptionId ?? '');
      setEnabled(configuration.connection?.hasCredentials ?? false);
      setTenant(configuration.connection?.tenantId ?? '');
      setClient(configuration.connection?.clientId ?? '');
      setUploads(history);
      setActive(history.find((item) => ['queued', 'running'].includes(item.job?.status ?? '')) ?? null);
    }).catch((failure: unknown) => { if (!controller.signal.aborted) setError(message(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [version]);

  const activeJob = active?.job;
  useEffect(() => {
    if (!active || !activeJob || !['queued', 'running'].includes(activeJob.status)) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const status = await getUpload(active!.datasetId, controller.signal);
        if (controller.signal.aborted) return;
        setActive(status);
        setUploads((items) => [status, ...items.filter((item) => item.datasetId !== status.datasetId)]);
        if (status.job?.status === 'succeeded') {
          setNotice('Replacement report is ready. The dashboard now uses this uploaded report.');
          ready.current();
        } else if (status.job?.status === 'failed' || status.job?.status === 'cancelled') {
          setError(status.job.error ?? 'Processing was cancelled. The previous complete dashboard is unchanged.');
        } else timer = setTimeout(() => void poll(), 2000);
      } catch (failure) { if (!controller.signal.aborted) setError(message(failure)); }
    }
    timer = setTimeout(() => void poll(), 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active?.datasetId, activeJob?.jobId, activeJob?.status]);

  useEffect(() => () => transfer.current?.abort(), []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true); setError(null); setNotice(null);
    try {
      const replacing = enabled && (!connection?.hasCredentials || connection.subscriptionId !== subscription.trim().toLowerCase()
        || connection.tenantId !== tenant.trim().toLowerCase() || connection.clientId !== client.trim().toLowerCase());
      if (replacing && !secret) throw new Error('Enter all three credential fields to enable or replace the optional service principal.');
      const result = await saveConnection({
        subscriptionId: subscription.trim(), clearCredentials: !enabled,
        ...(enabled && secret ? { tenantId: tenant.trim(), clientId: client.trim(), clientSecret: secret } : {}),
      });
      setConnection(result.connection); setSecret(''); onConfigurationChanged();
      const previous = uploads.find((item) => item.status === 'ready' && item.subscriptionId === result.connection.subscriptionId);
      if (previous) {
        const job = await retryUpload(previous.datasetId);
        setActive({ ...previous, status: 'queued', job });
        setNotice('Configuration saved. Rebuilding the uploaded report with the new enrichment setting.');
      } else setNotice('Target saved. Upload a FOCUS report to create the dashboard.');
    } catch (failure) { setError(message(failure)); }
    finally { setSaving(false); }
  }

  async function upload() {
    const controller = new AbortController();
    transfer.current = controller;
    setUploading(true); setError(null); setNotice(null);
    try {
      const interrupted = resume ? uploads.find((item) => item.status === 'uploading'
        && item.subscriptionId === connection?.subscriptionId && matchingFiles(item, files)) ?? null : null;
      const uploaded = await uploadFiles(files, interrupted, controller.signal, (status, done, total) => {
        setActive(status); setBytes({ done, total });
      });
      const job = await completeUpload(uploaded.datasetId);
      setActive({ ...uploaded, status: 'queued', job });
      setUploads((items) => [{ ...uploaded, status: 'queued', job }, ...items.filter((item) => item.datasetId !== uploaded.datasetId)]);
      setNotice('Upload complete. Processing in the background; the previous dashboard remains available.');
    } catch (failure) {
      setError(controller.signal.aborted ? 'Upload paused. Select the same files and resume; completed chunks are retained.' : message(failure));
      try { setUploads(await getUploads()); } catch (reloadFailure) { setError(`${message(failure)} ${message(reloadFailure)}`); }
    } finally { setUploading(false); transfer.current = null; }
  }

  async function retry(item: UploadStatus) {
    setError(null);
    try { setActive({ ...item, status: 'queued', job: await retryUpload(item.datasetId) }); }
    catch (failure) { setError(message(failure)); }
  }

  return <section className="manual-admin" aria-labelledby="admin-heading">
    <h1 id="admin-heading">Admin: upload-specific workspace</h1>
    <p>No account or login is required. This browser has one target subscription and one active report. Other visitors have separate workspaces.</p>
    <p className="manual-notice">Keep this browser's workspace cookie to reopen your report. Clearing it or workspace expiry requires a new upload. Do not share browser cookies.</p>
    {loading && <p role="status"><LoaderCircle className="spin" size={16} /> Loading workspace settings...</p>}
    {error && <p className="manual-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <form className="manual-card" onSubmit={(event) => void save(event)}>
      <h2>1. Target subscription</h2>
      <label htmlFor="target-subscription">Subscription ID<input id="target-subscription" required value={subscription} disabled={saving || uploading}
        onChange={(event) => setSubscription(event.target.value)} placeholder="Subscription UUID" autoComplete="off" /></label>
      <label className="manual-checkbox"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={saving || uploading} /> Add optional read-only service-principal credentials</label>
      <p>FOCUS files alone populate financial views. Add a manually provisioned service principal with <strong>Reader only</strong> to populate Azure inventory, Advisor and metric evidence. This application does not grant roles or change Azure resources.</p>
      {enabled && <fieldset disabled={saving || uploading}>
        <legend>Optional Azure enrichment</legend>
        <label htmlFor="target-tenant">Tenant ID<input id="target-tenant" required value={tenant} onChange={(event) => setTenant(event.target.value)} autoComplete="off" /></label>
        <label htmlFor="target-client">Application / client ID<input id="target-client" required value={client} onChange={(event) => setClient(event.target.value)} autoComplete="off" /></label>
        <label htmlFor="target-secret">Client secret<input id="target-secret" type="password" value={secret} onChange={(event) => setSecret(event.target.value)}
          autoComplete="new-password" placeholder={connection?.hasCredentials ? 'Leave unchanged, or enter a replacement' : 'Secret value, not secret ID'} /></label>
        <p>Secrets are stored in the private server-side Key Vault and are never returned to the browser.</p>
      </fieldset>}
      <button type="submit" className="dark-button" disabled={saving || uploading || loading}>{saving ? 'Validating / saving...' : 'Save target and optional credentials'}</button>
      {connection && <p>Current mode: <strong>{connection.hasCredentials ? 'FOCUS + Reader service principal' : 'FOCUS only; live Azure sections unavailable'}</strong></p>}
    </form>
    <section className="manual-card" aria-labelledby="upload-heading">
      <h2 id="upload-heading">2. Upload / replace FOCUS report</h2>
      <p>Select FOCUS 1.2 CSV or gzip CSV files (Cost Management export dataset version 1.2-preview) for any duration. Include all report partitions and all history you want to retain in this replacement. Reports are not appended to the previous upload.</p>
      <label htmlFor="focus-files">FOCUS files<input id="focus-files" type="file" accept=".csv,.csv.gz,.gz" multiple disabled={!connection || uploading || saving}
        onChange={(event) => setFiles(Array.from(event.target.files ?? []))} /></label>
      <label className="manual-checkbox"><input type="checkbox" checked={resume} onChange={(event) => setResume(event.target.checked)} disabled={uploading} /> Resume matching interrupted files (checks existing chunk hashes)</label>
      <div className="manual-actions"><button type="button" className="dark-button" disabled={!connection || !files.length || uploading || saving} onClick={() => void upload()}><Upload size={16} /> Upload and generate dashboard</button>
        {uploading && <button type="button" className="ghost-button" onClick={() => transfer.current?.abort()}>Pause upload</button>}</div>
      {uploading && <div role="status"><progress max={Math.max(1, bytes.total)} value={bytes.done} aria-label="FOCUS upload progress" /> {Math.round(bytes.done / Math.max(1, bytes.total) * 100)}%</div>}
      {active?.job && <p role="status">Processing: {active.job.status} / {active.job.phase}. {active.job.error}</p>}
    </section>
    <section className="manual-card" aria-labelledby="upload-history-heading">
      <h2 id="upload-history-heading">Workspace uploads</h2>
      <button type="button" className="ghost-button" disabled={uploading} onClick={() => { setError(null); setVersion((value) => value + 1); }}><RefreshCw size={15} /> Refresh upload status</button>
      {!uploads.length && <p>No FOCUS report has been uploaded in this browser workspace.</p>}
      {uploads.map((item) => <article className="manual-upload" key={item.datasetId}>
        <strong>{item.files.map((file) => file.name).join(', ')}</strong>
        <p>{item.subscriptionId} / {item.job?.status ?? item.status}</p>
        {item.coverage && <p>{item.coverage.startDate} through {item.coverage.endDate}: {item.coverage.rowCount.toLocaleString()} rows, {item.coverage.currency}. Only uploaded dates are evidence.</p>}
        {(item.error || item.job?.error) && <p role="alert">{item.job?.error ?? item.error}</p>}
        {item.status === 'uploading'
          ? <button type="button" disabled={uploading} onClick={() => {
            void discardUpload(item.datasetId).then(() => setVersion((value) => value + 1)).catch((failure: unknown) => setError(message(failure)));
          }}>Discard incomplete upload</button>
          : <button type="button" className="ghost-button" disabled={uploading || saving || ['queued', 'running'].includes(item.job?.status ?? '')
            || item.subscriptionId !== connection?.subscriptionId} onClick={() => void retry(item)}>Rebuild from this report</button>}
      </article>)}
    </section>
  </section>;
}
