// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it } from 'vitest';
import type { FindingLine } from '../findings/models';
import { SqlOptimization } from './SqlOptimization';

let container: HTMLDivElement;
let root: Root;
const line: FindingLine = {
  category: 'sql_databases_and_pools', resourceId: '/subscriptions/sub-1/databases/db', resourceName: 'Payments DB',
  subscriptionId: 'sub-1', subscriptionName: 'Finance', monthlyCost: 100, confidence: 0.5, evidenceType: 'inventory_candidate',
  detail: '', costEvidence: [], focusPricingEvidence: [],
  sqlContext: { schemaVersion: '1.0', deploymentModel: 'single_database', resourceType: 'microsoft.sql/servers/databases',
    poolResourceId: null, computeResourceId: null, classificationReason: 'Known resource type', evidenceGaps: [],
    configuration: { licenseType: 'LicenseIncluded' }, optimizationChecks: [
      { ruleId: 'SQL-R04', ruleVersion: '1.0', title: 'Review Azure Hybrid Benefit', status: 'review', reason: 'License entitlement required.',
        requiredEvidence: ['license_entitlement'], nextSteps: ['Confirm entitlement with the licensing owner.'], dependsOn: [], estimatedMonthlySavings: null },
      { ruleId: 'SQL-R01', ruleVersion: '1.0', title: 'Right-size capacity', status: 'needs_evidence', reason: 'Metrics not collected.',
        requiredEvidence: ['workload_peaks'], nextSteps: ['Collect utilization.'], dependsOn: [], estimatedMonthlySavings: null },
    ] },
};

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('renders resource-specific checks without claiming quantified savings', async () => {
  await act(async () => root.render(<SqlOptimization lines={[line]} />));
  expect(container.textContent).toContain('Payments DB');
  expect(container.textContent).toContain('Confirm entitlement with the licensing owner.');
  expect(container.textContent).toContain('Savings unquantified');
  expect(container.textContent).toContain('Workload telemetry was not collected');
});

it('filters model and analysis status without changing findings', async () => {
  await act(async () => root.render(<SqlOptimization lines={[line]} />));
  const selectors = container.querySelectorAll('select');
  await act(async () => { selectors[1].value = 'review'; selectors[1].dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.textContent).toContain('Review Azure Hybrid Benefit');
  expect(container.textContent).not.toContain('Collect utilization.');
  await act(async () => { selectors[0].value = 'sql_vm'; selectors[0].dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.textContent).toContain('No matching SQL resources');
});

it('keeps legacy snapshots readable without fabricating new analyses', async () => {
  await act(async () => root.render(<SqlOptimization lines={[{ ...line, sqlContext: null }]} />));
  expect(container.textContent).toContain('SQL analysis is unavailable for this snapshot');
  expect(container.querySelectorAll('details')).toHaveLength(0);
});