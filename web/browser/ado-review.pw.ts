import { expect, test, type Page } from '@playwright/test';
import { BRAND_NAME } from '../src/brand';
import { anomalyFixture, detailReportFixture, pricingReportFixture, rateOptimizationFixture, resourceReportFixture, snapshotFixture, tagReportFixture } from '../src/report/testFixtures';

async function runReport(page: Page) {
  await page.getByRole('button', { name: /^(Run report|Update report)$/ }).click({ timeout: 15000 });
  await expect(page.getByRole('heading', { name: 'Cost Overview' })).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: 'Update report', exact: true })).toBeEnabled();
}

async function selectReportPage(page: Page, name: string) {
  const navigation = page.getByRole('navigation', { name: 'Report navigation' });
  const mobileMenu = navigation.getByRole('button', { name: 'Report pages' });
  if (await mobileMenu.isVisible() && await mobileMenu.getAttribute('aria-expanded') === 'false') await mobileMenu.click();
  const target = navigation.getByRole('button', { name, exact: true, includeHidden: true });
  if (!await target.isVisible()) await navigation.locator('.left-nav-group').filter({ has: page.getByRole('button', { name, exact: true, includeHidden: true }) }).locator('.left-nav-group-toggle').click({ timeout: 10000 });
  await target.click({ timeout: 10000 });
  await expect(page.locator('#report-page-heading')).toHaveText(name);
}

test.beforeEach(async ({ page }) => {
  await page.route('**/src/apiIdentity.ts', (route) => route.fulfill({
    contentType: 'application/javascript',
    body: `
      export const IDENTITY_REQUIRED_EVENT = 'mkai-identity-required';
      export class ApiIdentityRequiredError extends Error {}
      export const apiFetch = (path, init) => fetch(path, init);
      export const initializeApiIdentity = async () => {
        if (sessionStorage.getItem('app-browser-session') === 'signed_out') throw new ApiIdentityRequiredError();
        return { userId: 'visual-user', userDetails: 'visual@example.com', tenantId: 'visual-tenant', features: { aiNarration: true } };
      };
      export const connectApiIdentity = initializeApiIdentity;
      export const redirectApiIdentity = async (hint) => { window.location.assign('/auth-callback.html?fixture_login_hint=' + encodeURIComponent(hint)); };
      export const signOutApiIdentity = async () => {};
    `,
  }));
  await page.addInitScript(() => {
    localStorage.setItem('mkai-theme', 'light');
  });
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

test('backend managed identity failure preserves sign-in without consent acquisition', async ({ page }, testInfo) => {
  const configuration = { tenantId: '11111111-1111-1111-1111-111111111111', apiClientId: '22222222-2222-2222-2222-222222222222', webClientId: '33333333-3333-3333-3333-333333333333', scope: 'api://22222222-2222-2222-2222-222222222222/access_as_user' };
  const account = { tenantId: configuration.tenantId, username: 'visual@example.com', homeAccountId: 'synthetic-browser-account' };
  const message = 'The backend managed identity could not authenticate to Azure. Contact the deployment administrator.';
  await page.unroute('**/src/apiIdentity.ts');
  await page.route('**/@azure_msal-browser.js*', (route) => route.fulfill({
    contentType: 'application/javascript',
    body: `
      const account = ${JSON.stringify(account)};
      export const BrowserCacheLocation = { SessionStorage: 'sessionStorage' };
      export class InteractionRequiredAuthError extends Error {}
      export class PublicClientApplication {
        async initialize() {}
        getActiveAccount() { return account; }
        getAllAccounts() { return [account]; }
        setActiveAccount() {}
        async acquireTokenSilent() { return { account, accessToken: 'synthetic-browser-token' }; }
        async acquireTokenRedirect(request) { sessionStorage.setItem('app-consent-requested', JSON.stringify(request)); }
        async loginRedirect(request) { sessionStorage.setItem('app-login-requested', JSON.stringify(request)); }
      }
    `,
  }));
  await page.route('**/api/auth/config', (route) => route.fulfill({ json: configuration }));
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: {
    userId: 'visual-user', userDetails: 'visual@example.com', tenantId: configuration.tenantId, features: { aiNarration: false },
  } }));
  let discoveryRequests = 0;
  const mutations: string[] = [];
  const browserArmRequests: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') mutations.push(request.url());
    if (new URL(request.url()).hostname === 'management.azure.com') browserArmRequests.push(request.url());
  });
  await page.route('**/api/schedules', (route) => {
    discoveryRequests += 1;
    expect(route.request().headers()['authorization']).toBe('Bearer synthetic-browser-token');
    return route.fulfill({ status: 503, json: { detail: {
      code: 'azure_managed_identity_unavailable', message,
    } } });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  expect(discoveryRequests).toBe(0);
  const failure = await page.evaluate(async () => {
    const modulePath = '/src/apiIdentity.ts';
    const identity = await import(modulePath);
    const response = await identity.apiFetch('/api/schedules');
    const payload = await response.json();
    const profile = await identity.initializeApiIdentity('');
    return { status: response.status, detail: payload.detail, userId: profile.userId, consentApiExposed: typeof identity.authorizeAzureAccess === 'function' };
  });
  expect(failure).toEqual({ status: 503, detail: { code: 'azure_managed_identity_unavailable', message }, userId: 'visual-user', consentApiExposed: false });
  await expect(page.getByRole('heading', { name: `Sign in to ${BRAND_NAME}`, exact: true })).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await expect(page.getByRole('region', { name: 'Azure access' })).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'Check Azure access' })).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'Authorize Azure access' })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`managed-identity-${width}-${theme}.png`), animations: 'disabled' });
    }
  }
  expect(await page.evaluate(() => sessionStorage.getItem('app-consent-requested'))).toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem('app-login-requested'))).toBeNull();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  expect(discoveryRequests).toBe(1);
  expect(mutations).toEqual([]);
  expect(browserArmRequests).toEqual([]);
});

test('main screen, billing comparison and hourly filters fit desktop and mobile in both themes', async ({ page }, testInfo) => {
  const errors: string[] = [];
  const layouts: Array<{ width: number; theme: string; scrollHeight: number; cardHeights: number[]; gap: string }> = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await runReport(page);
  for (const viewport of [{ width: 1280, height: 1080 }, { width: 1440, height: 1080 }, { width: 1920, height: 1080 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await selectReportPage(page, 'Executive Summary');
      await expect(page.getByRole('region', { name: 'Cost anomaly overview' })).toContainText('Demo subscription');
      await page.evaluate(() => window.scrollTo(0, 0));
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const layout = await page.evaluate(() => ({
        scrollHeight: document.documentElement.scrollHeight,
        cardHeights: [...document.querySelectorAll('.executive-hero-grid .kpi-card')].map((card) => card.getBoundingClientRect().height),
        gap: getComputedStyle(document.querySelector('.executive-hero-grid')!).gap,
      }));
      layouts.push({ width: viewport.width, theme, ...layout });
      expect(Math.max(...layout.cardHeights) - Math.min(...layout.cardHeights)).toBeLessThanOrEqual(1);
      const comparisonTop = await page.locator('.cost-window-overview').evaluate((element) => element.getBoundingClientRect().top);
      const titleBottom = await page.locator('.dashboard-titlebar').evaluate((element) => element.getBoundingClientRect().bottom);
      expect(comparisonTop).toBeGreaterThanOrEqual(titleBottom);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-overview.png`), fullPage: true, animations: 'disabled' });
      await selectReportPage(page, 'History');
      await page.getByLabel('Baseline billing date').fill('2026-09-05');
      await page.getByLabel('Comparison billing date').fill('2026-09-07');
      await expect(page.getByLabel('Billing cost change', { exact: true })).toContainText('96.00');
      await page.getByRole('button', { name: 'Swap billing dates' }).click();
      await expect(page.getByLabel('Billing cost change', { exact: true })).toContainText('-$96.00');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-history.png`), fullPage: true, animations: 'disabled' });
      await selectReportPage(page, 'Cost by Hour');
      await page.getByLabel('Billing day filter').selectOption('weekends');
      await page.getByLabel('Billing tag filter').selectOption({ label: 'Application: Finance' });
      await expect(page.getByLabel('Filtered average hourly cost', { exact: true })).toHaveText('$1.50/hr');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-hourly.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await testInfo.attach('responsive-layout-metrics.json', { body: JSON.stringify(layouts, null, 2), contentType: 'application/json' });
  expect(errors).toEqual([]);
});

