// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const id = '11111111-1111-4111-8111-111111111111';
const session = { userId: id, tenantId: id, userDetails: 'FOCUS workspace', csrfToken: 'a'.repeat(64),
  features: { assessmentMode: 'postgres', manualFocus: true } };

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear(); window.sessionStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(session))));
});
afterEach(() => vi.unstubAllGlobals());

it('opens an anonymous workspace without Microsoft scopes, redirects or browser token storage', async () => {
  const { initializeApiIdentity } = await import('./apiIdentity');
  const first = initializeApiIdentity();
  expect(initializeApiIdentity()).toBe(first);
  expect(await first).toEqual({ userId: id, tenantId: id, userDetails: session.userDetails, features: session.features });
  expect(fetch).toHaveBeenCalledOnce();
  expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/session');
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

it('uses same-origin cookies and a CSRF header for mutations, never delegated bearer tokens', async () => {
  const { apiFetch } = await import('./apiIdentity');
  await apiFetch('/api/admin/configuration', { method: 'PUT', headers: { Authorization: 'untrusted' }, body: '{}' });
  const [, init] = vi.mocked(fetch).mock.calls.at(-1)!;
  expect(init?.credentials).toBe('same-origin');
  expect(init?.redirect).toBe('error');
  expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe(session.csrfToken);
  expect(new Headers(init?.headers).has('Authorization')).toBe(false);
  expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
});

it('rejects external destinations and aborted requests before sending workspace information', async () => {
  const { apiFetch } = await import('./apiIdentity');
  await expect(apiFetch('https://unrelated.example/api/admin/configuration')).rejects.toThrow('only be sent');
  const controller = new AbortController(); controller.abort();
  await expect(apiFetch('/api/admin/uploads', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetch).not.toHaveBeenCalled();
});

it('rejects malformed server capabilities and signals workspace expiry instead of launching a login', async () => {
  const { initializeApiIdentity, apiFetch, IDENTITY_REQUIRED_EVENT } = await import('./apiIdentity');
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...session, csrfToken: '' })));
  await expect(initializeApiIdentity()).rejects.toThrow('invalid report workspace');
  await initializeApiIdentity();
  const expired = vi.fn();
  window.addEventListener(IDENTITY_REQUIRED_EVENT, expired);
  vi.mocked(fetch).mockResolvedValueOnce(new Response('{}', { status: 401 }));
  expect((await apiFetch('/api/report/latest')).status).toBe(401);
  expect(expired).toHaveBeenCalledOnce();
  window.removeEventListener(IDENTITY_REQUIRED_EVENT, expired);
});
