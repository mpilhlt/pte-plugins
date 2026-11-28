/**
 * Checks whether the text of a GROBID training document still matches the cached feature file of
 * its extraction, and reports the differences as diagnostics (source `grobid-sync`), registered as
 * a sync source of the core (`registerSyncSource()` in lint-utils.js) so that the sync prompt asks
 * reviewers to write intended changes into the PDF.
 *
 * Diagnostics have a fix (`replacement`) restoring the text of the feature file where it is known:
 * replaced, extra and missing tokens of token-based variants, and replaced first words of the lines
 * of segmentation documents (whose feature file only has the first words of each line).
 * Segmentation diagnostics name their page (`page`, 0-based). On pages whose text corrections were
 * written into the PDF page by page (`pending_pages`), the feature file still has the old text: their
 * word diagnostics are replaced by one note per page.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 * @import { Diagnostic } from '@codemirror/lint'
 * @import { EditorView } from '@codemirror/view'
 * @import { Text } from '@codemirror/state'
 */

// Tokenizer matching GROBID's GrobidDefaultAnalyzer (StringTokenizer with
// TextUtilities.delimiters from grobid-core). A token is either a maximal run of
// non-delimiter non-whitespace characters, or a single non-whitespace delimiter
// character. Delimiter set = TextUtilities.fullPunctuations + whitespace.
// Characters NOT in the delimiter set (§, <, >, & etc.) attach to adjacent chars,
// producing tokens like §2, <www, uk>.
//
// fullPunctuations (verbatim from TextUtilities.java):
//   ( （ [ space • * , : ; ? . ! / ) ） - − – ‐
//   « » „ “ ” " ‘ ’ ' ` $ # @ ] * ♦ ♥ ♣ ♠ NBSP 。 、 ， ・
const _DELIM_CLASS =
  '(\\uff08\\[\\u2022*,:;?.!/)\\uff09\\-\\u2212\\u2013\\u2010«»' +
  '„“”"‘’\'`$#@\\]♦♥♣♠' +
  ' 。、，・ \\t\\n\\r\\f‌';
const TOKEN_RE = new RegExp(
  `[^${_DELIM_CLASS}]+|[^ \\t\\n\\r\\f ‌]`,
  'g'
);

// The dashes GROBID normalizes to '-' (UnicodeUtil.normaliseText(): class Pd and the minus sign).
// The extractor restores them in the TEI from the PDF (dashes.py), the feature file keeps '-'.
const DASH_RE = /[\p{Pd}\u2212]/gu;

/**
 * Text with every dash replaced by '-', as GROBID normalizes it. Keeps the length of the text.
 * @param {string} text
 * @returns {string}
 */
export function foldDashes(text) {
  return text.replace(DASH_RE, '-');
}

/**
 * The first word of a line as GROBID's feature file may have it if the word ends with a dash:
 * GROBID adds no space after a token that ends with a dash (`PDFALTOSaxHandler`), so its row for
 * "— dieser Beitrag" starts with "-dieser", while the TEI has the PDF's space again (dashes.py).
 * Newer GROBID versions keep a wide enough space, so both forms must be accepted.
 * @param {string} text - Decoded text of the line
 * @returns {string|null} The words up to the first that does not end with a dash, joined and with
 *   dashes folded; null if the first word does not end with a dash or nothing follows it
 */
export function joinedFirstWord(text) {
  const words = foldDashes(text).split(/\s+/).filter(Boolean);
  if (words.length < 2 || !/** @type {string} */ (words[0]).endsWith('-')) return null;
  let joined = /** @type {string} */ (words[0]);
  for (let i = 1; i < words.length && joined.endsWith('-'); i++) joined += words[i];
  return joined;
}

/**
 * The forms of a line's first word that match a feature row: the word, and the joined form of
 * `joinedFirstWord()` if there is one.
 * @param {{token: string, joined?: string}} line
 * @returns {string|string[]}
 */
export function lineForms(line) {
  return line.joined ? [line.token, line.joined] : line.token;
}

