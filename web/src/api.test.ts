import { afterEach, expect, it, vi } from 'vitest';
import { ApiRequestError, getLatestReport, listCostSchedules, updateFinOpsAction } from './api';
import * as api from './api';

vi.mock('./apiIdentity', () => ({ apiFetch: (...args: Parameters<typeof fetch>) => fetch(...args) }));

afterEach(() => vi.unstubAllGlobals());

const exportConfiguration: api.FocusExportConfiguration = {
  subscriptionId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', exportName: 'app-focus',
  storageResourceId: '/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/teststore',
  container: 'cost-exports', rootFolderPath: 'focus/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', format: 'Csv',
  dataVersion: '1.2-preview', windowMonths: 6, nativeSchedule: 'Inactive', destinationRole: 'Storage Blob Data Contributor',
  destinationRoleScope: '/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/teststore/blobServices/default/containers/cost-exports',
  state: 'missing', canConfigure: true,
};

it('previews export configuration without creating it and forwards cancellation', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(exportConfiguration)));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();
  await expect(api.getCostExportConfiguration(exportConfiguration.subscriptionId, controller.signal)).resolves.toEqual(exportConfiguration);
  expect(fetchMock).toHaveBeenCalledWith(`/api/schedules/${exportConfiguration.subscriptionId}/export`, expect.objectContaining({ cache: 'no-store', signal: controller.signal }));
  expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
});

it.each([
  { subscriptionId: 'other' }, { windowMonths: 1 }, { nativeSchedule: 'Active' }, { state: 'unknown' },
  { canConfigure: false }, { destinationRoleScope: '/injected' }, { rootFolderPath: 'focus/other' },
  { destinationRole: 'Owner' }, { exportName: '' }, { format: 'Unknown' },
])('rejects an unsafe export preview: %o', async overrides => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...exportConfiguration, ...overrides }))));
  await expect(api.getCostExportConfiguration(exportConfiguration.subscriptionId)).rejects.toMatchObject({ status: 502 });
});

it('requires confirmation locally and sends no editable destination or schedule settings', async () => {
  const result = { ...exportConfiguration, state: 'configured', canConfigure: false, created: true };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(result)));
  vi.stubGlobal('fetch', fetchMock);
  await expect(api.configureCostExport(exportConfiguration.subscriptionId, false)).rejects.toMatchObject({ status: 400 });
  expect(fetchMock).not.toHaveBeenCalled();
  await expect(api.configureCostExport(exportConfiguration.subscriptionId, true)).resolves.toEqual(result);
  expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'PUT', cache: 'no-store', body: JSON.stringify({ allowDestinationRoleAssignment: true }) });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('rejects an unconfirmed creation receipt without retrying the mutation', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...exportConfiguration, created: true })));
  vi.stubGlobal('fetch', fetchMock);
  await expect(api.configureCostExport(exportConfiguration.subscriptionId, true)).rejects.toMatchObject({ status: 502 });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('preserves HTTP status and server error detail for schedule reads', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Identity is unverified.' }), { status: 403 })));
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 403, message: 'Identity is unverified.' });
});

it.each([
  { status: 403, code: 'azure_managed_identity_forbidden' },
  { status: 503, code: 'azure_managed_identity_unavailable' },
])('preserves a backend identity failure without treating it as an expired login: $status', async ({ status, code }) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: {
    code, message: 'Verify the backend managed identity.',
  } }), { status })));
  await expect(listCostSchedules()).rejects.toMatchObject({
    status, code, message: 'Verify the backend managed identity.',
  });
});

it('treats an authentication redirect as a sign-in failure rather than schedule data', async () => {
  const response = new Response('<html>Sign in</html>', { status: 200 });
  Object.defineProperty(response, 'redirected', { value: true });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 401 });
});

it('rejects a non-list success response', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')));
  await expect(listCostSchedules()).rejects.toBeInstanceOf(ApiRequestError);
});

