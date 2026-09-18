// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from './App';
import { getLatestReport, narrate } from './api';
import { ApiIdentityRequiredError, initializeApiIdentity, redirectApiIdentity, signOutApiIdentity, IDENTITY_REQUIRED_EVENT } from './apiIdentity';
import { runCostAssessment } from './collectors/costAssessment';
import type { ReportSnapshot } from './report/models';

vi.mock('./api', () => ({ getLatestReport: vi.fn(), narrate: vi.fn() }));
vi.mock('./collectors/costAssessment', () => ({ runCostAssessment: vi.fn() }));
vi.mock('./apiIdentity', () => ({
  ApiIdentityRequiredError: class extends Error {},
  initializeApiIdentity: vi.fn(), connectApiIdentity: vi.fn(), redirectApiIdentity: vi.fn(), signOutApiIdentity: vi.fn(),
  apiFetch: (...args: Parameters<typeof fetch>) => fetch(...args), IDENTITY_REQUIRED_EVENT: 'mkai-identity-required',
}));
vi.mock('./components/ChatWindow', () => ({ ChatWindow: () => <input aria-label="Chat draft" /> }));
vi.mock('./components/ScheduleManager', () => ({ ScheduleManager: () => <input aria-label="Schedule filter" /> }));
vi.mock('./components/SubscriptionPicker', () => ({
  SubscriptionPicker: ({ onRun, error }: { onRun: () => void; error: string | null }) => (
    <div>Scope picker<button type="button" onClick={onRun}>Run Report</button>{error && <p role="alert">{error}</p>}</div>
  ),
}));
vi.mock('./components/ReportView', () => ({ ReportView: ({ snapshotId, costWindow }: { snapshotId: string | null; costWindow?: { startDate: string; endDate: string } }) => <div data-testid="loaded-report" data-snapshot={snapshotId} data-window={costWindow ? `${costWindow.startDate}..${costWindow.endDate}` : ''}>Loaded report</div> }));

const snapshot = {
  snapshotId: 'report-1', subscriptionIds: ['subscription-1'], staleDays: 90,
  createdAt: '2026-09-09T10:00:00Z',
  report: { reportMetadata: { period: '2026-08', staleDays: 90 }, subscriptionBreakdown: [{ subscriptionId: 'subscription-1' }] },
} as ReportSnapshot;
let container: HTMLDivElement;
let root: Root;
const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === text)!;

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(initializeApiIdentity).mockResolvedValue({ userId: 'verified-user', userDetails: 'verified@example.com', tenantId: 'tenant-1', features: { aiNarration: true } });
  vi.mocked(getLatestReport).mockResolvedValue(null);
  window.localStorage.clear();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.startsWith('/.auth/')) throw new Error('SWA transport must not be used.');
    return { ok: true, json: async () => [{ subscriptionId: 'subscription-1', displayName: 'Test subscription' }] };
  }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('restores an authorized saved report on sign-in without starting an assessment or narration', async () => {
  vi.mocked(getLatestReport).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  expect(getLatestReport).toHaveBeenCalledOnce();
  expect(getLatestReport).toHaveBeenCalledWith(expect.any(AbortSignal), ['subscription-1'], 90);
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
  expect(container.querySelector('.report-awaiting')).toBeNull();
  expect(runCostAssessment).not.toHaveBeenCalled();
  expect(narrate).not.toHaveBeenCalled();
});

it('presents one report-wide cost window beside the saved-report controls and hands it to the report', async () => {
  const dates = Array.from({ length: 10 }, (_, index) => `2026-09-${String(index + 1).padStart(2, '0')}`);
  vi.mocked(getLatestReport).mockResolvedValue({ ...snapshot, report: { ...snapshot.report, costDetails: { dates } } } as ReportSnapshot);
  await act(async () => root.render(<App />));

  const strip = container.querySelector('.saved-report-controls')!;
  const picker = strip.querySelector('[aria-label="Report cost window"]');
  expect(picker).not.toBeNull();
  // It is presented once, not repeated per tab.
  expect(container.querySelectorAll('[aria-label="Report cost window"]')).toHaveLength(1);

  expect(container.querySelector('[data-testid="loaded-report"]')?.getAttribute('data-window')).toBe('2026-08-12..2026-09-10');

  await act(async () => button('7d').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="Cost window start"]')?.value).toBe('2026-09-04');
  expect(container.querySelector<HTMLInputElement>('[aria-label="Cost window end"]')?.value).toBe('2026-09-10');
  expect(container.querySelector('[data-testid="loaded-report"]')?.getAttribute('data-window')).toBe('2026-09-04..2026-09-10');
});