test('only Run Report starts an assessment, including after reload, and navigation remains keyboard accessible', async ({ page }) => {
  const assessmentRequests: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (['/api/report', '/api/report/latest', '/api/narrate'].includes(path)) assessmentRequests.push(path);
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Run report', exact: true })).toBeEnabled();
  await expect(page.locator('.report-awaiting')).toBeVisible();
  await expect(page.locator('.report-awaiting i')).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      expect(await page.locator('.report-awaiting').evaluate((element) => element.getAnimations({ subtree: true }).length)).toBe(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: test.info().outputPath(`${width}-${theme}-idle.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.getByRole('checkbox', { name: 'Load latest report on sign-in' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Load latest report', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Skip', exact: true })).toHaveCount(0);
  expect(assessmentRequests).toEqual(['/api/report/latest']);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Run report', exact: true })).toBeEnabled();
  await expect(page.getByRole('navigation', { name: 'Report navigation' })).toHaveCount(0);
  await expect(page.locator('.report-awaiting')).toBeVisible();
  expect(assessmentRequests).toEqual(['/api/report/latest', '/api/report/latest']);
  await runReport(page);
  expect(assessmentRequests).toEqual(['/api/report/latest', '/api/report/latest', '/api/report', '/api/report/latest', '/api/narrate']);
  await expect(page.getByRole('navigation', { name: 'Report navigation' })).toBeVisible();
  const branch = page.getByRole('button', { name: 'Cost Management', exact: true });
  await branch.focus(); await page.keyboard.press('Enter');
  await expect(branch).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await expect(branch).toHaveAttribute('aria-expanded', 'true');
  const revisitRequests: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/')) revisitRequests.push(path);
  });
  const revisitStarted = performance.now();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Report navigation' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Report', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Cost Overview' })).toBeVisible();
  expect(assessmentRequests).toEqual(['/api/report/latest', '/api/report/latest', '/api/report', '/api/report/latest', '/api/narrate']);
  expect(revisitRequests).toEqual([]);
  await test.info().attach('workspace-revisit-metrics.json', {
    body: JSON.stringify({ apiRequestsOnRevisit: revisitRequests.length, syntheticRoundTripMs: performance.now() - revisitStarted, note: 'Playwright-driven local navigation timing, not real-user INP.' }),
    contentType: 'application/json',
  });
});

test('light theme text retains readable contrast', async ({ page }, testInfo) => {
  await page.goto('/');
  await runReport(page);
  const ratios = await page.evaluate(() => {
    const luminance = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    }).reduce((total, value, index) => total + value * [0.2126, 0.7152, 0.0722][index], 0);
    const background = luminance(getComputedStyle(document.body).backgroundColor);
    const textRatios = ['.left-nav-children button:not(.active)', '.kpi-note', '.kpi-label', '.report-context-details', '.anomaly-overview-provenance'].map((selector) => {
      const foreground = luminance(getComputedStyle(document.querySelector(selector)!).color);
      return { selector, ratio: (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05) };
    });
    const button = getComputedStyle(document.querySelector('.scope-run-button')!);
    const buttonForeground = luminance(button.color);
    const buttonBackground = luminance(button.backgroundColor);
    const activePage = getComputedStyle(document.querySelector('.left-nav-children button.active')!);
    const activeForeground = luminance(activePage.color);
    const activeBackground = luminance(activePage.backgroundColor);
    return [...textRatios,
      { selector: '.scope-run-button', ratio: (Math.max(buttonForeground, buttonBackground) + 0.05) / (Math.min(buttonForeground, buttonBackground) + 0.05) },
      { selector: '.left-nav-children button.active', ratio: (Math.max(activeForeground, activeBackground) + 0.05) / (Math.min(activeForeground, activeBackground) + 0.05) },
    ];
  });
  for (const { selector, ratio } of ratios) expect(ratio, selector).toBeGreaterThanOrEqual(4.5);
  await expect(page.locator('.snapshot-banner-note')).toBeHidden();
  await testInfo.attach('light-theme-contrast.json', { body: JSON.stringify(ratios, null, 2), contentType: 'application/json' });
});

test('density scales spacing and detail expansion stays keyboard accessible', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/');
  await runReport(page);
  const gaps: number[] = [];
  for (const scale of [0.875, 1, 1.125]) {
    const gap = await page.evaluate((value) => {
      document.documentElement.style.setProperty('--ui-scale', String(value));
      return parseFloat(getComputedStyle(document.querySelector('.executive-hero-grid')!).gap);
    }, scale);
    expect(gap).toBeCloseTo(12 * scale);
    gaps.push(gap);
  }
  await page.evaluate(() => document.documentElement.style.removeProperty('--ui-scale'));
  const summary = page.locator('.cost-analysis-details > summary');
  await summary.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.executive-visual-grid')).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.locator('.executive-visual-grid')).toBeHidden();
  await testInfo.attach('density-metrics.json', { body: JSON.stringify({ scales: [0.875, 1, 1.125], gaps }), contentType: 'application/json' });
});

test('tag value filtering is keyboard accessible and fits desktop and mobile in both themes', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const longKey = `Owner/${'department-'.repeat(12)}`;
  const longValue = `service/${'production-workload-'.repeat(12)}`;
  const report = { ...tagReportFixture, tagCosts: {
    ...tagReportFixture.tagCosts,
    dimensions: [...tagReportFixture.tagCosts.dimensions, {
      tagKey: longKey, unallocatedCost: 0,
      rows: [{ value: longValue, monthlyCost: 250, pctOfTotal: 1, forecastNextMonth: 275 }],
    }],
  } };
  await page.route('**/api/report**', (route) => route.fulfill({
    json: new URL(route.request().url()).pathname.endsWith('/latest') ? { ...snapshotFixture, report } : report,
  }));
  await page.goto('/');
  await runReport(page);
  await selectReportPage(page, 'Cost by Tags/Application');
  const tagKey = page.getByLabel('Tag key', { exact: true });
  const tagValue = page.getByLabel('Tag value', { exact: true });
  const rows = page.locator('.app-cost-table tbody tr');
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await tagKey.selectOption('Team');
      await expect(tagValue).toHaveValue('');
      await expect(rows).toHaveCount(2);
      await tagKey.focus();
      await page.keyboard.press('Tab');
      await expect(tagValue).toBeFocused();
      await tagValue.selectOption({ label: 'Sales' });
      await expect(rows).toHaveCount(1);
      await expect(rows.first()).toContainText('Sales');
      await expect(rows.first().locator('td').nth(1)).toHaveText('$80');
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-tag-values.png`), fullPage: true, animations: 'disabled' });
      await tagKey.selectOption('Environment');
      await expect(tagValue).toHaveValue('');
      await expect(tagValue.locator('option')).toHaveText(['All values', 'Production']);
      await tagKey.selectOption(longKey);
      await tagValue.selectOption({ label: longValue });
      await expect(tagKey).toHaveAttribute('title', longKey);
      await expect(tagValue).toHaveAttribute('title', longValue);
      for (const label of await page.locator('.tag-cost-controls label > span').all()) await expect(label).toBeVisible();
      const layout = await page.locator('.tag-cost-controls').evaluate((controls) => ({
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: innerWidth,
        fields: [...controls.querySelectorAll('label')].map((label) => {
          const caption = label.querySelector('span')!.getBoundingClientRect();
          const field = label.querySelector('select')!.getBoundingClientRect();
          return { left: field.left, right: field.right, width: field.width, captionGap: field.left - caption.right };
        }),
      }));
      expect(layout.pageWidth).toBeLessThanOrEqual(layout.viewportWidth);
      for (const field of layout.fields) {
        expect(field.left).toBeGreaterThanOrEqual(0);
        expect(field.right).toBeLessThanOrEqual(layout.viewportWidth);
        expect(field.width).toBeGreaterThan(100);
        expect(field.captionGap).toBeGreaterThanOrEqual(0);
      }
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-long-tag-values.png`), fullPage: true, animations: 'disabled' });
    }
  }
  expect(errors).toEqual([]);
});

test('selected billing windows show actual totals and clearly marked business-hour estimates', async ({ page }, testInfo) => {
  await page.goto('/');
  await runReport(page);
  await selectReportPage(page, 'Cost by Hour');
  await page.getByRole('group', { name: 'Cost comparison range' }).getByRole('button', { name: '7d', exact: true }).click();
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await page.getByLabel('Billing day filter').selectOption('all');
      await page.getByLabel('Billing time filter').selectOption('all');
      await page.getByLabel('Billing tag filter').selectOption('');
      await expect(page.getByLabel('Filtered billed cost', { exact: true })).toHaveText(/^\$816(?:\.00)?$/);
      await page.getByLabel('Billing day filter').selectOption('weekends');
      await expect(page.getByLabel('Filtered billed cost', { exact: true })).toHaveText(/^\$96(?:\.00)?$/);
      await page.getByLabel('Billing day filter').selectOption('all');
      await page.getByLabel('Billing time filter').selectOption('business');
      await expect(page.getByLabel('Filtered estimated cost', { exact: true })).toHaveText(/^\$240(?:\.00)?$/);
      await expect(page.getByLabel('Filtered billed cost', { exact: true })).toHaveCount(0);
      await expect(page.getByLabel('Filtered average hourly cost', { exact: true })).toHaveText('$6.00/hr');
      await expect(page.getByRole('region', { name: 'Cost by hour', exact: true })).toContainText('40 selected hours');
      await expect(page.getByRole('region', { name: 'Cost by hour', exact: true })).toContainText('not measured hourly usage');
      await page.getByLabel('Billing time filter').focus();
      await page.keyboard.press('Tab');
      await expect(page.getByLabel('Billing tag filter')).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-business-hours.png`), fullPage: true, animations: 'disabled' });
      await page.getByLabel('Billing time filter').selectOption('off-hours');
      await expect(page.getByLabel('Filtered estimated cost', { exact: true })).toHaveText(/^\$576(?:\.00)?$/);
      await page.getByLabel('Billing day filter').selectOption('weekends');
      await page.getByLabel('Billing tag filter').selectOption({ label: 'Application: Finance' });
      await expect(page.getByLabel('Filtered estimated cost', { exact: true })).toHaveText(/^\$72(?:\.00)?$/);
      await page.getByLabel('Billing time filter').selectOption('business');
      await expect(page.getByLabel('Filtered estimated cost', { exact: true })).toHaveText('Unavailable');
      await expect(page.getByText('No billing dates match these filters.', { exact: true })).toBeVisible();
    }
  }
});

