/**
 * Unit tests for the annotation-review frontend extension.
 *
 * @testCovers plugins/annotation-review/src/pte_plugin_annotation_review/extensions/annotation-review.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';
import { coreImport } from '../../../../../scripts/core.js';

// The shared findings display of the editor, which the extension reaches via getDependency('llm-findings')
const llmFindings = await coreImport('app/src/modules/llm-findings.js');

// Stub browser-only base class before the dynamic import below.
class FakeFrontendExtensionPlugin {
  constructor(context, config = {}) {
    this.context = context;
    this._config = config;
    this._deps = {};
  }
  getDependency(name) {
    if (!(name in this._deps)) throw new Error(`no stub dependency registered for "${name}"`);
    return this._deps[name];
  }
  async callPluginApi() {
    throw new Error('callPluginApi must be stubbed per-test');
  }
}
global.FrontendExtensionPlugin = FakeFrontendExtensionPlugin;
global.document = { createElement: () => ({ addEventListener(t, fn) { this._click = fn; }, style: {} }) };

const { default: AnnotationReviewExtension, unitMarksForChunk } = await import('../extensions/annotation-review.js');

/**
 * In-memory stand-in for SessionStorage (app/src/modules/session-storage.js),
 * sharing its contexts through the given map like sessionStorage across reloads.
 * @param {Map<string, Record<string, any>>} store
 */
function fakeSessionStorageClass(store) {
  return class {
    getValue(contextId, key, defaultValue) { return store.get(contextId)?.[key] ?? defaultValue; }
    setState(contextId, updates) { store.set(contextId, { ...store.get(contextId), ...updates }); }
    clearState(contextId) { store.delete(contextId); }
  };
}

/**
 * Build an extension instance with stubbed dependencies.
 * @param {{xmlDoc?: object|null, editorContent?: string, defaultModel?: object|null, callPluginApiImpl?: Function}} opts
 */
function buildExtension(opts = {}) {
  const ext = new AnnotationReviewExtension({});
  const notifyCalls = [];
  const confirmCalls = [], lintCalls = [], dismissListeners = [], updateListeners = [], mergeViews = [], menuAdds = [], progressCalls = [], progressOpts = {}, handlers = {}, focusActions = {};
  ext._deps = {
    tools: { addMenuItems: (items, category) => menuAdds.push({ items, category }) },
    progress: {
      show: (id, o) => { progressOpts.show = o; progressCalls.push(['show', o.label]); },
      setValue: (id, v) => progressCalls.push(['value', v]),
      setLabel: (id, l) => progressCalls.push(['label', l]),
      hide: () => progressCalls.push(['hide']),
    },
    'lint-utils': {
      replacesDiagnostics: opts.replacesDiagnostics ?? (() => false),
      applyMergedDiagnostics: (view, source, own, options) => lintCalls.push({ view, source, own, options }),
      addDismissListener: (fn) => { dismissListeners.push(fn); },
    },
    xmleditor: {
      getView: () => ({ state: { doc: { toString: () => opts.docText ?? '' } } }),
      addUpdateListener: (fn) => { updateListeners.push(fn); },
      showMergeView: async (text) => { if (opts.showMergeViewImpl) return opts.showMergeViewImpl(text); mergeViews.push(text); },
      on: (event, fn) => { handlers[event] = fn; },
      getXmlTree: () => (opts.xmlDoc === undefined ? {} : opts.xmlDoc),
      setFocusAction: (id, action) => { focusActions[id] = action; },
      getEditorContent: () => opts.editorContent ?? '<TEI/>',
    },
    'sl-utils': {
      notify: (...args) => notifyCalls.push(args),
    },
    'session-storage': { SessionStorage: fakeSessionStorageClass(opts.sessionStore ?? new Map()) },
    'llm-findings': llmFindings,
    'tei-utils': {
      getEditorialDeclGuides: opts.getEditorialDeclGuides ?? (() => []),
    },
    'inference-settings': {
      getDefaultModel: () => opts.defaultModel ?? null,
      getModelLabel: (providerId, modelId) => `${providerId} label/${modelId} label`,
    },
    dialog: {
      confirm: async (message, title) => { confirmCalls.push({ message, title }); return opts.confirmAnswer ?? true; },
    },
  };
  if (opts.state) ext.state = opts.state;
  const impl = opts.callPluginApiImpl ?? (async () => { throw new Error('unexpected call'); });
  ext.callPluginApi = async (endpoint, ...rest) => {
    if (endpoint.endsWith('/plan')) {
      if (opts.planImpl) return opts.planImpl();
      // the plan request only counts chunks; existing impls answer review requests
      return { chunk_count: opts.chunkCount ?? 1 };
    }
    return impl(endpoint, ...rest);
  };
  return { ext, notifyCalls, confirmCalls, lintCalls, dismissListeners, updateListeners, mergeViews, menuAdds, progressCalls, progressOpts, handlers, focusActions };
}

