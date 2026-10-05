// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiRequestError, configureCostExport, createCostSchedule, getCostExportConfiguration, listCostSchedules, listScheduleRuns, runAllCostSchedules, runCostSchedule, setCostScheduleState, type CostSchedule, type FocusExportConfiguration, type ScheduleRun } from '../api';
import { ScheduleManager } from './ScheduleManager';

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  listCostSchedules: vi.fn(),
  listScheduleRuns: vi.fn(),
  createCostSchedule: vi.fn(),
  getCostExportConfiguration: vi.fn(),
  configureCostExport: vi.fn(),
  deleteCostSchedule: vi.fn(),
  runAllCostSchedules: vi.fn(),
  runCostSchedule: vi.fn(),
  setCostScheduleState: vi.fn(),
}));

let container: HTMLDivElement;
let root: Root;

const schedule: CostSchedule = {
  subscriptionId: '616dc9b8-b4aa-415f-8dcb-71bc462916c5',
  displayName: 'Subscription One',
  readAccess: true,
  costAccess: true,
  windowMonths: 6,
  state: 'active',
  recurrence: 'Monthly',
  scheduleStartAt: '2030-09-05T03:00:00Z',
  nextRunAt: '2030-09-05T03:00:00Z',
  latestRun: null,
  availability: 'available',
};

