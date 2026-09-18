import { expect, test } from '@playwright/test';

test.skip(process.env.MEGHKOSHA_LIVE_IDENTITY_CHECK !== 'true', 'Requires explicitly approved Entra configuration and the local API.');

test('rejects unsigned API requests and reaches the real tenant sign-in page', async ({ page, request }) => {
  const unauthorized = await request.get('http://127.0.0.1:8002/api/auth/me');
  expect(unauthorized.status()).toBe(401);
  const config = await request.get('http://127.0.0.1:8002/api/auth/config');
  expect(config.status()).toBe(200);
  const configuration = await config.json();
  expect(configuration.scope).toBe(`api://${configuration.apiClientId}/access_as_user`);
  await page.goto('/');
  const connect = page.getByRole('button', { name: 'Sign in with Microsoft', exact: true });
  await expect(connect).toBeEnabled();
  await connect.click();
  await page.waitForURL((url) => url.hostname === 'login.microsoftonline.com', { timeout: 30000 });
  await expect(page.getByRole('textbox').first()).toBeVisible();
});