// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ initialize: vi.fn(), getActiveAccount: vi.fn(), getAllAccounts: vi.fn(), acquireTokenSilent: vi.fn(), acquireTokenRedirect: vi.fn(), ssoSilent: vi.fn(), setActiveAccount: vi.fn(), loginPopup: vi.fn(), loginRedirect: vi.fn(), logoutRedirect: vi.fn(), handleRedirectPromise: vi.fn(), clearCache: vi.fn(), constructor: vi.fn() }));
vi.mock('@azure/msal-browser', () => ({
  BrowserCacheLocation: { SessionStorage: 'sessionStorage' },
  InteractionRequiredAuthError: class extends Error {},
  PublicClientApplication: function(configuration: unknown) { mocks.constructor(configuration); return mocks; },
}));
const configuration = { tenantId: '11111111-1111-4111-8111-111111111111', apiClientId: '22222222-2222-4222-8222-222222222222', webClientId: '33333333-3333-4333-8333-333333333333', scope: 'api://22222222-2222-4222-8222-222222222222/access_as_user' };
const account = { tenantId: configuration.tenantId, username: 'user@example.test', homeAccountId: 'account-one' };
const profile = { userId: 'verified-subject', userDetails: 'user@example.test', tenantId: configuration.tenantId };

beforeEach(() => {
  vi.resetModules(); vi.resetAllMocks();
  mocks.getActiveAccount.mockReturnValue(account);
  mocks.getAllAccounts.mockReturnValue([account]);
  mocks.acquireTokenSilent.mockResolvedValue({ accessToken: 'synthetic-token', account });
  vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(path === '/api/auth/config' ? configuration : profile))));
});
afterEach(() => vi.unstubAllGlobals());

it('requests only the dedicated API scope and sends tokens only to same-origin API paths', async () => {
  const { apiFetch } = await import('./apiIdentity');
  await expect(apiFetch('/api/report/latest')).resolves.toBeInstanceOf(Response);
  expect(mocks.acquireTokenSilent).toHaveBeenCalledWith({ account, scopes: [configuration.scope] });
  const request = vi.mocked(fetch).mock.calls.at(-1)!;
  expect(new Headers(request[1]?.headers).get('authorization')).toBe('Bearer synthetic-token');
  expect(new Headers(request[1]?.headers).has('x-meghkosha-user-token')).toBe(false);
  expect(request[1]?.cache).toBe('no-store');
  expect(request[1]?.redirect).toBe('error');
  await expect(apiFetch('https://other.example/api/report')).rejects.toThrow('only be sent');
  await expect(apiFetch('/not-api')).rejects.toThrow('only be sent');
  expect(mocks.constructor).toHaveBeenCalledWith(expect.objectContaining({ cache: { cacheLocation: 'sessionStorage' } }));
});

it('gets the displayed identity from API validation rather than browser account claims', async () => {
  const { initializeApiIdentity } = await import('./apiIdentity');
  await expect(initializeApiIdentity('untrusted-hint@example.test')).resolves.toEqual(profile);
  expect(vi.mocked(fetch).mock.calls.at(-1)![0]).toBe('/api/auth/me');
});

it('coalesces concurrent startup checks without caching a completed authorization profile', async () => {
  const { initializeApiIdentity } = await import('./apiIdentity');
  const first = initializeApiIdentity('user@example.test');
  const second = initializeApiIdentity(' USER@example.test ');
  expect(first).toBe(second);
  await expect(first).resolves.toEqual(profile);
  expect(vi.mocked(fetch).mock.calls.filter(([path]) => path === '/api/auth/me')).toHaveLength(1);
  await initializeApiIdentity('user@example.test');
  expect(vi.mocked(fetch).mock.calls.filter(([path]) => path === '/api/auth/me')).toHaveLength(2);
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.signal instanceof AbortSignal)).toBe(true);
});

it('never triggers an interactive redirect during an API request and preserves cancellation', async () => {
  const { apiFetch, ApiIdentityRequiredError } = await import('./apiIdentity');
  mocks.getActiveAccount.mockReturnValue(null); mocks.getAllAccounts.mockReturnValue([]);
  await expect(apiFetch('/api/report/latest')).rejects.toBeInstanceOf(ApiIdentityRequiredError);
  expect(mocks.loginPopup).not.toHaveBeenCalled();
  const controller = new AbortController(); controller.abort();
  await expect(apiFetch('/api/report/latest', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
});

it('offers explicit connection after silent SSO fails and verifies the resulting account', async () => {
  const { initializeApiIdentity, connectApiIdentity, ApiIdentityRequiredError } = await import('./apiIdentity');
  mocks.getActiveAccount.mockReturnValue(null); mocks.getAllAccounts.mockReturnValue([]);
  mocks.ssoSilent.mockRejectedValue(new Error('Interaction required'));
  await expect(initializeApiIdentity('user@example.test')).rejects.toBeInstanceOf(ApiIdentityRequiredError);
  mocks.loginPopup.mockResolvedValue({ account }); mocks.getActiveAccount.mockReturnValue(account);
  await expect(connectApiIdentity('user@example.test')).resolves.toEqual(profile);
  expect(mocks.loginPopup).toHaveBeenCalledWith({ scopes: [configuration.scope], loginHint: 'user@example.test', prompt: 'select_account' });
});

it('rejects malformed server configuration without creating an authentication client', async () => {
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ ...configuration, scope: 'https://graph.microsoft.com/.default' })));
  const { initializeApiIdentity } = await import('./apiIdentity');
  await expect(initializeApiIdentity('user@example.test')).rejects.toThrow('configuration is invalid');
  expect(mocks.constructor).not.toHaveBeenCalled();
});