/**
 * Whether a feature token matches a token or line of the document.
 * @param {string|undefined} featureToken
 * @param {string|string[]|null|undefined} forms - A token, or the forms of a line (see `lineForms()`)
 * @returns {boolean}
 */
function matches(featureToken, forms) {
  if (featureToken === undefined || forms === null || forms === undefined) return false;
  return Array.isArray(forms) ? forms.includes(featureToken) : forms === featureToken;
}

// Matches XML character and named entity references: &amp; &#60; &#x3c;
const ENTITY_RE = /&(?:#(\d+)|#x([0-9a-fA-F]+)|([a-zA-Z][a-zA-Z0-9]*));/g;
const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', apos: "'", quot: '"' };

// Lookahead window for the forward-scan diff recovery.
const WINDOW = 10;

// Lookahead of the line alignment for page breaks, as `WINDOW` in layout.py
const LINE_WINDOW = 5;

// Lines before or after a page start within which a <pb/> counts as its page break, as
// PAGE_BREAK_VICINITY in the editor's zone_lines.py
export const PAGE_BREAK_VICINITY = 3;

// Source of the diagnostics, which marks them as this extension's in the shared diagnostic set
export const SOURCE = 'grobid-sync';

// What the diagnostics report drift with, for the sync prompt of the core
export const SYNC_TARGET = 'GROBID training data';

// Tokens not preceded by a space when missing tokens are inserted
const NO_SPACE_BEFORE = /^[,.;:!?)\]}»”’%]/;

