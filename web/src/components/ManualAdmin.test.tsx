// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ManualAdmin from './ManualAdmin';
import { getConnection, getUploads, saveConnection } from '../manualApi';

vi.mock('../manualApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../manualApi')>()),
  getConnection: vi.fn(), getUploads: vi.fn(), saveConnection: vi.fn(),
}));

const SUB = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const CLIENT = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
const TENANT = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
let container: HTMLDivElement;
let root: Root;

function type(selector: string, value: string) {
  const input = container.querySelector<HTMLInputElement>(selector)!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

beforeEach(async () => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.mocked(getConnection).mockResolvedValue({ connection: null });
  vi.mocked(getUploads).mockResolvedValue([]);
  vi.mocked(saveConnection).mockResolvedValue({
    connection: { revision: 1, subscriptionId: SUB, subscriptionName: 'Target', tenantId: TENANT, clientId: CLIENT, hasCredentials: true },
  });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
  await act(async () => root.render(<ManualAdmin onConfigurationChanged={() => undefined} onReady={() => undefined} />));
  await act(async () => {
    type('#target-subscription', SUB);
    container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

async function submit() {
  await act(async () => { container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
}

it('needs only the client ID and secret; the tenant is detected by the server and shown afterwards', async () => {
  expect(container.querySelector('#target-tenant')!.hasAttribute('required')).toBe(false);
  await act(async () => { type('#target-client', CLIENT); type('#target-secret', 'secret-value'); });
  await submit();
  expect(saveConnection).toHaveBeenCalledWith({ subscriptionId: SUB, clearCredentials: false, clientId: CLIENT, clientSecret: 'secret-value' });
  expect(container.querySelector<HTMLInputElement>('#target-tenant')!.value).toBe(TENANT);
  expect(container.querySelector<HTMLInputElement>('#target-secret')!.value).toBe('');
});

it('sends an explicitly entered tenant and refuses credentials without a secret', async () => {
  await act(async () => { type('#target-client', CLIENT); });
  await submit();
  expect(saveConnection).not.toHaveBeenCalled();
  expect(container.textContent).toContain('Enter the client ID and client secret');
  await act(async () => { type('#target-secret', 'secret-value'); type('#target-tenant', TENANT); });
  await submit();
  expect(saveConnection).toHaveBeenCalledWith(expect.objectContaining({ tenantId: TENANT, clientId: CLIENT }));
});
