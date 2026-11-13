/**
 * Unit tests for the "Test model with current document" frontend extension.
 *
 * @testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/extensions/auto-annotation-model-test.js
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
}
global.FrontendExtensionPlugin = FakeFrontendExtensionPlugin;
global.document = { createElement: () => ({ addEventListener(t, fn) { this._click = fn; }, style: {} }) };

const {
  default: ModelTestExtension, findVersion, inputChoices, parseBatchSpecs, startRequest, progressLabel, progressValue,
} = await import('../extensions/auto-annotation-model-test.js');

const FILE_DATA = [{
  doc_metadata: { title: 'A paper' },
  artifacts: [
    { id: 'x1', label: 'Extraction', variant: 'refs' },
    { id: 'g1', label: 'Gold', variant: 'refs', is_gold_standard: true },
    { id: 'v2', label: 'Version 2', variant: 'refs' },
    { id: 's1', label: 'Segmentation', variant: 'seg' },
  ],
}];

/** @type {import('../extensions/auto-annotation-model-test.js').ProbeFormValues} */
const VALUES = {
  providerId: 'p', modelId: 'm', modelLabel: 'Model M', batches: ['units:4'], modes: ['all'], repeats: 1, concurrency: 1,
  retries: 4, temperature: 0, maxUnits: null, inputStableId: null, forceBusy: false,
};

describe('helpers', () => {
  it('find versions and the versions to correct', () => {
    assert.strictEqual(findVersion(FILE_DATA, 'g1')?.artifact.label, 'Gold');
    assert.strictEqual(findVersion(FILE_DATA, 'nope'), null);
    assert.deepStrictEqual(inputChoices(FILE_DATA, 'v2'), [{ id: 'x1', label: 'Extraction' }, { id: 'g1', label: 'Gold (gold)' }]);
  });

  it('parse batch specs', () => {
    assert.deepStrictEqual(parseBatchSpecs(['units:1', 'units:4'], ' chars:4000, units:4 '), ['units:1', 'units:4', 'chars:4000']);
    assert.throws(() => parseBatchSpecs([], 'units:0'), /not a batch size/);
    assert.throws(() => parseBatchSpecs([], 'lines:3'), /not a batch size/);
  });

  it('build the start request and the progress label', () => {
    assert.deepStrictEqual(startRequest({ ...VALUES, maxUnits: 5 }, 'g1', 'A paper'), {
      reference_stable_id: 'g1', input_stable_id: null, provider_id: 'p', model_id: 'm', model_label: 'Model M', title: 'A paper',
      batches: ['units:4'], modes: ['all'], repeats: 1, concurrency: 1, retries: 4, temperature: 0, max_units: 5, force_busy: false,
    });
    const status = { run_id: 'r', state: 'running', planned_runs: 4, finished_runs: 1, current_run: 2, current: 'units:4, all, #1', error: null, model_label: 'Model M', title: 'A paper' };
    assert.strictEqual(progressLabel(status), 'Testing Model M on "A paper": run 2 of 4 (units:4, all, #1)');
    assert.strictEqual(progressLabel({ ...status, current: null, title: null }), 'Testing Model M…');
    assert.strictEqual(progressValue(status), 25);
  });
});

