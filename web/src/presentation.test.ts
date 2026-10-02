// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { installTruncationTitles } from './presentation';

function clipped(text: string, scrollWidth: number, clientWidth: number) {
  const el = document.createElement('span');
  el.textContent = text;
  el.style.textOverflow = 'ellipsis';
  Object.defineProperty(el, 'scrollWidth', { value: scrollWidth, configurable: true });
  Object.defineProperty(el, 'clientWidth', { value: clientWidth, configurable: true });
  document.body.append(el);
  return el;
}

describe('installTruncationTitles', () => {
  let uninstall = () => {};
  afterEach(() => {
    uninstall();
    document.body.innerHTML = '';
  });

  it('gives a clipped element its full text as a title', () => {
    uninstall = installTruncationTitles();
    const el = clipped('  rg-production-eastus-analytics-platform  ', 400, 120);
    el.dispatchEvent(new Event('pointerover', { bubbles: true }));
    expect(el.title).toBe('rg-production-eastus-analytics-platform');
  });

  it('leaves an element that fits alone', () => {
    uninstall = installTruncationTitles();
    const el = clipped('short', 50, 120);
    el.dispatchEvent(new Event('pointerover', { bubbles: true }));
    expect(el.hasAttribute('title')).toBe(false);
  });

  it('never replaces a title a component set itself', () => {
    uninstall = installTruncationTitles();
    const el = clipped('long name', 400, 120);
    el.title = 'Set by the component';
    el.dispatchEvent(new Event('pointerover', { bubbles: true }));
    expect(el.title).toBe('Set by the component');
  });

  it('removes its title once the element is no longer clipped', () => {
    uninstall = installTruncationTitles();
    const el = clipped('name', 400, 120);
    el.dispatchEvent(new Event('pointerover', { bubbles: true }));
    Object.defineProperty(el, 'scrollWidth', { value: 50 });
    el.dispatchEvent(new Event('pointerover', { bubbles: true }));
    expect(el.hasAttribute('title')).toBe(false);
  });
});
