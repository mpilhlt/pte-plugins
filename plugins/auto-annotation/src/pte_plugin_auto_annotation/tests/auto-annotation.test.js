/**
 * Unit tests for the auto-annotation frontend extension.
 *
 * @testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/extensions/auto-annotation.js
 */

import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert';

class FakeFrontendExtensionPlugin {
  constructor(context, config = {}) {
    this.context = context;
    this._config = config;
    this._deps = {};
    this.state = {};
  }
  getDependency(name) {
    if (!(name in this._deps)) throw new Error(`no stub dependency registered for "${name}"`);
    return this._deps[name];
  }
  async dispatchStateChange(changes) {
    this.state = { ...this.state, ...changes };
    return this.state;
  }
}
global.FrontendExtensionPlugin = FakeFrontendExtensionPlugin;
global.document = { createElement: () => ({ addEventListener(t, fn) { this._click = fn; }, style: {} }) };

const { default: AutoAnnotationExtension, runCorrection } = await import('../extensions/auto-annotation.js');

/** @param {number[]} units @param {Record<number, string>} changed */
function batchResult(units, changed = {}, status = 'ok', error = null) {
  return { units: units.map(index => ({ index, status, xml: changed[index] ?? null })), error, calls: 1, retries: 0, seconds: 1 };
}

describe('runCorrection', () => {
  it('collects the corrections of all batches and reports progress', async () => {
    const progress = [];
    const outcome = await runCorrection(
      async units => batchResult(units, { 0: '<bibl>a</bibl>', 3: '<bibl>d</bibl>' }),
      [[0, 1, 2], [3]],
      { onProgress: (p, t, c) => progress.push([p, t, c]) }
    );
    assert.strictEqual(outcome.state, 'done');
    assert.deepStrictEqual([...outcome.corrections], [[0, '<bibl>a</bibl>'], [3, '<bibl>d</bibl>']]);
    assert.strictEqual(outcome.processed, 4);
    assert.deepStrictEqual(progress, [[3, 4, 1], [4, 4, 2]]);
  });

  it('counts invalid answers and stops at a failed request', async () => {
    let calls = 0;
    const outcome = await runCorrection(async units => {
      calls++;
      return calls === 1 ? batchResult(units, {}, 'text_changed') : batchResult(units, {}, 'error', 'provider down');
    }, [[0], [1], [2]]);
    assert.strictEqual(outcome.state, 'failed');
    assert.strictEqual(outcome.error, 'provider down');
    assert.deepStrictEqual([outcome.processed, outcome.invalid, outcome.errors, calls], [2, 1, 1, 2]);
  });

  it('stops before the next batch when cancelled, and on HTTP errors', async () => {
    let cancelled = false;
    const outcome = await runCorrection(async units => { cancelled = true; return batchResult(units, { 0: '<bibl/>' }); }, [[0], [1]], { isCancelled: () => cancelled });
    assert.deepStrictEqual([outcome.state, outcome.processed, outcome.corrections.size], ['cancelled', 1, 1]);
    const failed = await runCorrection(async () => { throw new Error('409 lock lost'); }, [[0]]);
    assert.deepStrictEqual([failed.state, failed.error, failed.processed], ['failed', '409 lock lost', 0]);
  });
});

describe('AutoAnnotationExtension.runCorrection', () => {
  /** @type {any} */
  let ext;
  /** @type {string[]} */
  let calls;
  /** @type {string[]} */
  let loaded;
  /** @type {boolean[]} */
  let confirmAnswers;
  /** @type {string[]} */
  let confirmMessages;

  beforeEach(() => {
    ext = new AutoAnnotationExtension({});
    ext._lockRefreshMs = 1e9;
    calls = [];
    loaded = [];
    confirmAnswers = [true];
    confirmMessages = [];
    ext.state = { xml: 'v1', editorReadOnly: false, user: { roles: ['annotator'] }, fileData: [{ artifacts: [{ id: 'v1' }], doc_metadata: { title: 'Doc' } }] };
    ext._deps = {
      'sl-utils': { notify: (msg) => calls.push(`notify:${msg}`) },
      dialog: { confirm: async (msg) => { confirmMessages.push(msg); return confirmAnswers.shift() ?? false; } },
      'inference-settings': { getDefaultModel: () => ({ providerId: 'kisski', modelId: 'ds' }), getModelLabel: () => 'DeepSeek' },
      xmleditor: { saveIfDirty: async () => calls.push('saveIfDirty'), getXmlTree: () => null, on() {} },
      progress: { show() {}, hide() {}, setValue() {}, setLabel() {} },
      client: { saveXml: async (xml, id, asNew) => { calls.push(`save:${id}:${asNew}`); return { file_id: 'v2' }; }, releaseLock: async (id) => { calls.push(`releaseLock:${id}`); } },
      'file-selection': { reload: async () => {} },
      services: { load: async ({ xml }) => { loaded.push(xml); ext.state.xml = xml; ext.state.editorReadOnly = false; } },
    };
    ext.callPluginApi = async (path, method, body) => {
      const route = path.split('/').pop();
      calls.push(route);
      if (route === 'start') {
        assert.strictEqual(ext.state.editorReadOnly, true, 'read-only before the server takes the lock');
        return { job_id: 'j1', doc_id: 'd', variant: null, unit_count: 2, batches: [[0], [1]] };
      }
      if (route === 'batch') return batchResult(body.units, { 0: '<bibl>x</bibl>' });
      if (route === 'finish') {
        assert.deepStrictEqual([body.provider_id, body.model_id, body.model_label, body.corrections], ['kisski', 'ds', 'DeepSeek', [{ index: 0, xml: '<bibl>x</bibl>' }]]);
        return { xml: '<TEI/>', changed: 1 };
      }
      return { status: 'released' };
    };
  });

  it('loads the new version without asking when the source is still open', async () => {
    await ext.runCorrection();
    assert.deepStrictEqual(calls.filter(c => !c.startsWith('notify')), ['saveIfDirty', 'start', 'batch', 'batch', 'finish', 'save:v1:true', 'release']);
    assert.deepStrictEqual(loaded, ['v2']);
    assert.strictEqual(confirmMessages.length, 1);
  });

  it('asks before loading the new version when the user opened another document', async () => {
    const batch = ext.callPluginApi;
    ext.callPluginApi = async (path, method, body) => {
      if (path.endsWith('/batch')) ext.state.xml = 'other';
      return batch(path, method, body);
    };
    confirmAnswers = [true, true];
    await ext.runCorrection();
    assert.ok(calls.includes('releaseLock:v2'));
    assert.match(confirmMessages[1], /Load it now\?/);
    assert.deepStrictEqual(loaded, ['v2']);
  });

  it('saves nothing and makes the source editable again when nothing changed', async () => {
    ext.callPluginApi = async (path, method, body) => {
      const route = path.split('/').pop();
      calls.push(route);
      if (route === 'start') return { job_id: 'j1', doc_id: 'd', variant: null, unit_count: 1, batches: [[0]] };
      if (route === 'batch') return batchResult(body.units);
      return { status: 'released' };
    };
    await ext.runCorrection();
    assert.ok(!calls.some(c => c.startsWith('save:')));
    assert.ok(calls.includes('release'));
    assert.deepStrictEqual(loaded, ['v1']);
  });

  it('restores the editable state when the start is refused', async () => {
    ext.callPluginApi = async () => { throw new Error('The document is being edited by another user'); };
    await ext.runCorrection();
    assert.strictEqual(ext.state.editorReadOnly, false);
    assert.ok(calls.some(c => c.includes('being edited by another user')));
    assert.strictEqual(ext._running.size, 0);
  });
});