describe('hasReviewableRules', () => {
  it('returns true when at least one category has a machine-subtype ref', () => {
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
    });
    assert.strictEqual(ext.hasReviewableRules({}), true);
  });

  it('returns false when no category has a machine-subtype ref', () => {
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'human' }] }],
    });
    assert.strictEqual(ext.hasReviewableRules({}), false);
  });

  it('returns false for an empty guides list', () => {
    const { ext } = buildExtension({ getEditorialDeclGuides: () => [] });
    assert.strictEqual(ext.hasReviewableRules({}), false);
  });
});

describe('review', () => {
  it('notifies and returns null when no document is open', async () => {
    const { ext, notifyCalls } = buildExtension({ xmlDoc: null });
    const result = await ext.review();
    assert.strictEqual(result, null);
    assert.strictEqual(notifyCalls.length, 1);
    assert.strictEqual(notifyCalls[0][1], 'warning');
  });

  it('notifies and returns null when the document has no reviewable rules', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'human' }] }],
    });
    const result = await ext.review();
    assert.strictEqual(result, null);
    assert.strictEqual(notifyCalls.length, 1);
  });

  it('notifies and returns null when no default model is configured', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: null,
    });
    const result = await ext.review();
    assert.strictEqual(result, null);
    assert.strictEqual(notifyCalls.length, 1);
  });

  it('calls the review endpoint with resolved default provider/model and returns findings', async () => {
    const calls = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: { providerId: 'kisski', modelId: 'gemma-3' },
      editorContent: '<TEI>content</TEI>',
      callPluginApiImpl: async (endpoint, method, params) => {
        calls.push({ endpoint, method, params });
        return { findings: [{ id: 0, old: 'a', new: 'b', rationale: 'r' }] };
      },
    });
    const result = await ext.review();
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].endpoint, '/api/plugins/annotation-review/review');
    assert.strictEqual(calls[0].method, 'POST');
    assert.deepStrictEqual(calls[0].params, {
      xml: '<TEI>content</TEI>',
      provider_id: 'kisski',
      model_id: 'gemma-3',
      chunk_index: 0,
    });
    assert.deepStrictEqual(result, [{ id: 0, old: 'a', new: 'b', rationale: 'r' }]);
  });

  it('uses an explicit override instead of the default model', async () => {
    const calls = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: { providerId: 'kisski', modelId: 'gemma-3' },
      callPluginApiImpl: async (endpoint, method, params) => {
        calls.push(params);
        return { findings: [] };
      },
    });
    await ext.review({ providerId: 'other', modelId: 'other-model' });
    assert.strictEqual(calls[0].provider_id, 'other');
    assert.strictEqual(calls[0].model_id, 'other-model');
  });

  it('notifies and returns null when the API call fails', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'primary', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: { providerId: 'kisski', modelId: 'gemma-3' },
      callPluginApiImpl: async () => { throw new Error('boom'); },
    });
    const result = await ext.review();
    assert.strictEqual(result, null);
    assert.strictEqual(notifyCalls.length, 1);
    assert.strictEqual(notifyCalls[0][1], 'danger');
  });
});

describe('dismissing diagnostics', () => {
  it('forgets the findings whose diagnostics were dismissed, keeping the others', async () => {
    const findings = [
      { id: 0, old: 'AAA', new: 'a', rationale: 'first' },
      { id: 1, old: 'BBB', new: 'b', rationale: 'second' },
    ];
    const { ext, dismissListeners } = buildExtension({ docText: 'x AAA BBB y' });
    await ext.start();
    ext.showFindings(findings);
    assert.strictEqual(dismissListeners.length, 1);
    dismissListeners[0]([
      { from: 2, to: 5, severity: 'info', message: 'first', source: 'annotation-review' },
      { from: 2, to: 5, severity: 'error', message: 'first' }, // foreign: ignored
    ]);
    assert.deepStrictEqual(ext._display.findings.map(f => f.id), [1]);
  });
});

describe('showFindings / clearFindings', () => {
  const finding = { id: 0, old: 'AAA', new: 'a', rationale: 'why' };

  it('merges diagnostics for locatable findings under the annotation-review source', () => {
    const { ext, lintCalls } = buildExtension({ docText: 'x AAA y' });
    ext.showFindings([finding]);
    assert.strictEqual(lintCalls.length, 1);
    assert.strictEqual(lintCalls[0].source, 'annotation-review');
    assert.strictEqual(lintCalls[0].own.length, 1);
    assert.strictEqual(lintCalls[0].own[0].message, 'why');
  });

  it('clearFindings merges an empty own list', () => {
    const { ext, lintCalls } = buildExtension({ docText: 'x AAA y' });
    ext.showFindings([finding]);
    ext.clearFindings();
    assert.deepStrictEqual(lintCalls.at(-1).own, []);
  });
});

