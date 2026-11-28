/**
 * Unit tests for the page break checks of the grobid-sync frontend extension.
 *
 * @testCovers plugins/grobid/src/pte_plugin_grobid/extensions/grobid-sync.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';

// Stub the browser-only base class before the dynamic import below.
global.FrontendExtensionPlugin = class {};

const { pageBreaksOf, alignLines, pageBreakDiagnostics, foldDashes, joinedFirstWord, lineForms, diffTokens } = await import('../extensions/grobid-sync.js');

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

  it('reports misplaced and misnumbered page breaks, but not missing ones', () => {
    const lines = lineTokens(DOC);
    const breaks = pageBreaksOf(DOC, lines);
    // no <pb/> for page 2
    assert.deepStrictEqual(pageBreakDiagnostics(lines, [breaks[0]], FEATURE_TOKENS, PAGE_STARTS), []);
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

describe('foldDashes', () => {
  it('folds the dashes GROBID normalizes to "-" and keeps the length', () => {
    const text = '— II. – x − y ‐ z - w';
    assert.strictEqual(foldDashes(text), '- II. - x - y - z - w');
    assert.strictEqual(foldDashes(text).length, text.length);
  });
});

describe('first words ending with a dash', () => {
  // The PDF and the TEI (dashes.py) have "— dieser Beitrag", GROBID's feature row "-dieser": GROBID
  // adds no space after a token that ends with a dash
  const xml = '<text>— dieser Beitrag <lb/>möchte</text>';
  const lines = [
    { token: '-', joined: '-dieser', from: 6, to: 7 },
    { token: 'möchte', from: 30, to: 36 }
  ];

  it('joins a first word ending with a dash with the following words', () => {
    assert.strictEqual(joinedFirstWord('— dieser Beitrag'), '-dieser');
    assert.strictEqual(joinedFirstWord('rechts- und links'), 'rechts-und');
    assert.strictEqual(joinedFirstWord('— – x y'), '--x');
    assert.strictEqual(joinedFirstWord('dieser Beitrag'), null);
    assert.strictEqual(joinedFirstWord('—'), null);
    assert.strictEqual(joinedFirstWord('  '), null);
  });

  it('matches the feature token with and without the space', () => {
    assert.deepStrictEqual(diffTokens(['-dieser', 'möchte'], lines, xml, true), []);
    // newer GROBID versions keep the space if pdfalto reports one
    assert.deepStrictEqual(diffTokens(['-', 'möchte'], lines, xml, true), []);
    const changed = diffTokens(['-jener', 'möchte'], lines, xml, true);
    assert.deepStrictEqual(changed.map(d => d.message), ['Sync: expected "-jener", found "-"']);
  });

  it('aligns lines by either form', () => {
    assert.deepStrictEqual(lineForms(lines[0]), ['-', '-dieser']);
    assert.strictEqual(lineForms(lines[1]), 'möchte');
    assert.deepStrictEqual(alignLines(lines.map(lineForms), ['-dieser', 'möchte']), [0, 1]);
    assert.deepStrictEqual(alignLines(['x', ...lines.map(lineForms)], ['x', 'y', '-dieser', 'möchte']), [0, 2, 3]);
  });
});
