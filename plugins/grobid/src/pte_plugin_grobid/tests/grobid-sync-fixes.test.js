/**
 * Unit tests for the fixes and pages of the grobid-sync diagnostics.
 *
 * @testCovers plugins/grobid/src/pte_plugin_grobid/extensions/grobid-sync.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';

// Stub the browser-only base class before the dynamic import below.
global.FrontendExtensionPlugin = class {};

const { diffTokens, joinTokens, linePages, withPages, pendingPageMessage } = await import('../extensions/grobid-sync.js');

/**
 * Whitespace-separated tokens of a text with their positions (enough for these tests).
 * @param {string} text
 */
function tokensOf(text) {
  return [...text.matchAll(/[^\s<>]+/g)].map(m => ({ token: m[0], from: /** @type {number} */ (m.index), to: /** @type {number} */ (m.index) + m[0].length }));
}

/**
 * The text with the fix of a diagnostic applied.
 * @param {string} text
 * @param {{from: number, to: number, replacement?: string}} d
 */
function applyFix(text, d) {
  assert.strictEqual(typeof d.replacement, 'string');
  return text.slice(0, d.from) + d.replacement + text.slice(d.to);
}

const FEATURE = ['The', 'quick', 'brown', 'fox', ',', 'jumps', 'over', 'the', 'dog'];
const ORIGINAL = 'The quick brown fox , jumps over the dog';

describe('grobid-sync fixes', () => {
  it('finds nothing in the original', () => {
    assert.deepStrictEqual(diffTokens(FEATURE, tokensOf(ORIGINAL), ORIGINAL, false), []);
  });

  it('restores a replaced token', () => {
    const text = 'The quick brown cat , jumps over the dog';
    const [d] = diffTokens(FEATURE, tokensOf(text), text, false);
    assert.match(d.message, /expected "fox", found "cat"/);
    assert.strictEqual(applyFix(text, d), ORIGINAL);
  });

  it('deletes extra tokens with the space before them', () => {
    const text = 'The quick brown fox , jumps high over the dog';
    const [d] = diffTokens(FEATURE, tokensOf(text), text, false);
    assert.match(d.message, /unexpected token/);
    assert.strictEqual(applyFix(text, d), ORIGINAL);
  });

  it('inserts missing tokens', () => {
    const text = 'The quick fox , jumps over the dog';
    const [d] = diffTokens(FEATURE, tokensOf(text), text, false);
    assert.match(d.message, /1 token\(s\) missing/);
    assert.strictEqual(applyFix(text, d), 'The quick brown fox , jumps over the dog');
  });

  it('escapes restored tokens', () => {
    const text = 'A and B';
    const [d] = diffTokens(['A', '&', 'B'], tokensOf(text), text, false);
    assert.strictEqual(d.replacement, '&amp;');
  });

  it('only restores replaced first words in line-based documents', () => {
    const text = 'Foo Bar Baz';
    const diagnostics = diffTokens(['Foo', 'Qux', 'Bar', 'Baz'], tokensOf(text), text, true);
    assert.strictEqual(diagnostics.length, 1);
    assert.strictEqual(diagnostics[0].replacement, undefined);
    const [replaced] = diffTokens(['Foo', 'Qux', 'Baz'], tokensOf(text), text, true);
    assert.strictEqual(applyFix(text, replaced), 'Foo Qux Baz');
  });

  it('joins tokens without spaces around punctuation', () => {
    assert.strictEqual(joinTokens(['fox', ',', '(', 'a', ')', 'b']), 'fox, (a) b');
  });
});

describe('grobid-sync pages', () => {
  it('gives every line the page of its feature row', () => {
    // rows 0-1 on page 0, rows 2-3 on page 1; line 'x' has no row
    assert.deepStrictEqual(linePages(['a', 'b', 'x', 'c', 'd'], ['a', 'b', 'c', 'd'], [0, 2]), [0, 0, 0, 1, 1]);
    assert.deepStrictEqual(linePages(['a'], ['a'], []), [null]);
  });

  it('replaces the diagnostics of pending pages with one note per page', () => {
    const d = (index, from) => ({ from, to: from + 1, severity: 'error', message: `m${index}`, index });
    const result = withPages([d(0, 0), d(1, 5), d(2, 9)], [0, 1, 1], new Set([1]));
    assert.deepStrictEqual(result, [
      { from: 0, to: 1, severity: 'error', message: 'm0', page: 0 },
      { from: 5, to: 6, severity: 'info', message: pendingPageMessage(1) }
    ]);
  });
});