describe('re-rendering after diagnostics are replaced', () => {
  it('re-merges the stored findings when another source replaced the diagnostics', async () => {
    const { ext, lintCalls, updateListeners } = buildExtension({
      docText: 'x AAA y',
      replacesDiagnostics: () => true,
    });
    await ext.start();
    ext.showFindings([{ id: 0, old: 'AAA', new: 'a', rationale: 'why' }]);
    const before = lintCalls.length;
    updateListeners[0]({ transactions: [] });
    await new Promise(r => setTimeout(r, 5));
    assert.strictEqual(lintCalls.length, before + 1);
  });

});

describe('Propose fix', () => {
  it('opens the merge view with only that finding applied', async () => {
    const { ext, mergeViews } = buildExtension({ docText: 'x AAA y' });
    await ext._proposeFix({ id: 0, old: 'AAA', new: 'a', rationale: 'r' }, 2, 5);
    assert.deepStrictEqual(mergeViews, ['x a y']);
  });

  it('notifies instead when the text no longer matches', async () => {
    const { ext, mergeViews, notifyCalls } = buildExtension({ docText: 'x ZZZ y' });
    await ext._proposeFix({ id: 0, old: 'AAA', new: 'a', rationale: 'r' }, 2, 5);
    assert.strictEqual(mergeViews.length, 0);
    assert.strictEqual(notifyCalls[0][1], 'warning');
  });
});

describe('onXmlChange', () => {
  it('drops stale findings', async () => {
    const { ext, lintCalls } = buildExtension({ docText: 'x AAA y' });
    ext.showFindings([{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }]);
    await ext.onXmlChange('newdoc');
    assert.deepStrictEqual(lintCalls.at(-1).own, []);
  });
});

describe('session cache', () => {
  const findings = [
    { id: 0, old: 'AAA', new: 'a', rationale: 'kept' },
    { id: 1, old: 'BBB', new: 'b', rationale: 'edited away' },
  ];

  it('restores the findings still in the document after a reload', async () => {
    const sessionStore = new Map();
    const before = buildExtension({ docText: 'x AAA BBB y', sessionStore, state: { xml: 'doc1' } });
    await before.ext.start();
    before.handlers.editorAfterLoad();
    before.ext.showFindings(findings);

    // page reload: new instance, same session storage, BBB edited meanwhile
    const after = buildExtension({ docText: 'x AAA CCC y', sessionStore, state: { xml: 'doc1' } });
    await after.ext.start();
    after.handlers.editorAfterLoad();
    assert.deepStrictEqual(after.lintCalls.at(-1).own.map(d => d.message), ['kept']);
  });

  it('saves and restores findings when the document id is set after the content is loaded', async () => {
    // services.load(): state.xml is null while the editor loads, then set to the new id
    const sessionStore = new Map();
    const load = async (/** @type {any} */ built) => {
      built.ext.state = { xml: null };
      await built.ext.start();
      await built.ext.onXmlChange(null);
      built.handlers.editorAfterLoad();
      built.ext.state = { xml: 'doc1' };
      await built.ext.onXmlChange('doc1');
    };
    const before = buildExtension({ docText: 'x AAA BBB y', sessionStore });
    await load(before);
    before.ext.showFindings(findings.slice(0, 1));
    assert.deepStrictEqual(sessionStore.get('doc1').review.findings.map(f => f.id), [0]);

    const after = buildExtension({ docText: 'x AAA BBB y', sessionStore });
    await load(after);
    assert.deepStrictEqual(after.ext._display.findings.map(f => f.id), [0]);
    assert.deepStrictEqual(after.lintCalls.at(-1).own.map(d => d.message), ['kept']);
    assert.strictEqual(after.lintCalls.at(-1).options.openPanel, true);
  });

  it('keeps the cache of a document when another one is loaded', async () => {
    const sessionStore = new Map();
    const { ext, handlers } = buildExtension({ docText: 'x AAA BBB y', sessionStore, state: { xml: 'doc1' } });
    await ext.start();
    handlers.editorAfterLoad();
    ext.showFindings(findings);
    ext.state = { xml: 'doc2' };
    await ext.onXmlChange('doc2');
    assert.deepStrictEqual(ext._display.findings, []);
    assert.deepStrictEqual(sessionStore.get('doc1').review.findings.map(f => f.id), [0, 1]);
  });

  it('removes findings dismissed in the diagnostics drawer from the cache', async () => {
    const sessionStore = new Map();
    const { ext, handlers, dismissListeners } = buildExtension({ docText: 'x AAA BBB y', sessionStore, state: { xml: 'doc1' } });
    await ext.start();
    handlers.editorAfterLoad();
    ext.showFindings(findings);
    dismissListeners[0]([{ from: 6, to: 9, severity: 'info', message: 'edited away', source: 'annotation-review' }]);
    assert.deepStrictEqual(sessionStore.get('doc1').review.findings.map(f => f.id), [0]);
  });
});