test('EA commitment evidence and Rate Optimization text remain readable across themes and viewports', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/report**', (route) => route.fulfill({
    json: new URL(route.request().url()).pathname.endsWith('/latest') ? { ...snapshotFixture, report: pricingReportFixture } : pricingReportFixture,
  }));
  await page.route('**/api/rate-optimization**', (route) => route.fulfill({ json: rateOptimizationFixture }));
  await page.goto('/');
  await runReport(page);
  await page.locator('summary').filter({ hasText: 'Explore costs and findings' }).click();
  const mapAsset = page.getByRole('img', { name: 'Azure region spend world map' }).locator('image');
  await expect(mapAsset).toHaveAttribute('href', /\.svg/);
  expect(await mapAsset.evaluate(async (image: SVGImageElement) => (await fetch(image.href.baseVal)).ok)).toBe(true);
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await selectReportPage(page, 'EA Pricing');
      await expect(page.getByRole('heading', { name: 'Reservations (RI) and Savings Plans' })).toBeVisible();
      const evidence = page.getByRole('region', { name: 'Commitment evidence', exact: true });
      await expect(evidence.getByRole('rowheader', { name: 'Reservations (RI)' })).toBeVisible();
      await expect(evidence.getByRole('rowheader', { name: 'Savings Plans' })).toBeVisible();
      const rowTexts = await evidence.locator('tbody tr').allTextContents();
      await evidence.focus();
      await expect(evidence).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-ea-commitments.png`), fullPage: true, animations: 'disabled' });
      await selectReportPage(page, 'Rate Optimization');
      await expect(page.getByRole('button', { name: 'Refresh', exact: true })).toBeEnabled();
      await expect(page.getByText('Synthetic recommendation available', { exact: false })).toBeVisible();
      expect(await page.locator('.commitment-table tbody tr').allTextContents()).toEqual(rowTexts);
      const samples = await page.locator('.rate-optimization-panel').evaluate((panel) => {
        const parseColor = (value: string) => {
          const channels = value.match(/[\d.]+/g)!.map(Number);
          // color-mix() serialises as CSS Color Level 4 `color(srgb r g b / a)`, whose
          // r/g/b channels are 0-1 rather than the 0-255 used by rgb()/rgba(). Without
          // this, a white 40% glass surface reads as near-black and inverts the result.
          if (!value.startsWith('color(')) return channels;
          return [channels[0] * 255, channels[1] * 255, channels[2] * 255, channels[3] ?? 1];
        };
        const luminance = (channels: number[]) => channels.slice(0, 3).map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
        const background = (element: Element) => {
          let remaining = 1;
          const channels = [0, 0, 0];
          let ancestor: Element | null = element;
          while (ancestor && remaining > 0) {
            const color = parseColor(getComputedStyle(ancestor).backgroundColor);
            const alpha = color[3] ?? 1;
            for (let index = 0; index < 3; index += 1) channels[index] += color[index] * alpha * remaining;
            remaining *= 1 - alpha;
            ancestor = ancestor.parentElement;
          }
          return channels.map((channel) => channel + 255 * remaining);
        };
        return [
          '.section-subtitle', '.rate-controls label', '.rate-controls select', '.rate-scope-note', '.rate-status',
          '.rate-table th', '.rate-table td', '.rate-table small', '.rate-table strong', '.rate-table .portal-link',
          '.commitment-actual-card > span', '.commitment-actual-card > strong', '.commitment-actual-card > small',
          '.commitment-coverage-panel header > span', '.commitment-coverage-panel header > small', '.coverage-legend li',
          '.coverage-org-reservation', '.commitment-trend-panel header > span', '.commitment-trend-panel header > small',
          '.monthly-column > strong', '.monthly-column > span', '.commitment-legend', '.rate-generated', '.live-badge',
        ].flatMap((selector) => [...panel.querySelectorAll(selector)].map((element) => {
          const style = getComputedStyle(element);
          const foreground = luminance(parseColor(style.color));
          const surface = luminance(background(element));
          return { selector, fontSize: parseFloat(style.fontSize), contrast: (Math.max(foreground, surface) + 0.05) / (Math.min(foreground, surface) + 0.05) };
        }));
      });
      expect(samples.length).toBeGreaterThan(20);
      expect(samples.filter((sample) => sample.fontSize < (sample.selector === '.live-badge' ? 12 : 14))).toEqual([]);
      for (const sample of samples) {
        expect(sample.contrast, sample.selector).toBeGreaterThanOrEqual(4.5);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await testInfo.attach(`${viewport.width}-${theme}-rate-readability.json`, { body: JSON.stringify(samples, null, 2), contentType: 'application/json' });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-rate-optimization.png`), fullPage: true, animations: 'disabled' });
    }
  }
  expect(errors).toEqual([]);
});

