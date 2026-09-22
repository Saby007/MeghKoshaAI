// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { DailyBarChart, DailyTrendChart } from './TrendChart';

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