it('does not start silent SSO without an account or login hint', async () => {
  mocks.getActiveAccount.mockReturnValue(null); mocks.getAllAccounts.mockReturnValue([]);
  const { initializeApiIdentity, ApiIdentityRequiredError } = await import('./apiIdentity');
  await expect(initializeApiIdentity('')).rejects.toBeInstanceOf(ApiIdentityRequiredError);
  expect(mocks.ssoSilent).not.toHaveBeenCalled();
});

it('uses the same approved API scope for explicit same-window sign-in', async () => {
  const { redirectApiIdentity } = await import('./apiIdentity');
  await redirectApiIdentity('');
  expect(mocks.loginRedirect).toHaveBeenCalledWith({ scopes: [configuration.scope], loginHint: undefined, prompt: 'select_account' });
});

it('does not expose an in-app consent acquisition API', async () => {
  const identity = await import('./apiIdentity');
  expect(identity).not.toHaveProperty('authorizeAzureAccess');
  expect(mocks.acquireTokenRedirect).not.toHaveBeenCalled();
  expect(mocks.loginRedirect).not.toHaveBeenCalled();
  expect(mocks.logoutRedirect).not.toHaveBeenCalled();
  expect(mocks.clearCache).not.toHaveBeenCalled();
});

it.each([
  { name: 'permission denied', status: 403, code: 'azure_managed_identity_forbidden', claims: undefined },
  { name: 'identity unavailable', status: 503, code: 'azure_managed_identity_unavailable', claims: undefined },
  { name: 'denied with unrelated claims', status: 403, code: 'azure_managed_identity_forbidden', claims: JSON.stringify({ access_token: { acrs: { value: 'c1' } } }) },
  { name: 'unavailable with unrelated claims', status: 503, code: 'azure_managed_identity_unavailable', claims: JSON.stringify({ access_token: { acrs: { value: 'c1' } } }) },
])('preserves user sign-in after a backend identity failure: $name', async ({ status, code, claims }) => {
  vi.mocked(fetch).mockImplementation(async (path) => {
    if (path === '/api/auth/config') return new Response(JSON.stringify(configuration));
    if (path === '/api/schedules') return new Response(JSON.stringify({ detail: { code, message: 'Verify the backend managed identity.' } }), {
      status, headers: claims ? { 'WWW-Authenticate': `Bearer error="insufficient_claims", claims="${btoa(claims)}"` } : {},
    });
    return new Response(JSON.stringify(profile));
  });
  const { apiFetch, initializeApiIdentity, redirectApiIdentity, IDENTITY_REQUIRED_EVENT } = await import('./apiIdentity');
  const requireIdentity = vi.fn();
  window.addEventListener(IDENTITY_REQUIRED_EVENT, requireIdentity);
  try {
    const response = await apiFetch('/api/schedules');
    expect(response.status).toBe(status);
    expect((await response.json()).detail.code).toBe(code);
    await expect(initializeApiIdentity('')).resolves.toEqual(profile);
    expect(mocks.acquireTokenSilent).toHaveBeenLastCalledWith({ account, scopes: [configuration.scope] });
    expect(requireIdentity).not.toHaveBeenCalled();
    expect(mocks.acquireTokenRedirect).not.toHaveBeenCalled();
    expect(mocks.loginRedirect).not.toHaveBeenCalled();
    expect(mocks.clearCache).not.toHaveBeenCalled();
    await redirectApiIdentity('');
    expect(mocks.loginRedirect).toHaveBeenCalledWith({ scopes: [configuration.scope], loginHint: undefined, prompt: 'select_account' });
    expect(mocks.acquireTokenRedirect).not.toHaveBeenCalled();
    expect(vi.mocked(fetch).mock.calls.filter(([path]) => path === '/api/schedules')).toHaveLength(1);
  } finally {
    window.removeEventListener(IDENTITY_REQUIRED_EVENT, requireIdentity);
  }
});