describe('ModelTestExtension', () => {
  /** @type {any} */
  let ext;
  /** @type {Array<[string, string, any]>} */
  let calls;
  /** @type {string[]} */
  let errors;
  /** @type {string[]} */
  let notes;
  /** @type {boolean[]} */
  let confirmAnswers;
  /** @type {string[]} */
  let confirms;
  /** @type {any[]} */
  let statuses;
  /** @type {Array<[string, ...any]>} */
  let progressCalls;
  /** @type {any[]} */
  let shown;
  /** @type {any} */
  let startResult;

  beforeEach(() => {
    calls = []; errors = []; notes = []; confirmAnswers = []; confirms = []; statuses = []; progressCalls = []; shown = [];
    startResult = { run_id: 'run-1', planned_runs: 2, units: 10, reference_is_gold: true, input_stable_id: 'x1' };
    ext = new ModelTestExtension({});
    ext._sleep = async () => {};
    ext.state = { user: { username: 'rev', roles: ['reviewer'] }, xml: 'g1', fileData: FILE_DATA };
    ext._deps = {
      'acl-utils': { userHasRole: (user, roles) => roles.some(r => user.roles.includes(r)) },
      xmleditor: { getXmlTree: () => ({}), on() {} },
      'tei-utils': { getEditorialDeclGuides: () => [{ refs: [{ subtype: 'machine' }] }] },
      'inference-settings': { getModels: () => [{ providerId: 'p', modelId: 'm', label: 'Model M' }], getDefaultModel: () => null, onProvidersChange() {} },
      tools: { addMenuItems() {} },
      dialog: {
        confirm: async message => { confirms.push(message); return confirmAnswers.shift() ?? true; },
        error: message => errors.push(message),
      },
      'sl-utils': { notify: message => notes.push(message) },
      progress: {
        show: (id, options) => progressCalls.push(['show', id, options]),
        setValue: (id, value) => progressCalls.push(['value', id, value]),
        setLabel: (id, label) => progressCalls.push(['label', id, label]),
        hide: id => progressCalls.push(['hide', id]),
      },
      'backend-plugins': { execute: async (...args) => { shown.push(args); } },
    };
    ext.callPluginApi = async (path, method, body) => {
      calls.push([method, path, body]);
      if (path.endsWith('/start')) {
        if (startResult instanceof Error) throw startResult;
        return startResult;
      }
      if (path.endsWith('/status')) return statuses.shift();
      if (path.endsWith('/jobs')) return [];
      if (path.endsWith('/cancel')) return {};
      throw new Error(`unexpected ${path}`);
    };
    ext.openForm = async () => VALUES;
  });

  it('shows the menu items to reviewers only and disables the test without machine rules', async () => {
    await ext.start();
    assert.strictEqual(ext._testItem.style.display, '');
    assert.strictEqual(ext._testItem.disabled, false);
    ext._deps['tei-utils'].getEditorialDeclGuides = () => [];
    ext._updateMenuState();
    assert.strictEqual(ext._testItem.disabled, true);
    assert.match(ext._testItem.title, /machine-readable/);
    ext.state.user = { username: 'ann', roles: ['annotator'] };
    await ext.onUserChange();
    assert.strictEqual(ext._testItem.style.display, 'none');
    assert.strictEqual(ext._resultsItem.style.display, 'none');
  });

  it('runs a test on a gold version, follows it and shows the results', async () => {
    statuses = [
      { run_id: 'run-1', state: 'running', planned_runs: 2, finished_runs: 1, current_run: 2, current: 'units:4, all, #1', error: null, model_label: 'Model M', title: 'A paper' },
      { run_id: 'run-1', state: 'done', planned_runs: 2, finished_runs: 2, current_run: 2, current: null, error: null, model_label: 'Model M', title: 'A paper' },
    ];
    await ext.testModel();
    await ext._tracked.get('run-1');
    assert.deepStrictEqual(confirms, []);
    assert.strictEqual(calls[0][1], '/api/plugins/auto-annotation/probe/start');
    assert.strictEqual(calls[0][2].reference_stable_id, 'g1');
    assert.strictEqual(calls[0][2].title, 'A paper');
    assert.match(notes[0], /has started \(2 runs on 10 units\)/);
    const [kind, id, options] = progressCalls[0];
    assert.deepStrictEqual([kind, id, options.minimized, options.cancellable], ['show', 'auto-annotation-probe-run-1', true, true]);
    assert.ok(progressCalls.some(c => c[0] === 'value' && c[2] === 50));
    assert.deepStrictEqual(progressCalls.at(-1), ['hide', 'auto-annotation-probe-run-1']);
    assert.deepStrictEqual(shown, [['auto-annotation', 'probe_results', { run: 'run-1' }]]);
    assert.deepStrictEqual(errors, []);
  });

  it('warns before testing a version that is not gold', async () => {
    ext.state.xml = 'v2';
    confirmAnswers = [false];
    await ext.testModel();
    assert.match(confirms[0], /not a gold version/);
    assert.deepStrictEqual(calls, []);
  });

  it('shows errors in an alert, not a toast', async () => {
    startResult = new Error('The model is very busy');
    await ext.testModel();
    assert.strictEqual(errors.length, 1);
    assert.match(errors[0], /could not be started: The model is very busy/);
    assert.deepStrictEqual(notes, []);

    statuses = [{ run_id: 'run-2', state: 'failed', planned_runs: 3, finished_runs: 1, current_run: 2, current: null, error: 'All requests failed: 401', model_label: 'Model M', title: null }];
    await ext.track({ ...statuses[0], state: 'running', error: null });
    assert.match(errors[1], /failed: All requests failed: 401\. The results of the 1 finished run\(s\) are kept/);
    assert.deepStrictEqual(shown, []);
  });

  it('cancels through the widget', async () => {
    statuses = [{ run_id: 'run-3', state: 'cancelled', planned_runs: 3, finished_runs: 0, current_run: 1, current: null, error: null, model_label: 'Model M', title: null }];
    const tracking = ext.track({ ...statuses[0], state: 'running' });
    progressCalls[0][2].onCancel();
    await tracking;
    assert.ok(calls.some(([method, path, body]) => method === 'POST' && path.endsWith('/cancel') && body.run_id === 'run-3'));
    assert.match(notes[0], /cancelled before a run finished/);
    assert.deepStrictEqual(shown, []);
  });
});
