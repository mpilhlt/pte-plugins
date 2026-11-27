/**
 * Annotation Review frontend extension.
 *
 * Adds a "Review document annotations" item to the Tools menu (annotation category),
 * enabled only when the open document has machine-readable rules. Running it
 * calls the backend review endpoint and shows the findings as diagnostics
 * merged into the editor's diagnostic set, each with a "Propose fix" action
 * that opens the merge view and a `replacement` that the diagnostics drawer
 * applies directly. When node navigation is on (the `xpath` state is
 * set), the "Annotation auto-review" button of focus mode reviews only the navigated-to node (for a
 * milestone such as `<pb/>`, the text up to the next one, e.g. a page); the
 * menu item always reviews the whole document. While the editor's unit review mode is
 * on, the model also records its result as a reviewer: the review units of each chunk
 * are marked OK, or not OK with the rationale of the findings in them. Zone documents
 * (such as GROBID's segmentation training data) are reviewed in page windows: the
 * server shows the model the pages as numbered lines with zone labels and turns its
 * label changes into findings of the same form, and every page of a window is marked.
 * Without the document's page layout, the review of zone documents is disabled. See
 * docs/superpowers/specs/2026-09-24-llm-annotation-review-design.md
 * for the design rationale.
 *
 * The display of the findings (diagnostics, session cache, pruning) is the editor's shared
 * `FindingsDisplay` of `app/src/modules/llm-findings.js`, reached via `getDependency('llm-findings')`.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 * @import { Finding, FindingsDisplay } from '../../../../app/src/modules/llm-findings.js'
 */

/** Diagnostic source tag identifying this plugin's diagnostics. */
const SOURCE = 'annotation-review';

/** Maximum length of the comment of a unit marked not OK by the LLM. */
const UNIT_COMMENT_MAX = 200;

/**
 * @typedef {{fingerprint: string, from: number, to: number, name: string}} ReviewUnit
 * @typedef {{fingerprint: string, state: 'ok'|'not-ok', comment: string}} UnitMark
 */

/**
 * Unit review marks for one reviewed chunk: each review unit whose text lies in the chunk is
 * not OK if a finding falls into it (with the findings' rationales as comment), OK otherwise.
 * Pages (units of zone documents, which are text ranges) are located by the chunk's range
 * instead of by its text.
 * @param {(finding: Finding, docText: string) => {from: number, to: number}|null} locateFinding -
 *   `locateFinding` of the llm-findings module
 * @param {ReviewUnit[]} units - Review units with positions in docText
 * @param {string} docText - Current editor text
 * @param {string} chunkText - Text of the reviewed chunk
 * @param {Finding[]} findings - Findings of the chunk
 * @param {{start: number, end: number}} [chunkRange] - Position of the chunk in docText
 * @returns {UnitMark[]}
 */
export function unitMarksForChunk(locateFinding, units, docText, chunkText, findings, chunkRange) {
  const ranges = findings
    .map(finding => ({ finding, range: locateFinding(finding, docText) }))
    .filter(entry => entry.range !== null);
  /** @type {UnitMark[]} */
  const marks = [];
  for (const unit of units) {
    if (unit.name === 'page' && chunkRange) {
      if (unit.from < chunkRange.start || unit.to > chunkRange.end) continue;
    } else if (!chunkText.includes(docText.slice(unit.from, unit.to))) continue;
    const rationales = ranges
      .filter(({ range }) => range.from < unit.to && range.to > unit.from)
      .map(({ finding }) => finding.rationale);
    let comment = rationales.join('; ');
    if (comment.length > UNIT_COMMENT_MAX) comment = comment.slice(0, UNIT_COMMENT_MAX - 1) + '…';
    marks.push({ fingerprint: unit.fingerprint, state: rationales.length ? 'not-ok' : 'ok', comment });
  }
  return marks;
}

