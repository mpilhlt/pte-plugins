/**
 * MPCDF LLM plugin API tests: access control and the session state.
 * No test calls a route that reaches MPCDF.
 *
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/routes.py
 * @testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
 */
import { test, describe, before, after } from 'node:test';
import assert from 'node:assert';
import {
  authenticatedApiCall,
  authenticatedRequest,
  loginAsAdmin,
  loginAsBasicUser,
  logout
} from '../helpers/test-auth.js';

const BASE = '/api/plugins/mpcdf-llm';

describe('MPCDF LLM plugin API', () => {
  /** @type {string} */
  let admin;
  /** @type {string} */
  let user;

  before(async () => {
    admin = (await loginAsAdmin()).sessionId;
    user = (await loginAsBasicUser()).sessionId;
  });

  after(async () => {
    await logout(admin);
    await logout(user);
  });

  test('non-admins get 403 on presets and the management page', async () => {
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/admin/presets`, 'GET')).status, 403);
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/manage`, 'GET')).status, 403);
  });

  test('non-admins get 403 on the managed endpoints', async () => {
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/managed`, 'GET')).status, 403);
  });

  test('admins can read presets and open the management page', async () => {
    const presets = await authenticatedRequest(admin, `${BASE}/admin/presets`, 'GET');
    assert.strictEqual(presets.status, 200);
    const page = await authenticatedRequest(admin, `${BASE}/manage`, 'GET');
    assert.strictEqual(page.status, 200);
    assert.match(await page.text(), /MPCDF LLM/);
  });

  test('admins can import presets without an MPCDF session', async () => {
    const preset = { name: 'api-test-import', framework: 'vLLM', model: 'openai/gpt-oss-120b', machine: 'Viper', gpu_type: 'mi300a', gpus: 1 };
    const result = await authenticatedApiCall(admin, `${BASE}/presets/import`, 'POST', [preset, { model: 'no name' }]);
    assert.deepStrictEqual(result.saved, ['api-test-import']);
    assert.deepStrictEqual(Object.keys(result.errors), ['#2']);
    const listed = await authenticatedApiCall(admin, `${BASE}/admin/presets`, 'GET');
    assert.ok(listed.some((p) => p.name === 'api-test-import'));
    const deleted = await authenticatedRequest(admin, `${BASE}/admin/presets/api-test-import`, 'DELETE');
    assert.strictEqual(deleted.status, 204);
  });

  test('non-admins get 403 on the preset import', async () => {
    const response = await authenticatedRequest(user, `${BASE}/presets/import`, 'POST', []);
    assert.strictEqual(response.status, 403);
  });

  test('the session starts out as login_required', async () => {
    const session = await authenticatedApiCall(admin, `${BASE}/admin/session`, 'GET');
    assert.strictEqual(session.state, 'login_required');
  });

  test('the library read router (API keys) is not mounted', async () => {
    const response = await authenticatedRequest(admin, `${BASE}/endpoints`, 'GET');
    assert.strictEqual(response.status, 404);
  });
});