it.each([
  { readAccess: true, costAccess: false, windowMonths: 6 },
  { readAccess: false, costAccess: true, windowMonths: 6 },
  { readAccess: true, costAccess: true, windowMonths: 1 },
])('rejects unverified or non-six-month schedule candidates: %o', async (evidence) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([{
    subscriptionId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', displayName: 'Unverified', ...evidence,
  }]))));
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 502 });
});

it('forwards cancellation and preserves a valid empty result', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('[]'));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();
  await expect(listCostSchedules(controller.signal)).resolves.toEqual([]);
  expect(fetchMock).toHaveBeenCalledWith('/api/schedules', expect.objectContaining({ signal: controller.signal, credentials: 'same-origin' }));
});

it('preserves an explicitly unavailable access row without granting schedule access', async () => {
  const row = { subscriptionId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', displayName: 'Authorized subscription',
    readAccess: false, costAccess: false, accessCheckMode: 'permissions', windowMonths: 6,
    state: 'unknown', availability: 'access_unavailable', statusMessage: 'Permission lookup was throttled.',
    latestRun: null, nextRunAt: null, scheduleStartAt: null };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([row]))));
  await expect(listCostSchedules()).resolves.toEqual([row]);
});

it.each([
  { state: 'active' }, { availability: 'available' }, { statusMessage: '' },
  { latestRun: { status: 'succeeded' } }, { readAccess: undefined },
])('rejects unsafe unverified row data: %o', async (overrides) => {
  const row = { subscriptionId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', displayName: 'Unverified',
    readAccess: false, costAccess: false, windowMonths: 6, state: 'unknown', availability: 'access_unavailable',
    statusMessage: 'Access unavailable.', latestRun: null, nextRunAt: null, scheduleStartAt: null, ...overrides };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([row]))));
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 502 });
});

it('sends the observed action version and preserves a conflict response', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Action changed' }), { status: 409 }));
  vi.stubGlobal('fetch', fetchMock);
  await expect(updateFinOpsAction('snapshot-1', 'action-1', {
    expectedVersion: 3, status: 'open', owner: 'Owner', note: 'Draft', dueDate: null, realizedSavingMonth: null,
  })).rejects.toMatchObject({ status: 409, message: 'Action changed' });
  const request = fetchMock.mock.calls[0][1];
  expect(request.method).toBe('PUT');
  expect(JSON.parse(request.body)).toMatchObject({ snapshotId: 'snapshot-1', expectedVersion: 3, note: 'Draft' });
});

it('loads saved reports for the exact subscription scope and threshold without caching financial data', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();
  await getLatestReport(controller.signal, ['sub-1', 'sub-2'], 30);
  const [path, init] = fetchMock.mock.calls[0];
  const parameters = new URL(path, 'https://example.test').searchParams;
  expect(parameters.getAll('subscriptionId')).toEqual(['sub-1', 'sub-2']);
  expect(parameters.get('staleDays')).toBe('30');
  expect(init).toMatchObject({ signal: controller.signal, cache: 'no-store' });
});

it('distinguishes no saved report from a saved-report service failure', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{}', { status: 404 })).mockResolvedValueOnce(new Response(JSON.stringify({ detail: { message: 'History unavailable' } }), { status: 503 })));
  await expect(getLatestReport()).resolves.toBeNull();
  await expect(getLatestReport()).rejects.toMatchObject({ status: 503, message: 'History unavailable' });
});

it('does not expose subscription onboarding helpers', () => {
  expect(api).not.toHaveProperty('discoverSubscriptions');
  expect(api).not.toHaveProperty('validateSubscriptionSelection');
  expect(api).not.toHaveProperty('onboardingTemplateUrl');
});

it('preserves unavailable and denied schedule status', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Schedules unavailable' }), { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Access denied' }), { status: 403 })));
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 503 });
  await expect(listCostSchedules()).rejects.toMatchObject({ status: 403 });
});