describe('menu item', () => {
  it('adds a disabled "Review document annotations" item to the annotation category', async () => {
    const { ext, menuAdds } = buildExtension({ xmlDoc: null });
    await ext.start();
    assert.strictEqual(menuAdds.length, 1);
    assert.strictEqual(menuAdds[0].category, 'annotation');
    assert.strictEqual(menuAdds[0].items[0].textContent, 'Review document annotations');
    assert.strictEqual(menuAdds[0].items[0].disabled, true);
  });

  it('enables the item once the document has reviewable rules', async () => {
    const { ext, menuAdds, handlers } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
    });
    await ext.start();
    handlers.editorReady();
    assert.strictEqual(menuAdds[0].items[0].disabled, false);
  });

  it('disables the item of a zone document without page layout, with the reason as title', async () => {
    const { ext, menuAdds, focusActions } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      state: { xml: 'seg1' },
    });
    const requested = [];
    ext._deps.client = { apiClient: { filesGetLayout: async (id) => { requested.push(id); return { status: 'missing', pages: [], hints: [], message: 'The layout information of this document is missing.' }; } } };
    await ext.start();
    await ext.onXmlChange('seg1');
    assert.deepStrictEqual(requested.slice(-1), ['seg1']);
    assert.strictEqual(menuAdds[0].items[0].disabled, true);
    assert.match(menuAdds[0].items[0].title, /layout information/);
    assert.strictEqual(focusActions['annotation-review'], null);
    ext._deps.client.apiClient.filesGetLayout = async () => ({ status: 'ok', pages: [0], hints: [[]] });
    await ext.onXmlChange('seg1');
    assert.strictEqual(menuAdds[0].items[0].disabled, false);
  });

  it('sends the stable id with the plan request, so that zone documents are reviewed by page', async () => {
    const bodies = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: { providerId: 'p', modelId: 'm' },
      state: { xml: 'seg1' },
      callPluginApiImpl: async (endpoint, method, body) => { bodies.push(body); return { findings: [], chunk_count: 1 }; },
    });
    await ext.review();
    assert.strictEqual(bodies[0].stable_id, 'seg1');
  });

  it('keeps the item disabled when there is no machine rule ref', async () => {
    const { ext, menuAdds, handlers } = buildExtension({ getEditorialDeclGuides: () => [] });
    await ext.start();
    handlers.editorReady();
    assert.strictEqual(menuAdds[0].items[0].disabled, true);
  });

  it('offers an "Annotation auto-review" focus-mode button only while the review is available', async () => {
    let guides = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];
    const { ext, handlers, focusActions } = buildExtension({ getEditorialDeclGuides: () => guides });
    await ext.start();
    handlers.editorReady();
    const action = focusActions['annotation-review'];
    assert.strictEqual(action.label, 'Annotation auto-review');
    let ran = 0;
    ext.runReview = async () => { ran++; };
    action.run();
    assert.strictEqual(ran, 1);
    guides = [];
    handlers.editorReady();
    assert.strictEqual(focusActions['annotation-review'], null);
  });
});

describe('runReview', () => {
  const rules = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];

  it('shows a progress widget, displays findings and reports the count', async () => {
    const { ext, progressCalls, notifyCalls, lintCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'x AAA y',
      callPluginApiImpl: async () => ({ findings: [{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }] }),
    });
    await ext.runReview();
    assert.deepStrictEqual(progressCalls.filter(c => c[0] === 'show' || c[0] === 'hide').map(c => c[0]), ['show', 'hide']);
    assert.strictEqual(lintCalls.at(-1).own.length, 1);
    assert.match(notifyCalls.at(-1)[0], /1 suggestion/);
  });

  it('reports when nothing was found', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      callPluginApiImpl: async () => ({ findings: [] }),
    });
    await ext.runReview();
    assert.strictEqual(notifyCalls.at(-1)[1], 'success');
  });

  it('reports discarded findings instead of "no issues" when nothing usable came back', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      callPluginApiImpl: async () => ({ findings: [], discarded: 2 }),
    });
    await ext.runReview();
    assert.strictEqual(notifyCalls.at(-1)[1], 'warning');
    assert.match(notifyCalls.at(-1)[0], /proposed 2 fix/);
  });

  it('hides the progress widget and shows nothing when review() fails', async () => {
    const { ext, progressCalls, lintCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      callPluginApiImpl: async () => { throw new Error('boom'); },
    });
    await ext.runReview();
    assert.deepStrictEqual(progressCalls.filter(c => c[0] === 'show' || c[0] === 'hide').map(c => c[0]), ['show', 'hide']);
    assert.strictEqual(lintCalls.length, 0);
  });

  it('keeps the findings of the parts already reviewed when a later part fails', async () => {
    let calls = 0;
    const { ext, lintCalls, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'x AAA y',
      chunkCount: 3,
      callPluginApiImpl: async () => {
        if (calls++ === 0) return { chunk_count: 3, findings: [{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }] };
        throw new Error('LLM provider request failed: Could not reach the Gemini API');
      },
    });
    await ext.runReview();
    assert.deepStrictEqual(ext._display.findings.map(f => f.old), ['AAA']);
    assert.strictEqual(lintCalls.at(-1).own.length, 1);
    assert.match(notifyCalls.at(-1)[0], /failed in part 2 of 3/);
  });
});

