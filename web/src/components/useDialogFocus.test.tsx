// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useDialogFocus } from './useDialogFocus';

let container: HTMLDivElement;
let trigger: HTMLButtonElement;
let root: Root;

function Harness({ onClose, empty = false }: { onClose: () => void; empty?: boolean }) {
  const ref = useDialogFocus(onClose);
  return <div className="remediation-backdrop"><section role="dialog" tabIndex={-1} ref={ref}>
    {!empty && <><button disabled>Unavailable</button><input aria-label="First" /><button hidden>Hidden</button>
      <button data-not-visible>Not rendered</button><button tabIndex={-2}>Skipped</button><button>Last</button></>}
  </section></div>;
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement): DOMRectList {
    const visible = !this.hasAttribute('data-not-visible');
    const rect = new DOMRect(0, 0, 100, 30);
    return { 0: rect, length: visible ? 1 : 0, item: index => visible && index === 0 ? rect : null,
      [Symbol.iterator]: () => (visible ? [rect] : []).values() };
  });
  trigger = document.createElement('button');
  trigger.textContent = 'Open dialog';
  container = document.createElement('div');
  document.body.append(trigger, container);
  trigger.focus();
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  trigger.remove(); container.remove();
  document.body.style.overflow = '';
  vi.restoreAllMocks();
});

it('skips unavailable controls, traps Tab both ways and redirects background focus', async () => {
  await act(async () => root.render(<Harness onClose={vi.fn()} />));
  const first = container.querySelector('input')!;
  const buttons = container.querySelectorAll('button');
  const last = buttons[buttons.length - 1];
  expect(document.activeElement).toBe(first);
  expect(trigger.hasAttribute('inert')).toBe(true);
  await act(async () => first.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true })));
  expect(document.activeElement).toBe(last);
  await act(async () => last.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })));
  expect(document.activeElement).toBe(first);
  await act(async () => trigger.focus());
  expect(document.activeElement).toBe(first);
});

it('retains focus on rerender and closes through the latest callback', async () => {
  const oldClose = vi.fn();
  const nextClose = vi.fn();
  await act(async () => root.render(<Harness onClose={oldClose} />));
  const buttons = container.querySelectorAll('button');
  const last = buttons[buttons.length - 1];
  await act(async () => last.focus());
  await act(async () => root.render(<Harness onClose={nextClose} />));
  expect(document.activeElement).toBe(last);
  await act(async () => last.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(nextClose).toHaveBeenCalledOnce();
  expect(oldClose).not.toHaveBeenCalled();
});

it('restores the trigger, existing inert state and the previous scroll setting', async () => {
  const alreadyInert = document.createElement('div');
  alreadyInert.setAttribute('inert', 'keep');
  document.body.append(alreadyInert);
  document.body.style.overflow = 'scroll';
  try {
    await act(async () => root.render(<Harness onClose={vi.fn()} />));
    expect(document.body.style.overflow).toBe('hidden');
    await act(async () => root.render(null));
    expect(document.activeElement).toBe(trigger);
    expect(trigger.hasAttribute('inert')).toBe(false);
    expect(alreadyInert.getAttribute('inert')).toBe('keep');
    expect(document.body.style.overflow).toBe('scroll');
  } finally {
    alreadyInert.remove();
  }
});

it('focuses an empty dialog and uses the page heading if the trigger disappears', async () => {
  const heading = document.createElement('h1');
  heading.id = 'report-page-heading';
  heading.tabIndex = -1;
  document.body.append(heading);
  try {
    await act(async () => root.render(<Harness onClose={vi.fn()} empty />));
    const dialog = container.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(document.activeElement).toBe(dialog);
    await act(async () => dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })));
    expect(document.activeElement).toBe(dialog);
    trigger.remove();
    await act(async () => root.render(null));
    expect(document.activeElement).toBe(heading);
  } finally {
    heading.remove();
  }
});
