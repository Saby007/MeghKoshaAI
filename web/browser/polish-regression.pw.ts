import { expect, test, type Page } from '@playwright/test';
import { anomalyFixture, detailReportFixture, snapshotFixture } from '../src/report/testFixtures';

async function prepare(page: Page, theme: 'dark' | 'light') {
  const writes: string[] = [];
  let budgetUnavailable = false;
  const budget = { subscriptionId: 'sub-1', name: 'Polish budget', category: 'Cost', amount: 1000,
    currency: 'USD', timeGrain: 'Monthly', periodStart: '2026-01-01', periodEnd: '2030-10-31',
    currentSpend: 100, forecastSpend: 200 };
  await page.addInitScript(value => localStorage.setItem('mkai-theme', value), theme);
  await page.route('**/src/apiIdentity.ts', route => route.fulfill({
    contentType: 'application/javascript',
    body: `
      export const IDENTITY_REQUIRED_EVENT = 'mkai-identity-required';
      export class ApiIdentityRequiredError extends Error {}
      export const apiFetch = (path, init) => fetch(path, init);
      export const initializeApiIdentity = async () => ({ userId: 'visual-user', userDetails: 'visual@example.com', tenantId: 'visual-tenant', features: { aiNarration: true } });
      export const connectApiIdentity = initializeApiIdentity;
      export const redirectApiIdentity = async () => {};
      export const signOutApiIdentity = async () => {};
    `,
  }));
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET' && ['/api/report', '/api/budgets', '/api/report/email'].some(value => path === value || path.startsWith(`${value}/`))) {
      writes.push(path);
      return route.fulfill({ status: 409, json: { detail: 'Unexpected write during a read-only UI regression.' } });
    }
    if (path === '/api/subscriptions') return route.fulfill({ json: [{ subscriptionId: 'sub-1', displayName: 'Demo subscription', isOnboarded: true }] });
    if (path === '/api/report/latest') return route.fulfill({ json: { ...snapshotFixture, report: detailReportFixture } });
    if (path === '/api/anomalies') return route.fulfill({ json: anomalyFixture });
    if (path === '/api/exchange-rates') return route.fulfill({ json: {
      base: 'USD', provider: 'ECB', publishedDate: '2026-09-08', providerUrl: 'https://www.ecb.europa.eu/',
      stale: false, rates: { USD: 1, EUR: 0.9 },
    } });
    if (path === '/api/budgets') return budgetUnavailable
      ? route.fulfill({ status: 503, json: { detail: 'Synthetic budget read failed.' } })
      : route.fulfill({ json: [budget] });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await expect(page.locator('#report-page-heading')).toHaveText('Executive Summary');
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
  return { writes, failBudgetReads: (value: boolean) => { budgetUnavailable = value; } };
}

for (const theme of ['dark', 'light'] as const) {
  test(`polished read-only interactions and mobile recovery ${theme}`, async ({ page }) => {
    const state = await prepare(page, theme);
    const nav = page.getByRole('navigation', { name: 'Report navigation' });
    await expect(page.locator('.cost-delta i').first()).toHaveCSS('opacity', '1');
    await page.getByRole('button', { name: 'Update report', exact: true }).hover();
    await expect(page.getByRole('button', { name: 'Update report', exact: true })).toHaveCSS('filter', 'none');

    await nav.getByRole('button', { name: 'Collapse navigation' }).click();
    await nav.getByRole('button', { name: 'Cost Management', exact: true }).focus();
    await expect(page.getByRole('tooltip')).toContainText('8 pages');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('tooltip')).toHaveCount(0);
    await nav.getByRole('button', { name: 'Cost Management', exact: true }).click();

    await nav.getByRole('button', { name: 'Cost by Hour', exact: true }).click();
    const bars = page.locator('.cost-bar-hit');
    await bars.first().focus();
    await page.keyboard.press('ArrowRight');
    await expect(bars.nth(1)).toBeFocused();
    await expect(page.getByRole('tooltip')).toContainText('2026-08-02');
    await page.keyboard.press('Enter');
    await expect(page.getByRole('region', { name: 'Resource costs on 2026-08-02', exact: true })).toContainText('finance-vm');

    const reports = page.getByRole('button', { name: 'Download stakeholder reports', exact: true });
    await reports.click();
    const close = page.getByRole('button', { name: 'Close report downloads', exact: true });
    await expect(close).toBeFocused();
    expect(await page.locator('.app-header').evaluate(element => element.hasAttribute('inert'))).toBe(true);
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByRole('button', { name: 'Open builder', exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(close).toBeFocused();
    await page.getByRole('button', { name: 'Open builder', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Custom Report Builder', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(reports).toBeFocused();
    expect(await page.locator('.app-header').evaluate(element => element.hasAttribute('inert'))).toBe(false);

    await nav.getByRole('button', { name: 'Budgets', exact: true }).click();
    await page.getByLabel('Budget name', { exact: true }).fill('Retained draft');
    state.failBudgetReads(true);
    await page.getByRole('button', { name: 'Refresh managed budgets', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Budget lookup unavailable', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'No budgets in this scope', exact: true })).toHaveCount(0);
    state.failBudgetReads(false);
    await page.getByRole('button', { name: 'Retry budget lookup', exact: true }).click();
    await expect(page.locator('.budget-management-scroll')).toContainText('Polish budget');
    await expect(page.getByLabel('Budget name', { exact: true })).toHaveValue('Retained draft');

    await page.setViewportSize({ width: 390, height: 900 });
    await expect(page.locator('.budget-management-scroll .mobile-card-table')).toHaveCSS('display', 'block');
    const edit = page.getByRole('button', { name: 'Edit budget Polish budget', exact: true });
    expect((await edit.boundingBox())!.height).toBeGreaterThanOrEqual(43.5);
    await page.getByRole('searchbox', { name: 'Search managed budgets' }).fill('absent');
    await page.getByRole('button', { name: 'Show all budgets', exact: true }).click();
    await expect(edit).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(state.writes).toEqual([]);
  });
}
