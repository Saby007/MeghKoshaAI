import { useState } from 'react';
import { Database } from 'lucide-react';
import type { FindingLine, SqlResourceContext, SqlOptimizationCheck } from '../findings/models';
import './SqlOptimization.css';

const models: Record<SqlResourceContext['deploymentModel'], string> = {
  single_database: 'Single database', pooled_database: 'Pooled database', elastic_pool: 'Elastic pool',
  managed_instance: 'Managed Instance', instance_pool: 'Instance pool', sql_vm: 'SQL Server on VM',
  logical_server: 'Logical server', unknown: 'Unclassified',
};
const statuses: Record<SqlOptimizationCheck['status'], string> = {
  needs_evidence: 'Needs evidence', review: 'Review opportunity', not_applicable: 'Not applicable', blocked: 'Blocked',
};
const readable = (value: string) => value.replaceAll('_', ' ');
const utcDate = (value: string) => Number.isFinite(Date.parse(value))
  ? new Date(value).toISOString().slice(0, 10) : 'Unavailable';
const percent = (value: number | null) => value !== null && Number.isFinite(value) ? `${value.toFixed(1)}%` : 'Unavailable';

export function SqlOptimization({ lines }: { lines: FindingLine[] }) {
  const [model, setModel] = useState('all');
  const [status, setStatus] = useState('all');
  const [search, setSearch] = useState('');
  const analyzed = lines.filter(line => (line.sqlContext?.optimizationChecks?.length ?? 0) > 0);
  const visible = analyzed.filter(line => (model === 'all' || line.sqlContext?.deploymentModel === model)
    && (status === 'all' || line.sqlContext?.optimizationChecks?.some(check => check.status === status))
    && `${line.resourceName} ${line.resourceId} ${line.subscriptionName}`.toLowerCase().includes(search.toLowerCase()));

  return <section className="sql-analysis" aria-labelledby="sql-analysis-title">
    <header className="sql-analysis-heading"><h2 id="sql-analysis-title"><Database size={18} /> SQL analysis</h2><span>{analyzed.length} resource{analyzed.length === 1 ? '' : 's'} assessed</span></header>
    <div className="sql-analysis-filters">
      <label>Resource or subscription<input type="search" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <label>Deployment model<select value={model} onChange={event => setModel(event.target.value)}><option value="all">All models</option>{Object.entries(models).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
      <label>Analysis status<select value={status} onChange={event => setStatus(event.target.value)}><option value="all">All statuses</option>{Object.entries(statuses).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
    </div>
    {analyzed.length === 0 ? <p role="status">SQL analysis is unavailable for this snapshot.</p> : visible.length === 0 ? <p role="status">No matching SQL resources.</p> : visible.map(line => {
      const context = line.sqlContext!;
      const evidence = context.workloadEvidence;
      const checks = (context.optimizationChecks ?? []).filter(check => status === 'all' || check.status === status);
      return <details key={line.resourceId} className="sql-analysis-resource">
        <summary><strong>{line.resourceName || line.resourceId}</strong><span>{models[context.deploymentModel]}</span><span>{line.subscriptionName || line.subscriptionId}</span></summary>
        <div className="sql-resource-content">
          <code>{line.resourceId}</code>
          <p>{context.classificationReason}</p>
          <dl className="sql-configuration">{Object.entries(context.configuration ?? {}).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
          <div className="sql-evidence">
            <h3>Workload evidence</h3>
            {evidence ? <><p>{evidence.source} · {readable(evidence.status)} · {utcDate(evidence.windowStart)} to {utcDate(evidence.windowEnd)} UTC (end exclusive)</p><p>Collected {utcDate(evidence.collectedAt)} UTC. {evidence.reason}</p>
              <div className="sql-table-scroll"><table><thead><tr><th>Metric</th><th>Days observed</th><th>Mean daily average</th><th>Maximum</th></tr></thead><tbody>{evidence.metrics.map(metric => <tr key={metric.name}><th scope="row">{metric.name}</th><td>{metric.observedDays}/{evidence.expectedDays}</td><td>{percent(metric.average)}</td><td>{percent(metric.maximum)}</td></tr>)}</tbody></table></div></> : <p>Workload telemetry was not collected in this snapshot.</p>}
          </div>
          <div className="sql-checks">{checks.map(check => <section key={check.ruleId} className="sql-check" aria-label={check.title}>
            <header><h3>{check.ruleId} · {check.title}</h3><span className={`sql-check-state ${check.status}`}>{statuses[check.status]}</span></header>
            <p>{check.reason}</p>
            <h4>Next steps</h4><ol>{check.nextSteps.map(step => <li key={step}>{step}</li>)}</ol>
            <p><strong>Required evidence:</strong> {check.requiredEvidence.map(readable).join('; ') || 'None recorded'}</p>
            {check.dependsOn.length > 0 && <p><strong>After:</strong> {check.dependsOn.join(', ')}</p>}
            <p className="sql-saving-status">Savings unquantified · Review only · Rule {check.ruleVersion}</p>
          </section>)}</div>
        </div>
      </details>;
    })}
  </section>;
}