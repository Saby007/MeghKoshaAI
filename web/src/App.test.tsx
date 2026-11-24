// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from './App';
import { getLatestReport } from './api';
import { initializeApiIdentity } from './apiIdentity';
import { snapshotFixture, detailReportFixture } from './report/testFixtures';

vi.mock('./api', () => ({ getLatestReport: vi.fn() }));
vi.mock('./collectors/costAssessment', () => ({ runCostAssessment: vi.fn() }));
vi.mock('./apiIdentity', () => ({
  initializeApiIdentity: vi.fn(), apiFetch: (...args: Parameters<typeof fetch>) => fetch(...args),
  IDENTITY_REQUIRED_EVENT: 'mkai-identity-required',
}));
vi.mock('./components/ReportView', () => ({
  ReportView: ({ snapshotId, costWindow }: { snapshotId: string; costWindow: { startDate: string; endDate: string } }) =>
    <div data-snapshot={snapshotId} data-window={`${costWindow.startDate}..${costWindow.endDate}`}>Financial report</div>,
}));
vi.mock('./components/ChatWindow', () => ({ ChatWindow: () => <div>Chat view</div> }));
vi.mock('./components/ManualAdmin', () => ({
  default: ({ onReady }: { onReady: () => void }) => <button type="button" onClick={onReady}>Complete replacement upload</button>,
}));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.mocked(initializeApiIdentity).mockResolvedValue({ userId: 'workspace', tenantId: 'workspace', userDetails: 'FOCUS workspace' });
  vi.mocked(getLatestReport).mockResolvedValue({
    ...snapshotFixture, report: { ...detailReportFixture, costDetails: {
      ...detailReportFixture.costDetails!, dates: ['2024-01-03', '2025-03-16'],
    } },
  });
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([{ subscriptionId: 'sub-1', displayName: 'Target' }]))));
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove(); vi.unstubAllGlobals();
});

it('opens the uploaded financial report without login and defaults to all uploaded dates, beyond 366 days', async () => {
  await act(async () => root.render(<App />));
  expect(container.textContent).not.toMatch(/sign in|sign out|Schedules/);
  expect(container.querySelector('[data-snapshot]')?.getAttribute('data-window')).toBe('2024-01-03..2025-03-16');
  expect(getLatestReport).toHaveBeenCalledWith(expect.any(AbortSignal), ['sub-1'], 90);
  expect([...container.querySelectorAll('nav button')].map((item) => item.textContent)).toEqual([' Report', ' Chat', ' Admin']);
  // The page rail's content offset is keyed to this wrapper; without it the rail overlaps the report.
  expect(container.querySelector('.view-panel:not([hidden]) [data-snapshot]')).not.toBeNull();
});

it('replaces the visible dashboard when Admin completes a replacement upload', async () => {
  await act(async () => root.render(<App />));
  vi.mocked(getLatestReport).mockResolvedValue({ ...snapshotFixture, snapshotId: 'replacement', report: detailReportFixture });
  const complete = [...container.querySelectorAll('button')].find((item) => item.textContent === 'Complete replacement upload')!;
  await act(async () => complete.click());
  expect(container.querySelector('[data-snapshot]')?.getAttribute('data-snapshot')).toBe('replacement');
  expect(getLatestReport).toHaveBeenCalledTimes(2);
});
