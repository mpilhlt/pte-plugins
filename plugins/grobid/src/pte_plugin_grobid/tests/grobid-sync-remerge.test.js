/**
 * Unit tests for the grobid-sync frontend extension merging its diagnostics in again after another
 * source replaced the diagnostic set (e.g. the well-formedness check after a document is loaded).
 *
 * @testCovers plugins/grobid/src/pte_plugin_grobid/extensions/grobid-sync.js
 */

import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert';

/** @type {Record<string, object>} */
let dependencies = {};

// Stub the browser-only base class before the dynamic import below.
global.FrontendExtensionPlugin = class {
  async install() {}
  /** @param {string} name */
  getDependency(name) { return dependencies[name]; }
  async callPluginApi() {
    return { status: 'ok', tokens: ['Hello', 'world'], line_based: false, page_starts: [] };
  }
};

const { default: GrobidSyncExtension, SOURCE } = await import('../extensions/grobid-sync.js');

const XML_IN_SYNC = '<TEI><text><body>Hello world</body></text></TEI>';
const XML_OUT_OF_SYNC = '<TEI><text><body>Hello there</body></text></TEI>';

/** Waits for the timers the extension schedules. */
const tick = () => new Promise(resolve => setTimeout(resolve, 5));

/**
 * Sets up the extension with a fake XML editor and lint-utils showing `xml`.
 * @param {string} xml
 */
async function setup(xml) {
  const doc = { toString: () => xml };
  const view = { state: { doc, diagnostics: /** @type {object[]} */ ([]) } };
  /** @type {Array<(update: object) => void>} */
  const updateListeners = [];
  /** @type {Array<(dismissed: object[]) => void>} */
  const dismissListeners = [];
  /** @type {object[][]} */
  const merges = [];
  dependencies = {
    xmleditor: {
      registerLintSource() {},
      refreshLinting() {},
      getView: () => view,
      /** @param {(update: object) => void} fn */
      addUpdateListener: fn => updateListeners.push(fn),
    },
    'lint-utils': {
      /** @param {{replaces: boolean}} update */
      replacesDiagnostics: update => update.replaces,
      /** @param {{diagnostics: object[]}} state */
      collectDiagnostics: state => state.diagnostics,
      /** @param {(dismissed: object[]) => void} fn */
      addDismissListener: fn => dismissListeners.push(fn),
      /**
       * @param {object} _view
       * @param {string} source
       * @param {object[]} own
       */
      applyMergedDiagnostics: (_view, source, own) => {
        assert.strictEqual(source, SOURCE);
        merges.push(own);
      },
    },
  };
  const extension = new GrobidSyncExtension({});
  await extension.install({ xml: 'stable-1' });
  /** @param {object[]} diagnostics */
  const replaceDiagnostics = diagnostics => {
    view.state.diagnostics = diagnostics;
    for (const fn of updateListeners) fn({ docChanged: false, replaces: true, state: view.state });
  };
  /** @param {object[]} dismissed */
  const dismiss = dismissed => {
    replaceDiagnostics([]);
    for (const fn of dismissListeners) fn(dismissed);
  };
  const edit = () => { for (const fn of updateListeners) fn({ docChanged: true, replaces: true, state: view.state }); };
  return { merges, replaceDiagnostics, dismiss, edit };
}

describe('grobid-sync diagnostics after a replacement of the diagnostic set', () => {
  beforeEach(() => { dependencies = {}; });

  it('merges its diagnostics in again when another source removed them', async () => {
    const { merges, replaceDiagnostics } = await setup(XML_OUT_OF_SYNC);
    replaceDiagnostics([]);
    await tick();
    assert.strictEqual(merges.length, 1);
    assert.ok(merges[0].length > 0);
    assert.ok(merges[0].every(d => d.source === SOURCE));
  });

  it('does not merge when its diagnostics are still there', async () => {
    const { merges, replaceDiagnostics } = await setup(XML_OUT_OF_SYNC);
    replaceDiagnostics([{ from: 0, to: 1, message: 'x', source: SOURCE }]);
    await tick();
    assert.strictEqual(merges.length, 0);
  });

  it('merges nothing for a document in sync', async () => {
    const { merges, replaceDiagnostics } = await setup(XML_IN_SYNC);
    replaceDiagnostics([]);
    await tick();
    assert.deepStrictEqual(merges, [[]]);
  });

  it('leaves out dismissed diagnostics until the next edit', async () => {
    const { merges, replaceDiagnostics, dismiss, edit } = await setup(XML_OUT_OF_SYNC);
    replaceDiagnostics([]);
    await tick();
    const [first] = merges[0];
    dismiss([first]);
    await tick();
    assert.ok(!merges[1].some(d => d.from === first.from && d.message === first.message));
    edit();
    replaceDiagnostics([]);
    await tick();
    assert.ok(merges[2].some(d => d.from === first.from && d.message === first.message));
  });
});
