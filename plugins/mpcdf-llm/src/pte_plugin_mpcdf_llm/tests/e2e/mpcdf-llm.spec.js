/**
 * MPCDF LLM plugin E2E smoke test: an admin opens the dialog and sees the login form.
 * The real login cannot run here.
 *
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/static/manage.html
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/routes.py
 */

import { test, expect } from '../fixtures/debug-on-failure.js';
import { performLogin } from './helpers/login-helper.js';

test.describe('MPCDF LLM plugin', () => {
  test('an admin opens the dialog and sees the MPCDF login form', async ({ page }) => {
    await page.goto('/');
    await performLogin(page, 'testadmin', 'adminpass');

    await page.locator('sl-button[name="pluginsBtn"]').click();
    await page.locator('sl-menu-item[data-plugin-id="mpcdf-llm"][data-endpoint-name="manage"]').click();

    const frame = page.frameLocator('iframe[src*="/api/plugins/mpcdf-llm/manage"]');
    await expect(frame.locator('h1, h2').first()).toContainText('MPCDF LLM', { timeout: 10000 });
    await expect(frame.locator('#login')).toBeVisible({ timeout: 10000 });
    await expect(frame.locator('#username')).toBeVisible();
    await expect(frame.locator('#password')).toBeVisible();
    await expect(frame.locator('#otp')).toBeVisible();
  });
});