test('workspace states support sign-in, report retry, chat retry and schedule recovery', async ({ page }, testInfo) => {
  const errors: string[] = [];
  const legacyRequests: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => { if (new URL(request.url()).pathname.startsWith('/.auth/')) legacyRequests.push(request.url()); });
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('app-browser-session')) sessionStorage.setItem('app-browser-session', 'signed_out');
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: `Sign in to ${BRAND_NAME}`, exact: true })).toBeVisible();
  /* Entra is the only credential path, so there is no email field to validate
     and nothing to type before the redirect. */
  await expect(page.getByLabel('Email address')).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-sign-in.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await page.route('**/auth-callback.html?fixture_login_hint=*', (route) => route.fulfill({ contentType: 'text/html', body: '<title>MSAL callback fixture</title>' }));
  await page.getByRole('button', { name: 'Sign in with Microsoft', exact: true }).click();
  await expect(page).toHaveURL(/\/auth-callback.html\?fixture_login_hint=/);
  /* An empty hint is the point: Entra prompts for account selection instead of
     the app pre-filling an address the user is about to choose anyway. */
  expect(new URL(page.url()).searchParams.get('fixture_login_hint')).toBe('');
  await page.evaluate(() => sessionStorage.setItem('app-browser-session', 'signed_in'));
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Run report', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'No completed report' })).toBeVisible();
  await page.getByRole('button', { name: 'Open report', exact: true }).click();
  await page.route('**/api/report', (route) => route.fulfill({ status: 503, json: { detail: 'Synthetic report retry required.' } }));
  await page.getByRole('button', { name: 'Run report', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(1);
  await expect(page.getByRole('alert')).toContainText('Synthetic report retry required.');
  await expect(page.locator('.report-awaiting')).toHaveCount(0);
  await page.route('**/api/report', (route) => route.fulfill({ json: snapshotFixture.report }));
  await page.route('**/api/report/latest?**', (route) => route.fulfill({ json: snapshotFixture }));
  await runReport(page);
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.route('**/api/chat', (route) => route.fulfill({ status: 503, json: { detail: 'Synthetic chat retry required.' } }));
  await page.getByLabel('Ask about this report').fill('What changed in this report?');
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic chat retry required.');
  await expect(page.getByLabel('Ask about this report')).toHaveValue('What changed in this report?');
  await expect(page.locator('.chat-message.user')).toHaveCount(0);
  await page.route('**/api/chat', (route) => route.fulfill({ json: {
    intent: 'overview', answer: 'Synthetic verified answer.', metrics: [{ label: 'Monthly spend', value: '$3,120', detail: 'August 2026' }], resources: [], suggestions: [], evidenceKeys: ['summary'], disclaimer: 'Synthetic report evidence.', responseMode: 'deterministic_fallback', selectedModel: null, usage: null, dataAsOf: '2026-09-09T10:00:00Z',
  } }));
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByText('Synthetic verified answer.', { exact: true })).toBeVisible();
  await expect(page.locator('.chat-message.user')).toHaveCount(1);
  await expect(page.locator('.chat-error')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('390-chat-recovery.png'), fullPage: true, animations: 'disabled' });
  await page.route('**/api/schedules', (route) => route.fulfill({ status: 503, json: { detail: 'Synthetic schedule lookup unavailable.' } }));
  await page.getByRole('button', { name: 'Schedules', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic schedule lookup unavailable.');
  await expect(page.getByRole('button', { name: 'Run all', exact: true })).toBeDisabled();
  await page.route('**/api/schedules', (route) => route.fulfill({ json: [] }));
  await page.getByRole('button', { name: 'Refresh schedules' }).click();
  await expect(page.getByText('No accessible subscriptions returned', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('390-schedules-empty.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('heading', { name: `Sign in to ${BRAND_NAME}`, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Download FocusCost files' })).toHaveCount(0);
  expect(legacyRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test('all report pages and export dialogs remain usable in the redesigned workspace', async ({ page }, testInfo) => {
  test.setTimeout(180000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const report = { ...pricingReportFixture, tagCosts: tagReportFixture.tagCosts };
  await page.route('**/api/report**', (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith('/latest') ? { ...snapshotFixture, report } : path === '/api/report' ? report : [] });
  });
  await page.route('**/api/rate-optimization**', (route) => route.fulfill({ json: rateOptimizationFixture }));
  await page.route('**/api/**focus**', (route) => route.fulfill({ json: { subscriptionId: 'sub-1', files: [] } }));
  await page.goto('/');
  await runReport(page);
  const tabs = ['Executive Summary', 'Subscription Breakdown', 'History', 'Cost by Hour', 'Cost by Tags/Application', 'EA Pricing', 'Rate Optimization', 'Cost Anomalies', 'Budgets', 'Stale Resources', 'Governance & Risk', 'Advisor Reconciliation', 'Savings Roadmap', 'Compute Optimization', 'Storage Optimization', 'Network Optimization', 'Azure SQL Optimization', 'AI Optimization', 'Action Plan'];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      for (const tab of tabs) {
        await selectReportPage(page, tab);
        await expect(page.locator('.tab-panel')).toBeVisible();
        expect((await page.locator('.tab-panel').innerText()).trim().length, tab).toBeGreaterThan(10);
        if (tab === 'Budgets') {
          await expect(page.locator('.budget-form label')).toHaveCount(7);
          for (const label of await page.locator('.budget-form label > span').all()) await expect(label).toBeVisible();
          const amount = await page.getByLabel('Budget amount', { exact: true }).boundingBox();
          expect(amount?.width).toBeGreaterThanOrEqual(140);
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} ${theme} ${tab}`).toBe(true);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-${tab.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.png`), fullPage: true, animations: 'disabled' });
      }
      const trigger = page.getByRole('button', { name: 'Download stakeholder reports' });
      await trigger.click();
      const dialog = page.getByRole('dialog', { name: 'Share reports' });
      await expect(dialog).toBeVisible();
      const close = dialog.getByRole('button', { name: 'Close report downloads' });
      await expect(close).toBeFocused();
      await page.keyboard.press('Shift+Tab');
      await expect(dialog.getByRole('button', { name: 'Open builder' })).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(close).toBeFocused();
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-share-reports.png`), fullPage: true, animations: 'disabled' });
      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
      await expect(trigger).toBeFocused();
      await trigger.click();
      await page.getByRole('button', { name: 'Open builder' }).click();
      await expect(page.getByRole('dialog', { name: 'Custom Report Builder' })).toBeVisible();
      await expect(page.getByText(/No cataloged completed snapshots/)).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.getByRole('dialog')).toHaveCount(0);
      await page.getByRole('button', { name: 'Download FocusCost files' }).click();
      await expect(page.getByRole('dialog', { name: 'FocusCost files' })).toBeVisible();
      await expect(page.getByText('No FocusCost file is available yet.', { exact: true })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.getByRole('dialog')).toHaveCount(0);
    }
  }
  expect(errors).toEqual([]);
});

test('subscription and schedule loading states stay distinct from empty and populated results', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let releaseSubscriptions!: () => void;
  const subscriptionGate = new Promise<void>((resolve) => { releaseSubscriptions = resolve; });
  await page.route('**/api/subscriptions', async (route) => {
    await subscriptionGate;
    await route.fulfill({ json: [] });
  });
  await page.goto('/');
  const favicon = page.locator('link[rel="icon"]');
  await expect(favicon).toHaveAttribute('href', /\.(?:png|svg)$/);
  expect(await favicon.evaluate(async (link: HTMLLinkElement) => (await fetch(link.href)).ok)).toBe(true);
  await expect(page.getByLabel('Loading subscriptions')).toBeVisible();
  await expect(page.locator('.scope-ribbon')).toHaveAttribute('aria-busy', 'true');
  releaseSubscriptions();
  await expect(page.getByText('No accessible subscriptions returned.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run report', exact: true })).toBeDisabled();
  const skip = page.getByRole('link', { name: 'Skip to workspace' });
  await skip.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#workspace-main')).toBeFocused();
  let releaseSchedules!: () => void;
  const scheduleGate = new Promise<void>((resolve) => { releaseSchedules = resolve; });
  await page.route('**/api/schedules', async (route) => {
    await scheduleGate;
    await route.fulfill({ json: [{
      subscriptionId: '616dc9b8-b4aa-415f-8dcb-71bc462916c5', displayName: 'Synthetic subscription', state: 'active', recurrence: 'Monthly', scheduleStartAt: '2026-10-05T03:00:00Z', nextRunAt: '2026-10-05T03:00:00Z', availability: 'available',
      readAccess: true, costAccess: true, windowMonths: 6,
      latestRun: { runId: 'synthetic-run', subscriptionId: '616dc9b8-b4aa-415f-8dcb-71bc462916c5', period: '2026-03 to 2026-08', status: 'running', startedAt: '2026-09-10T06:00:00Z', completedAt: null, durationSeconds: null, error: null, completedMonths: 2, windowMonths: 6, months: [
        { period: '2026-03', status: 'succeeded' }, { period: '2026-04', status: 'succeeded' },
        { period: '2026-05', status: 'queued' }, { period: '2026-06', status: 'pending' },
        { period: '2026-07', status: 'pending' }, { period: '2026-08', status: 'pending' },
      ] },
    }] });
  });
  await page.getByRole('button', { name: 'Schedules', exact: true }).click();
  await expect(page.getByText('Loading FOCUS exports...', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run all', exact: true })).toBeDisabled();
  releaseSchedules();
  await expect(page.getByText('2026-03 to 2026-08 in progress', { exact: true })).toBeVisible();
  const monthProgress = page.getByRole('list', { name: '2026-03 to 2026-08 monthly export status' });
  await expect(monthProgress).toBeVisible();
  for (const label of ['Mar Done', 'Apr Done', 'May Queued', 'Jun Pending', 'Jul Pending', 'Aug Pending']) {
    await expect(monthProgress.getByRole('listitem', { name: label })).toBeVisible();
  }
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await page.getByRole('region', { name: 'FOCUS export schedules', exact: true }).focus();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-schedules.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await page.getByRole('button', { name: 'Delete Synthetic subscription', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Confirm delete Synthetic subscription', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel delete', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Confirm delete Synthetic subscription', exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('Schedules manages manually configured subscriptions without onboarding', async ({ page }, testInfo) => {
  const subscriptionId = '616dc9b8-b4aa-415f-8dcb-71bc462916c5';
  const unavailableId = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  const candidate = {
    subscriptionId, displayName: 'Verified Cost Reporting Subscription', readAccess: true, costAccess: true,
    windowMonths: 6, state: 'not_scheduled', recurrence: 'Monthly', scheduleStartAt: null, nextRunAt: null,
    latestRun: null, availability: 'available', statusMessage: null,
  };
  const requests: unknown[] = [];
  const onboardingRequests: string[] = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/onboarding') || path.startsWith('/api/subscriptions/discovery')) onboardingRequests.push(path);
  });
  let saved = false;
  await page.route('**/api/schedules', async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      requests.push(body);
      expect(Object.keys(body).sort()).toEqual(['scheduleStartAt', 'subscriptionId']);
      expect(body.subscriptionId).toBe(subscriptionId);
      saved = true;
      return route.fulfill({ status: 201, json: { ...candidate, state: 'active', scheduleStartAt: body.scheduleStartAt } });
    }
    return route.fulfill({ json: [
      { ...candidate, state: saved ? 'active' : 'not_scheduled' },
      { ...candidate, subscriptionId: unavailableId, displayName: 'Verified But Missing Export Prerequisites', state: 'unknown', availability: 'export_unavailable', statusMessage: 'An existing approved FOCUS export is required.' },
    ] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Schedules', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Subscription', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Onboard subscription', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Verify & schedule', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Reschedule Verified But Missing Export Prerequisites', exact: true })).toBeDisabled();
  await expect(page.getByRole('link', { name: 'Deploy access' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Subscription ID', exact: true })).toHaveCount(0);
  expect(requests).toEqual([]);
  await page.getByRole('button', { name: 'Schedule Verified Cost Reporting Subscription', exact: true }).click();
  await page.getByLabel('First monthly run (UTC)').fill('2030-10-05T03:00');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      await expect(page.getByLabel('First monthly run (UTC)')).toHaveValue('2030-10-05T03:00');
      await expect(page.getByRole('button', { name: 'Save schedule', exact: true })).toBeEnabled();
      expect(await page.locator('.schedule-editor').evaluate(editor => {
        const visible = editor.closest('.schedule-table-wrap')!.getBoundingClientRect();
        return [...editor.querySelectorAll('input, button')].every(control => {
          const bounds = control.getBoundingClientRect();
          return bounds.left >= visible.left && bounds.right <= visible.right && control.scrollWidth <= control.clientWidth;
        });
      })).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-manual-subscriptions.png`), fullPage: true, animations: 'disabled' });
    }
  }
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'monthly UTC schedule updated' })).toBeVisible();
  expect(requests).toEqual([{ subscriptionId, scheduleStartAt: '2030-10-05T03:00:00Z' }]);
  expect(onboardingRequests).toEqual([]);
  await expect(page.getByRole('button', { name: 'Reschedule Verified Cost Reporting Subscription', exact: true })).toBeVisible();
});

