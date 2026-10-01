// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SubscriptionPicker } from './SubscriptionPicker';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

const props = {
  subscriptions: [{ subscriptionId: 'sub-1', displayName: 'Production' }],
  selectedIds: new Set(['sub-1']),
  loading: false,
  running: false,
  runningLabel: 'Collecting Azure data',
  hasReport: true,
  periodLabel: null,
  scopeChanged: false,
  error: null,
  onToggle: vi.fn(),
  onSelectAll: vi.fn(),
  onClearAll: vi.fn(),
  onRun: vi.fn(),
};

it('shows what it is given in the same row, between the subscription picker and the run controls', async () => {
  await act(async () => root.render(<SubscriptionPicker {...props}><span data-testid="window">Month 7d</span></SubscriptionPicker>));
  const row = container.querySelector('.scope-ribbon-main')!;
  const order = [...row.children].map((child) => child.className.split(' ')[0]);
  expect(order.indexOf('subscription-select')).toBeLessThan(order.indexOf('scope-range'));
  expect(order.indexOf('scope-range')).toBeLessThan(order.indexOf('scope-run-area'));
  expect(row.querySelector('.scope-range [data-testid="window"]')).not.toBeNull();
});

it('adds no extra cell when it is given nothing', async () => {
  await act(async () => root.render(<SubscriptionPicker {...props} hasReport={false} />));
  expect(container.querySelector('.scope-range')).toBeNull();
});
