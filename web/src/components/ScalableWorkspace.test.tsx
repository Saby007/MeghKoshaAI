// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  cancelAssessment, getAssessmentJob, getAssessmentRecords, getAssessmentSnapshot,
  getLatestAssessmentJob, getScalableLatestReport, getInteractiveAssessment, queueAssessment,
  type AssessmentJob, type ScalableSnapshot,
} from '../api';
import { reportFixture } from '../report/testFixtures';
import ScalableWorkspace from './ScalableWorkspace';

vi.mock('../api', () => ({
  cancelAssessment: vi.fn(), getAssessmentJob: vi.fn(), getAssessmentRecords: vi.fn(),
  getAssessmentSnapshot: vi.fn(), getLatestAssessmentJob: vi.fn(),
  getScalableLatestReport: vi.fn(), getInteractiveAssessment: vi.fn(), queueAssessment: vi.fn(),
}));
vi.mock('./ReportView', () => ({ ReportView: () => <div data-testid="classic-report">Complete classic report</div> }));

const snapshot: ScalableSnapshot = {
  snapshotId: 'snapshot-1', scopeHash: 'scope-1', subscriptionIds: ['sub-1'], staleDays: 90,
  createdAt: '2026-10-27T00:00:00Z',
  report: { ...reportFixture, costHierarchy: [], executiveSummary: { ...reportFixture.executiveSummary, activeResources: 15000 } },
  collections: [{ path: 'costHierarchy', total: 15000 }],
};
const activeJob: AssessmentJob = {
  jobId: 'job-1', subscriptionIds: ['sub-1'], staleDays: 90, createdAt: snapshot.createdAt,
  status: 'running', phase: 'collecting', attempts: 1, snapshotId: null, errorCode: null, error: null,
};
let root: Root;
let container: HTMLDivElement;
let handled: { current: number };
const busy = vi.fn();
const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>('button')]
  .find((item) => item.textContent?.trim() === text)!;
const render = (refreshToken = 0, ids = ['sub-1']) => root.render(
  <ScalableWorkspace subscriptionIds={ids} staleDays={90} refreshToken={refreshToken}
    handledRefresh={handled} onBusyChange={busy} />,
);

beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.mocked(getScalableLatestReport).mockResolvedValue(snapshot);
  vi.mocked(getLatestAssessmentJob).mockResolvedValue(null);
  vi.mocked(getAssessmentSnapshot).mockResolvedValue(snapshot);
  vi.mocked(getAssessmentRecords).mockResolvedValue({
    path: 'costHierarchy', total: 15000,
    items: [{ resourceName: 'vm-1', monthlySpend: 10 }], nextCursor: 99, filtered: false,
  });
  handled = { current: 0 };
  container = document.createElement('div'); document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

it('opens a bounded summary without queuing an assessment or loading detail collections', async () => {
  await act(async () => render());
  expect(container.querySelector('.scalable-metrics')?.textContent).toContain('15,000');
  expect(queueAssessment).not.toHaveBeenCalled();
  expect(getAssessmentRecords).not.toHaveBeenCalled();
  expect(getInteractiveAssessment).not.toHaveBeenCalled();
});

it('opens original tabs only after an explicit bounded full-snapshot request', async () => {
  vi.mocked(getInteractiveAssessment).mockResolvedValue({
    snapshotId: snapshot.snapshotId, scopeHash: snapshot.scopeHash,
    subscriptionIds: snapshot.subscriptionIds, staleDays: 90, createdAt: snapshot.createdAt,
    reportSchemaVersion: '1.0', report: reportFixture,
  });
  await act(async () => render());
  await act(async () => button('Open classic tabs').click());
  expect(getInteractiveAssessment).toHaveBeenCalledWith(snapshot.snapshotId, expect.any(AbortSignal));
  expect(container.querySelector('[data-testid="classic-report"]')).not.toBeNull();
  await act(async () => button('Back to paginated explorer').click());
  expect(container.querySelector('.scalable-metrics')).not.toBeNull();
});