// Tokens not followed by a space when missing tokens are inserted
const NO_SPACE_AFTER = /[(\[{«„“‘]$/;

/**
 * A diagnostic of this extension
 * @typedef {Diagnostic & {replacement?: string, page?: number}} SyncDiagnostic
 */

/**
 * A token of the document with its position in the XML; `joined` is the other form of the first
 * word of a line (see `joinedFirstWord()`)
 * @typedef {{token: string, from: number, to: number, joined?: string}} XmlToken
 */

/**
 * The text as XML character data.
 * @param {string} text
 * @returns {string}
 */
export function escapeXml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Tokens joined with spaces, except before closing and after opening punctuation.
 * @param {string[]} tokens
 * @returns {string}
 */
export function joinTokens(tokens) {
  let result = '';
  tokens.forEach((token, i) => {
    if (i > 0 && !NO_SPACE_BEFORE.test(token) && !NO_SPACE_AFTER.test(/** @type {string} */ (tokens[i - 1]))) result += ' ';
    result += token;
  });
  return result;
}

/**
 * The page (0-based) of every line, by the feature file row it is aligned with (see
 * `alignLines()`); a line without a row is on the page of the line before it.
 * @param {(string|string[])[]} lineTokens - See `lineForms()`
 * @param {string[]} rowTokens
 * @param {number[]} pageStarts - Indices of the rows that start a page, ascending
 * @returns {(number|null)[]} null for lines before the first aligned line or without page starts
 */
export function linePages(lineTokens, rowTokens, pageStarts) {
  if (pageStarts.length === 0) return lineTokens.map(() => null);
  const mapping = alignLines(lineTokens, rowTokens);
  /** @type {(number|null)[]} */
  const result = [];
  /** @type {number|null} */
  let page = null;
  for (const row of mapping) {
    if (row !== null) {
      let p = 0;
      while (p + 1 < pageStarts.length && /** @type {number} */ (pageStarts[p + 1]) <= row) p++;
      page = p;
    }
    result.push(page);
  }
  return result;
}

/**
 * Forward-scan diff of the feature tokens and the document's tokens with a small lookahead window
 * to recover alignment after a mismatch, emitting one diagnostic per problem region, each with the
 * index of its first document token (`index`). Fixes restore the feature tokens: for line-based
 * documents (whose tokens are the first words of the lines) only replaced words get one.
 * @param {string[]} featureTokens
 * @param {XmlToken[]} xmlTokens
 * @param {string} xmlText - The document the tokens are from
 * @param {boolean} lineBased
 * @returns {(SyncDiagnostic & {index: number})[]}
 */
export function diffTokens(featureTokens, xmlTokens, xmlText, lineBased) {
  /** @type {(SyncDiagnostic & {index: number})[]} */
  const diagnostics = [];
  let fi = 0;
  let xi = 0;

  while (fi < featureTokens.length && xi < xmlTokens.length) {
    const xmlToken = /** @type {XmlToken} */ (xmlTokens[xi]);
    if (matches(featureTokens[fi], lineForms(xmlToken))) {
      fi++;
      xi++;
      continue;
    }

    let recovered = false;
    for (let d = 1; d <= WINDOW; d++) {
      // Missing tokens: feature list is ahead of the XML
      if (fi + d < featureTokens.length && matches(featureTokens[fi + d], lineForms(xmlToken))) {
        /** @type {SyncDiagnostic & {index: number}} */
        const diagnostic = {
          from: xmlToken.from,
          to: xmlToken.to,
          severity: 'warning',
          message: `Sync: ${d} token(s) missing before here (expected "${featureTokens[fi]}")`,
          index: xi
        };
        if (!lineBased) {
          const missing = featureTokens.slice(fi, fi + d);
          diagnostic.replacement = escapeXml(joinTokens([...missing, xmlToken.token])).slice(0, -escapeXml(xmlToken.token).length) + xmlText.slice(xmlToken.from, xmlToken.to);
        }
        diagnostics.push(diagnostic);
        fi += d;
        recovered = true;
        break;
      }
      // Extra tokens: XML has tokens the feature file does not
      if (xi + d < xmlTokens.length && matches(featureTokens[fi], lineForms(/** @type {XmlToken} */ (xmlTokens[xi + d])))) {
        const to = /** @type {XmlToken} */ (xmlTokens[xi + d - 1]).to;
        /** @type {SyncDiagnostic & {index: number}} */
        const diagnostic = {
          from: xmlToken.from,
          to,
          severity: 'warning',
          message: `Sync: unexpected token(s) "${xmlToken.token}" (expected "${featureTokens[fi]}")`,
          index: xi
        };
        // only text between the tokens, no markup, is deleted with them
        if (!lineBased && !xmlText.slice(xmlToken.from, to).includes('<')) {
          let from = xmlToken.from;
          while (from > 0 && /[ \t]/.test(xmlText.charAt(from - 1))) from--;
          // keep the space if the tokens start the text after a tag or a line break
          if (from > 0 && /[>\n]/.test(xmlText.charAt(from - 1))) from = xmlToken.from;
          diagnostic.from = from;
          diagnostic.replacement = '';
        }
        diagnostics.push(diagnostic);
        xi += d;
        recovered = true;
        break;
      }
    }

    if (!recovered) {
      diagnostics.push({
        from: xmlToken.from,
        to: xmlToken.to,
        severity: 'error',
        message: `Sync: expected "${featureTokens[fi]}", found "${xmlToken.token}"`,
        replacement: escapeXml(/** @type {string} */ (featureTokens[fi])),
        index: xi
      });
      fi++;
      xi++;
    }
  }

  if (xi < xmlTokens.length && fi >= featureTokens.length) {
    diagnostics.push({
      from: /** @type {XmlToken} */ (xmlTokens[xi]).from,
      to: /** @type {XmlToken} */ (xmlTokens[xmlTokens.length - 1]).to,
      severity: 'warning',
      message: `Sync: ${xmlTokens.length - xi} extra token(s) at end (feature file has ${featureTokens.length} tokens)`,
      index: xi
    });
  }

  return diagnostics;
}

/**
 * The note shown instead of the word diagnostics of a page whose text corrections were written
 * into the PDF page by page.
 * @param {number} page - 0-based
 * @returns {string}
 */
export function pendingPageMessage(page) {
  return `Page ${page + 1}: the GROBID training data does not include the text corrections written to the PDF for this page yet. The next "Write text corrections to PDF" for the whole document (Tools menu) updates it.`;
}

/**
 * The diagnostics of a line-based document with their pages: diagnostics on pages in
 * `pendingPages` are replaced by one note (info) per page, at the first of them.
 * @param {(SyncDiagnostic & {index: number})[]} diagnostics
 * @param {(number|null)[]} pages - Page of every line token (see `linePages()`)
 * @param {Set<number>} pendingPages
 * @returns {SyncDiagnostic[]}
 */
export function withPages(diagnostics, pages, pendingPages) {
  /** @type {SyncDiagnostic[]} */
  const result = [];
  /** @type {Set<number>} */
  const noted = new Set();
  for (const { index, ...diagnostic } of diagnostics) {
    const page = pages[index] ?? null;
    if (page === null) {
      result.push(diagnostic);
    } else if (!pendingPages.has(page)) {
      result.push({ ...diagnostic, page });
    } else if (!noted.has(page)) {
      noted.add(page);
      result.push({ from: diagnostic.from, to: diagnostic.to, severity: 'info', message: pendingPageMessage(page) });
    }
  }
  return result;
}

const PB_RE = /<(?:[\w.-]+:)?pb\b((?:"[^"]*"|'[^']*'|[^>"'])*)\/?>/g;

/**
 * @typedef {object} LineToken
 * @property {string} token - First word of the line
 * @property {number} from - Offset of the word in the XML
 * @property {number} to - Offset after the word
 * @property {string} [joined] - Its other form if it ends with a dash (see `joinedFirstWord()`)
 */

/**
 * @typedef {object} FeatureTokens
 * @property {string[]} tokens - Tokens of the feature file
 * @property {boolean} lineBased - Whether the tokens are the first words of the lines
 * @property {number[]} pageStarts - Indices of the tokens whose row starts a page (segmentation only)
 * @property {number[]} pendingPages - Pages (0-based) whose text corrections were written into the
 *   PDF page by page, which the feature file does not have yet
 */

/**
 * @typedef {object} PageBreakTag
 * @property {number} from - Offset of the tag in the XML
 * @property {number} to - Offset after the tag
 * @property {string|null} n - Its `n` attribute
 * @property {number} line - Index of the line (in the line tokens) that starts its page
 */

/**
 * The `<pb/>` elements in `<text>`, each with the line whose page it starts: the first line
 * whose first word follows it.
 * @param {string} xmlText
 * @param {LineToken[]} lines - The line tokens of the document (see `#extractFirstLineTokens`)
 * @returns {PageBreakTag[]}
 */
export function pageBreaksOf(xmlText, lines) {
  const openMatch = /<text[\s>\/]/.exec(xmlText);
  if (!openMatch) return [];
  const closeIdx = xmlText.lastIndexOf('</text>');
  const end = closeIdx !== -1 ? closeIdx : xmlText.length;
  /** @type {PageBreakTag[]} */
  const result = [];
  let line = 0;
  PB_RE.lastIndex = openMatch.index;
  for (let m; (m = PB_RE.exec(xmlText)) && m.index < end;) {
    while (line < lines.length && /** @type {LineToken} */ (lines[line]).from < m.index) line++;
    const n = /\bn\s*=\s*(?:"([^"]*)"|'([^']*)')/.exec(m[1] ?? '');
    result.push({ from: m.index, to: m.index + m[0].length, n: n ? (n[1] ?? n[2] ?? null) : null, line });
  }
  return result;
}

/**
 * The feature file row of every line (null for lines without a row), by first tokens: the
 * forward scan of `align()` in layout.py.
 * @param {(string|string[])[]} lineTokens - The first word of each line, or its forms (see `lineForms()`)
 * @param {string[]} rowTokens
 * @returns {(number|null)[]}
 */
export function alignLines(lineTokens, rowTokens) {
  /** @type {(number|null)[]} */
  const result = new Array(lineTokens.length).fill(null);
  let li = 0;
  let ri = 0;
  while (li < lineTokens.length && ri < rowTokens.length) {
    if (matches(rowTokens[ri], lineTokens[li])) {
      result[li++] = ri++;
      continue;
    }
    let skipped = false;
    for (let d = 1; d <= LINE_WINDOW; d++) {
      if (ri + d < rowTokens.length && matches(rowTokens[ri + d], lineTokens[li])) {
        ri += d;
        skipped = true;
        break;
      }
      if (li + d < lineTokens.length && matches(rowTokens[ri], lineTokens[li + d])) {
        li += d;
        skipped = true;
        break;
      }
    }
    if (!skipped) result[li++] = ri++;
  }
  return result;
}

/**
 * Diagnostics for `<pb/>` elements that disagree with the page starts of the feature file: a
 * `<pb/>` without a page start within PAGE_BREAK_VICINITY lines, and a `<pb/>` whose `n` is not the
 * number of its page. A page start without a `<pb/>` gets none.
 * @param {LineToken[]} lines - The line tokens of the document
 * @param {PageBreakTag[]} pageBreaks - Its `<pb/>` elements
 * @param {string[]} featureTokens - First tokens of the feature file rows
 * @param {number[]} pageStarts - Indices of the rows that start a page
 * @returns {Diagnostic[]}
 */
export function pageBreakDiagnostics(lines, pageBreaks, featureTokens, pageStarts) {
  if (pageBreaks.length === 0 || pageStarts.length === 0 || lines.length === 0) return [];
  const mapping = alignLines(lines.map(lineForms), featureTokens);
  // the first line at or after each page start row
  /** @type {{line: number, page: number}[]} */
  const starts = [];
  let line = 0;
  pageStarts.forEach((row, index) => {
    while (line < lines.length && (mapping[line] === null || /** @type {number} */ (mapping[line]) < row)) line++;
    if (line < lines.length && !starts.some(s => s.line === line)) starts.push({ line, page: index + 1 });
  });
  /** @type {Diagnostic[]} */
  const diagnostics = [];
  for (const pb of pageBreaks) {
    const near = starts
      .filter(s => Math.abs(s.line - pb.line) <= PAGE_BREAK_VICINITY)
      .sort((a, b) => Math.abs(a.line - pb.line) - Math.abs(b.line - pb.line) || a.line - b.line)[0];
    if (!near) {
      diagnostics.push({ from: pb.from, to: pb.to, severity: 'warning', message: 'This <pb/> is not at a page start of the PDF (feature file)' });
    } else if (pb.n !== null && /^\d+$/.test(pb.n) && Number(pb.n) !== near.page) {
      diagnostics.push({ from: pb.from, to: pb.to, severity: 'warning', message: `This <pb/> starts page ${near.page} of the PDF, not page ${pb.n}` });
    }
  }
  return diagnostics;
}

export default class GrobidSyncExtension extends FrontendExtensionPlugin {
  /** @param {PluginContext} context */
  constructor(context) {
    super(context, { name: 'grobid-sync', deps: ['xmleditor'] });
  }

  /**
   * Cache: `<stable_id>|<pdf stable_id>` → feature tokens, or null when the file has no
   * associated feature file (non-GROBID or cache missing). Keyed by the PDF too: writing text
   * corrections page by page attaches the document to a new PDF, which changes `pendingPages`.
   * @type {Map<string, FeatureTokens|null>}
   */
  #cache = new Map();

  /** @type {string|null} */
  #currentStableId = null;

  /** @type {string|null} */
  #currentPdf = null;

  /**
   * The diagnostics last computed, with the document and feature tokens they were computed from
   * @type {{doc: Text, cached: FeatureTokens, diagnostics: Diagnostic[]}|null}
   */
  #last = null;

  /**
   * Keys (`from:to:message`) of the diagnostics the user dismissed since the last edit, which are
   * not merged in again
   * @type {Set<string>}
   */
  #dismissed = new Set();

  /** @param {object} state */
  async install(state) {
    await super.install(state);
    const xmleditor = this.getDependency('xmleditor');
    xmleditor.registerLintSource(this.#lintSource.bind(this));
    // Another source replacing the diagnostic set without ours (e.g. the well-formedness check
    // that runs after a document is loaded) removes them until the next edit: merge them in again,
    // not from inside the update. A merge that changes nothing is not dispatched.
    const lintUtils = this.getDependency('lint-utils');
    // cores before the sync prompt have no sync sources
    if (typeof lintUtils.registerSyncSource === 'function') lintUtils.registerSyncSource(SOURCE, { target: SYNC_TARGET });
    lintUtils.addDismissListener(dismissed => {
      for (const d of dismissed) if (d.source === SOURCE) this.#dismissed.add(`${d.from}:${d.to}:${d.message}`);
    });
    xmleditor.addUpdateListener(update => {
      if (update.docChanged) {
        this.#dismissed.clear();
        return;
      }
      if (!lintUtils.replacesDiagnostics(update)) return;
      if (lintUtils.collectDiagnostics(update.state).some(d => d.source === SOURCE)) return;
      setTimeout(() => this.#remergeDiagnostics(), 0);
    });
    this.#currentPdf = state.pdf ?? null;
    if (state.xml) {
      await this.onXmlChange(state.xml);
    }
  }

  /**
   * Fetch feature tokens whenever a new XML document is loaded.
   * Tokens are cached so subsequent edits to the same file skip the fetch.
   * @param {string|null} newStableId
   */
  async onXmlChange(newStableId) {
    this.#currentStableId = newStableId;
    await this.#fetchTokens();
  }

  /**
   * Fetch feature tokens again when the document is attached to another PDF (e.g. after its text
   * corrections were written page by page), which changes the pages the feature file lacks.
   * @param {string|null} newPdf
   */
  async onPdfChange(newPdf) {
    this.#currentPdf = newPdf;
    await this.#fetchTokens();
  }

  /** @returns {string} */
  #cacheKey() {
    return `${this.#currentStableId}|${this.#currentPdf ?? ''}`;
  }

  /** Fetches the feature tokens of the current document and PDF unless they are cached. */
  async #fetchTokens() {
    const stableId = this.#currentStableId;
    const key = this.#cacheKey();
    if (!stableId || this.#cache.has(key)) return;
    try {
      const result = await this.callPluginApi(
        `/api/plugins/grobid/feature-tokens?stable_id=${encodeURIComponent(stableId)}`
      );
      this.#cache.set(key, result.status === 'ok'
        ? {
          tokens: result.tokens,
          lineBased: result.line_based === true,
          pageStarts: Array.isArray(result.page_starts) ? result.page_starts : [],
          pendingPages: Array.isArray(result.pending_pages) ? result.pending_pages : []
        }
        : null);
    } catch (_) {
      this.#cache.set(key, null);
    }
    // Force the linter to re-run now that tokens are available.
    this.getDependency('xmleditor').refreshLinting();
  }

  /**
   * Merges the diagnostics of the current document into the editor's diagnostic set.
   */
  #remergeDiagnostics() {
    const view = this.getDependency('xmleditor').getView();
    if (!view) return;
    const own = this.#lintSource(view).filter(d => !this.#dismissed.has(`${d.from}:${d.to}:${d.message}`));
    this.getDependency('lint-utils').applyMergedDiagnostics(view, SOURCE, own);
  }

  /**
   * CodeMirror lint source: compares cached feature tokens against the current
   * XML document and returns diagnostics for mismatches.
   * @param {EditorView} view
   * @returns {Diagnostic[]}
   */
  #lintSource(view) {
    const cached = this.#cache.get(this.#cacheKey());
    if (!cached) return [];
    const doc = view.state.doc;
    if (this.#last && this.#last.doc === doc && this.#last.cached === cached) return this.#last.diagnostics;
    const { tokens: featureTokens, lineBased, pageStarts, pendingPages } = cached;
    const xmlText = doc.toString();
    const xmlTokens = lineBased
      ? this.#extractFirstLineTokens(xmlText)
      : this.#extractXmlTokens(xmlText);
    const found = diffTokens(featureTokens, xmlTokens, xmlText, lineBased);
    /** @type {SyncDiagnostic[]} */
    const diagnostics = lineBased
      ? withPages(found, linePages(xmlTokens.map(lineForms), featureTokens, pageStarts), new Set(pendingPages))
      : found.map(({ index: _index, ...d }) => d);
    if (lineBased && pageStarts.length > 0) {
      diagnostics.push(...pageBreakDiagnostics(xmlTokens, pageBreaksOf(xmlText, xmlTokens), featureTokens, pageStarts));
    }
    const own = diagnostics.map(d => ({ ...d, source: SOURCE }));
    this.#last = { doc, cached, diagnostics: own };
    return own;
  }

  /**
   * Extract tokens from the raw XML string, restricted to the content of the
   * `<text>` element.  Each token carries its exact character offsets in the
   * raw XML so they can be used directly as CodeMirror diagnostic positions.
   *
   * Uses a simple tag-stripping state machine instead of DOMParser so that
   * offsets remain valid even when the document is not yet well-formed.
   *
   * @param {string} xmlText
   * @returns {Array<{token: string, from: number, to: number}>}
   */
  #extractXmlTokens(xmlText) {
    // Restrict to the <text> element to exclude the <teiHeader> metadata.
    const openMatch = /<text[\s>\/]/.exec(xmlText);
    if (!openMatch) return [];
    const closeIdx = xmlText.lastIndexOf('</text>');
    const slice = closeIdx !== -1
      ? xmlText.slice(openMatch.index, closeIdx)
      : xmlText.slice(openMatch.index);
    const base = openMatch.index;

    const result = [];
    let inTag = false;
    let segStart = 0;

    for (let i = 0; i < slice.length; i++) {
      if (slice[i] === '<') {
        if (!inTag && i > segStart) {
          this.#tokenizeSegment(slice.slice(segStart, i), base + segStart, result);
        }
        inTag = true;
      } else if (slice[i] === '>') {
        inTag = false;
        segStart = i + 1;
      }
    }

    return result;
  }

  /**
   * Extract tokens from the XML restricted to the `<text>` element, yielding
   * exactly one token per `<lb/>`-delimited segment: the first *whitespace-
   * delimited* word of each segment.  Used for line-based feature files (e.g.
   * segmentation) where each feature entry represents a full PDF layout line
   * and the "token" is its first whitespace-split word — including any attached
   * punctuation such as `Author(s):` or `854-875`.
   *
   * @param {string} xmlText
   * @returns {Array<{token: string, from: number, to: number}>}
   */
  #extractFirstLineTokens(xmlText) {
    const openMatch = /<text[\s>\/]/.exec(xmlText);
    if (!openMatch) return [];
    const closeIdx = xmlText.lastIndexOf('</text>');
    const slice = closeIdx !== -1
      ? xmlText.slice(openMatch.index, closeIdx)
      : xmlText.slice(openMatch.index);
    const base = openMatch.index;

    const result = [];
    let lineSegs = [];
    let inTag = false;
    let tagStart = -1;
    let segStart = 0;

    for (let i = 0; i < slice.length; i++) {
      if (slice[i] === '<') {
        if (!inTag && i > segStart) {
          lineSegs.push({ seg: slice.slice(segStart, i), absStart: base + segStart });
        }
        inTag = true;
        tagStart = i;
      } else if (slice[i] === '>') {
        if (inTag && tagStart >= 0 && /^<lb[\s\/]|^<lb>/.test(slice.slice(tagStart, i + 1))) {
          const token = this.#firstWordOfLine(lineSegs);
          if (token) result.push(token);
          lineSegs = [];
        }
        inTag = false;
        segStart = i + 1;
        tagStart = -1;
      }
    }

    return result;
  }

  /**
   * Find the first whitespace-delimited word across a sequence of raw XML text
   * segments, decoding entity references so the result matches the feature file
   * token (which is also decoded).
   *
   * @param {{seg: string, absStart: number}[]} segs
   * @returns {LineToken|null}
   */
  #firstWordOfLine(segs) {
    const decoded = [];
    const rawSpans = [];
    for (const { seg, absStart } of segs) {
      ENTITY_RE.lastIndex = 0;
      let plainAt = 0;
      let em;
      while ((em = ENTITY_RE.exec(seg)) !== null) {
        for (let i = plainAt; i < em.index; i++) {
          decoded.push(seg[i]);
          rawSpans.push([absStart + i, absStart + i + 1]);
        }
        const rawFrom = absStart + em.index;
        const rawTo = absStart + em.index + em[0].length;
        const decodedStr = em[1] ? String.fromCodePoint(parseInt(em[1], 10))
          : em[2] ? String.fromCodePoint(parseInt(em[2], 16))
          : (NAMED_ENTITIES[em[3]] ?? em[0]);
        for (const c of decodedStr) {
          decoded.push(c);
          rawSpans.push([rawFrom, rawTo]);
        }
        plainAt = em.index + em[0].length;
      }
      for (let i = plainAt; i < seg.length; i++) {
        decoded.push(seg[i]);
        rawSpans.push([absStart + i, absStart + i + 1]);
      }
    }
    const text = decoded.join('');
    const m = /\S+/.exec(text);
    if (!m) return null;
    /** @type {LineToken} */
    const result = {
      token: foldDashes(m[0]),
      from: rawSpans[m.index][0],
      to: rawSpans[m.index + m[0].length - 1][1],
    };
    const joined = joinedFirstWord(text);
    if (joined !== null) result.joined = joined;
    return result;
  }

  /**
   * Tokenize a text segment (between XML tags), decoding entity references so
   * that `&amp;` produces one token `&` and `&lt;www` produces one token `<www`.
   *
   * Builds a decoded string with a parallel raw-position table, then applies
   * TOKEN_RE to the decoded string so that entity chars merge correctly with
   * adjacent plain-text chars before token boundaries are computed.
   *
   * @param {string} seg - Raw XML text segment (may contain entity refs)
   * @param {number} absStart - Absolute offset of `seg[0]` in the full XML string
   * @param {Array<{token: string, from: number, to: number}>} out - Accumulator
   */
  #tokenizeSegment(seg, absStart, out) {
    // Build decoded text and a parallel table of raw XML spans.
    // rawSpans[i] = [rawFrom, rawTo] for the i-th decoded character.
    const decoded = [];
    const rawSpans = [];

    ENTITY_RE.lastIndex = 0;
    let plainAt = 0;
    let em;
    while ((em = ENTITY_RE.exec(seg)) !== null) {
      // Plain chars before this entity
      for (let i = plainAt; i < em.index; i++) {
        decoded.push(seg[i]);
        rawSpans.push([absStart + i, absStart + i + 1]);
      }
      // Decode entity; all its chars map to the full &xxx; span in the raw XML
      const rawFrom = absStart + em.index;
      const rawTo = absStart + em.index + em[0].length;
      const decodedStr = em[1] ? String.fromCodePoint(parseInt(em[1], 10))
        : em[2] ? String.fromCodePoint(parseInt(em[2], 16))
        : (NAMED_ENTITIES[em[3]] ?? em[0]);
      for (const ch of decodedStr) {
        decoded.push(ch);
        rawSpans.push([rawFrom, rawTo]);
      }
      plainAt = em.index + em[0].length;
    }
    // Remaining plain chars
    for (let i = plainAt; i < seg.length; i++) {
      decoded.push(seg[i]);
      rawSpans.push([absStart + i, absStart + i + 1]);
    }

    const decodedText = foldDashes(decoded.join(''));
    TOKEN_RE.lastIndex = 0;
    let m;
    while ((m = TOKEN_RE.exec(decodedText)) !== null) {
      const from = rawSpans[m.index][0];
      const to = rawSpans[m.index + m[0].length - 1][1];
      out.push({ token: m[0], from, to });
    }
  }
}