test('Schedules configures a FOCUS export and saves its schedule as separate portal actions', async ({ page }, testInfo) => {
  const subscriptionId = '616dc9b8-b4aa-415f-8dcb-71bc462916c5';
  const storageId = `/subscriptions/${subscriptionId}/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/teststore`;
  const candidate = { subscriptionId, displayName: 'Pilot subscription', readAccess: true, costAccess: true,
    windowMonths: 6, recurrence: 'Monthly', scheduleStartAt: null, nextRunAt: null, latestRun: null };
  const details = { subscriptionId, exportName: 'focus-closed-month-app-pilot', storageResourceId: storageId,
    container: 'cost-exports', rootFolderPath: `focus/${subscriptionId}`, format: 'Csv', dataVersion: '1.2-preview',
    windowMonths: 6, nativeSchedule: 'Inactive', destinationRole: 'Storage Blob Data Contributor',
    destinationRoleScope: `${storageId}/blobServices/default/containers/cost-exports` };
  let configured = false;
  let saved = false;
  const mutations: { path: string; body: unknown }[] = [];
  const browserAzureRequests: string[] = [];
  page.on('request', request => {
    if (new URL(request.url()).hostname === 'management.azure.com') browserAzureRequests.push(request.url());
  });
  await page.route(`**/api/schedules/${subscriptionId}/export`, route => {
    if (route.request().method() === 'PUT') {
      expect(route.request().postDataJSON()).toEqual({ allowDestinationRoleAssignment: true });
      mutations.push({ path: 'export', body: route.request().postDataJSON() });
      configured = true;
      return route.fulfill({ json: { ...details, state: 'configured', canConfigure: false, created: true } });
    }
    return route.fulfill({ json: { ...details, state: configured ? 'configured' : 'missing', canConfigure: !configured } });
  });
  await page.route('**/api/schedules', route => {
    if (route.request().method() === 'POST') {
      expect(configured).toBe(true);
      const body = route.request().postDataJSON();
      expect(body).toEqual({ subscriptionId, scheduleStartAt: '2030-10-05T03:00:00Z' });
      mutations.push({ path: 'schedule', body });
      saved = true;
      return route.fulfill({ status: 201, json: { ...candidate, state: 'active', availability: 'available', scheduleStartAt: body.scheduleStartAt } });
    }
    return route.fulfill({ json: [{ ...candidate, state: saved ? 'active' : configured ? 'not_scheduled' : 'unknown',
      availability: configured ? 'available' : 'export_unavailable', statusMessage: configured ? null : 'FOCUS export is not configured.' }] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Schedules', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reschedule Pilot subscription', exact: true })).toBeDisabled();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      await page.getByRole('button', { name: 'Configure export for Pilot subscription', exact: true }).click();
      const panel = page.getByRole('region', { name: 'Export configuration for Pilot subscription', exact: true });
      await expect(panel.getByText('focus-closed-month-app-pilot', { exact: true })).toBeVisible();
      const confirmation = panel.getByRole('checkbox', { name: 'Allow Storage Blob Data Contributor for the export identity on this container', exact: true });
      await expect(confirmation).not.toBeChecked();
      await expect(panel.getByRole('button', { name: 'Configure export', exact: true })).toBeDisabled();
      await confirmation.check();
      await expect(panel.getByRole('button', { name: 'Configure export', exact: true })).toBeEnabled();
      expect(await panel.evaluate(editor => {
        const visible = editor.closest('.schedule-table-wrap')!.getBoundingClientRect();
        return [...editor.querySelectorAll('input, button, dt, dd')].every(control => {
          const bounds = control.getBoundingClientRect();
          return bounds.left >= visible.left && bounds.right <= visible.right && control.scrollWidth <= control.clientWidth;
        });
      })).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(mutations).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-export-configuration.png`), fullPage: true, animations: 'disabled' });
      await panel.getByRole('button', { name: 'Close', exact: true }).click();
    }
  }
  await page.getByRole('button', { name: 'Configure export for Pilot subscription', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Export configuration for Pilot subscription', exact: true });
  await panel.getByRole('checkbox').check();
  await panel.getByRole('button', { name: 'Configure export', exact: true }).click();
  await expect(page.getByLabel('First monthly run (UTC)')).toBeVisible();
  expect(mutations).toEqual([{ path: 'export', body: { allowDestinationRoleAssignment: true } }]);
  await page.getByLabel('First monthly run (UTC)').fill('2030-10-05T03:00');
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'monthly UTC schedule updated' })).toBeVisible();
  expect(mutations).toEqual([
    { path: 'export', body: { allowDestinationRoleAssignment: true } },
    { path: 'schedule', body: { subscriptionId, scheduleStartAt: '2030-10-05T03:00:00Z' } },
  ]);
  expect(browserAzureRequests).toEqual([]);
  await expect(page.getByRole('heading', { name: 'Onboard subscription', exact: true })).toHaveCount(0);
});

test('Schedules keeps ready and unavailable subscriptions visible without unsafe actions', async ({ page }, testInfo) => {
  const base = { readAccess: true, costAccess: true, accessCheckMode: 'permissions', windowMonths: 6,
    state: 'not_scheduled', recurrence: 'Monthly', scheduleStartAt: null, nextRunAt: null,
    latestRun: null, availability: 'available', statusMessage: null };
  const mutations: string[] = [];
  const costRequests: string[] = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/schedules') && request.method() !== 'GET') mutations.push(url.pathname);
    if (url.hostname === 'management.azure.com' || url.pathname.includes('Microsoft.CostManagement/query')) costRequests.push(url.toString());
  });
  await page.route('**/api/schedules', route => route.fulfill({ json: [
    { ...base, subscriptionId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', displayName: 'Ready subscription' },
    { ...base, subscriptionId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', displayName: 'Throttled subscription',
      state: 'unknown', readAccess: false, costAccess: false, availability: 'access_unavailable',
      statusMessage: 'Azure subscription access check was throttled. Retry after 120 seconds.' },
    { ...base, subscriptionId: 'cccccccc-cccc-cccc-cccc-cccccccccccc', displayName: 'Pending processor setup',
      state: 'unknown', availability: 'configuration_unavailable', statusMessage: 'The six-month FOCUS worker is not enabled.' },
  ] }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Schedules', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Schedule Ready subscription', exact: true })).toBeEnabled();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.locator('.operations-metrics strong[data-unavailable="true"]')).toHaveText(['Unavailable', 'Unavailable']);
  await expect(page.getByRole('button', { name: 'Run all', exact: true })).toBeDisabled();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      for (const name of ['Throttled subscription', 'Pending processor setup']) {
        const row = page.getByRole('row').filter({ hasText: name });
        await expect(row).toBeVisible();
        for (const button of await row.getByRole('button').all()) {
          if (name === 'Pending processor setup' && await button.getAttribute('aria-label') === `Configure export for ${name}`) {
            await expect(button).toBeEnabled();
          } else await expect(button).toBeDisabled();
        }
      }
      await expect(page.getByText('Access unavailable', { exact: true })).toBeAttached();
      await expect(page.getByText('Scheduler setup incomplete', { exact: true })).toBeAttached();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-schedule-readiness.png`), fullPage: true, animations: 'disabled' });
      for (const message of ['Azure subscription access check was throttled. Retry after 120 seconds.', 'The six-month FOCUS worker is not enabled.']) {
        const status = page.getByText(message, { exact: true });
        await status.scrollIntoViewIfNeeded();
        await expect(status).toBeVisible();
        const bounds = await status.boundingBox();
        expect(bounds).not.toBeNull();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
      }
      if (width === 390) await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-schedule-readiness-status.png`), fullPage: true, animations: 'disabled' });
      await page.getByText('Ready subscription', { exact: true }).scrollIntoViewIfNeeded();
    }
  }
  expect(mutations).toEqual([]);
  expect(costRequests).toEqual([]);
  await expect(page.getByRole('heading', { name: 'Onboard subscription', exact: true })).toHaveCount(0);
});

test('populated resource evidence and page search stay usable across viewports', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/report**', (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith('/latest') ? { ...snapshotFixture, report: resourceReportFixture } : path === '/api/report' ? resourceReportFixture : [] });
  });
  await page.goto('/');
  await runReport(page);
  await page.evaluate(() => document.fonts.ready);
  const fontRoles = await page.evaluate(() => ['--font-geist', '--font-geist-mono'].map((name) => getComputedStyle(document.documentElement).getPropertyValue(name)));
  expect(fontRoles.every((font) => font.trim().startsWith("'Segoe UI'"))).toBe(true);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await selectReportPage(page, 'Storage Optimization');
      const finding = page.getByRole('button', { name: /Unattached disks/ });
      if (await finding.getAttribute('aria-expanded') === 'false') await finding.click();
      await expect(page.locator('.resource-table')).toContainText('finance-archive-evidence-disk');
      await expect(page.getByText('Last-access inventory not collected', { exact: true })).toBeVisible();
      const evidenceFontSizes = await page.locator('.storage-tier-table small, .tier-legend span').evaluateAll((elements) => elements.map((element) => parseFloat(getComputedStyle(element).fontSize)));
      expect(Math.min(...evidenceFontSizes)).toBeGreaterThanOrEqual(12);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-storage-evidence.png`), fullPage: true, animations: 'disabled' });
      const navigation = page.getByRole('navigation', { name: 'Report navigation' });
      const mobileMenu = navigation.getByRole('button', { name: 'Report pages' });
      if (await mobileMenu.isVisible()) await mobileMenu.click();
      const search = page.getByRole('searchbox', { name: 'Find a report page' });
      await search.fill('no results for this query');
      await expect(page.getByText('No matching pages', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: 'Clear page search' }).click();
      await search.fill('SQL');
      await search.press('Enter');
      await expect(page.locator('#report-page-heading')).toHaveText('Azure SQL Optimization');
      await page.locator('.sql-analysis-resource > summary').click();
      await expect(page.getByText('Representative workload evidence is required before resizing.', { exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-sql-evidence.png`), fullPage: true, animations: 'disabled' });
      await selectReportPage(page, 'Action Plan');
      await expect(page.getByLabel('Owner for Review unattached disk')).toBeEnabled();
      await page.getByLabel('Owner for Review unattached disk').fill('Finance team');
      await expect(page.getByLabel('Owner for Review unattached disk')).toHaveValue('Finance team');
      const actionLayout = await page.evaluate(() => ({
        viewport: innerWidth, page: document.documentElement.scrollWidth,
        regions: ['.dashboard-main', '.tab-panel', '.action-plan-scroll'].map((selector) => {
          const element = document.querySelector(selector)!;
          const bounds = element.getBoundingClientRect();
          return { selector, width: bounds.width, right: bounds.right, scrollWidth: element.scrollWidth };
        }),
      }));
      expect(actionLayout.page, JSON.stringify(actionLayout)).toBeLessThanOrEqual(actionLayout.viewport);
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-action-plan.png`), fullPage: true, animations: 'disabled' });
    }
  }
  expect(errors).toEqual([]);
});

