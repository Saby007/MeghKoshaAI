// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { chartTooltipPlacement, DailyBarChart, DailyTrendChart } from './TrendChart';

const format = (value: number) => `$${value.toFixed(2)}`;
let container: HTMLDivElement;
let root: Root;

function days(count: number, from = Date.UTC(2026, 7, 9)) {
  return Array.from({ length: count }, (_, index) => new Date(from + index * 86400000).toISOString().slice(0, 10));
}

const axisLabels = () => Array.from(container.querySelectorAll('svg text'))
  .filter((node) => /^\d{2}-\d{2}$/.test(node.textContent ?? ''))
  .map((node) => node.textContent);

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

/* The axis used to draw one label every ceil(n / 8) days, so a 30-day window
   named only eight of its days while offering all thirty as controls. */
it('names every day on the bar axis and tilts the labels rather than dropping any', async () => {
  const dates = days(30);
  await act(async () => root.render(
    <DailyBarChart dates={dates} values={dates.map(() => 2)} formatMoney={format} seriesName="Cost per hour" />,
  ));
  expect(axisLabels()).toEqual(dates.map((date) => date.slice(5)));
  expect(container.querySelectorAll('svg text[transform^="rotate(-60"]').length).toBe(30);
});

/* Past the point where even tilted labels collide, the chart widens and the
   surrounding container scrolls - dropping a day is never the answer. */
it('grows the plot past the measured box rather than thinning a long window', async () => {
  const dates = days(90);
  await act(async () => root.render(
    <DailyBarChart dates={dates} values={dates.map(() => 2)} formatMoney={format} seriesName="Cost per hour" />,
  ));
  expect(axisLabels().length).toBe(90);
  const svg = container.querySelector('svg')!;
  expect(svg.getAttribute('viewBox')).toBe('0 0 1718 294');
  expect(svg.style.minWidth).toBe('1718px');
});

it('names every day on the multi-series line axis', async () => {
  const dates = days(21);
  await act(async () => root.render(
    <DailyTrendChart dates={dates} formatMoney={format} series={[{ id: 'a', name: 'A', points: dates.map(() => 3) }]} />,
  ));
  expect(axisLabels()).toEqual(dates.map((date) => date.slice(5)));
});

/* A short window still reads horizontally: tilting labels that fit costs
   legibility for nothing. */
it('keeps short windows horizontal', async () => {
  const dates = days(7);
  await act(async () => root.render(
    <DailyBarChart dates={dates} values={dates.map(() => 2)} formatMoney={format} seriesName="Cost per hour" />,
  ));
  expect(axisLabels().length).toBe(7);
  expect(container.querySelectorAll('svg text[transform]').length).toBe(0);
  expect(container.querySelector('svg')!.getAttribute('viewBox')).toBe('0 0 880 260');
});

it('shows bar values and reference on hover/focus while keeping zero distinct from missing evidence', async () => {
  const dates = days(3);
  await act(async () => root.render(<DailyBarChart dates={dates} values={[0, null, 2]}
    formatMoney={format} seriesName="Daily spend" reference={{ value: 5, label: 'Even daily share' }} />));
  const buttons = [...container.querySelectorAll<HTMLButtonElement>('.cost-bar-hit')];
  await act(async () => buttons[0].dispatchEvent(new Event('pointerover', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')?.textContent).toContain('$0.00');
  expect(container.querySelector('[role="tooltip"]')?.textContent).toContain('$5.00');
  expect(buttons[0].getAttribute('aria-describedby')).toBe(container.querySelector('[role="tooltip"]')?.id);
  await act(async () => buttons[0].dispatchEvent(new Event('pointerout', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).toBeNull();
  await act(async () => buttons[1].focus());
  await act(async () => buttons[0].dispatchEvent(new Event('pointerout', { bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')?.textContent).toContain('Unavailable');
  expect(buttons[1].getAttribute('aria-pressed')).toBeNull();
  expect(container.querySelectorAll('rect')).toHaveLength(2);
  await act(async () => buttons[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(container.querySelector('[role="tooltip"]')).toBeNull();
});

it('moves between days without triggering drilldown and preserves native click selection', async () => {
  const onSelect = vi.fn();
  const dates = days(3);
  await act(async () => root.render(<DailyBarChart dates={dates} values={[1, 2, 3]}
    formatMoney={format} seriesName="Daily spend" onSelectDate={onSelect} selectedDate={dates[1]} />));
  const buttons = [...container.querySelectorAll<HTMLButtonElement>('.cost-bar-hit')];
  await act(async () => buttons[0].focus());
  for (const [key, target] of [['ArrowRight', 1], ['End', 2], ['ArrowRight', 2], ['Home', 0], ['ArrowLeft', 0]] as const) {
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })));
    expect(document.activeElement).toBe(buttons[target]);
  }
  expect(onSelect).not.toHaveBeenCalled();
  await act(async () => buttons[1].click());
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(dates[1]);
  expect(buttons[1].getAttribute('aria-pressed')).toBe('true');
});

it('keeps tooltip placement inside the visible part of a scrolled chart', async () => {
  expect(chartTooltipPlacement(1000, 900, 500, 300)).toEqual({ left: 508, width: 284 });
  const dates = days(30);
  await act(async () => root.render(<DailyBarChart dates={dates} values={dates.map(() => 2)} formatMoney={format} seriesName="Daily spend" />));
  const scroll = container.querySelector<HTMLDivElement>('.cost-chart-scroll')!;
  Object.defineProperty(scroll, 'clientWidth', { value: 300 });
  await act(async () => {
    scroll.scrollLeft = 500;
    scroll.dispatchEvent(new Event('scroll'));
    container.querySelectorAll<HTMLButtonElement>('.cost-bar-hit')[29].focus();
  });
  expect(container.querySelector<HTMLElement>('[role="tooltip"]')!.style.left).toBe('508px');
});

it('draws negative credits below zero instead of as a one-pixel positive bar', async () => {
  const dates = days(3);
  await act(async () => root.render(<DailyBarChart dates={dates} values={[20, -10, null]} formatMoney={format} seriesName="Daily spend" />));
  const zero = Number(container.querySelector('.cost-chart-zero')!.getAttribute('y1'));
  const positive = container.querySelectorAll('rect')[0];
  const credit = container.querySelectorAll('rect')[1];
  expect(Number(positive.getAttribute('y'))).toBeLessThan(zero);
  expect(Number(credit.getAttribute('y'))).toBe(zero);
  expect(Number(credit.getAttribute('height'))).toBeGreaterThan(1);
  expect(Number(credit.getAttribute('y')) + Number(credit.getAttribute('height'))).toBe(220);
  await act(async () => container.querySelectorAll<HTMLButtonElement>('.cost-bar-hit')[1].focus());
  expect(container.querySelector('[role="tooltip"]')?.textContent).toContain('$-10.00');
});