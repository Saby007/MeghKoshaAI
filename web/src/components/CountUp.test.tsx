// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CountUp } from './CountUp';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const money = (value: number) => `$${value.toLocaleString('en-US')}`;

describe('CountUp', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function show(value: number) {
    act(() => root.render(<CountUp value={value} format={money} />));
  }

  it('shows the final value straight away when motion is reduced', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    show(12480);
    expect(container.textContent).toBe('$12,480');
  });

  it('shows the final value when the preference cannot be read', () => {
    vi.stubGlobal('matchMedia', undefined);
    show(12480);
    expect(container.textContent).toBe('$12,480');
  });

  it('starts at zero, then settles exactly on the value', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }));
    vi.useFakeTimers();
    let now = 0;
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(now), 16) as unknown as number);
    vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id));

    show(1000);
    expect(container.textContent).toBe('$0');

    // The clock starts on the first frame, however late it arrives.
    act(() => { now = 5000; vi.advanceTimersByTime(16); });
    expect(container.textContent).toBe('$0');

    act(() => { now = 5350; vi.advanceTimersByTime(16); });
    const midway = Number((container.textContent ?? '').replace(/[^0-9]/g, ''));
    expect(midway).toBeGreaterThan(0);
    expect(midway).toBeLessThan(1000);

    act(() => { now = 5800; vi.advanceTimersByTime(16); });
    expect(container.textContent).toBe('$1,000');
  });
});