it('shows no cost window until a report is loaded', async () => {
  vi.mocked(getLatestReport).mockResolvedValue(null);
  await act(async () => root.render(<App />));
  expect(container.querySelector('.saved-report-controls')).not.toBeNull();
  expect(container.querySelector('[aria-label="Report cost window"]')).toBeNull();
});

it('waits for Run Report before collection when no saved report exists', async () => {
  vi.mocked(runCostAssessment).mockResolvedValue(snapshot.report);
  vi.mocked(getLatestReport).mockResolvedValueOnce(null).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  expect(runCostAssessment).not.toHaveBeenCalled();
  expect(getLatestReport).toHaveBeenCalledOnce();
  expect(container.textContent).toContain('Scope picker');
  expect(container.querySelector('.report-awaiting')?.textContent).toContain('No saved report for this scope');
  expect(container.querySelector('.report-awaiting i')).toBeNull();
  expect(container.querySelector('.assessment-progress')).toBeNull();
  expect(container.textContent).not.toContain('Load latest report on sign-in');
  expect(container.textContent).not.toContain('Load latest report');
  expect(button('Skip')).toBeUndefined();
  expect(container.querySelector<HTMLAnchorElement>('.skip-link')?.getAttribute('href')).toBe('#workspace-main');
  await act(async () => button('Run Report').click());
  expect(runCostAssessment).toHaveBeenCalledWith(['subscription-1'], 90);
  expect(getLatestReport).toHaveBeenCalledTimes(2);
  expect(narrate).toHaveBeenCalledWith(['subscription-1'], snapshot.report);
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
});

it('shows assessment failures and waits for an explicit retry', async () => {
  vi.mocked(runCostAssessment).mockRejectedValueOnce(new Error('Cost exports are not ready.'));
  await act(async () => root.render(<App />));
  await act(async () => button('Run Report').click());
  expect(container.querySelector('[role="alert"]')?.textContent).toBe('Cost exports are not ready.');
  expect(getLatestReport).toHaveBeenCalledOnce();
  expect(narrate).not.toHaveBeenCalled();
  expect(runCostAssessment).toHaveBeenCalledTimes(1);
  vi.mocked(runCostAssessment).mockResolvedValueOnce(snapshot.report);
  vi.mocked(getLatestReport).mockResolvedValueOnce(snapshot);
  await act(async () => button('Run Report').click());
  expect(runCostAssessment).toHaveBeenCalledTimes(2);
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
});

it('does not retrieve or narrate a report while collection is still running', async () => {
  let completeAssessment!: (report: ReportSnapshot['report']) => void;
  vi.mocked(runCostAssessment).mockReturnValue(new Promise((resolve) => { completeAssessment = resolve; }));
  vi.mocked(getLatestReport).mockResolvedValueOnce(null).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  await act(async () => button('Run Report').click());
  expect(container.textContent).toContain('Collecting Azure data');
  expect(container.querySelector('.report-awaiting')).toBeNull();
  expect(container.querySelector('.assessment-progress')).not.toBeNull();
  expect(getLatestReport).toHaveBeenCalledOnce();
  expect(narrate).not.toHaveBeenCalled();
  await act(async () => button('Run Report').click());
  expect(runCostAssessment).toHaveBeenCalledTimes(1);
  await act(async () => completeAssessment(snapshot.report));
  expect(getLatestReport).toHaveBeenCalledTimes(2);
  expect(narrate).toHaveBeenCalledTimes(1);
});

