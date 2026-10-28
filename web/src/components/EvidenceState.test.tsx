// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { EvidenceState } from './EvidenceState';

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('shows meaningful loading text with a decorative skeleton and disabled actions', async () => {
  const retry = vi.fn();
  await act(async () => root.render(<EvidenceState loading title="Checking evidence" detail="Reading the selected scope."
    action={{ label: 'Retry', onClick: retry }} />));
  expect(container.querySelector('.evidence-state')?.getAttribute('aria-busy')).toBe('true');
  expect(container.querySelector('[role="status"]')?.textContent).toContain('Reading the selected scope.');
  expect(container.querySelector('.evidence-skeleton')?.getAttribute('aria-hidden')).toBe('true');
  const button = container.querySelector('button')!;
  expect(button.disabled).toBe(true);
  await act(async () => button.click());
  expect(retry).not.toHaveBeenCalled();
});

it('announces errors separately from the action and never submits a containing form', async () => {
  const retry = vi.fn();
  await act(async () => root.render(<form><EvidenceState tone="error" title="Evidence unavailable" detail="Lookup denied."
    action={{ label: 'Retry lookup', onClick: retry }} /></form>));
  expect(container.querySelector('[role="alert"]')?.textContent).toBe('Evidence unavailableLookup denied.');
  expect(container.querySelector('[role="alert"] button')).toBeNull();
  expect(container.querySelector('button')?.type).toBe('button');
  await act(async () => container.querySelector('button')!.click());
  expect(retry).toHaveBeenCalledOnce();
  expect(container.querySelector('.evidence-skeleton')).toBeNull();
});

it('keeps a caller-disabled recovery action disabled', async () => {
  await act(async () => root.render(<EvidenceState title="No evidence" detail="Choose another scope."
    action={{ label: 'Reset', onClick: vi.fn(), disabled: true }} />));
  expect(container.querySelector('button')?.disabled).toBe(true);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});