describe('diagnostics drawer reveal', () => {
  it('showFindings asks to expand the diagnostics drawer', () => {
    const { ext, lintCalls } = buildExtension({ docText: 'x AAA y' });
    ext.showFindings([{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }]);
    assert.deepStrictEqual(lintCalls[0].options, { openPanel: true });
  });

  it('the listener-driven re-render does not expand the drawer', async () => {
    const { ext, lintCalls, updateListeners } = buildExtension({ docText: 'x AAA y', replacesDiagnostics: () => true });
    await ext.start();
    ext.showFindings([{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }]);
    updateListeners[0]({ transactions: [] });
    await new Promise(r => setTimeout(r, 5));
    assert.strictEqual(lintCalls.length, 2);
    assert.deepStrictEqual(lintCalls[1].options, { openPanel: false });
  });
});

describe('hardening', () => {
  it('_proposeFix notifies when showMergeView throws', async () => {
    const { ext, notifyCalls } = buildExtension({
      docText: 'x AAA y',
      showMergeViewImpl: async () => { throw new Error('nope'); },
    });
    await ext._proposeFix({ id: 0, old: 'AAA', new: 'a', rationale: 'r' }, 2, 5);
    assert.strictEqual(notifyCalls.at(-1)[1], 'danger');
    assert.match(notifyCalls.at(-1)[0], /nope/);
  });

  it('clearFindings without findings dispatches nothing', async () => {
    const { ext, lintCalls } = buildExtension({ docText: 'x AAA y' });
    await ext.onXmlChange('doc');
    ext.clearFindings();
    assert.strictEqual(lintCalls.length, 0);
  });

  it('runReview discards findings when the document changed meanwhile', async () => {
    const state = { xml: 'a' };
    const { ext, lintCalls, notifyCalls } = buildExtension({
      state,
      getEditorialDeclGuides: () => [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }],
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'x AAA y',
      callPluginApiImpl: async () => {
        state.xml = 'b';
        return { findings: [{ id: 0, old: 'AAA', new: 'a', rationale: 'r' }] };
      },
    });
    await ext.runReview();
    assert.strictEqual(lintCalls.length, 0);
    assert.strictEqual(notifyCalls.length, 0);
  });

  it('does not subscribe to editorXmlNotWellFormed', async () => {
    const { ext, handlers } = buildExtension();
    await ext.start();
    assert.strictEqual('editorXmlNotWellFormed' in handlers, false);
  });
});

describe('runReview confirmation', () => {
  const rules = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];

  it('asks for confirmation naming the provider/model and puts it in the progress label', async () => {
    const { ext, confirmCalls, progressCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'kisski', modelId: 'gemma' },
      callPluginApiImpl: async () => ({ findings: [] }),
    });
    await ext.runReview();
    assert.strictEqual(
      confirmCalls[0].message,
      'Review the annotations in the current document using kisski label/gemma label?'
    );
    assert.strictEqual(progressCalls[0][1], 'Reviewing annotations using kisski label/gemma label…');
  });

  it('does nothing when the user cancels', async () => {
    let apiCalled = false;
    const { ext, progressCalls, lintCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'kisski', modelId: 'gemma' },
      confirmAnswer: false,
      callPluginApiImpl: async () => { apiCalled = true; return { findings: [] }; },
    });
    await ext.runReview();
    assert.strictEqual(apiCalled, false);
    assert.deepStrictEqual(progressCalls, []);
    assert.strictEqual(lintCalls.length, 0);
  });

  it('escapes HTML in the label shown in the dialog', async () => {
    const { ext, confirmCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'a<b>', modelId: 'm&n' },
      callPluginApiImpl: async () => ({ findings: [] }),
    });
    await ext.runReview();
    assert.ok(confirmCalls[0].message.includes('a&lt;b&gt; label/m&amp;n label'));
  });

  it('skips the dialog and lets review() notify when no model is configured', async () => {
    const { ext, confirmCalls, notifyCalls } = buildExtension({ getEditorialDeclGuides: () => rules });
    await ext.runReview();
    assert.strictEqual(confirmCalls.length, 0);
    assert.match(notifyCalls[0][0], /No default model/);
  });

  it('uses an explicit override for the label and the request', async () => {
    const calls = [];
    const { ext, confirmCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'kisski', modelId: 'gemma' },
      callPluginApiImpl: async (e, m, params) => { calls.push(params); return { findings: [] }; },
    });
    await ext.runReview({ providerId: 'other', modelId: 'big' });
    assert.ok(confirmCalls[0].message.includes('other label/big label'));
    assert.strictEqual(calls[0].provider_id, 'other');
  });
});