it('lazily opens workspaces and preserves drafts and loaded report across navigation', async () => {
  vi.mocked(runCostAssessment).mockResolvedValue(snapshot.report);
  vi.mocked(getLatestReport).mockResolvedValueOnce(null).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  expect(container.querySelector('[aria-label="Chat draft"]')).toBeNull();
  expect(container.querySelector('[aria-label="Schedule filter"]')).toBeNull();
  await act(async () => button('Run Report').click());
  const loadedReport = container.querySelector('[data-testid="loaded-report"]');
  await act(async () => button('Chat').click());
  const chatDraft = container.querySelector<HTMLInputElement>('[aria-label="Chat draft"]')!;
  chatDraft.value = 'Draft question';
  await act(async () => button('Schedules').click());
  const scheduleFilter = container.querySelector<HTMLInputElement>('[aria-label="Schedule filter"]')!;
  scheduleFilter.value = 'Development';
  await act(async () => button('Report').click());
  expect(container.querySelector('[data-testid="loaded-report"]')).toBe(loadedReport);
  expect(loadedReport?.closest('[hidden]')).toBeNull();
  expect(chatDraft.closest('[hidden]')).not.toBeNull();
  await act(async () => button('Chat').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="Chat draft"]')?.value).toBe('Draft question');
  await act(async () => button('Schedules').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="Schedule filter"]')?.value).toBe('Development');
  expect(runCostAssessment).toHaveBeenCalledTimes(1);
  expect(getLatestReport).toHaveBeenCalledTimes(2);
});

