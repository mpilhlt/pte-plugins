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

const { default: AutoAnnotationExtension, runCorrection, transferSources, preferredSource } = await import('../extensions/auto-annotation.js');

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

describe('AutoAnnotationExtension layout check', () => {
  it('disables the menu item when the layout of a zone document is missing', async () => {
    const ext = new AutoAnnotationExtension({});
    ext._menuItem = { disabled: false, title: '' };
    ext.state = { xml: 'z1', user: { roles: ['annotator'] } };
    const guides = [{ refs: [{ subtype: 'machine' }] }];
    let status = 'missing';
    ext._deps = {
      'acl-utils': { userHasRole: () => true },
      xmleditor: { getXmlTree: () => ({}) },
      'tei-utils': { getEditorialDeclGuides: () => guides },
      client: { apiClient: { filesGetLayout: async () => ({ status, message: 'The layout information of this document is missing.' }) } },
    };
    await ext._checkLayout();
    assert.strictEqual(ext._menuItem.disabled, true);
    assert.match(ext._menuItem.title, /layout information/);
    status = 'ok';
    await ext.onXmlChange('z1');
    assert.strictEqual(ext._menuItem.disabled, false);
  });
});

describe('transfer sources', () => {
  const fileData = [
    { artifacts: [
      { id: 'ext', variant: 'grobid.training.segmentation', status: 'extraction', updated_at: '2026-11-18' },
      { id: 'old', label: 'Old OCR', variant: 'grobid.training.segmentation', status: 'approved', updated_at: '2026-10-01' },
      { id: 'older', variant: 'grobid.training.segmentation', status: 'in-review', updated_at: '2026-09-01' },
      { id: 'refs', variant: 'grobid.training.references', status: 'gold', is_gold_standard: true },
    ] },
    { artifacts: [{ id: 'elsewhere', variant: 'grobid.training.segmentation' }] },
  ];

  it('lists the other versions of the same document and variant', () => {
    assert.deepStrictEqual(transferSources(fileData, 'ext'), [{ id: 'old', label: 'Old OCR' }, { id: 'older', label: 'older' }]);
    assert.deepStrictEqual(transferSources(fileData, 'missing'), []);
  });

  it('prefers the gold version, else the most recently changed annotated one', () => {
    assert.strictEqual(preferredSource(fileData, 'ext'), 'old');
    const withGold = [{ artifacts: [...fileData[0].artifacts, { id: 'gold', variant: 'grobid.training.segmentation', is_gold_standard: true }] }];
    assert.strictEqual(preferredSource(withGold, 'ext'), 'gold');
    assert.strictEqual(preferredSource([{ artifacts: [{ id: 'a', variant: 'x', status: 'extraction' }, { id: 'b', variant: 'x', status: 'extraction' }] }], 'a'), 'b');
    assert.strictEqual(preferredSource(fileData, 'refs'), null);
  });
});