describe('node-scoped review', () => {
  const rules = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];
  const docText = '<TEI><text><bibl>One</bibl><bibl>Two</bibl></text></TEI>';

  it('review() sends the navigated-to node as scope with the editor text', async () => {
    const calls = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText,
      planImpl: () => ({ chunk_count: 1 }),
      callPluginApiImpl: async (endpoint, method, params) => { calls.push(params); return { findings: [] }; },
    });
    const from = docText.indexOf('<bibl>Two');
    ext._deps.xmleditor.getRangeByXpath = (xpath) => {
      assert.strictEqual(xpath, '//tei:bibl[2]');
      return { from, to: from + '<bibl>Two</bibl>'.length };
    };
    const result = await ext.review(undefined, { xpath: '//tei:bibl[2]' });
    assert.deepStrictEqual(result, []);
    assert.deepStrictEqual(calls[0], { xml: docText, scope: '<bibl>Two</bibl>', provider_id: 'p', model_id: 'm', chunk_index: 0 });
  });

  it('review() notifies and returns null when the node cannot be resolved', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText,
    });
    ext._deps.xmleditor.getRangeByXpath = () => { throw new Error('No XML node found'); };
    assert.strictEqual(await ext.review(undefined, { xpath: '//tei:bibl[9]' }), null);
    assert.strictEqual(notifyCalls.length, 1);
    assert.match(notifyCalls[0][0], /selected node/);
  });

  it('runReview() reviews only the node, without confirmation, when the xpath state is set', async () => {
    const { ext, confirmCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      state: { xml: 'a', xpath: '//tei:bibl[1]' },
    });
    /** @type {any[]} */
    const reviewArgs = [];
    ext.review = async (override, hooks) => { reviewArgs.push(hooks.xpath); return []; };
    await ext.runReview();
    assert.deepStrictEqual(reviewArgs, ['//tei:bibl[1]']);
    assert.strictEqual(confirmCalls.length, 0);
  });

  it('runReview() reviews the whole document when the xpath state is unset', async () => {
    const { ext, confirmCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      state: { xml: 'a', xpath: null },
    });
    const reviewArgs = [];
    ext.review = async (override, hooks) => { reviewArgs.push(hooks.xpath); return []; };
    await ext.runReview();
    assert.deepStrictEqual(reviewArgs, [null]);
    assert.match(confirmCalls[0].message, /current document/);
  });

  it('the menu item reviews the whole document even when the xpath state is set', async () => {
    const { ext, menuAdds, handlers, confirmCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      state: { xml: 'a', xpath: '//tei:bibl[1]' },
    });
    /** @type {any[]} */
    const reviewArgs = [];
    ext.review = async (override, hooks) => { reviewArgs.push([override, hooks.xpath]); return []; };
    await ext.start();
    handlers.editorReady();
    menuAdds[0].items[0]._click();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepStrictEqual(reviewArgs, [[{ providerId: 'p', modelId: 'm' }, null]]);
    assert.match(confirmCalls[0].message, /current document/);
  });
});

