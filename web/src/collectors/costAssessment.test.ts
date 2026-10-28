// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { parseAssessmentJob } from '../api';
import { reportFixture } from '../report/testFixtures';
import { runCostAssessment } from './costAssessment';

vi.mock('../apiIdentity', () => ({ apiFetch: (...args: Parameters<typeof fetch>) => fetch(...args) }));
const jobId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const job = { jobId, status: 'queued', snapshotId: null, error: null };
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([null, {}, { ...job, jobId: '-'.repeat(36) }, { ...job, status: 'invented' }, { ...job, error: {} }])(
  'rejects invalid background protocol data: %o', value => {
    expect(() => parseAssessmentJob(value)).toThrow('invalid job');
  },
);

it('loads the exact completed SQL snapshot through the existing assessment action', async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(job), { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...job, status: 'succeeded', snapshotId: 'exact-snapshot' })))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      snapshotId: 'exact-snapshot', servingVersion: 1, collections: [], report: reportFixture,
    })));
  vi.stubGlobal('fetch', fetchMock);
  const result = runCostAssessment(['sub-1'], 90);
  await vi.advanceTimersByTimeAsync(2001);
  const report = await result;
  expect(report.executiveSummary).toEqual(reportFixture.executiveSummary);
  expect(report.remote).toBeDefined();
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    '/api/report', `/api/assessments/jobs/${jobId}`, '/api/assessments/snapshots/exact-snapshot',
  ]);
});

it('surfaces a worker failure rather than returning a partial report', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    ...job, status: 'failed', error: 'The worker memory budget was exceeded.',
  }), { status: 202 })));
  await expect(runCostAssessment(['sub-1'], 90)).rejects.toThrow('memory budget');
});

it('rejects metadata from a pre-SQL-serving snapshot rather than rendering empty resources', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...job, status: 'succeeded', snapshotId: 'old' }), { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ snapshotId: 'old', collections: [], report: reportFixture }))));
  await expect(runCostAssessment(['sub-1'], 90)).rejects.toThrow('predates SQL dashboard serving');
});

it('stops browser polling on cancellation while the durable server job remains queued', async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(job), { status: 202 }));
  vi.stubGlobal('fetch', fetchMock);
  const result = runCostAssessment(['sub-1'], 90, controller.signal);
  const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
  await vi.advanceTimersByTimeAsync(1);
  controller.abort();
  await rejected;
  await vi.advanceTimersByTimeAsync(3000);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