describe('AutoAnnotationExtension transfer', () => {
  /** @type {any} */
  let ext;
  /** @type {any[]} */
  let requests;
  /** @type {any[]} */
  let recorded;

  beforeEach(() => {
    ext = new AutoAnnotationExtension({});
    ext._lockRefreshMs = 1e9;
    requests = [];
    recorded = [];
    ext.state = { xml: 'new', editorReadOnly: false, user: { roles: ['annotator'] }, fileData: [{ artifacts: [{ id: 'new' }, { id: 'old', status: 'approved' }] }] };
    ext._deps = {
      'sl-utils': { notify: () => {} },
      dialog: { confirm: async () => { throw new Error('no confirmation expected'); } },
      'inference-settings': { getDefaultModel: () => ({ providerId: 'kisski', modelId: 'qwen/3' }), getModelLabel: () => 'Qwen' },
      xmleditor: { saveIfDirty: async () => {}, getXmlTree: () => null, on() {} },
      progress: { show() {}, hide() {}, setValue() {}, setLabel() {} },
      client: { saveXml: async () => ({ file_id: 'v3' }), releaseLock: async () => {} },
      'file-selection': { reload: async () => {} },
      services: { load: async ({ xml }) => { ext.state.xml = xml; } },
      'unit-review': { setMarks: async (who, marks) => { recorded.push([who, marks]); return true; } },
    };
    ext.openTransferForm = async (target, sources, preferred) => {
      assert.deepStrictEqual([target, sources.map(s => s.id), preferred], ['new', ['old'], 'old']);
      return { sourceStableId: 'old', sourceLabel: 'old', marks: true };
    };
    ext.callPluginApi = async (path, method, body) => {
      const route = path.split('/').pop();
      requests.push([route, body]);
      if (route === 'start') return { job_id: 'j', doc_id: 'd', variant: null, unit_count: 3, batches: [[1]], doubt_batches: [[0, 2]], transfer: { copied: 2, model: 1, unmatched: 0 } };
      if (route === 'batch' && body.mode === 'transfer') return { ...batchResult(body.units, { 1: '<bibl>b</bibl>' }), units: [{ index: 1, status: 'ok', xml: '<bibl>b</bibl>', doubt: 'not a title' }] };
      if (route === 'batch') return { ...batchResult(body.units), units: body.units.map(index => ({ index, status: 'ok', xml: null, doubt: index === 2 ? 'a note?' : null })) };
      if (route === 'finish') return { xml: '<TEI/>', changed: 3, marks: [{ fingerprint: 'f1', comment: 'not a title' }, { fingerprint: 'f2', comment: 'a note?' }] };
      return { status: 'released' };
    };
  });

  it('transfers, checks the copied units and records the doubts as marks of the model', async () => {
    await ext.runTransfer();
    const routes = requests.map(([r, b]) => (r === 'batch' ? `batch:${b.mode}` : r));
    assert.deepStrictEqual(routes, ['start', 'batch:transfer', 'batch:doubts', 'finish', 'release']);
    const start = requests[0][1];
    assert.deepStrictEqual([start.source_stable_id, start.marks], ['old', true]);
    const finish = requests[3][1];
    assert.deepStrictEqual(finish.corrections, [{ index: 1, xml: '<bibl>b</bibl>' }]);
    assert.deepStrictEqual(finish.doubts, [{ index: 1, comment: 'not a title' }, { index: 2, comment: 'a note?' }]);
    assert.strictEqual(finish.source_stable_id, 'old');
    assert.strictEqual(ext.state.xml, 'v3');
    assert.strictEqual(recorded.length, 1);
    assert.deepStrictEqual(recorded[0][0], { id: 'llm-kisski-qwen-3', name: 'Qwen' });
    assert.deepStrictEqual(recorded[0][1], [{ fingerprint: 'f1', state: 'not-ok', comment: 'not a title' }, { fingerprint: 'f2', state: 'not-ok', comment: 'a note?' }]);
  });

  it('saves the copied units when the model has nothing to change', async () => {
    const api = ext.callPluginApi;
    ext.openTransferForm = async () => ({ sourceStableId: 'old', sourceLabel: 'old', marks: false });
    ext.callPluginApi = async (path, method, body) => {
      if (path.endsWith('/start')) { requests.push(['start', body]); return { job_id: 'j', doc_id: 'd', variant: null, unit_count: 2, batches: [], doubt_batches: [], transfer: { copied: 2, model: 0, unmatched: 0 } }; }
      if (path.endsWith('/finish')) { requests.push(['finish', body]); return { xml: '<TEI/>', changed: 2 }; }
      return api(path, method, body);
    };
    await ext.runTransfer();
    assert.deepStrictEqual(requests.map(([r]) => r), ['start', 'finish', 'release']);
    assert.strictEqual(ext.state.xml, 'v3');
    assert.strictEqual(recorded.length, 0);
  });

  it('does not start when the dialog is cancelled', async () => {
    ext.openTransferForm = async () => null;
    await ext.runTransfer();
    assert.deepStrictEqual(requests, []);
  });

  it('reports marks that cannot be recorded without the unit review plugin', async () => {
    delete ext._deps['unit-review'];
    assert.strictEqual(await ext._recordMarks({ providerId: 'p', modelId: 'm' }, 'M', [{ fingerprint: 'f', comment: 'c' }]), false);
  });
});