describe('unit review marks', () => {
  const doc = '<TEI><text><listBibl><bibl>A <title>T1</title></bibl><bibl>B <title>T2</title></bibl><bibl>C</bibl></listBibl></text></TEI>';
  /** @param {string} needle @param {string} fingerprint */
  const unit = (needle, fingerprint) => {
    const from = doc.indexOf(needle);
    return { fingerprint, from, to: from + needle.length, name: 'bibl' };
  };
  const units = [unit('<bibl>A <title>T1</title></bibl>', 'a'), unit('<bibl>B <title>T2</title></bibl>', 'b'), unit('<bibl>C</bibl>', 'c')];

  it('marks the units of the chunk, not OK where a finding falls into them', () => {
    const chunk = doc.slice(doc.indexOf('<bibl>A'), doc.indexOf('<bibl>C'));
    const marks = unitMarksForChunk(llmFindings.locateFinding, units, doc, chunk, [
      { id: 0, old: '<title>T2</title>', new: '<title level="a">T2</title>', rationale: 'missing level' },
    ]);
    assert.deepStrictEqual(marks, [
      { fingerprint: 'a', state: 'ok', comment: '' },
      { fingerprint: 'b', state: 'not-ok', comment: 'missing level' },
    ]);
  });

  it('marks the pages of a zone document by the range of the page window', () => {
    const zoneDoc = '<TEI><text><body>Page one <lb/></body><body>Page two <lb/>more <lb/></body><body>Page three <lb/></body></text></TEI>';
    /** @param {string} needle @param {string} fingerprint */
    const page = (needle, fingerprint) => {
      const from = zoneDoc.indexOf(needle);
      return { fingerprint, from, to: from + needle.length, name: 'page' };
    };
    const pages = [page('Page one <lb/>', 'p1'), page('Page two <lb/>more <lb/>', 'p2'), page('Page three <lb/>', 'p3')];
    const start = zoneDoc.indexOf('Page one');
    const end = zoneDoc.indexOf('more <lb/>') + 'more <lb/>'.length;
    const marks = unitMarksForChunk(llmFindings.locateFinding, pages, zoneDoc, zoneDoc.slice(start, end), [
      { id: 0, old: '<body>Page two <lb/>more <lb/></body>', new: '<listBibl>Page two <lb/>more <lb/></listBibl>', rationale: 'references' },
    ], { start, end });
    assert.deepStrictEqual(marks, [
      { fingerprint: 'p1', state: 'ok', comment: '' },
      { fingerprint: 'p2', state: 'not-ok', comment: 'references' },
    ]);
  });

  it('records the marks through the unit review plugin when review mode is on', async () => {
    const calls = [];
    const { ext } = buildExtension({ docText: doc });
    ext._deps['unit-review'] = {
      isEnabled: () => true,
      getUnits: () => units,
      setMarks: async (reviewer, marks) => { calls.push({ reviewer, marks }); return true; },
    };
    await ext._recordUnitReview(doc, { findings: [], chunk_start: 0, chunk_end: doc.length }, { providerId: 'kisski', modelId: 'llama 3.3' });
    assert.strictEqual(calls.length, 1);
    assert.deepStrictEqual(calls[0].reviewer, { id: 'llm-kisski-llama-3.3', name: 'kisski label/llama 3.3 label' });
    assert.deepStrictEqual(calls[0].marks.map(m => m.state), ['ok', 'ok', 'ok']);
  });

  it('records nothing when review mode is off, unit review is missing, or the chunk position is unknown', async () => {
    const calls = [];
    const { ext } = buildExtension({ docText: doc });
    await ext._recordUnitReview(doc, { findings: [], chunk_start: 0, chunk_end: doc.length }, { providerId: 'p', modelId: 'm' });
    ext._deps['unit-review'] = { isEnabled: () => false, getUnits: () => units, setMarks: async () => { calls.push(1); } };
    await ext._recordUnitReview(doc, { findings: [], chunk_start: 0, chunk_end: doc.length }, { providerId: 'p', modelId: 'm' });
    ext._deps['unit-review'].isEnabled = () => true;
    await ext._recordUnitReview(doc, { findings: [] }, { providerId: 'p', modelId: 'm' });
    assert.strictEqual(calls.length, 0);
  });
});

