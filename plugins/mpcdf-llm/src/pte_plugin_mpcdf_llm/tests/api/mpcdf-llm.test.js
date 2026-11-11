/**
 * MPCDF LLM plugin API tests: access control, per-user routes and the session state.
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

  test('every user opens the management page and reads the presets', async () => {
    for (const session of [admin, user]) {
      const page = await authenticatedRequest(session, `${BASE}/manage`, 'GET');
      assert.strictEqual(page.status, 200);
      assert.match(await page.text(), /My MPCDF endpoints/);
      const presets = await authenticatedRequest(session, `${BASE}/my/presets`, 'GET');
      assert.strictEqual(presets.status, 200);
    }
  });

  test('every user gets their own managed endpoints and context', async () => {
    assert.deepStrictEqual(await authenticatedApiCall(user, `${BASE}/managed`, 'GET'), []);
    const context = await authenticatedApiCall(user, `${BASE}/context`, 'GET');
    assert.strictEqual(context.is_admin, false);
    assert.strictEqual(typeof context.hf_token_stored, 'boolean');
    assert.strictEqual((await authenticatedApiCall(admin, `${BASE}/context`, 'GET')).is_admin, true);
  });

  test('non-admins cannot change the preset catalog', async () => {
    const preset = { name: 'api-test-user', framework: 'vLLM', model: 'openai/gpt-oss-120b', machine: 'Viper', gpu_type: 'mi300a', gpus: 1 };
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/my/presets/api-test-user`, 'PUT', preset)).status, 403);
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/my/presets/api-test-user`, 'DELETE')).status, 403);
  });

  test('the overview of all users and session removal are admin only', async () => {
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/overview`, 'GET')).status, 403);
    assert.strictEqual((await authenticatedRequest(user, `${BASE}/users/someone/session`, 'DELETE')).status, 403);
    const overview = await authenticatedRequest(admin, `${BASE}/overview`, 'GET');
    assert.strictEqual(overview.status, 200);
    assert.ok(Array.isArray(await overview.json()));
  });

  test('the Hugging Face read token is an API key users can store', async () => {
    const services = await authenticatedApiCall(user, '/api/v1/secrets', 'GET');
    assert.ok(services.some((s) => s.id === 'hf-read-token'));
    assert.ok(!services.some((s) => s.id === 'mpcdf-llm-session'));
  });

  test('admins can import presets without an MPCDF session', async () => {
    const preset = { name: 'api-test-import', framework: 'vLLM', model: 'openai/gpt-oss-120b', machine: 'Viper', gpu_type: 'mi300a', gpus: 1 };
    const result = await authenticatedApiCall(admin, `${BASE}/presets/import`, 'POST', [preset, { model: 'no name' }]);
    assert.deepStrictEqual(result.saved, ['api-test-import']);
    assert.deepStrictEqual(Object.keys(result.errors), ['#2']);
    const listed = await authenticatedApiCall(admin, `${BASE}/my/presets`, 'GET');
    assert.ok(listed.some((p) => p.name === 'api-test-import'));
    const deleted = await authenticatedRequest(admin, `${BASE}/my/presets/api-test-import`, 'DELETE');
    assert.strictEqual(deleted.status, 204);
  });

  test('non-admins get 403 on the preset import', async () => {
    const response = await authenticatedRequest(user, `${BASE}/presets/import`, 'POST', []);
    assert.strictEqual(response.status, 403);
  });

  test('every user starts without an MPCDF session', async () => {
    for (const session of [admin, user]) {
      const state = await authenticatedApiCall(session, `${BASE}/my/session`, 'GET');
      assert.strictEqual(state.state, 'login_required');
    }
  });

  test('the library read router (API keys) is not mounted', async () => {
    for (const path of ['/endpoints', '/my/available', '/llm/available']) {
      const response = await authenticatedRequest(admin, `${BASE}${path}`, 'GET');
      assert.strictEqual(response.status, 404, path);
    }
  });
});
