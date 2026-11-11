/**
 * MPCDF LLM plugin E2E smoke test: every user opens the dialog and sees the login form; only admins see the presets.
 * The real login cannot run here.
 *
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/static/manage.html
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/routes.py
 */

/**
 * @import { Page } from '@playwright/test'
 */

import { test, expect } from '../fixtures/debug-on-failure.js';
import { performLogin } from './helpers/login-helper.js';

/**
 * Open the plugin's dialog and return its frame.
 * @param {Page} page
 */
async function openDialog(page) {
  await page.locator('sl-button[name="pluginsBtn"]').click();
  await page.locator('sl-menu-item[data-plugin-id="mpcdf-llm"][data-endpoint-name="manage"]').click();
  const frame = page.frameLocator('iframe[src*="/api/plugins/mpcdf-llm/manage"]');
  await expect(frame.locator('h2').first()).toContainText('My MPCDF endpoints', { timeout: 10000 });
  await expect(frame.locator('#login')).toBeVisible({ timeout: 10000 });
  await expect(frame.locator('#username')).toBeVisible();
  await expect(frame.locator('#password')).toBeVisible();
  await expect(frame.locator('#otp')).toBeVisible();
  return frame;
}

test.describe('MPCDF LLM plugin', () => {
  test('an admin sees the login form, the presets and the overview', async ({ page }) => {
    await page.goto('/');
    await performLogin(page, 'testadmin', 'adminpass');
    const frame = await openDialog(page);
    await expect(frame.locator('#admin-presets')).toBeVisible();
    await expect(frame.locator('#admin-overview')).toBeVisible();
  });

  test('a user sees the login form but not the presets', async ({ page }) => {
    await page.goto('/');
    await performLogin(page, 'testuser', 'testpass');
    const frame = await openDialog(page);
    await expect(frame.locator('#hf-hint')).toBeVisible();
    await expect(frame.locator('#admin-presets')).toBeHidden();
    await expect(frame.locator('#admin-overview')).toBeHidden();
  });
});
