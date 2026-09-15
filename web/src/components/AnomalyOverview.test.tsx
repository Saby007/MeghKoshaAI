// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getCostAnomalies } from '../api';
import { AICostAlerts, AnomalyOverview, useAnomalySummary } from './AnomalyOverview';
import { anomalyFixture } from '../report/testFixtures';
import type { AnomalySummary, CostAnomaly, FullReport } from '../report/models';

vi.mock('../api', () => ({ getCostAnomalies: vi.fn() }));
const signal = (dimensionType: CostAnomaly['dimensionType'], absoluteDelta: number): CostAnomaly => ({
  anomalyId: dimensionType, dimensionType, dimensionName: dimensionType, severity: 'High', date: '2026-08-31', absoluteDelta,
} as CostAnomaly);
const result: AnomalySummary = { algorithmVersion: 'weekday-v1', label: '', status: 'ready', statusMessage: '', historyStart: '2026-07-01', historyEnd: '2026-08-31', completeDays: 62, requiredDays: 35, currency: 'USD', generatedAt: '2026-09-09', trend: [], anomalies: [signal('subscription', 24), signal('service', 24)] };
const report = { subscriptionBreakdown: [{ subscriptionId: 'subscription-1' }], reportMetadata: { currency: 'USD', generatedAt: '2026-09-09' } } as FullReport;
const open = vi.fn();
let container: HTMLDivElement;
let root: Root;
function Harness({ source = report }: { source?: FullReport }) {
  const state = useAnomalySummary(source);
  return <AnomalyOverview state={state} onOpenDetails={open} formatMoney={(value) => `$${value}`} />;
}
beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('shows anomalies on the overview without double counting overlapping impact', async () => {
  vi.mocked(getCostAnomalies).mockResolvedValue(result);
  await act(async () => root.render(<Harness />));
  expect(container.textContent).toContain('Subscription spike impact $24');
  expect(container.textContent).not.toContain('$48');
  expect(container.textContent).toContain('2026-07-01 - 2026-08-31');
  await act(async () => container.querySelector<HTMLButtonElement>('header button')!.click());
  expect(open).toHaveBeenCalledOnce();
  expect(getCostAnomalies).toHaveBeenCalledOnce();
});

it('distinguishes unavailable detection from a ready result with no anomalies and supports retry', async () => {
  vi.mocked(getCostAnomalies).mockRejectedValueOnce(new Error('Service unavailable')).mockResolvedValue({ ...result, anomalies: [] });
  await act(async () => root.render(<Harness />));
  expect(container.querySelector('[role="alert"]')?.textContent).toBe('Service unavailable');
  expect(container.textContent).not.toContain('No anomalies');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Retry anomaly detection"]')!.click());
  expect(container.textContent).toContain('No anomalies crossed');
});

it('shows insufficient history without claiming zero signals', async () => {
  vi.mocked(getCostAnomalies).mockResolvedValue({ ...result, status: 'insufficient_history', completeDays: 10 });
  await act(async () => root.render(<Harness />));
  expect(container.textContent).toContain('Insufficient complete history: 10/35 days');
  expect(container.textContent).not.toContain('0 signals');
});

it('ignores late results for an old scope and refuses a mismatched currency', async () => {
  let finish!: (value: AnomalySummary) => void;
  vi.mocked(getCostAnomalies).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValue({ ...result, currency: 'EUR' });
  await act(async () => root.render(<Harness />));
  const abort = vi.mocked(getCostAnomalies).mock.calls[0][1]!;
  await act(async () => root.render(<Harness source={{ ...report, subscriptionBreakdown: [{ ...report.subscriptionBreakdown[0], subscriptionId: 'subscription-2' }] }} />));
  expect(abort.aborted).toBe(true);
  await act(async () => finish(result));
  expect(container.textContent).toContain('Anomaly currency does not match');
  expect(container.textContent).not.toContain('Subscription spike impact');
});

it('announces a busy region and reserves a skeleton shell until evidence is available', async () => {
  let finish!: (value: AnomalySummary) => void;
  vi.mocked(getCostAnomalies).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  await act(async () => root.render(<Harness />));
  expect(container.querySelector('[aria-label="Cost anomaly overview"]')?.getAttribute('aria-busy')).toBe('true');
  expect(container.querySelectorAll('.ui-skeleton[aria-hidden="true"]')).toHaveLength(2);
  expect(container.querySelector('[role="status"]')?.textContent).toContain('Checking latest complete');
  expect(container.textContent).not.toContain('$0');
  await act(async () => finish(result));
  expect(container.querySelector('[aria-label="Cost anomaly overview"]')?.getAttribute('aria-busy')).toBe('false');
  expect(container.querySelectorAll('.ui-skeleton')).toHaveLength(0);
});

it('shows detailed AI cost spikes and drops without requesting or inventing additional data', async () => {
  const base = anomalyFixture.anomalies[0];
  const aiAnomalies: CostAnomaly[] = [
    { ...base, anomalyId: 'ai-spike', dimensionType: 'resource', dimensionName: 'OpenAI account', actualCost: 240, expectedCost: 120, absoluteDelta: 120, percentageDelta: 1, anomalyType: 'spike' },
    { ...base, anomalyId: 'ai-drop', dimensionType: 'service', dimensionName: 'Azure OpenAI', actualCost: 40, expectedCost: 120, absoluteDelta: -80, percentageDelta: -2 / 3, anomalyType: 'drop' },
  ];
  const refresh = vi.fn();
  await act(async () => root.render(<AICostAlerts state={{ result: { ...anomalyFixture, aiAnomalies }, error: null, loading: false, refresh }} formatMoney={(value) => `$${value}`} displayCurrency="USD" />));
  expect(container.querySelector('.is-spike')?.textContent).toContain('Cost spike');
  expect(container.querySelector('.is-drop')?.textContent).toContain('Cost drop');
  expect(container.querySelector('.is-spike')?.textContent).toContain('+$120');
  expect(container.querySelector('.is-drop')?.textContent).toContain('$-80');
  expect(container.textContent).toContain('same-weekday samples');
  expect(container.textContent).toContain('not a verified saving');
  expect(container.textContent).toContain('not additive');
  expect(container.querySelectorAll('a')).toHaveLength(2);
  const filter = container.querySelector<HTMLSelectElement>('[aria-label="AI anomaly type"]')!;
  await act(async () => { filter.value = 'drop'; filter.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.querySelectorAll('article')).toHaveLength(1);
  expect(container.querySelector('.is-spike')).toBeNull();
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Refresh AI cost alerts"]')!.click());
  expect(refresh).toHaveBeenCalledOnce();
  expect(getCostAnomalies).not.toHaveBeenCalled();
});

it.each(['loading', 'failed', 'insufficient', 'legacy', 'empty'])('keeps the AI alert %s state explicit', async (mode) => {
  const response = { ...anomalyFixture, aiAnomalies: mode === 'legacy' ? undefined : [], status: mode === 'insufficient' ? 'insufficient_history' as const : 'ready' as const };
  await act(async () => root.render(<AICostAlerts state={{ result: response, error: mode === 'failed' ? 'History unavailable' : null, loading: mode === 'loading', refresh: vi.fn() }} formatMoney={String} displayCurrency="USD" />));
  expect(container.textContent).toContain({ loading: 'Checking AI billing history', failed: 'History unavailable', insufficient: 'complete history days', legacy: 'unavailable in this response', empty: 'No AI billing anomalies crossed' }[mode]);
  if (mode !== 'empty') expect(container.textContent).not.toContain('No AI billing anomalies crossed');
  expect(container.querySelectorAll('article')).toHaveLength(0);
});