test('chat never displays a reply from a previous report snapshot', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let releaseAnswer!: () => void;
  const gate = new Promise<void>((resolve) => { releaseAnswer = resolve; });
  const answer = { intent: 'overview', metrics: [], resources: [], suggestions: [], evidenceKeys: [], disclaimer: 'Synthetic snapshot evidence.', responseMode: 'deterministic_fallback', selectedModel: null, usage: null, dataAsOf: '2026-09-10T10:00:00Z' };
  await page.route('**/api/chat', async (route) => {
    await gate;
    await route.fulfill({ json: { ...answer, answer: 'Reply for the previous report.' } });
  });
  await page.goto('/');
  await runReport(page);
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await page.getByLabel('Ask about this report').fill('Explain the old report');
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByLabel('Ask about this report')).toBeDisabled();
  await page.route('**/api/report/latest?**', (route) => route.fulfill({ json: { ...snapshotFixture, snapshotId: 'visual-report-2' } }));
  await page.getByRole('button', { name: 'Report', exact: true }).click();
  await page.getByRole('button', { name: 'Update report', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Update report', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(page.getByLabel('Ask about this report')).toBeEnabled();
  await expect(page.locator('.chat-message')).toHaveCount(0);
  const previousReply = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat');
  releaseAnswer();
  await (await previousReply).finished();
  await page.route('**/api/chat', (route) => route.fulfill({ json: { ...answer, answer: 'Reply for the current report.' } }));
  await page.getByLabel('Ask about this report').fill('Explain the new report');
  await page.getByRole('button', { name: 'Send question' }).click();
  await expect(page.getByText('Reply for the current report.', { exact: true })).toBeVisible();
  await expect(page.getByText('Reply for the previous report.', { exact: true })).toHaveCount(0);
  await expect(page.locator('.chat-message.assistant')).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('saved-report startup restores history without assessment and retains compact layout', async ({ page }, testInfo) => {
  const requests: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (['/api/report', '/api/report/latest', '/api/narrate'].includes(path)) requests.push(path);
  });
  await page.route('**/api/report/latest?**', (route) => {
    const query = new URL(route.request().url()).searchParams;
    expect(query.getAll('subscriptionId')).toEqual(['sub-1']);
    expect(query.get('staleDays')).toBe('90');
    return route.fulfill({ json: snapshotFixture });
  });
  await page.goto('/');
  await expect(page.locator('#report-page-heading')).toHaveText('Executive Summary', { timeout: 15000 });
  await expect(page.getByRole('button', { name: 'Update report', exact: true })).toBeEnabled();
  expect(requests).toEqual(['/api/report/latest']);
  await expect(page.locator('.cost-analysis-details .billing-filters')).toHaveCount(0);
  await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
  const compactHeight = await page.locator('.dashboard-frame').evaluate((element) => element.getBoundingClientRect().height);
  await page.getByRole('button', { name: 'Switch to comfortable layout' }).click();
  const comfortableHeight = await page.locator('.dashboard-frame').evaluate((element) => element.getBoundingClientRect().height);
  expect(compactHeight).toBeLessThanOrEqual(comfortableHeight);
  await page.reload();
  await expect(page.locator('#report-page-heading')).toHaveText('Executive Summary', { timeout: 15000 });
  await expect(page.locator('html')).toHaveAttribute('data-density', 'comfortable');
  expect(requests).toEqual(['/api/report/latest', '/api/report/latest']);
  await testInfo.attach('historical-startup.json', { body: JSON.stringify({ requests, compactHeight, comfortableHeight, note: 'Synthetic saved-report path; no Azure assessment or narration.' }), contentType: 'application/json' });
});