it('keeps large reports in the explorer instead of rendering partial classic tabs', async () => {
  vi.mocked(getInteractiveAssessment).mockRejectedValue(new Error('Report exceeds the interactive read budget'));
  await act(async () => render());
  await act(async () => button('Open classic tabs').click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('interactive read budget');
  expect(container.querySelector('[data-testid="classic-report"]')).toBeNull();
  expect(container.querySelector('.scalable-metrics')).not.toBeNull();
});

it('pages details on demand and keeps complete totals distinct from visible rows', async () => {
  await act(async () => render());
  const section = container.querySelector<HTMLSelectElement>('label > select')!;
  await act(async () => { section.value = 'costHierarchy'; section.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(container.textContent).toContain('15,000 records');
  expect(container.textContent).toContain('1 rows on this page; 15,000 in the complete collection');
  expect(getAssessmentRecords).toHaveBeenLastCalledWith('snapshot-1', 'costHierarchy', -1, '', expect.any(AbortSignal));
  await act(async () => button('Next page').click());
  expect(getAssessmentRecords).toHaveBeenLastCalledWith('snapshot-1', 'costHierarchy', 99, '', expect.any(AbortSignal));
  expect(container.querySelector('.scalable-metrics')?.textContent).toContain('15,000');
});

it('does not send an unindexed one-character detail search to the API', async () => {
  vi.useFakeTimers();
  await act(async () => render());
  const section = container.querySelector<HTMLSelectElement>('label > select')!;
  await act(async () => { section.value = 'costHierarchy'; section.dispatchEvent(new Event('change', { bubbles: true })); });
  vi.mocked(getAssessmentRecords).mockClear();
  const input = container.querySelector<HTMLInputElement>('input')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, 'v');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => vi.advanceTimersByTimeAsync(400));
  expect(container.textContent).toContain('Use at least three characters');
  expect(getAssessmentRecords).not.toHaveBeenCalled();
});

it('handles a run requested before lazy mounting and preserves the previous result while polling', async () => {
  vi.useFakeTimers();
  vi.mocked(queueAssessment).mockResolvedValue(activeJob);
  vi.mocked(getAssessmentJob).mockResolvedValue({ ...activeJob, status: 'succeeded', phase: 'complete', snapshotId: 'snapshot-1' });
  await act(async () => render(1));
  expect(queueAssessment).toHaveBeenCalledOnce();
  expect(container.textContent).toContain('Assessment running: collecting');
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(getAssessmentSnapshot).toHaveBeenCalledWith('snapshot-1', expect.any(AbortSignal));
  expect(container.querySelector('.scalable-metrics')).not.toBeNull();
  expect(busy).toHaveBeenLastCalledWith(false);
});

it('resumes observation after reconnect without creating a duplicate job', async () => {
  vi.useFakeTimers();
  vi.mocked(getLatestAssessmentJob).mockResolvedValue(activeJob);
  vi.mocked(getAssessmentJob).mockResolvedValue({ ...activeJob, status: 'succeeded', phase: 'complete', snapshotId: 'snapshot-1' });
  await act(async () => render());
  expect(container.textContent).toContain('last completed assessment remains available');
  expect(queueAssessment).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(container.textContent).toContain('Assessment succeeded: complete');
});

it('retains the complete saved result when queue admission fails', async () => {
  await act(async () => render());
  vi.mocked(queueAssessment).mockRejectedValue(new Error('Assessment queue is full'));
  await act(async () => render(1));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('queue is full');
  expect(container.querySelector('.scalable-metrics')).not.toBeNull();
  expect(busy).toHaveBeenLastCalledWith(false);
});

it('ignores a late response from a previous subscription scope', async () => {
  let resolveOld!: (value: ScalableSnapshot) => void;
  vi.mocked(getScalableLatestReport).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
  await act(async () => render());
  const next = { ...snapshot, subscriptionIds: ['sub-2'], report: {
    ...snapshot.report, reportMetadata: { ...snapshot.report.reportMetadata, period: 'new-scope-period' },
  } };
  vi.mocked(getScalableLatestReport).mockResolvedValue(next);
  await act(async () => render(0, ['sub-2']));
  await act(async () => resolveOld({ ...snapshot, report: {
    ...snapshot.report, reportMetadata: { ...snapshot.report.reportMetadata, period: 'old-scope-period' },
  } }));
  expect(container.textContent).toContain('new-scope-period');
  expect(container.textContent).not.toContain('old-scope-period');
  expect(cancelAssessment).not.toHaveBeenCalled();
});