/**
 * Escape text for use in the dialog's HTML message.
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export default class AnnotationReviewExtension extends FrontendExtensionPlugin {
  /**
   * @param {PluginContext} context
   */
  constructor(context) {
    super(context, { name: 'annotation-review', deps: ['xmleditor', 'tools'] });
    /** @type {FindingsDisplay|undefined} Created on first use, see _display. */
    this._findingsDisplay = undefined;
    /** @type {HTMLElement|undefined} The Tools-menu item, created in start(). */
    this._menuItem = undefined;
    /**
     * @type {string|null} Why the open document cannot be reviewed: a zone document without
     *   its page layout; null otherwise.
     */
    this._layoutMissing = null;
    /** Button shown on the focused node in the XML editor's focus mode. */
    this._focusAction = {
      label: 'Annotation auto-review',
      title: 'Review the annotations of this node',
      run: () => { this.runReview(); },
    };
    /** The focus-mode button while a review runs (disabled with editor cores that support it). */
    this._focusActionBusy = { ...this._focusAction, disabled: true, title: 'A review is running' };
    /** True while a review runs. */
    this._running = false;
  }

  /**
   * The display of the findings of the last review (created on first use).
   * @returns {FindingsDisplay}
   */
  get _display() {
    if (!this._findingsDisplay) {
      const { FindingsDisplay } = this.getDependency('llm-findings');
      const { SessionStorage } = this.getDependency('session-storage');
      this._findingsDisplay = new FindingsDisplay({
        source: SOURCE,
        getXmleditor: () => this.getDependency('xmleditor'),
        getLintUtils: () => this.getDependency('lint-utils'),
        sessionStorage: new SessionStorage(SOURCE),
        proposeFix: (finding, from, to) => this._proposeFix(finding, from, to),
        getDocId: () => this.state?.xml || null,
      });
    }
    return this._findingsDisplay;
  }

  /**
   * Add the Tools-menu item, attach the findings display (re-merge when another source replaces
   * the diagnostics, prune after edits, session cache) and restore the cached findings.
   * @returns {Promise<void>}
   */
  async start() {
    const item = document.createElement('sl-menu-item');
    item.textContent = 'Review document annotations';
    item.disabled = true;
    item.addEventListener('click', () => this.runReview(undefined, { wholeDocument: true }));
    this._menuItem = item;
    this.getDependency('tools').addMenuItems([item], 'annotation');
    const xmleditor = this.getDependency('xmleditor');
    xmleditor.on('editorReady', () => this._updateMenuState());
    this._updateMenuState();

    // services.load() loads the editor content before it sets `state.xml`, so the restore
    // usually happens in onXmlChange(); attach() also restores on editorAfterLoad
    this._display.attach();
    this._display.restore(this.state?.xml || null);
    this._checkLayout();
  }

  /**
   * Store findings and display them as diagnostics.
   * @param {Finding[]} findings
   * @param {boolean} [openPanel] Expand the diagnostics drawer
   * @returns {void}
   */
  showFindings(findings, openPanel = true) {
    this._display.show(findings, openPanel);
  }

  /**
   * Remove all findings and their diagnostics.
   * @returns {void}
   */
  clearFindings() {
    this._display.clear();
  }

  /**
   * Open the merge view showing only the given finding's change.
   * @param {Finding} finding
   * @param {number} from
   * @param {number} to
   * @returns {Promise<void>}
   */
  async _proposeFix(finding, from, to) {
    const xmleditor = this.getDependency('xmleditor');
    const { buildModifiedText } = this.getDependency('llm-findings');
    const modified = buildModifiedText(xmleditor.getView().state.doc.toString(), from, to, finding);
    if (modified === null) {
      this.getDependency('sl-utils').notify(
        'The document changed; this finding no longer applies.',
        'warning',
        'exclamation-triangle'
      );
      return;
    }
    try {
      await xmleditor.showMergeView(modified);
    } catch (err) {
      this.getDependency('sl-utils').notify(
        `Could not show the proposed fix: ${err.message}`,
        'danger',
        'exclamation-octagon'
      );
    }
  }

  /**
   * A different document is being loaded; drop the findings of the previous
   * one from the editor but keep them in the session cache, so that they are
   * restored when it is loaded again (see `FindingsDisplay.switchDocument()`).
   * @param {string} [_doc] New document identifier
   * @returns {Promise<void>}
   */
  async onXmlChange(_doc) {
    this._display.switchDocument(this.state?.xml || null);
    this._layoutMissing = null;
    this._updateMenuState();
    await this._checkLayout();
  }

  /**
   * Find out whether the open document is a zone document without page layout, which cannot
   * be reviewed (`GET /api/v1/files/{stable_id}/layout`), and update the menu item.
   * @returns {Promise<void>}
   */
  async _checkLayout() {
    const stableId = this.state?.xml || null;
    let missing = null;
    let apiClient = null;
    try {
      apiClient = stableId ? this.getDependency('client').apiClient : null;
    } catch {
      // no API client (e.g. in tests)
    }
    if (apiClient && typeof apiClient.filesGetLayout === 'function') {
      try {
        const layout = await apiClient.filesGetLayout(stableId);
        if (layout.status === 'missing') missing = layout.message || 'The layout information of this document is missing.';
      } catch {
        // the review reports the problem
      }
    }
    if (this.state?.xml !== stableId) return;
    this._layoutMissing = missing;
    this._updateMenuState();
  }

  /**
   * Enable the menu item, and the "Annotation auto-review" button on the node focused in
   * focus mode, only if a document is open and has reviewable rules; while a review runs, both
   * are disabled.
   * No-op before start() has created the item.
   * @returns {void}
   */
  _updateMenuState() {
    if (!this._menuItem) return;
    const tree = this.getDependency('xmleditor').getXmlTree();
    const available = Boolean(tree) && this.hasReviewableRules(tree) && this._layoutMissing === null;
    this._menuItem.disabled = !available || this._running;
    this._menuItem.title = this._layoutMissing ?? (available ? '' : 'Requires an open document whose editorialDecl has machine-readable annotation rules (a ref with subtype="machine").');
    this.getDependency('xmleditor').setFocusAction(SOURCE, available ? (this._running ? this._focusActionBusy : this._focusAction) : null);
  }

  /**
   * Ask for confirmation naming the provider/model (not when only the node
   * selected in node navigation is reviewed), then review the document
   * chunk by chunk behind a minimizable, cancellable progress widget. Findings
   * are displayed as each chunk completes; cancelling keeps those found so
   * far. review() notifies on its own failure paths (including "no default
   * model"), so a null result is silent.
   * @param {{providerId: string, modelId: string}} [override]
   * @param {{wholeDocument?: boolean}} [options] wholeDocument reviews the whole
   *   document even while node navigation has selected a node (the Tools menu item)
   * @returns {Promise<void>}
   */
  async runReview(override, options = {}) {
    if (this._running) return;
    const resolved = override ?? this.getDependency('inference-settings').getDefaultModel();
    const xpath = options.wholeDocument ? null : (this.state?.xpath || null);
    let label = '';
    if (resolved) {
      label = this.getDependency('inference-settings').getModelLabel(resolved.providerId, resolved.modelId);
    }
    if (resolved && !xpath) {
      const confirmed = await this.getDependency('dialog').confirm(
        `Review the annotations in the current document using ${escapeHtml(label)}?`,
        'Review document annotations'
      );
      if (!confirmed) return;
    }
    const progress = this.getDependency('progress');
    const progressId = 'annotation-review';
    const what = xpath ? 'annotations in the selected node' : 'annotations';
    const heading = label ? `Reviewing ${what} using ${label}…` : `Reviewing ${what}…`;
    const xmlBefore = this.state?.xml;
    const documentChanged = () => this.state?.xml !== xmlBefore;
    let cancelled = false;
    let done = 0;
    let total = 0;
    let discarded = 0;
    // findings of the review received so far, of which the display holds those not dismissed or applied
    let received = 0;
    let appended = 0;
    this._running = true;
    this._updateMenuState();
    progress.show(progressId, { title: 'Annotation auto-review', label: heading, value: null, cancellable: true, onCancel: () => { cancelled = true; } });
    try {
      const findings = await this.review(resolved ?? undefined, {
        xpath,
        onStart: (chunksTotal) => {
          total = chunksTotal;
          progress.setValue(progressId, 0);
          progress.setLabel(progressId, `${heading} (part 1 of ${chunksTotal})`);
        },
        isCancelled: () => cancelled || documentChanged(),
        onDiscarded: (count) => { discarded += count; },
        onProgress: (chunksDone, chunksTotal, foundSoFar) => {
          done = chunksDone;
          total = chunksTotal;
          progress.setValue(progressId, Math.round((chunksDone / chunksTotal) * 100));
          progress.setLabel(progressId, `${heading} (part ${Math.min(chunksDone + 1, chunksTotal)} of ${chunksTotal})`);
          // findings from a different document must not attach to another one
          if (documentChanged()) return;
          // the findings of the previous review are replaced by those of this one
          if (chunksDone === 1) this._display.clear();
          // appended, so that findings dismissed or applied meanwhile do not come back; the drawer
          // opens for the first findings only
          const added = foundSoFar.slice(received);
          received = foundSoFar.length;
          if (added.length) appended += this._display.append(added, appended === 0);
        },
      });
      if (findings === null || documentChanged()) return;
      const count = findings.length;
      if (cancelled) {
        this.getDependency('sl-utils').notify(
          `Review cancelled after ${done} of ${total} parts; ${count} suggestion(s) so far.`,
          'warning',
          'exclamation-triangle'
        );
        return;
      }
      if (!count && discarded) {
        this.getDependency('sl-utils').notify(
          `No usable suggestions: the model proposed ${discarded} fix(es), but their quoted text does not match the document.`,
          'warning',
          'exclamation-triangle'
        );
        return;
      }
      this.getDependency('sl-utils').notify(
        count ? `${count} suggestion(s) - see the highlighted passages.` : 'No issues found.',
        count ? 'primary' : 'success',
        count ? 'info-circle' : 'check-circle'
      );
    } finally {
      this._running = false;
      this._updateMenuState();
      progress.hide(progressId);
    }
  }

  /**
   * True if the given document has at least one editorialDecl category with
   * a "machine"-subtype ref — i.e. there is something to review against.
   * @param {Document} xmlDoc
   * @returns {boolean}
   */
  hasReviewableRules(xmlDoc) {
    const guides = this.getDependency('tei-utils').getEditorialDeclGuides(xmlDoc);
    return guides.some(g => g.refs.some(r => r.subtype === 'machine'));
  }

  /**
   * Return the raw XML of the node the given navigation xpath points to, as it appears in the
   * given editor text, or null (user notified) if it cannot be resolved. For a milestone such as
   * `<pb/>`, this is the text up to the next one: the page.
   * @param {string} xpath
   * @param {string} docText
   * @returns {string|null}
   */
  _nodeText(xpath, docText) {
    try {
      const { from, to } = this.getDependency('xmleditor').getRangeByXpath(xpath);
      return docText.slice(from, to);
    } catch (err) {
      this.getDependency('sl-utils').notify(
        `Cannot review the selected node: ${err.message}`,
        'warning',
        'exclamation-triangle'
      );
      return null;
    }
  }

  /**
   * Records the review of a chunk in the editor's unit review mode (if it is on): the review units
   * of the chunk are marked OK or not OK by the model, as a reviewer of its own. Editors without
   * unit review, and responses without the chunk position, are ignored.
   * @param {string} xml - The document text sent for review
   * @param {{findings: Finding[], chunk_start?: number|null, chunk_end?: number|null}} response
   * @param {{providerId: string, modelId: string}} model
   * @returns {Promise<void>}
   */
  async _recordUnitReview(xml, response, model) {
    if (response.chunk_start == null || response.chunk_end == null) return;
    let unitReview;
    try {
      unitReview = this.getDependency('unit-review');
    } catch {
      return;
    }
    if (!unitReview?.isEnabled?.()) return;
    const docText = this.getDependency('xmleditor').getView().state.doc.toString();
    const chunkText = xml.slice(response.chunk_start, response.chunk_end);
    const chunkRange = docText === xml ? { start: response.chunk_start, end: response.chunk_end } : undefined;
    const { locateFinding } = this.getDependency('llm-findings');
    const marks = unitMarksForChunk(locateFinding, unitReview.getUnits(), docText, chunkText, response.findings, chunkRange);
    if (!marks.length) return;
    let name = model.modelId;
    try {
      name = this.getDependency('inference-settings').getModelLabel(model.providerId, model.modelId) || name;
    } catch {
      // keep the model id
    }
    const id = `llm-${model.providerId}-${model.modelId}`.replace(/[^\w.-]/g, '-');
    await unitReview.setMarks({ id, name }, marks);
  }

  /**
   * Review the current editor document's annotations against its own
   * editorialDecl rules, one backend chunk at a time. Resolves provider/model
   * from the given override, or from the shared default (Part F), unless one
   * isn't configured.
   * @param {{providerId: string, modelId: string}} [override]
   * @param {{xpath?: string|null, isCancelled?: () => boolean, onStart?: (total: number) => void, onProgress?: (done: number, total: number, findings: Finding[]) => void, onDiscarded?: (count: number) => void}} [hooks]
   *   xpath limits the review to that node (the navigation mode's `xpath` state); onStart runs once the chunk count is known; isCancelled is polled before each further chunk; onProgress runs after each chunk with all findings so far; onDiscarded runs for each chunk in which the backend discarded findings whose quoted text does not occur exactly once in the document.
   * @returns {Promise<Finding[]|null>}
   *   The findings (possibly partial if cancelled); null when the review could not be started or a chunk failed (user already notified).
   */
  async review(override, hooks = {}) {
    const xmleditorApi = this.getDependency('xmleditor');
    const xmlDoc = xmleditorApi.getXmlTree();
    if (!xmlDoc) {
      this.getDependency('sl-utils').notify('No document open.', 'warning', 'exclamation-triangle');
      return null;
    }

    if (!this.hasReviewableRules(xmlDoc)) {
      this.getDependency('sl-utils').notify(
        'This document has no machine-readable annotation rules to review against.',
        'warning',
        'exclamation-triangle'
      );
      return null;
    }

    if (this._layoutMissing) {
      this.getDependency('sl-utils').notify(this._layoutMissing, 'warning', 'exclamation-triangle');
      return null;
    }

    const resolved = override ?? this.getDependency('inference-settings').getDefaultModel();
    if (!resolved) {
      this.getDependency('sl-utils').notify(
        'No default model configured. Set one via Tools → Inference → Default Model.',
        'warning',
        'exclamation-triangle'
      );
      return null;
    }

    let xml = xmleditorApi.getEditorContent();
    /** @type {{scope?: string, stable_id?: string}} */
    const scoped = {};
    // zone documents are reviewed by page, which needs the layout of the saved document
    if (this.state?.xml) scoped.stable_id = this.state.xml;
    if (hooks.xpath) {
      // positions of the syntax tree refer to the editor text, so slice and send that
      xml = xmleditorApi.getView().state.doc.toString();
      const scope = this._nodeText(hooks.xpath, xml);
      if (scope === null) return null;
      scoped.scope = scope;
    }
    /** @type {Finding[]} */
    const findings = [];
    let total;
    try {
      ({ chunk_count: total } = await this.callPluginApi('/api/plugins/annotation-review/plan', 'POST', { xml, ...scoped }));
    } catch (err) {
      this.getDependency('sl-utils').notify(`Annotation review failed: ${err.message}`, 'danger', 'exclamation-octagon');
      return null;
    }
    hooks.onStart?.(total);
    for (let index = 0; index < total; index++) {
      if (index > 0 && hooks.isCancelled?.()) break;
      try {
        const response = await this.callPluginApi(
          '/api/plugins/annotation-review/review',
          'POST',
          { xml, ...scoped, provider_id: resolved.providerId, model_id: resolved.modelId, chunk_index: index }
        );
        total = response.chunk_count ?? total;
        for (const finding of response.findings) findings.push({ ...finding, id: findings.length });
        if (response.discarded) hooks.onDiscarded?.(response.discarded);
        await this._recordUnitReview(xml, response, resolved);
      } catch (err) {
        this.getDependency('sl-utils').notify(
          `Annotation review failed${total > 1 ? ` in part ${index + 1} of ${total}` : ''}: ${err.message}`,
          'danger',
          'exclamation-octagon'
        );
        return null;
      }
      hooks.onProgress?.(index + 1, total, [...findings]);
    }
    return findings;
  }
}