it('requires explicit MSAL sign-in when no verified account is available', async () => {
  vi.mocked(initializeApiIdentity).mockRejectedValue(new ApiIdentityRequiredError());
  vi.mocked(getLatestReport).mockResolvedValue(null);
  await act(async () => root.render(<App />));
  expect(getLatestReport).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain('Scope picker');
  const input = container.querySelector<HTMLInputElement>('input[type="email"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'user@example.test');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(redirectApiIdentity).toHaveBeenCalledWith('user@example.test');
  expect(getLatestReport).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

it('retries a transient identity failure using cached identity without forcing another interactive login', async () => {
  vi.mocked(initializeApiIdentity).mockRejectedValueOnce(new Error('Identity service temporarily unavailable.'))
    .mockResolvedValue({ userId: 'verified-user', userDetails: 'verified@example.com', tenantId: 'tenant-1' });
  await act(async () => root.render(<App />));
  expect(getLatestReport).not.toHaveBeenCalled();
  await act(async () => button('Retry sign-in check').click());
  expect(initializeApiIdentity).toHaveBeenCalledTimes(2);
  expect(redirectApiIdentity).not.toHaveBeenCalled();
  expect(getLatestReport).toHaveBeenCalledOnce();
});

it('retries saved evidence without collecting new data', async () => {
  vi.mocked(getLatestReport).mockRejectedValueOnce(new Error('Snapshot storage unavailable')).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Snapshot storage unavailable');
  await act(async () => button('Retry saved report').click());
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
  expect(runCostAssessment).not.toHaveBeenCalled();
  expect(narrate).not.toHaveBeenCalled();
});

it('makes saved-report loading optional and allows a pending lookup to be skipped without accepting its late result', async () => {
  window.localStorage.setItem('mkai-open-saved-report', 'false');
  await act(async () => root.render(<App />));
  expect(getLatestReport).not.toHaveBeenCalled();
  expect(container.textContent).toContain('Report workspace ready');
  let finish!: (value: ReportSnapshot) => void;
  vi.mocked(getLatestReport).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  await act(async () => button('Open saved report').click());
  await act(async () => button('Skip saved report').click());
  expect(vi.mocked(getLatestReport).mock.calls[0][0]?.aborted).toBe(true);
  await act(async () => finish(snapshot));
  expect(container.querySelector('[data-testid="loaded-report"]')).toBeNull();
  expect(runCostAssessment).not.toHaveBeenCalled();
});

it('times out saved-report loading and ignores a late response', async () => {
  vi.useFakeTimers();
  let finish!: (value: ReportSnapshot) => void;
  vi.mocked(getLatestReport).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  await act(async () => root.render(<App />));
  expect(container.textContent).toContain('Opening saved report');
  await act(async () => vi.advanceTimersByTimeAsync(25001));
  expect(container.textContent).toContain('Saved report loading timed out');
  expect(container.textContent).not.toContain('Opening saved report');
  expect(vi.mocked(getLatestReport).mock.calls[0][0]?.aborted).toBe(true);
  await act(async () => finish(snapshot));
  expect(container.querySelector('[data-testid="loaded-report"]')).toBeNull();
  expect(runCostAssessment).not.toHaveBeenCalled();
});

it('does not let startup evidence overwrite an explicitly generated report', async () => {
  let finish!: (value: ReportSnapshot) => void;
  const updated = { ...snapshot, snapshotId: 'new-report' };
  vi.mocked(getLatestReport).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValue(updated);
  vi.mocked(runCostAssessment).mockResolvedValue(updated.report);
  await act(async () => root.render(<App />));
  await act(async () => button('Run Report').click());
  await act(async () => finish(snapshot));
  expect(container.querySelector('[data-testid="loaded-report"]')?.getAttribute('data-snapshot')).toBe('new-report');
});

it('never restores a snapshot outside the authorized scope', async () => {
  vi.mocked(getLatestReport).mockResolvedValue({ ...snapshot, subscriptionIds: ['forbidden-subscription'] });
  await act(async () => root.render(<App />));
  expect(container.querySelector('[data-testid="loaded-report"]')).toBeNull();
  expect(container.textContent).toContain('does not match the authorized scope');
  expect(runCostAssessment).not.toHaveBeenCalled();
});

it('uses only the authorized part of a user-specific remembered scope', async () => {
  window.localStorage.setItem('mkai-report-scope:tenant-1:verified-user', JSON.stringify({ subscriptionIds: ['SUBSCRIPTION-1', 'revoked-subscription'], staleDays: 30 }));
  const saved = { ...snapshot, staleDays: 30, report: { ...snapshot.report, reportMetadata: { ...snapshot.report.reportMetadata, staleDays: 30 } } } as ReportSnapshot;
  vi.mocked(getLatestReport).mockResolvedValue(saved);
  await act(async () => root.render(<App />));
  expect(getLatestReport).toHaveBeenCalledWith(expect.any(AbortSignal), ['subscription-1'], 30);
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
  expect(JSON.parse(window.localStorage.getItem('mkai-report-scope:tenant-1:verified-user')!)).toEqual({ subscriptionIds: ['subscription-1'], staleDays: 30 });
});

it('clears restored evidence when API identity expires', async () => {
  vi.mocked(getLatestReport).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  await act(async () => window.dispatchEvent(new Event(IDENTITY_REQUIRED_EVENT)));
  expect(container.querySelector('[data-testid="loaded-report"]')).toBeNull();
  expect(container.textContent).not.toContain('Scope picker');
  expect(container.querySelector('[aria-label="Schedule filter"]')).toBeNull();
  expect(container.textContent).toContain('Reconnect your Microsoft account');
});

it('offers recovery rather than an endless sign-in status spinner', async () => {
  vi.useFakeTimers();
  vi.mocked(initializeApiIdentity).mockReturnValue(new Promise(() => {}));
  await act(async () => root.render(<App />));
  await act(async () => vi.advanceTimersByTimeAsync(15001));
  expect(button('Retry sign-in check')).toBeDefined();
  expect(container.textContent).toContain('Sign-in status could not be loaded');
  expect(getLatestReport).not.toHaveBeenCalled();
});

it('does not request AI narration when the verified profile disables it', async () => {
  vi.mocked(initializeApiIdentity).mockResolvedValue({ userId: 'verified-user', userDetails: 'verified@example.com', tenantId: 'tenant-1', features: { aiNarration: false } });
  vi.mocked(runCostAssessment).mockResolvedValue(snapshot.report);
  vi.mocked(getLatestReport).mockResolvedValueOnce(null).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  await act(async () => button('Run Report').click());
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
  expect(container.querySelector('.assessment-progress')).toBeNull();
  expect(narrate).not.toHaveBeenCalled();
});

it('opens the signed-in workspace without a diagnostic panel or consent acquisition', async () => {
  vi.mocked(getLatestReport).mockResolvedValue(snapshot);
  await act(async () => root.render(<App />));
  expect(container.querySelector('[data-testid="loaded-report"]')).not.toBeNull();
  expect(container.querySelector('.account-name')?.textContent).toBe('verified@example.com');
  expect(container.querySelector('input[type="email"]')).toBeNull();
  expect(container.querySelector('[aria-label="Azure access"]')).toBeNull();
  expect(button('Check Azure access')).toBeUndefined();
  expect(button('Authorize Azure access')).toBeUndefined();
  expect(button('Onboard subscription')).toBeUndefined();
  expect(redirectApiIdentity).not.toHaveBeenCalled();
  expect(signOutApiIdentity).not.toHaveBeenCalled();
  expect(getLatestReport).toHaveBeenCalledOnce();
  expect(runCostAssessment).not.toHaveBeenCalled();
  expect(narrate).not.toHaveBeenCalled();
});