it.each([
  { name: 'ordinary denial', code: 'azure_access_denied', claims: JSON.stringify({ access_token: { acrs: { value: 'c1' } } }) },
  { name: 'malformed claims', code: 'azure_managed_identity_forbidden', claims: '{}' },
])('does not forward upstream claims after $name', async ({ code, claims }) => {
  vi.mocked(fetch).mockImplementation(async (path) => path === '/api/auth/config'
    ? new Response(JSON.stringify(configuration))
    : new Response(JSON.stringify({ detail: { code } }), { status: 403, headers: { 'WWW-Authenticate': `Bearer error="insufficient_claims", claims="${btoa(claims)}"` } }));
  const { apiFetch, redirectApiIdentity } = await import('./apiIdentity');
  const response = await apiFetch('/api/schedules');
  expect(response.status).toBe(403);
  expect((await response.json()).detail.code).toBe(code);
  await redirectApiIdentity('');
  expect(mocks.loginRedirect).toHaveBeenCalledWith({ scopes: [configuration.scope], loginHint: undefined, prompt: 'select_account' });
  expect(mocks.acquireTokenRedirect).not.toHaveBeenCalled();
});

it('carries a claims challenge into explicit sign-in without replaying the failed request', async () => {
  const claims = JSON.stringify({ access_token: { acrs: { essential: true, value: 'c1' } } });
  vi.mocked(fetch).mockImplementation(async (path) => path === '/api/auth/config'
    ? new Response(JSON.stringify(configuration))
    : new Response('{}', { status: 401, headers: { 'WWW-Authenticate': `Bearer error="insufficient_claims", claims="${btoa(claims)}"` } }));
  const { apiFetch, redirectApiIdentity } = await import('./apiIdentity');
  const response = await apiFetch('/api/schedules', { method: 'POST', body: '{}' });
  expect(response.status).toBe(401);
  expect(vi.mocked(fetch).mock.calls.filter(([path]) => path === '/api/schedules')).toHaveLength(1);
  expect(mocks.loginRedirect).not.toHaveBeenCalled();
  await redirectApiIdentity('user@example.test');
  expect(mocks.loginRedirect).toHaveBeenCalledWith(expect.objectContaining({ claims, scopes: [configuration.scope] }));
  await apiFetch('/api/auth/me');
  expect(mocks.acquireTokenSilent).toHaveBeenLastCalledWith({ account, scopes: [configuration.scope], forceRefresh: true, claims });
});

it('rejects token/account substitution before sending an API request', async () => {
  const { apiFetch, ApiIdentityRequiredError } = await import('./apiIdentity');
  mocks.acquireTokenSilent.mockResolvedValue({ accessToken: 'other-user-token', account: { ...account, homeAccountId: 'account-two' } });
  await expect(apiFetch('/api/auth/me')).rejects.toBeInstanceOf(ApiIdentityRequiredError);
  expect(vi.mocked(fetch).mock.calls.filter(([path]) => path !== '/api/auth/config')).toHaveLength(0);
});

it('rejects a foreign tenant profile and foreign-tenant login result', async () => {
  const { initializeApiIdentity, connectApiIdentity } = await import('./apiIdentity');
  vi.mocked(fetch).mockImplementation(async (path) => new Response(JSON.stringify(path === '/api/auth/config'
    ? configuration : { ...profile, tenantId: configuration.apiClientId })));
  await expect(initializeApiIdentity('')).rejects.toThrow('invalid identity profile');
  mocks.loginPopup.mockResolvedValue({ account: { ...account, tenantId: configuration.apiClientId } });
  await expect(connectApiIdentity('user@example.test')).rejects.toThrow();
  expect(mocks.setActiveAccount).toHaveBeenCalledTimes(1);
});

it('uses MSAL logout rather than a Static Web Apps endpoint', async () => {
  const { signOutApiIdentity } = await import('./apiIdentity');
  await signOutApiIdentity();
  expect(mocks.clearCache).toHaveBeenCalledOnce();
  expect(mocks.logoutRedirect).toHaveBeenCalledWith({ account, postLogoutRedirectUri: window.location.origin });
  expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).includes('/.auth/'))).toBe(false);
});

it('does not transfer a pending challenge to another account', async () => {
  const claims = JSON.stringify({ access_token: { acrs: { value: 'c1' } } });
  const { apiFetch, redirectApiIdentity } = await import('./apiIdentity');
  vi.mocked(fetch).mockImplementation(async (path) => path === '/api/auth/config'
    ? new Response(JSON.stringify(configuration))
    : new Response('{}', { status: 401, headers: { 'WWW-Authenticate': `Bearer error="insufficient_claims", claims="${btoa(claims)}"` } }));
  await apiFetch('/api/auth/me');
  mocks.getActiveAccount.mockReturnValue({ ...account, homeAccountId: 'account-two' });
  await redirectApiIdentity('other@example.test');
  expect(mocks.loginRedirect).toHaveBeenCalledWith({ scopes: [configuration.scope], loginHint: 'other@example.test', prompt: 'select_account' });
});