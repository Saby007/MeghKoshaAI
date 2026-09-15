// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiRequestError, getFinOpsActions, updateFinOpsAction } from '../api';
import type { FinOpsActionState } from '../report/models';
import { ActionPlanTab } from './ReportView';

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  getFinOpsActions: vi.fn(),
  updateFinOpsAction: vi.fn(),
}));

const report = { actionPlan: [{ actionId: 'disk', action: 'Review disk', savingMonth: 20, prerequisite: 'Owner approval', affectedSubscriptions: [] }] };
const savedState: FinOpsActionState = {
  actionId: 'disk', scopeHash: 'scope-1', status: 'open', owner: 'Saved owner', dueDate: null, completedAt: null,
  realizedSavingMonth: null, note: 'Saved note', updatedAt: '2026-09-07T00:00:00Z', updatedBy: 'test', version: 3,
};
let container: HTMLDivElement;
let root: Root;
const view = (snapshotId = 'snapshot-1') => <ActionPlanTab report={report} snapshotId={snapshotId} formatMoney={value => `$${value}`} displayCurrency="USD" />;
const input = (label: string) => container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.includes(text))!;

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Value>((resolveValue, rejectValue) => { resolve = resolveValue; reject = rejectValue; });
  return { promise, resolve, reject };
}

async function editOwner(value: string) {
  await act(async () => {
    const field = input('Owner for Review disk');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); });

it('does not allow edits until saved versions load, including a failed load', async () => {
  const pending = deferred<FinOpsActionState[]>();
  vi.mocked(getFinOpsActions).mockReturnValue(pending.promise);
  await act(async () => root.render(view()));
  expect(input('Owner for Review disk').disabled).toBe(true);
  expect(button('Save Review disk').disabled).toBe(true);
  await act(async () => pending.reject(new Error('Storage unavailable')));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Storage unavailable');
  expect(button('Save Review disk').disabled).toBe(true);
});

it('submits the observed version and advances it only after a confirmed save', async () => {
  vi.mocked(getFinOpsActions).mockResolvedValue([savedState]);
  vi.mocked(updateFinOpsAction).mockResolvedValue({ ...savedState, owner: 'New owner', version: 4 });
  await act(async () => root.render(view()));
  await editOwner('New owner');
  await act(async () => button('Save Review disk').click());
  expect(updateFinOpsAction).toHaveBeenLastCalledWith('snapshot-1', 'disk', expect.objectContaining({ expectedVersion: 3, owner: 'New owner' }));
  expect(container.textContent).toContain('Action saved.');
  await act(async () => button('Save Review disk').click());
  expect(updateFinOpsAction).toHaveBeenLastCalledWith('snapshot-1', 'disk', expect.objectContaining({ expectedVersion: 4 }));
});

it('preserves a conflicting draft and requires explicit confirmation before reload', async () => {
  vi.mocked(getFinOpsActions).mockResolvedValueOnce([savedState]).mockResolvedValue([{ ...savedState, owner: 'Other editor', version: 4 }]);
  vi.mocked(updateFinOpsAction).mockRejectedValue(new ApiRequestError('This action changed. Reload saved actions before saving again.', 409));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
  await act(async () => root.render(view()));
  await editOwner('My unsaved edit');
  await act(async () => button('Save Review disk').click());
  expect(input('Owner for Review disk').value).toBe('My unsaved edit');
  expect(button('Save Review disk').disabled).toBe(true);
  expect(container.textContent).not.toContain('Action saved.');
  await act(async () => button('Reload saved actions').click());
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(getFinOpsActions).toHaveBeenCalledTimes(1);
  expect(input('Owner for Review disk').value).toBe('My unsaved edit');
  await act(async () => button('Reload saved actions').click());
  expect(input('Owner for Review disk').value).toBe('Other editor');
  expect(button('Save Review disk').disabled).toBe(false);
});

it('ignores a late load after switching the selected report', async () => {
  const pending = deferred<FinOpsActionState[]>();
  vi.mocked(getFinOpsActions).mockReturnValueOnce(pending.promise).mockResolvedValue([{ ...savedState, owner: 'Current report' }]);
  await act(async () => root.render(view()));
  const signal = vi.mocked(getFinOpsActions).mock.calls[0][1]!;
  await act(async () => root.render(view('snapshot-2')));
  expect(signal.aborted).toBe(true);
  await act(async () => pending.resolve([{ ...savedState, owner: 'Old report' }]));
  expect(input('Owner for Review disk').value).toBe('Current report');
});

it('ignores a late save after switching reports and prevents duplicate pending saves', async () => {
  const pending = deferred<FinOpsActionState>();
  vi.mocked(getFinOpsActions).mockResolvedValueOnce([savedState]).mockResolvedValue([{ ...savedState, owner: 'Current report' }]);
  vi.mocked(updateFinOpsAction).mockReturnValue(pending.promise);
  await act(async () => root.render(view()));
  await act(async () => { button('Save Review disk').click(); button('Save Review disk').click(); });
  expect(updateFinOpsAction).toHaveBeenCalledTimes(1);
  expect(input('Owner for Review disk').disabled).toBe(true);
  await act(async () => root.render(view('snapshot-2')));
  await act(async () => pending.resolve({ ...savedState, owner: 'Old result', version: 4 }));
  expect(input('Owner for Review disk').value).toBe('Current report');
  expect(container.textContent).not.toContain('Action saved.');
});

it('uses version zero for legacy state and unsaved actions', async () => {
  vi.mocked(getFinOpsActions).mockResolvedValue([{ ...savedState, version: undefined }]);
  vi.mocked(updateFinOpsAction).mockResolvedValue({ ...savedState, version: 1 });
  await act(async () => root.render(view()));
  await act(async () => button('Save Review disk').click());
  expect(updateFinOpsAction).toHaveBeenCalledWith('snapshot-1', 'disk', expect.objectContaining({ expectedVersion: 0 }));
});