import { expect, it, vi } from 'vitest';
import type { CostGroup, CostWindow } from './costDetails';
import { createReportDataReader, type CostQueryBody, type CostTransport } from './postgresData';

const window: CostWindow = { startDate: '2026-08-01', endDate: '2026-08-31' };
const metrics = {
  total: 15000, currentTotal: 125000, previousTotal: 120000, largest: 1000,
  spikeCount: 175, subscriptionCount: 5, observedCurrentTotal: 125000,
};
const group = (index: number): CostGroup => ({
  id: `vm-${index}`, name: `VM ${index}`, subscriptionId: 'sub', subscriptionName: 'Synthetic',
  current: 1, previous: 2, delta: -1, percentage: -50, sources: [], owners: [], sourceCount: 1,
});
const transport = (resolve: (body: CostQueryBody, signal: AbortSignal) => unknown | Promise<unknown>) => {
  const request = vi.fn(resolve);
  const send: CostTransport = async <T>(body: CostQueryBody, signal: AbortSignal) => await request(body, signal) as T;
  return { send, request };
};
async function settled<T>(read: () => T): Promise<T> {
  for (let attempt = 0; attempt < 20; attempt++) {
    try { return read(); } catch (error) {
      if (!(error instanceof Promise)) throw error;
      await error;
    }
  }
  throw new Error('Projection did not settle within the test request budget.');
}

it('preserves complete financial totals while only fetching the visible original-table prefix', async () => {
  const rpc = transport(body => ({
    ...metrics, items: Array.from({ length: 50 }, (_, index) => group(Number(body.offset) + index)),
    nextOffset: Number(body.offset) + 50,
  }));
  const reader = createReportDataReader([], rpc.send);
  const result = await settled(() => reader.groups(window, {}, 'resource', undefined, { limit: 75 }));
  expect(result.items).toHaveLength(75);
  expect(result.currentTotal).toBe(125000);
  expect(result.total).toBe(15000);
  expect(result.nextOffset).toBe(75);
  expect(rpc.request.mock.calls.map(([body]) => [body.offset, body.limit])).toEqual([[0, 50], [50, 50]]);
  expect(reader.groups(window, {}, 'resource', undefined, { limit: 50 }).items).toHaveLength(50);
  expect(rpc.request).toHaveBeenCalledTimes(2);
});

it('keeps the original source scope through nested group and chart queries', async () => {
  const rpc = transport(() => ({ series: [] }));
  const reader = createReportDataReader([], rpc.send);
  await settled(() => reader.withFilters({ subscriptionId: 'sub' })
    .forGroup('resourceGroup', 'sub/finance').daily(window, { serviceName: 'SQL Database' }, true));
  expect(rpc.request.mock.calls[0][0]).toMatchObject({
    constraints: [{ subscriptionId: 'sub' }],
    groupConstraints: [{ dimension: 'resourceGroup', sourceId: 'sub/finance' }],
    filters: { serviceName: 'SQL Database' }, observedOnly: true, kind: 'daily',
  });
});

it('retains complete record statistics and collection context independently of visible rows', async () => {
  const rpc = transport(body => ({
    items: Array.from({ length: 50 }, (_, index) => ({
      path: 'domains.sql.categories.0.lines', value: { name: `sql-${Number(body.offset) + index}` },
      context: { category: 'Idle SQL', domain: 'sql' },
    })),
    total: 15000, unfilteredTotal: 18000, sums: { saving: 45000 }, facets: {},
    nextOffset: Number(body.offset) + 50,
  }));
  const reader = createReportDataReader([{ path: 'domains.sql.categories.0.lines', total: 18000 }], rpc.send);
  const result = await settled(() => reader.records<{ name: string }>(
    ['domains.sql.categories.0.lines'], { search: 'sql', sums: { saving: { field: 'monthlySaving' } } }, 75,
  ));
  expect(result.items).toHaveLength(75);
  expect(result.items[0].context.domain).toBe('sql');
  expect(result.sums.saving).toBe(45000);
  expect(result.total).toBe(15000);
  expect(reader.total('domains.sql.categories.0.lines')).toBe(18000);
  expect(rpc.request).toHaveBeenCalledTimes(2);
});

it('rejects non-advancing pagination instead of silently showing incomplete evidence', async () => {
  const rpc = transport(() => ({ ...metrics, items: [group(0)], nextOffset: 0 }));
  const reader = createReportDataReader([], rpc.send);
  await expect(settled(() => reader.groups(window, {}, 'resource'))).rejects.toThrow('did not advance');
});

it('surfaces projection failures without substituting zero totals or empty records', async () => {
  const rpc = transport(() => Promise.reject(new Error('SQL serving is unavailable')));
  const reader = createReportDataReader([], rpc.send);
  await expect(settled(() => reader.metrics(window, {}))).rejects.toThrow('SQL serving is unavailable');
  expect(rpc.request).toHaveBeenCalledTimes(1);
});

it('aborts closed report queries and ignores late responses', async () => {
  let complete: ((value: unknown) => void) | undefined;
  const rpc = transport(() => new Promise(resolve => { complete = resolve; }));
  const reader = createReportDataReader([], rpc.send);
  let pending: Promise<unknown> | undefined;
  try { reader.metrics(window, {}); } catch (error) {
    if (!(error instanceof Promise)) throw error;
    pending = error;
  }
  reader.dispose();
  expect(rpc.request.mock.calls[0][1].aborted).toBe(true);
  complete?.(metrics);
  await pending;
  expect(() => reader.metrics(window, {})).toThrow('has been closed');
  expect(JSON.stringify({ remote: reader })).toBe('{}');
});
