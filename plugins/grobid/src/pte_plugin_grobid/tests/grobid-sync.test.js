/**
 * Unit tests for the page break checks of the grobid-sync frontend extension.
 *
 * @testCovers plugins/grobid/src/pte_plugin_grobid/extensions/grobid-sync.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';

// Stub the browser-only base class before the dynamic import below.
global.FrontendExtensionPlugin = class {};

const { pageBreaksOf, alignLines, pageBreakDiagnostics } = await import('../extensions/grobid-sync.js');

const DOC = `<TEI><teiHeader/><text>
  <pb n="1"/>
  <front>A Sample Article <lb/>Jane Doe <lb/></front>
  <body>Introduction text <lb/>more text <lb/></body>
  <pb n="2"/>
  <page>2</page> <note place="headnote">Doe, A Sample Article <lb/></note>
  <body>Second page <lb/>end of page <lb/></body>
</text></TEI>`;
const FEATURE_TOKENS = ['A', 'Jane', 'Introduction', 'more', '2', 'Second', 'end'];
const PAGE_STARTS = [0, 4];

/**
 * The line tokens of a document: the first word after each <lb/> (the extension's line-based extraction).
 * @param {string} xml
 */
function lineTokens(xml) {
  const text = xml.slice(xml.indexOf('<text'));
  const offset = xml.indexOf('<text');
  const tokens = [];
  for (const m of text.matchAll(/(^|<lb\/>)((?:\s|<[^>]*>)*)([^\s<]+)/g)) {
    if (m.index === 0 && m[1] === '') continue;
    const from = offset + /** @type {number} */ (m.index) + m[1].length + m[2].length;
    tokens.push({ token: m[3], from, to: from + m[3].length });
  }
  // the first line starts after <text>'s first zone tag
  const first = /<front>(?:<pb[^>]*>)?(\S+)/.exec(xml);
  const firstFrom = /** @type {RegExpExecArray} */ (first).index + first[0].length - first[1].length;
  return [{ token: first[1], from: firstFrom, to: firstFrom + first[1].length }, ...tokens].filter(t => t.token !== '</text></TEI>');
}

describe('grobid-sync page breaks', () => {
  it('finds the <pb/> elements and the lines they start', () => {
    const lines = lineTokens(DOC);
    assert.deepStrictEqual(lines.map(l => l.token), FEATURE_TOKENS);
    const breaks = pageBreaksOf(DOC, lines);
    assert.deepStrictEqual(breaks.map(pb => [pb.n, pb.line]), [['1', 0], ['2', 4]]);
    assert.strictEqual(DOC.slice(breaks[1].from, breaks[1].to), '<pb n="2"/>');
  });

  it('aligns lines with feature rows like layout.py', () => {
    assert.deepStrictEqual(alignLines(['a', 'b', 'c', 'd'], ['a', 'x', 'b', 'd']), [0, 2, null, 3]);
    assert.deepStrictEqual(alignLines(['a', 'q', 'c'], ['a', 'z', 'c']), [0, 1, 2]);
  });

  it('accepts page breaks at or near the page starts', () => {
    const lines = lineTokens(DOC);
    assert.deepStrictEqual(pageBreakDiagnostics(lines, pageBreaksOf(DOC, lines), FEATURE_TOKENS, PAGE_STARTS), []);
    const near = DOC.replace('<pb n="2"/>\n  <page>2</page>', '<page>2</page>').replace('more text <lb/>', '<pb n="2"/>more text <lb/>');
    const nearLines = lineTokens(near);
    assert.deepStrictEqual(pageBreakDiagnostics(nearLines, pageBreaksOf(near, nearLines), FEATURE_TOKENS, PAGE_STARTS), []);
  });

  it('reports missing, misplaced and misnumbered page breaks', () => {
    const lines = lineTokens(DOC);
    const breaks = pageBreaksOf(DOC, lines);
    // no <pb/> for page 2
    const missing = pageBreakDiagnostics(lines, [breaks[0]], FEATURE_TOKENS, PAGE_STARTS);
    assert.strictEqual(missing.length, 1);
    assert.match(missing[0].message, /Page 2 of the PDF starts here/);
    assert.strictEqual(missing[0].from, lines[4].from);
    // a <pb/> far from any page start (with more lines in between)
    const far = pageBreakDiagnostics(lines, [...breaks, { from: 1, to: 2, n: null, line: 20 }], FEATURE_TOKENS, PAGE_STARTS);
    assert.deepStrictEqual(far.map(d => d.message), ['This <pb/> is not at a page start of the PDF (feature file)']);
    // a wrong number
    const wrong = pageBreakDiagnostics(lines, [breaks[0], { ...breaks[1], n: '5' }], FEATURE_TOKENS, PAGE_STARTS);
    assert.deepStrictEqual(wrong.map(d => d.message), ['This <pb/> starts page 2 of the PDF, not page 5']);
  });

  it('says nothing about documents without page breaks', () => {
    const lines = lineTokens(DOC);
    assert.deepStrictEqual(pageBreakDiagnostics(lines, [], FEATURE_TOKENS, PAGE_STARTS), []);
  });
});
