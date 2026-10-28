import { expect, test, type Page } from '@playwright/test';

const backend = process.env.SQL_UI_FIXTURE_URL;
test.skip(!backend, 'Run with the dedicated loopback PostgreSQL fixture server.');
const tabs = [
  'Executive Summary', 'Savings Roadmap', 'Stale Resources', 'Subscription Breakdown', 'History', 'Cost by Hour',
  'EA Pricing', 'Rate Optimization', 'Cost Anomalies', 'Compute Optimization', 'Storage Optimization',
  'Network Optimization', 'Azure SQL Optimization', 'AI Optimization', 'Advisor Reconciliation',
  'Governance & Risk', 'Cost by Tags/Application', 'Budgets', 'Action Plan',
];

async function prepare(page: Page, sql: boolean, theme: 'dark' | 'light') {
  page.setDefaultTimeout(15000);
  const response = await page.request.get(`${backend}/fixture`);
  expect(response.ok()).toBe(true);
  const data = await response.json();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript(value => localStorage.setItem('mkai-theme', value), theme);
  await page.route('**/src/apiIdentity.ts', route => route.fulfill({
    contentType: 'application/javascript',
    body: `
      export const IDENTITY_REQUIRED_EVENT = 'mkai-identity-required';
      export class ApiIdentityRequiredError extends Error {}
      export const apiFetch = (path, init) => fetch(path, init);
      export const initializeApiIdentity = async () => ({ userId: 'fixture-user', userDetails: 'fixture@example.test',
        tenantId: '${data.full.subscriptionIds[0]}', features: { aiNarration: false } });
      export const connectApiIdentity = initializeApiIdentity;
      export const redirectApiIdentity = async () => {};
      export const signOutApiIdentity = async () => {};
    `,
  }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (/\/(cost|records|hierarchy)-query$/.test(path)) {
      const projection = await page.request.post(`${backend}${path}`, { data: request.postDataJSON() });
      if (!projection.ok()) errors.push(`${path}: ${await projection.text()}`);
      return route.fulfill({ response: projection });
    }
    if (path === '/api/subscriptions') return route.fulfill({ json: [{
      subscriptionId: data.full.subscriptionIds[0], displayName: 'Demo subscription', isOnboarded: true,
    }] });
    if (path === '/api/report/latest') return route.fulfill({ json: sql ? data.sql : data.full });
    if (path === '/api/anomalies') return route.fulfill({ json: data.anomaly });
    if (path === '/api/rate-optimization') return route.fulfill({ json: data.rates });
    if (path === '/api/exchange-rates') return route.fulfill({ json: {
      baseCurrency: 'USD', provider: 'ECB', publishedDate: '2026-09-08', fetchedAt: '2026-09-09',
      providerUrl: 'https://www.ecb.europa.eu/', stale: false, rates: { USD: 1, EUR: 0.9 },
    } });
    if (request.method() !== 'GET') errors.push(`Unexpected write: ${path}`);
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await expect(page.locator('#report-page-heading'), `Report startup errors: ${errors.join('; ')}`).toHaveText('Executive Summary', { timeout: 25000 });
  await page.evaluate(() => document.fonts.ready);
  return errors;
}

async function openTab(page: Page, name: string) {
  const nav = page.getByRole('navigation', { name: 'Report navigation' });
  const target = nav.getByRole('button', { name, exact: true });
  if (!await target.isVisible()) {
    for (const group of await nav.locator('button[aria-expanded="false"]').all()) await group.click();
  }
  await target.click();
  await expect(page.locator('#report-page-heading')).toHaveText(name);
  await page.waitForLoadState('networkidle');
}

for (const theme of ['dark', 'light'] as const) {
  test(`original navigation, data, and appearance remain identical with SQL serving: ${theme}`, async ({ browser }, testInfo) => {
    test.setTimeout(240000);
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const legacy = await context.newPage();
    const sql = await context.newPage();
    const baselineErrors = await prepare(legacy, false, theme);
    const sqlErrors = await prepare(sql, true, theme);
    try {
      for (const name of tabs) {
        await openTab(legacy, name);
        await openTab(sql, name);
        const baseline = legacy.locator('.dashboard-frame');
        const actual = sql.locator('.dashboard-frame');
        await expect(actual).toHaveText(await baseline.innerText(), { timeout: 15000, useInnerText: true });
        if (name === 'Executive Summary' || name === 'Subscription Breakdown' || name === 'Azure SQL Optimization') {
          const original = await legacy.screenshot({ path: testInfo.outputPath(`${name}-${theme}-original.png`), animations: 'disabled' });
          const actual = await sql.screenshot({ path: testInfo.outputPath(`${name}-${theme}-sql.png`), animations: 'disabled' });
          expect(actual.equals(original), `${name}: original and SQL screenshots differ`).toBe(true);
        }
      }
      for (const page of [legacy, sql]) {
        await openTab(page, 'Cost by Tags/Application');
        await page.getByLabel('Cost window start').fill('2026-09-01');
        await page.getByLabel('Cost window end').fill('2026-09-07');
        await page.getByLabel('Tag key', { exact: true }).selectOption({ label: 'application' });
        await page.getByLabel('Tag value', { exact: true }).selectOption({ label: 'Finance' });
        const breakdown = page.getByRole('region', { name: 'Tagged cost breakdown', exact: true });
        await expect(breakdown).toContainText('$1,632.00');
        await page.getByRole('group', { name: 'Group cost by', exact: true })
          .getByRole('button', { name: 'Resource', exact: true }).click();
        await expect(breakdown).toContainText('finance-vm');
        await expect(breakdown).not.toContainText('shared-disk');
        await page.getByLabel('Cost tag value', { exact: true }).selectOption({ label: 'Platform' });
        await expect(page.getByLabel('Tag value', { exact: true })).toHaveValue(JSON.stringify('Platform'));
        await expect(breakdown).toContainText('$204.00');
        await expect(breakdown).toContainText('shared-disk');
        await expect(breakdown).not.toContainText('finance-vm');
      }
      expect(baselineErrors).toEqual([]);
      expect(sqlErrors).toEqual([]);
    } finally {
      await context.close();
    }
  });
}
