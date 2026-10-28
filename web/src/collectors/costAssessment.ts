import type { FullReport } from '../report/models';
import { apiFetch } from '../apiIdentity';
import { getSavedAssessment, parseAssessmentJob, waitForBackgroundPoll } from '../api';

export type StaleDays = 7 | 14 | 30 | 60 | 90 | 180 | 365;

/**
 * Calls the same-origin /api/report instead of querying ARM directly from the browser - see
 * api/services/arm_client.py and api/reports/builder.py. The backend runs under
 * the Container App's own managed identity (granted Reader + Cost Management
 * Reader on the subscription), avoiding the ARM `user_impersonation` delegated
 * scope, which this tenant's consent policy blocks from self-service user consent.
 */
export async function runCostAssessment(subscriptionIds: string[], staleDays: StaleDays, signal?: AbortSignal): Promise<FullReport> {
  const res = await apiFetch('/api/report', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscriptionIds, staleDays }),
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const message = typeof body?.detail === 'string' ? body.detail : body?.detail?.message;
    throw new Error(message ?? `Assessment failed: ${res.status}`);
  }
  if (res.status === 202) {
    let job = parseAssessmentJob(await res.json());
    const jobId = job.jobId;
    const deadline = Date.now() + 60 * 60 * 1000;
    while (true) {
      if (job.status === 'succeeded') {
        if (!job.snapshotId) throw new Error('Assessment completed without a published snapshot.');
        return (await getSavedAssessment(job.snapshotId, signal)).report;
      }
      if (job.status === 'failed') throw new Error(job.error || 'Assessment failed. Review worker diagnostics.');
      if (job.status === 'cancelled') throw new Error('Assessment was cancelled.');
      if (Date.now() >= deadline) throw new Error('Stopped waiting for the background assessment. The job remains durable; reopen the report to check it.');
      await waitForBackgroundPoll(2000, signal);
      const progress = await apiFetch(`/api/assessments/jobs/${encodeURIComponent(job.jobId)}`, {
        credentials: 'same-origin', cache: 'no-store',
        ...(signal ? { signal } : {}),
      });
      if (!progress.ok) {
        const body = await progress.json().catch(() => null);
        throw new Error(typeof body?.detail === 'string' ? body.detail : `Assessment status unavailable: ${progress.status}`);
      }
      job = parseAssessmentJob(await progress.json());
      if (job.jobId !== jobId) throw new Error('Assessment status returned a different background job.');
    }
  }
  return res.json();
}