describe('chunked review', () => {
  const rules = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];
  const finding = (old) => ({ id: 0, old, new: 'n', rationale: 'r' });

  it('review() requests every chunk, merges the findings and renumbers ids', async () => {
    const indexes = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 3,
      callPluginApiImpl: async (e, m, params) => {
        indexes.push(params.chunk_index);
        return { findings: [finding(`A${params.chunk_index}`)], chunk_count: 3 };
      },
    });
    const result = await ext.review();
    assert.deepStrictEqual(indexes, [0, 1, 2]);
    assert.deepStrictEqual(result.map(f => [f.id, f.old]), [[0, 'A0'], [1, 'A1'], [2, 'A2']]);
  });

  it('review() reports progress after each chunk with the findings so far', async () => {
    const seen = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 2,
      callPluginApiImpl: async (e, m, params) => ({ findings: [finding(`A${params.chunk_index}`)], chunk_count: 2 }),
    });
    await ext.review(undefined, { onProgress: (done, total, found) => seen.push([done, total, found.length]) });
    assert.deepStrictEqual(seen, [[1, 2, 1], [2, 2, 2]]);
  });

  it('review() stops before the next chunk when cancelled and returns what it has', async () => {
    let cancelled = false;
    const indexes = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 5,
      callPluginApiImpl: async (e, m, params) => {
        indexes.push(params.chunk_index);
        return { findings: [finding('A')], chunk_count: 5 };
      },
    });
    const result = await ext.review(undefined, { isCancelled: () => cancelled, onProgress: () => { cancelled = true; } });
    assert.deepStrictEqual(indexes, [0]);
    assert.strictEqual(result.length, 1);
  });

  it('review() names the failing part and returns null when a later chunk fails', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 3,
      callPluginApiImpl: async (e, m, params) => {
        if (params.chunk_index === 1) throw new Error('boom');
        return { findings: [], chunk_count: 3 };
      },
    });
    assert.strictEqual(await ext.review(), null);
    assert.match(notifyCalls.at(-1)[0], /part 2 of 3: boom/);
  });

  it('runReview shows findings as chunks complete and keeps them when a later chunk fails', async () => {
    const { ext, lintCalls, progressCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'A0 A1 A2',
      chunkCount: 3,
      callPluginApiImpl: async (e, m, params) => {
        if (params.chunk_index === 1) throw new Error('boom');
        return { findings: [finding('A0')], chunk_count: 3 };
      },
    });
    await ext.runReview();
    assert.strictEqual(lintCalls.at(-1).own.length, 1);
    assert.strictEqual(progressCalls.at(-1)[0], 'hide');
  });

  it('runReview keeps a finding dismissed during the run dismissed when later chunks complete', async () => {
    let dismiss;
    const { ext, lintCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'A0 A1 A2',
      chunkCount: 2,
      callPluginApiImpl: async (e, m, params) => {
        if (params.chunk_index === 1) dismiss();
        return { findings: [finding(`A${params.chunk_index}`)], chunk_count: 2 };
      },
    });
    await ext.start();
    dismiss = () => ext._display._forgetDismissed([{ from: 0, to: 2, severity: 'info', message: 'r', source: 'annotation-review' }]);
    await ext.runReview();
    assert.deepStrictEqual(ext._display.findings.map(f => f.old), ['A1']);
    assert.deepStrictEqual(lintCalls.at(-1).own.map(d => d.from), [3]);
  });

  it('runReview updates the progress value and label per chunk', async () => {
    const { ext, progressCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 2,
      callPluginApiImpl: async () => ({ findings: [], chunk_count: 2 }),
    });
    await ext.runReview();
    assert.match(progressCalls.filter(c => c[0] === 'label').map(c => c[1]).join('|'), /part 1 of 2.*part 2 of 2/);
    assert.deepStrictEqual(progressCalls.filter(c => c[0] === 'value').map(c => c[1]), [0, 50, 100]);
  });

  it('cancelling via the widget stops the review and reports the partial result', async () => {
    const indexes = [];
    let ctx;
    ctx = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'A0 A1 A2',
      chunkCount: 3,
      callPluginApiImpl: async (e, m, params) => {
        indexes.push(params.chunk_index);
        if (params.chunk_index === 0) ctx.progressOpts.show.onCancel();
        return { findings: [finding(`A${params.chunk_index}`)], chunk_count: 3 };
      },
    });
    await ctx.ext.runReview();
    assert.deepStrictEqual(indexes, [0]);
    assert.match(ctx.notifyCalls.at(-1)[0], /cancelled after 1 of 3 parts; 1 suggestion/);
    assert.strictEqual(ctx.progressCalls.at(-1)[0], 'hide');
  });

  it('opens the lint panel only for the first batch of findings', async () => {
    const { ext, lintCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      docText: 'A0 A1',
      chunkCount: 2,
      callPluginApiImpl: async (e, m, params) => ({ findings: [finding(`A${params.chunk_index}`)], chunk_count: 2 }),
    });
    await ext.runReview();
    assert.deepStrictEqual(lintCalls.map(c => c.options.openPanel), [true, false]);
  });
});

describe('plan request', () => {
  const rules = [{ category: 'p', refs: [{ target: 'x', contentType: null, subtype: 'machine' }] }];

  it('asks for the chunk count before any review request and reports it to onStart', async () => {
    const endpoints = [];
    const { ext } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      chunkCount: 2,
      callPluginApiImpl: async (endpoint) => { endpoints.push(endpoint); return { findings: [], chunk_count: 2 }; },
    });
    const started = [];
    await ext.review(undefined, { onStart: (t) => started.push(t) });
    assert.deepStrictEqual(started, [2]);
    assert.deepStrictEqual(endpoints, ['/api/plugins/annotation-review/review', '/api/plugins/annotation-review/review']);
  });

  it('notifies and returns null when the plan request fails', async () => {
    const { ext, notifyCalls } = buildExtension({
      getEditorialDeclGuides: () => rules,
      defaultModel: { providerId: 'p', modelId: 'm' },
      planImpl: async () => { throw new Error('not well-formed'); },
    });
    assert.strictEqual(await ext.review(), null);
    assert.match(notifyCalls.at(-1)[0], /not well-formed/);
  });
});