const exportConfiguration: FocusExportConfiguration = {
  subscriptionId: schedule.subscriptionId, exportName: 'app-focus',
  storageResourceId: '/subscriptions/616dc9b8-b4aa-415f-8dcb-71bc462916c5/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/teststore',
  container: 'cost-exports', rootFolderPath: `focus/${schedule.subscriptionId}`, format: 'Csv', dataVersion: '1.2-preview',
  windowMonths: 6, nativeSchedule: 'Inactive', destinationRole: 'Storage Blob Data Contributor',
  destinationRoleScope: '/subscriptions/616dc9b8-b4aa-415f-8dcb-71bc462916c5/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/teststore/blobServices/default/containers/cost-exports',
  state: 'missing', canConfigure: true,
};

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<Value>((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
  return { promise, resolve, reject };
}

async function click(label: string) {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  expect(button).not.toBeNull();
  await act(async () => button!.click());
}

beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe('Open Schedules', () => {
  it('has no subscription onboarding form or access deployment link', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([]);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).not.toContain('Deploy access');
    expect(container.textContent).not.toContain('Onboard subscription');
    expect(container.textContent).not.toContain('Verify & schedule');
    expect(container.querySelector('select[aria-label="Subscription"]')).toBeNull();
    expect(container.querySelector('input')).toBeNull();
    expect(container.querySelector('a[href*="onboarding/template"]')).toBeNull();
  });

  it('does not describe a failed lookup as no accessible subscriptions', async () => {
    vi.mocked(listCostSchedules).mockRejectedValue(new Error('Subscription authorization is temporarily unavailable.'));
    await act(async () => root.render(<ScheduleManager />));

    expect(container.textContent).toContain('Subscription authorization is temporarily unavailable.');
    expect(container.textContent).not.toContain('No accessible subscriptions');
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
  });

  it('shows loading before a successful empty result', async () => {
    const request = deferred<CostSchedule[]>();
    vi.mocked(listCostSchedules).mockReturnValue(request.promise);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Loading FOCUS exports...');
    expect(container.textContent).not.toContain('No accessible subscriptions');
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    await act(async () => request.resolve([]));
    expect(container.textContent).toContain('No accessible subscriptions returned');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('retries a failed lookup and clears only the load error', async () => {
    vi.mocked(listCostSchedules).mockRejectedValueOnce(new ApiRequestError('Lookup unavailable.', 503)).mockResolvedValue([schedule]);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Subscription status unavailable');
    await click('Refresh schedules');
    expect(container.textContent).toContain('Subscription One');
    expect(container.textContent).toContain('Checked');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each([401, 403, 503])('removes stale subscriptions after refresh fails with HTTP %s', async (status) => {
    vi.mocked(listCostSchedules).mockResolvedValueOnce([schedule]).mockRejectedValue(new ApiRequestError('Request denied or unavailable.', status));
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Subscription One');
    await click('Refresh schedules');
    expect(container.textContent).not.toContain('Subscription One');
    expect(container.textContent).not.toContain('No accessible subscriptions');
    expect(container.textContent).toContain(status === 401 ? 'Sign-in required' : status === 403 ? 'Access not verified' : 'Subscription status unavailable');
    expect([...container.querySelectorAll('button')].some(button => button.textContent === 'Sign in')).toBe(status === 401);
    expect(container.querySelector('a[href="/.auth/login/aad"]')).toBeNull();
    expect([...container.querySelectorAll('button')].find(button => button.textContent?.includes('Run all'))?.disabled).toBe(true);
  });

  it('shows partial failures without inventing first-run or paused state', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, state: 'unknown', availability: 'export_unavailable', statusMessage: 'Export lookup failed.' },
      { ...schedule, subscriptionId: 'other', displayName: 'Other', availability: 'history_unavailable', statusMessage: 'History lookup failed.' },
    ]);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Export status unavailable');
    expect(container.textContent).toContain('Execution history unavailable');
    expect(container.textContent).not.toContain('Awaiting first export');
    // Export must stay clickable when the export itself is unavailable: it is the only action
    // allowed to create it, so it cannot be gated behind an export already existing.
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Export Subscription One now"]')?.disabled).toBe(false);
  });

  it('enables Run all to bootstrap a never-scheduled or missing-export subscription', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, state: 'unknown', availability: 'export_unavailable', statusMessage: 'Export lookup failed.' },
    ]);
    await act(async () => root.render(<ScheduleManager />));
    expect([...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Run all'))?.disabled).toBe(false);
  });

  it('keeps failed-readiness subscriptions visible with disabled controls and unknown totals', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, state: 'unknown', readAccess: false, costAccess: false,
        availability: 'access_unavailable', statusMessage: 'Permission lookup was throttled. Retry after 120 seconds.',
        latestRun: null, nextRunAt: null, scheduleStartAt: null },
      { ...schedule, subscriptionId: 'other', displayName: 'Other subscription', state: 'unknown',
        availability: 'configuration_unavailable', statusMessage: 'The six-month FOCUS worker is not enabled.' },
    ]);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Subscription One');
    expect(container.textContent).toContain('Other subscription');
    expect(container.textContent).toContain('Access unavailable');
    expect(container.textContent).toContain('Scheduler setup incomplete');
    expect(container.textContent).toContain('Retry after 120 seconds');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect([...container.querySelectorAll('.operations-metrics strong[data-unavailable="true"]')].map(item => item.textContent)).toEqual(['Unavailable', 'Unavailable']);
    expect([...container.querySelectorAll<HTMLButtonElement>('.schedule-actions button')].filter(button => !button.getAttribute('aria-label')?.startsWith('Configure export for')).every(button => button.disabled)).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Configure export for Subscription One"]')?.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Configure export for Other subscription"]')?.disabled).toBe(false);
    expect([...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Run all'))?.disabled).toBe(true);
    expect(container.textContent).not.toContain('Onboard subscription');
  });

  it('keeps Run all disabled when a ready subscription sits alongside unavailable ones', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, state: 'not_scheduled', availability: 'available', statusMessage: null,
        latestRun: null, nextRunAt: null, scheduleStartAt: null },
      { ...schedule, subscriptionId: 'throttled', displayName: 'Throttled subscription', state: 'unknown',
        readAccess: false, costAccess: false, availability: 'access_unavailable',
        statusMessage: 'Azure subscription access check was throttled. Retry after 120 seconds.',
        latestRun: null, nextRunAt: null, scheduleStartAt: null },
      { ...schedule, subscriptionId: 'pending', displayName: 'Pending processor setup', state: 'unknown',
        availability: 'configuration_unavailable', statusMessage: 'The six-month FOCUS worker is not enabled.',
        latestRun: null, nextRunAt: null, scheduleStartAt: null },
    ]);
    await act(async () => root.render(<ScheduleManager />));
    // The ready subscription stays individually actionable, but the bulk action must not be
    // offered while other rows cannot be run: it would act on a subset of the listed estate.
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Schedule Subscription One"]')?.disabled).toBe(false);
    expect([...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Run all'))?.disabled).toBe(true);
    expect(runAllCostSchedules).not.toHaveBeenCalled();
  });

  it('never labels an unknown execution as exported and tolerates invalid timestamps', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, latestRun: {
      runId: 'run', subscriptionId: schedule.subscriptionId, period: '2026-08', status: 'unknown',
      startedAt: 'invalid', completedAt: null, durationSeconds: null, error: null,
    } }]);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.textContent).toContain('Execution status unknown');
    expect(container.textContent).toContain('Timestamp unavailable');
    expect(container.textContent).not.toContain('2026-08 exported');
  });

  it('shows the status of each month in the six-month refresh', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, latestRun: {
      runId: 'run', subscriptionId: schedule.subscriptionId, period: '2026-03 to 2026-08', status: 'running',
      startedAt: '2026-09-22T06:43:51Z', completedAt: null, durationSeconds: null, error: null,
      completedMonths: 2, windowMonths: 6, months: [
        { period: '2026-03', status: 'succeeded' }, { period: '2026-04', status: 'succeeded' },
        { period: '2026-05', status: 'queued' }, { period: '2026-06', status: 'pending' },
        { period: '2026-07', status: 'pending' }, { period: '2026-08', status: 'pending' },
      ],
    } }]);
    await act(async () => root.render(<ScheduleManager />));
    const progress = container.querySelector('[aria-label="2026-03 to 2026-08 monthly export status"]');
    expect(progress).not.toBeNull();
    expect([...progress!.querySelectorAll('li')].map(item => item.getAttribute('aria-label'))).toEqual([
      'Mar Done', 'Apr Done', 'May Queued', 'Jun Pending', 'Jul Pending', 'Aug Pending',
    ]);
  });

  it('distinguishes pending, failed and empty execution history', async () => {
    const history = deferred<ScheduleRun[]>();
    vi.mocked(listCostSchedules).mockResolvedValue([schedule]);
    vi.mocked(listScheduleRuns).mockReturnValueOnce(history.promise).mockResolvedValue([]);
    await act(async () => root.render(<ScheduleManager />));
    await click('Execution history for Subscription One');
    expect(container.textContent).toContain('Loading execution history...');
    expect(container.textContent).not.toContain('No native execution history');
    await act(async () => history.reject(new Error('History unavailable.')));
    expect(container.textContent).toContain('History unavailable.');
    expect(container.textContent).not.toContain('No native execution history');
    await click('Execution history for Subscription One');
    await click('Execution history for Subscription One');
    expect(container.textContent).toContain('No native execution history');
    expect(container.textContent).not.toContain('History unavailable.');
  });

  it('does not repeatedly probe user cost access in the background', async () => {
    vi.useFakeTimers();
    const request = deferred<CostSchedule[]>();
    vi.mocked(listCostSchedules).mockReturnValue(request.promise);
    await act(async () => root.render(<ScheduleManager />));
    await act(async () => vi.advanceTimersByTime(45_000));
    expect(listCostSchedules).toHaveBeenCalledTimes(1);
    await act(async () => request.resolve([schedule]));
  });

  it('aborts the pending list request on unmount', async () => {
    const request = deferred<CostSchedule[]>();
    vi.mocked(listCostSchedules).mockReturnValue(request.promise);
    await act(async () => root.render(<ScheduleManager />));
    const signal = vi.mocked(listCostSchedules).mock.calls[0][0]!;
    await act(async () => root.render(null));
    expect(signal.aborted).toBe(true);
    await act(async () => request.resolve([schedule]));
    expect(container.textContent).toBe('');
  });

  it('does not erase an action failure when an explicit refresh succeeds', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([schedule]);
    vi.mocked(setCostScheduleState).mockRejectedValue(new Error('Pause was denied.'));
    await act(async () => root.render(<ScheduleManager />));
    await click('Pause Subscription One');
    expect(container.textContent).toContain('Pause was denied.');
    await click('Refresh schedules');
    expect(container.textContent).toContain('Pause was denied.');
  });

  it('configures a missing export explicitly, then saves its schedule separately', async () => {
    vi.mocked(listCostSchedules).mockResolvedValueOnce([{ ...schedule, state: 'unknown', availability: 'export_unavailable', statusMessage: 'Export is missing.' }])
      .mockResolvedValue([{ ...schedule, state: 'not_scheduled', latestRun: null }]);
    vi.mocked(getCostExportConfiguration).mockResolvedValue(exportConfiguration);
    vi.mocked(configureCostExport).mockResolvedValue({ ...exportConfiguration, state: 'configured', canConfigure: false, created: true });
    vi.mocked(createCostSchedule).mockResolvedValue(schedule);
    await act(async () => root.render(<ScheduleManager />));
    expect(getCostExportConfiguration).not.toHaveBeenCalled();
    await click('Configure export for Subscription One');
    expect(container.textContent).toContain('app-focus');
    expect(container.textContent).toContain('Storage Blob Data Contributor');
    expect(configureCostExport).not.toHaveBeenCalled();
    expect(createCostSchedule).not.toHaveBeenCalled();
    const configure = [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Configure export'))!;
    expect(configure.disabled).toBe(true);
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    expect(configure.disabled).toBe(false);
    await act(async () => configure.click());
    expect(configureCostExport).toHaveBeenCalledTimes(1);
    expect(configureCostExport).toHaveBeenCalledWith(schedule.subscriptionId, true, expect.any(AbortSignal));
    expect(createCostSchedule).not.toHaveBeenCalled();
    expect(container.querySelector('input[type="datetime-local"]')).not.toBeNull();
    const save = [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Save schedule'))!;
    await act(async () => save.click());
    expect(createCostSchedule).toHaveBeenCalledTimes(1);
  });

  it('shows setup prerequisites without creating exports or granting permissions', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, state: 'unknown', availability: 'configuration_unavailable' }]);
    vi.mocked(getCostExportConfiguration).mockRejectedValue(new ApiRequestError('ADLS configuration is incomplete.', 503));
    await act(async () => root.render(<ScheduleManager />));
    await click('Configure export for Subscription One');
    expect(container.textContent).toContain('ADLS configuration is incomplete.');
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(configureCostExport).not.toHaveBeenCalled();
    expect(createCostSchedule).not.toHaveBeenCalled();
  });

  it('requires a fresh preview after an uncertain creation response without replaying', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, state: 'unknown', availability: 'export_unavailable' }]);
    vi.mocked(getCostExportConfiguration).mockResolvedValue(exportConfiguration);
    vi.mocked(configureCostExport).mockRejectedValue(new ApiRequestError('Refresh status before trying again.', 503));
    await act(async () => root.render(<ScheduleManager />));
    await click('Configure export for Subscription One');
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    const configure = [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Configure export'))!;
    await act(async () => configure.click());
    expect(configureCostExport).toHaveBeenCalledTimes(1);
    expect(getCostExportConfiguration).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain('Refresh status before trying again.');
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(createCostSchedule).not.toHaveBeenCalled();
  });

  it('refreshes row readiness after finding an existing export without recreating it', async () => {
    vi.mocked(listCostSchedules).mockResolvedValueOnce([{ ...schedule, state: 'unknown', availability: 'export_unavailable' }])
      .mockResolvedValue([{ ...schedule, state: 'not_scheduled', availability: 'available' }]);
    vi.mocked(getCostExportConfiguration).mockResolvedValue({ ...exportConfiguration, state: 'configured', canConfigure: false });
    await act(async () => root.render(<ScheduleManager />));
    await click('Configure export for Subscription One');
    const next = [...container.querySelectorAll<HTMLButtonElement>('.export-config-actions button')].find(button => button.textContent?.includes('Schedule export'))!;
    expect(next.disabled).toBe(false);
    expect(listCostSchedules).toHaveBeenCalledTimes(2);
    expect(configureCostExport).not.toHaveBeenCalled();
    expect(createCostSchedule).not.toHaveBeenCalled();
  });

  it('aborts export preview and ignores its result after leaving the view', async () => {
    const preview = deferred<FocusExportConfiguration>();
    vi.mocked(listCostSchedules).mockResolvedValue([schedule]);
    vi.mocked(getCostExportConfiguration).mockReturnValue(preview.promise);
    await act(async () => root.render(<ScheduleManager />));
    await click('Configure export for Subscription One');
    const signal = vi.mocked(getCostExportConfiguration).mock.calls[0][1]!;
    await act(async () => root.render(null));
    expect(signal.aborted).toBe(true);
    await act(async () => preview.resolve(exportConfiguration));
    expect(container.textContent).toBe('');
    expect(configureCostExport).not.toHaveBeenCalled();
  });

  it('creates a schedule from an existing accessible subscription row', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, state: 'not_scheduled' },
      { ...schedule, subscriptionId: 'hidden', displayName: 'No cost access', costAccess: false },
    ]);
    vi.mocked(createCostSchedule).mockResolvedValue(schedule);
    await act(async () => root.render(<ScheduleManager />));
    expect(container.querySelector('select')).toBeNull();
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Reschedule No cost access"]')?.disabled).toBe(true);
    await click('Schedule Subscription One');
    expect(container.querySelector('input[type="datetime-local"]')).not.toBeNull();
    const save = [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Save schedule'))!;
    expect(save.disabled).toBe(false);
    await act(async () => save.click());
    expect(createCostSchedule).toHaveBeenCalledWith(schedule.subscriptionId, expect.stringMatching(/Z$/), 6, { enabled: true, timeUtc: '06:00' });
    expect(container.textContent).toContain('monthly UTC schedule updated');
  });

  it('removes a revoked subscription and its schedule editor on refresh', async () => {
    vi.mocked(listCostSchedules).mockResolvedValueOnce([{ ...schedule, state: 'not_scheduled' }]).mockResolvedValue([]);
    await act(async () => root.render(<ScheduleManager />));
    await click('Schedule Subscription One');
    expect(container.querySelector('input[type="datetime-local"]')).not.toBeNull();
    await click('Refresh schedules');
    expect(container.textContent).not.toContain('Subscription One');
    expect(container.querySelector('input')).toBeNull();
    expect(createCostSchedule).not.toHaveBeenCalled();
  });

  it('saves the chosen history window for new and existing schedules', async () => {
    const second = { ...schedule, subscriptionId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', displayName: 'Subscription Two', windowMonths: 4 as const };
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, state: 'not_scheduled', scheduleStartAt: null, nextRunAt: null }, second]);
    vi.mocked(createCostSchedule).mockResolvedValue(schedule);
    vi.mocked(setCostScheduleState).mockResolvedValue(second);
    const choose = async (value: string) => {
      const select = container.querySelector<HTMLSelectElement>('select')!;
      await act(async () => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })); });
    };
    const save = async () => act(async () => [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Save schedule'))!.click());
    await act(async () => root.render(<ScheduleManager />));
    await click('Schedule Subscription One');
    const select = container.querySelector<HTMLSelectElement>('select')!;
    expect([...select.options].map(option => option.value)).toEqual(['3', '4', '5', '6']);
    expect(select.value).toBe('6');
    await choose('3');
    await save();
    expect(createCostSchedule).toHaveBeenCalledWith(schedule.subscriptionId, expect.stringMatching(/Z$/), 3, { enabled: true, timeUtc: '06:00' });
    expect(container.textContent).toContain('with 3 months of history');
    await click('Reschedule Subscription Two');
    expect(container.querySelector<HTMLSelectElement>('select')!.value).toBe('4');
    await choose('5');
    await save();
    expect(setCostScheduleState).toHaveBeenCalledWith(second.subscriptionId, 'active', expect.stringMatching(/Z$/), 5, { enabled: true, timeUtc: '06:00' });
  });

  it('shows each daily pull status and saves daily settings', async () => {
    const daily = { enabled: true, timeUtc: '06:00', status: 'succeeded' as const, dataThrough: '2030-10-09', lastRunAt: '2030-10-10T06:00:00Z', error: null };
    const off = { ...schedule, subscriptionId: 'cccccccc-cccc-cccc-cccc-cccccccccccc', displayName: 'Subscription Three',
      daily: { ...daily, enabled: false, timeUtc: '05:00', status: 'idle' as const, dataThrough: null, lastRunAt: null } };
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, daily },
      { ...schedule, subscriptionId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', displayName: 'Subscription Two',
        daily: { ...daily, status: 'unavailable', dataThrough: null, error: "The daily export isn't configured." } },
      off,
    ]);
    vi.mocked(setCostScheduleState).mockResolvedValue(off);
    await act(async () => root.render(<ScheduleManager />));
    const labels = [...container.querySelectorAll('.daily-status')].map(item => item.textContent ?? '');
    expect(labels[0]).toMatch(/^Daily data through .*2030$/);
    expect(labels.slice(1)).toEqual(['Daily export not configured', 'Daily pull off']);
    await click('Reschedule Subscription Three');
    const toggle = container.querySelector<HTMLInputElement>('.schedule-daily-toggle input')!;
    const time = container.querySelector<HTMLInputElement>('input[type="time"]')!;
    expect(toggle.checked).toBe(false);
    expect(time.value).toBe('05:00');
    expect(time.disabled).toBe(true);
    await act(async () => toggle.click());
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(time, '07:15');
      time.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Save schedule'))!.click());
    expect(setCostScheduleState).toHaveBeenCalledWith(off.subscriptionId, 'active', expect.stringMatching(/Z$/), 6, { enabled: true, timeUtc: '07:15' });
    expect(container.textContent).toContain('daily pull at 07:15 UTC');
  });

  it('shows each period of the daily pull and when the next one starts', async () => {
    const base = { enabled: true, timeUtc: '06:00', lastRunAt: '2030-10-05T06:00:00Z', error: null, day: '2030-10-05' };
    const running = { ...base, status: 'running' as const, dataThrough: '2030-10-03', completedAt: null, nextRunAt: null,
      runs: [{ start: '2030-09-01', end: '2030-09-30', status: 'succeeded' as const }, { start: '2030-10-01', end: '2030-10-04', status: 'queued' as const }] };
    const done = { ...base, status: 'succeeded' as const, dataThrough: '2030-10-04', completedAt: '2030-10-05T06:40:00Z',
      nextRunAt: '2030-10-06T06:00:00Z', runs: [{ start: '2030-10-01', end: '2030-10-01', status: 'succeeded' as const }] };
    vi.mocked(listCostSchedules).mockResolvedValue([
      { ...schedule, daily: running },
      { ...schedule, subscriptionId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', displayName: 'Subscription Two', daily: done },
    ]);
    await act(async () => root.render(<ScheduleManager />));
    const strips = [...container.querySelectorAll('.daily-progress.desktop-month-progress')];
    expect(strips).toHaveLength(2);
    expect([...strips[0].querySelectorAll('li')].map(item => item.getAttribute('aria-label')))
      .toEqual(['Daily Sep late charges Done', 'Daily 1\u20134 Oct Queued']);
    expect(strips[1].querySelector('li')?.getAttribute('aria-label')).toBe('Daily 1 Oct Done');
    expect(container.querySelectorAll('.daily-progress.mobile-month-progress')).toHaveLength(2);
    const labels = [...container.querySelectorAll('.daily-status')].map(item => item.textContent ?? '');
    expect(labels[0]).toMatch(/^Daily pull in progress · data through .*2030$/);
    expect(labels[1]).toMatch(/^Daily data through .*2030 · next pull .*2030/);
  });

  it('offers Configure export when only the daily export is missing', async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([schedule]);
    vi.mocked(getCostExportConfiguration).mockResolvedValue({
      ...exportConfiguration, state: 'configured', canConfigure: false,
      daily: { exportName: 'app-focus-daily', rootFolderPath: `focus-daily/${schedule.subscriptionId}`, state: 'missing', canConfigure: true },
    });
    await act(async () => root.render(<ScheduleManager />));
    await click('Configure export for Subscription One');
    expect(container.textContent).toContain('app-focus-daily');
    expect(container.textContent).toContain(`cost-exports/focus-daily/${schedule.subscriptionId}`);
    const configure = [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.includes('Configure export'));
    expect(configure).toBeDefined();
    expect(container.querySelector('.export-role-confirmation')).not.toBeNull();
  });

  it("describes Export with each schedule's own history window", async () => {
    vi.mocked(listCostSchedules).mockResolvedValue([{ ...schedule, windowMonths: 4 }]);
    vi.mocked(runCostSchedule).mockResolvedValue({ subscriptionId: schedule.subscriptionId, status: 'queued' });
    await act(async () => root.render(<ScheduleManager />));
    const exportButton = container.querySelector<HTMLButtonElement>('button[aria-label="Export Subscription One now"]')!;
    expect(exportButton.title).toContain('last 4 months');
    await act(async () => exportButton.click());
    expect(runCostSchedule).toHaveBeenCalledWith(schedule.subscriptionId);
    expect(container.textContent).toContain('pulling and overwriting the last 4 months');
  });
});