test('ADO period comparisons, resource detail, budgets and downloads work across themes and viewports', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/report**', (route) => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('/latest') ? { ...snapshotFixture, report: detailReportFixture } : detailReportFixture }));
  await page.route('**/api/budgets?**', (route) => route.fulfill({ json: [50, 105, 140].map((currentSpend, index) => ({ subscriptionId: 'sub-1', name: `Finance budget ${index + 1}`, category: 'Cost', amount: 100, currency: 'USD', currentSpend, forecastSpend: null, timeGrain: 'Monthly', periodStart: '2026-09-01', periodEnd: '2027-09-01', filter: { tags: { name: 'application', operator: 'In', values: ['Finance'] } }, scope: '/subscriptions/sub-1', observedAt: '2026-09-12T00:00:00Z' })) }));
  await page.route('**/api/report/resource-availability?**', (route) => route.fulfill({ json: { resourceId: detailReportFixture.costDetails!.rows[0].resourceId, date: '2026-09-07', status: 'partial', statusMessage: 'Missing minutes are unknown', availableHours: null, observedAvailableHours: 12, coverageMinutes: 720, expectedMinutes: 1440, metric: 'VmAvailabilityMetric', observedAt: '2026-09-12T00:00:00Z' } }));
  let exportRequest: Record<string, unknown> | null = null;
  await page.route('**/api/report/cost-details/export', (route) => {
    exportRequest = route.request().postDataJSON();
    return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: 'Synthetic workbook transport; real workbook contents validated by pytest' });
  });
  await page.goto('/');
  await expect(page.locator('#report-page-heading')).toHaveText('Executive Summary');
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 720, height: 500 }]) {
    await page.setViewportSize(viewport);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await selectReportPage(page, 'Executive Summary');
      await page.getByRole('group', { name: 'Cost comparison range' }).getByRole('button', { name: '7d', exact: true }).click();
      await expect(page.getByLabel('Cost window start')).toHaveValue('2026-09-01');
      await expect(page.locator('.budget-within')).toContainText('Within budget');
      await expect(page.locator('.budget-warning')).toContainText('10%');
      await expect(page.locator('.budget-over')).toContainText('More than');
      const point = page.locator('[data-cost-date="2026-09-07"]');
      await point.focus(); await point.press('Enter');
      const detail = page.getByRole('region', { name: 'Selected day resource detail' });
      await expect(detail).toContainText('2026-09-07 vs 2026-08-31');
      await expect(detail).toContainText('Finance team');
      await detail.getByRole('button', { name: /Check VM availability/ }).click();
      await expect(detail).toContainText('12.00 h observed (partial)');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-resource-comparison.png`), fullPage: true, animations: 'disabled' });
      await page.locator('.period-anomaly-link').click();
      await expect(page.getByRole('region', { name: 'Selected period anomalies' })).toContainText('finance-vm');
      await expect(page.getByLabel('Cost window start')).toHaveValue('2026-09-01');
      await selectReportPage(page, 'Subscription Breakdown');
      await page.getByLabel('Cost grouping').selectOption('region');
      await expect(page.getByRole('region', { name: 'Grouped subscription costs' })).toContainText('centralindia');
      await page.getByLabel('Cost tag key').selectOption('application');
      await page.getByLabel('Cost tag value').selectOption(JSON.stringify('Finance'));
      await page.getByLabel('Cost window start').fill('2026-09-05');
      await page.getByLabel('Cost window end').fill('2026-09-06');
      const download = page.waitForEvent('download');
      await page.getByRole('button', { name: 'Download cost report', exact: true }).click();
      expect((await download).suggestedFilename()).toContain('2026-09-05-2026-09-06');
      expect(exportRequest).toMatchObject({ snapshotId: snapshotFixture.snapshotId, startDate: '2026-09-05', endDate: '2026-09-06', filters: { tagKey: 'application', tagValue: 'Finance' } });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${viewport.width}-${theme}-cost-breakdown.png`), fullPage: true, animations: 'disabled' });
    }
  }
  expect(errors).toEqual([]);
});

