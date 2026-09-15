// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LeftNavSidebar } from './ReportView';

let container: HTMLDivElement;
let root: Root;
const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('.report-nav-groups button')].find((item) => item.textContent === label)!;

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('expands and collapses navigation branches and selects billing pages', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<LeftNavSidebar activeTab="Executive Summary" onSelect={onSelect} />));
  const group = button('Cost Management');
  expect(group.getAttribute('aria-expanded')).toBe('true');
  await act(async () => group.click());
  expect(container.querySelector<HTMLUListElement>('#report-nav-costManagement')!.hidden).toBe(true);
  await act(async () => group.click());
  await act(async () => button('History').click());
  expect(onSelect).toHaveBeenCalledWith('History');
  await act(async () => button('Cost by Hour').click());
  expect(onSelect).toHaveBeenCalledWith('Cost by Hour');
});

it('reveals an active child when navigation changes from another view', async () => {
  await act(async () => root.render(<LeftNavSidebar activeTab="Executive Summary" onSelect={vi.fn()} />));
  expect(button('Recommendations').getAttribute('aria-expanded')).toBe('false');
  await act(async () => root.render(<LeftNavSidebar activeTab="Azure SQL Optimization" onSelect={vi.fn()} />));
  expect(button('Recommendations').getAttribute('aria-expanded')).toBe('true');
  expect(button('Azure SQL Optimization').getAttribute('aria-current')).toBe('page');
});

it('collapses the mobile report menu after selecting a view', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<LeftNavSidebar activeTab="Executive Summary" onSelect={onSelect} />));
  const toggle = container.querySelector<HTMLButtonElement>('[aria-label="Report pages"]')!;
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  await act(async () => toggle.click());
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  await act(async () => button('History').click());
  expect(onSelect).toHaveBeenCalledWith('History');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
});

it('finds report pages inside collapsed groups and resets the search after selection', async () => {
  const onSelect = vi.fn();
  await act(async () => root.render(<LeftNavSidebar activeTab="Executive Summary" onSelect={onSelect} />));
  const search = container.querySelector<HTMLInputElement>('[aria-label="Find a report page"]');
  expect(search).not.toBeNull();
  const changeSearch = async (value: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, value);
    search!.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await changeSearch('  SQL  ');
  expect(button('Azure SQL Optimization').closest('ul')!.hidden).toBe(false);
  expect(button('History')).toBeUndefined();
  await act(async () => button('Azure SQL Optimization').click());
  expect(onSelect).toHaveBeenCalledWith('Azure SQL Optimization');
  expect(search!.value).toBe('');
  expect(button('History')).toBeDefined();
  await changeSearch('no matching view');
  expect(container.querySelector('[role="status"]')?.textContent).toContain('No matching pages');
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Clear page search"]')!.click());
  expect(search!.value).toBe('');
  expect(button('History')).toBeDefined();
});