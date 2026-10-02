// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ReportSideNav } from './ReportView';

let container: HTMLDivElement;
let root: Root;
const pageButton = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('.report-sidenav-pages button')].find((item) => item.textContent?.trim() === label)!;
const groupToggle = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('.report-sidenav-group-toggle')].find((item) => item.textContent?.trim() === label)!;
const setSearch = async (value: string) => act(async () => {
  const search = container.querySelector<HTMLInputElement>('[aria-label="Find a report page"]')!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, value);
  search.dispatchEvent(new Event('input', { bubbles: true }));
});

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  window.localStorage.removeItem('mkai-nav-collapsed');
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); window.localStorage.removeItem('mkai-nav-collapsed'); });

it('is a left navigation with every area open and the current page marked', async () => {
  await act(async () => root.render(<ReportSideNav activeTab="History" onSelect={vi.fn()} />));
  const nav = container.querySelector('nav[aria-label="Report navigation"]')!;
  expect(nav.classList.contains('report-sidenav')).toBe(true);
  const toggles = [...container.querySelectorAll<HTMLButtonElement>('.report-sidenav-group-toggle')];
  expect(toggles.map((item) => item.textContent?.trim())).toEqual(['Dashboard', 'Cost Management', 'Resources', 'Analytics', 'Recommendations', 'Reports']);
  expect(toggles.every((item) => item.getAttribute('aria-expanded') === 'true')).toBe(true);
  expect(container.querySelectorAll('.report-sidenav-pages button')).toHaveLength(19);
  expect(pageButton('History').getAttribute('aria-current')).toBe('page');
  const current = container.querySelectorAll('.report-sidenav-group[data-current="true"]');
  expect(current).toHaveLength(1);
  expect(current[0].querySelector('.report-sidenav-group-toggle')?.textContent?.trim()).toBe('Cost Management');
});

it('selects pages and keeps a closed area closed until its own page opens', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<ReportSideNav activeTab="Executive Summary" onSelect={onSelect} />));
  await act(async () => groupToggle('Recommendations').click());
  expect(groupToggle('Recommendations').getAttribute('aria-expanded')).toBe('false');
  expect(container.querySelector<HTMLUListElement>('#report-sidenav-recommendations')!.hidden).toBe(true);
  await act(async () => pageButton('Cost by Hour').click());
  expect(onSelect).toHaveBeenCalledWith('Cost by Hour');
  await act(async () => root.render(<ReportSideNav activeTab="History" onSelect={onSelect} />));
  expect(groupToggle('Recommendations').getAttribute('aria-expanded')).toBe('false');
  await act(async () => root.render(<ReportSideNav activeTab="Azure SQL Optimization" onSelect={onSelect} />));
  expect(groupToggle('Recommendations').getAttribute('aria-expanded')).toBe('true');
});

it('collapses to an icon rail, reopens on the chosen area and remembers the choice', async () => {
  await act(async () => root.render(<ReportSideNav activeTab="Executive Summary" onSelect={vi.fn()} />));
  const nav = container.querySelector('.report-sidenav')!;
  const collapse = container.querySelector<HTMLButtonElement>('.report-sidenav-collapse')!;
  await act(async () => groupToggle('Resources').click());
  await act(async () => collapse.click());
  expect(nav.classList.contains('is-collapsed')).toBe(true);
  expect(collapse.getAttribute('aria-label')).toBe('Expand navigation');
  expect(window.localStorage.getItem('mkai-nav-collapsed')).toBe('true');
  expect(groupToggle('Resources').getAttribute('title')).toBe('Resources');
  await act(async () => groupToggle('Resources').click());
  expect(nav.classList.contains('is-collapsed')).toBe(false);
  expect(groupToggle('Resources').getAttribute('aria-expanded')).toBe('true');
});

it('places the rail below the sticky header and at the workspace edge', async () => {
  const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 0; });
  const header = document.createElement('header');
  header.className = 'app-header';
  header.getBoundingClientRect = () => ({ bottom: 58, top: 0, left: 0, right: 0, width: 0, height: 58, x: 0, y: 0, toJSON: () => ({}) });
  const main = document.createElement('main');
  main.id = 'workspace-main';
  main.getBoundingClientRect = () => ({ bottom: 900, top: 58, left: 24, right: 1000, width: 976, height: 842, x: 24, y: 58, toJSON: () => ({}) });
  document.body.append(header, main);
  const mounted = document.createElement('div');
  main.appendChild(mounted);
  const mountedRoot = createRoot(mounted);
  try {
    await act(async () => mountedRoot.render(<ReportSideNav activeTab="Executive Summary" onSelect={vi.fn()} />));
    const nav = mounted.querySelector<HTMLElement>('.report-sidenav')!;
    expect(nav.style.getPropertyValue('--sidenav-top')).toBe('58px');
    expect(nav.style.getPropertyValue('--sidenav-left')).toBe('24px');
  } finally {
    await act(async () => mountedRoot.unmount());
    header.remove(); main.remove(); frame.mockRestore();
  }
});

it('becomes one mobile menu that closes after selection and on Escape', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<ReportSideNav activeTab="History" onSelect={onSelect} />));
  const toggle = container.querySelector<HTMLButtonElement>('[aria-label="Report pages"]')!;
  expect(toggle.textContent).toContain('History');
  await act(async () => toggle.click());
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  await act(async () => pageButton('Budgets').click());
  expect(onSelect).toHaveBeenCalledWith('Budgets');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  await act(async () => toggle.click());
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(document.activeElement).toBe(toggle);
});

it('finds pages, selects the first with Enter and resets the search', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<ReportSideNav activeTab="Executive Summary" onSelect={onSelect} />));
  await act(async () => groupToggle('Recommendations').click());
  await setSearch('  SQL  ');
  expect([...container.querySelectorAll('.report-sidenav-pages button')].map((item) => item.textContent)).toEqual(['Azure SQL Optimization']);
  const search = container.querySelector<HTMLInputElement>('[aria-label="Find a report page"]')!;
  await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  expect(onSelect).toHaveBeenCalledWith('Azure SQL Optimization');
  expect(search.value).toBe('');
  await setSearch('no matching view');
  expect(container.querySelector('.report-sidenav-empty')?.textContent).toBe('No matching pages');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Clear page search"]')!.click());
  expect(search.value).toBe('');
  expect(container.querySelectorAll('.report-sidenav-pages button').length).toBeGreaterThan(1);
});
