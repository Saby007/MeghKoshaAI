export const IDENTITY_REQUIRED_EVENT = 'mkai-identity-required';
export type VerifiedIdentity = {
  userId: string; userDetails: string; tenantId: string;
  features?: { aiNarration?: boolean; assessmentMode?: 'postgres'; manualFocus?: boolean };
};
type WorkspaceSession = VerifiedIdentity & { csrfToken: string };
let pendingSession: Promise<VerifiedIdentity> | null = null;
let csrfToken: string | null = null;

export class ApiIdentityRequiredError extends Error {
  constructor() {
    super('Your report workspace expired. Reload the application and upload your report again.');
    this.name = 'ApiIdentityRequiredError';
  }
}

export function initializeApiIdentity(_loginHint = ''): Promise<VerifiedIdentity> {
  if (pendingSession) return pendingSession;
  pendingSession = (async () => {
    const response = await fetch('/api/session', {
      credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error('Report workspace storage is unavailable. Retry or contact the deployment operator.');
    const session: WorkspaceSession = await response.json();
    const uuid = /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i;
    if (!uuid.test(session.userId) || session.tenantId !== session.userId
      || typeof session.csrfToken !== 'string' || !/^[\da-f]{64}$/.test(session.csrfToken)) {
      throw new Error('The server returned an invalid report workspace.');
    }
    csrfToken = session.csrfToken;
    return { userId: session.userId, tenantId: session.tenantId,
      userDetails: session.userDetails, features: session.features };
  })().finally(() => { pendingSession = null; });
  return pendingSession;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const target = new URL(path, window.location.origin);
  if (target.origin !== window.location.origin || !target.pathname.startsWith('/api/') || target.username || target.password) {
    throw new Error('Workspace requests can only be sent to this application API.');
  }
  if (init?.signal?.aborted) throw new DOMException('Request aborted', 'AbortError');
  if (!csrfToken) await initializeApiIdentity();
  if (init?.signal?.aborted) throw new DOMException('Request aborted', 'AbortError');
  const headers = new Headers(init?.headers);
  headers.delete('Authorization');
  headers.delete('x-ms-client-principal');
  headers.delete('x-meghkosha-user-token');
  if (!['GET', 'HEAD', 'OPTIONS'].includes((init?.method ?? 'GET').toUpperCase()) && csrfToken) {
    headers.set('X-CSRF-Token', csrfToken);
  }
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
  if (response.status === 401) {
    csrfToken = null;
    window.dispatchEvent(new Event(IDENTITY_REQUIRED_EVENT));
  }
  return response;
}

export async function completeIdentityRedirect(): Promise<void> {
  window.location.replace('/');
}
