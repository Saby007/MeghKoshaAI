import type { FullReport } from '../report/models';
import { apiFetch } from '../apiIdentity';

export type StaleDays = 7 | 14 | 30 | 60 | 90 | 180 | 365;

/**
 * Calls the backend's /api/report (same-origin, via the Static Web App's linked
 * Container App backend) instead of querying ARM directly from the browser - see
 * api/services/arm_client.py and api/reports/builder.py. The backend runs under
 * the Container App's own managed identity (granted Reader + Cost Management
 * Reader on the subscription), avoiding the ARM `user_impersonation` delegated
 * scope, which this tenant's consent policy blocks from self-service user consent.
 */
export async function runCostAssessment(subscriptionIds: string[], staleDays: StaleDays): Promise<FullReport> {
  const res = await apiFetch('/api/report', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscriptionIds, staleDays }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const message = typeof body?.detail === 'string' ? body.detail : body?.detail?.message;
    throw new Error(message ?? `Assessment failed: ${res.status}`);
  }
  return res.json();
}