test('AI billing alerts expose spike and drop evidence in both themes and on mobile', async ({ page }, testInfo) => {
  const base = anomalyFixture.anomalies[0];
  const aiAnomalies = [
    { ...base, anomalyId: 'ai-spike', dimensionType: 'resource', dimensionName: 'OpenAI account', anomalyType: 'spike', actualCost: 240, expectedCost: 120, absoluteDelta: 120, percentageDelta: 1 },
    { ...base, anomalyId: 'ai-drop', dimensionType: 'service', dimensionName: 'Azure OpenAI', anomalyType: 'drop', actualCost: 40, expectedCost: 120, absoluteDelta: -80, percentageDelta: -2 / 3 },
  ];
  await page.route('**/api/anomalies', (route) => route.fulfill({ json: { ...anomalyFixture, aiAnomalies } }));
  await page.goto('/');
  await runReport(page);
  await selectReportPage(page, 'AI Optimization');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await page.getByLabel('AI anomaly type').selectOption('all');
      const spike = page.getByRole('article', { name: 'Cost spike: OpenAI account' });
      const drop = page.getByRole('article', { name: 'Cost drop: Azure OpenAI' });
      await expect(spike).toContainText('+$120.00');
      await expect(drop).toContainText('-$80.00');
      await spike.locator('details').evaluate((element) => { (element as HTMLDetailsElement).open = true; });
      await expect(spike.getByRole('link', { name: 'Investigate in Azure' })).toHaveAttribute('href', base.investigationUrl);
      await expect(spike).toContainText('same-weekday samples');
      const colors = await page.locator('.ai-signal-direction').evaluateAll((elements) => elements.map((element) => getComputedStyle(element).color));
      expect(colors[0]).not.toBe(colors[1]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${width}-${theme}-ai-alerts.png`), fullPage: true, animations: 'disabled' });
      await page.getByLabel('AI anomaly type').selectOption('drop');
      await expect(page.locator('.ai-cost-signal')).toHaveCount(1);
      await expect(page.getByRole('region', { name: 'AI billing anomaly alerts' })).toContainText('not a verified saving');
    }
  }
});