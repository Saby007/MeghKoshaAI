import { expect, test, type Page } from '@playwright/test';
import { anomalyFixture, snapshotFixture } from '../src/report/testFixtures';

async function runReport(page: Page) {
  await page.getByRole('button', { name: /^(Run report|Update report)$/ }).click({ timeout: 20000 });
  await expect(page.getByRole('heading', { name: 'Cost Overview' })).toBeVisible({ timeout: 25000 });
}

test.beforeEach(async ({ page }) => {
  await page.route('**/src/apiIdentity.ts', (route) => route.fulfill({
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
  let reportPublished = false;
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    let json: unknown = [];
    if (path.includes('subscriptions')) json = [{ subscriptionId: 'sub-1', displayName: 'Demo subscription', isOnboarded: true }];
    else if (path === '/api/report') { reportPublished = true; json = snapshotFixture.report; }
    else if (path === '/api/narrate') json = { executive_summary: 'Synthetic report narrative.', prioritized_findings: [] };
    else if (path.includes('latest')) {
      if (!reportPublished) return route.fulfill({ status: 404, json: { detail: 'No saved report' } });
      json = snapshotFixture;
    }
    else if (path.includes('anomalies')) json = anomalyFixture;
    else if (path.includes('exchange-rates')) json = { base: 'USD', provider: 'ECB', publishedDate: '2026-09-08', providerUrl: 'https://www.ecb.europa.eu/', stale: false, rates: { USD: 1, EUR: 0.9 } };
    return route.fulfill({ json });
  });
});

for (const theme of ['dark', 'light'] as const) {
  test(`premium shell ${theme}`, async ({ page }, testInfo) => {
    await page.addInitScript((value) => localStorage.setItem('mkai-theme', value), theme);
    await page.goto('/');
    await page.waitForTimeout(1500);
    await page.screenshot({ path: testInfo.outputPath(`scope-${theme}.png`) });
    await runReport(page);
    await page.waitForTimeout(1500);
    await page.screenshot({ path: testInfo.outputPath(`report-${theme}.png`) });
    const navigation = page.getByRole('navigation', { name: 'Report navigation' });
    await navigation.getByRole('button', { name: 'Subscription Breakdown', exact: true, includeHidden: true }).click({ timeout: 15000 });
    await page.waitForTimeout(1200);
    await page.screenshot({ path: testInfo.outputPath(`breakdown-${theme}.png`) });
    await page.getByRole('button', { name: 'Chat', exact: true }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: testInfo.outputPath(`chat-${theme}.png`) });
    await page.getByRole('button', { name: 'Schedules', exact: true }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: testInfo.outputPath(`schedules-${theme}.png`) });
  });
}

test('premium sign-in', async ({ page }, testInfo) => {
  await page.unroute('**/src/apiIdentity.ts');
  await page.route('**/src/apiIdentity.ts', (route) => route.fulfill({
    contentType: 'application/javascript',
    body: `
      export const IDENTITY_REQUIRED_EVENT = 'mkai-identity-required';
      export class ApiIdentityRequiredError extends Error {}
      export const apiFetch = (path, init) => fetch(path, init);
      export const initializeApiIdentity = async () => { throw new ApiIdentityRequiredError('sign in'); };
      export const connectApiIdentity = initializeApiIdentity;
      export const redirectApiIdentity = async () => {};
      export const signOutApiIdentity = async () => {};
    `,
  }));
  await page.goto('/');
  await page.waitForTimeout(2000);
  await page.screenshot({ path: testInfo.outputPath('signin-dark.png') });
});