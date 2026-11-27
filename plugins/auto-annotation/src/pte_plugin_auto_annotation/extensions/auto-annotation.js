/**
 * Auto-annotation frontend extension.
 *
 * Adds "Correct annotations in new version" to the Tools menu (annotation category): the model
 * selected in Tools > Inference corrects the review units of the open document against the
 * document's machine rules, and the result is saved as a new version of it. The work is done one
 * batch of units per request behind a cancellable progress widget. Meanwhile the document is
 * locked for the job on the server, so it opens read-only everywhere. When the new version is
 * saved, it is loaded if the source document is still open; otherwise the user is asked whether to
 * load it. Documents reviewed by page (zone documents such as segmentation documents) are corrected
 * page by page; without their layout, the menu item is disabled with the reason. Routes:
 * correction_routes.py.
 *
 * Also adds "Transfer annotations from another version…": a dialog asks for another version of the
 * same document and variant (e.g. the gold version from an older text layer) and whether the model
 * should mark the units where it doubts the earlier decisions. The server aligns the texts; units
 * with the same text are copied, the others are sent to the model with the corresponding part of
 * the earlier version (transfer.py). The doubts are recorded as not-OK marks of the model in the
 * new version, which is then always loaded.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 */

/** Seconds between refreshes of the job's lock while a batch is under way (locks expire after 90 s). */
const LOCK_REFRESH_SECONDS = 30;

/** Roles that may create versions. */
const ALLOWED_ROLES = ['annotator', 'reviewer', 'admin'];

/** Statuses of units whose answer failed the checks (see checks.py). */
const INVALID_STATUSES = new Set(['missing', 'malformed', 'wrong_root', 'text_changed', 'bad_labels']);

/**
 * @typedef {{index: number, status: string, xml: string|null}} UnitResult
 * @typedef {{units: UnitResult[], error: string|null, calls: number, retries: number, seconds: number}} BatchResult
 * @typedef {{index: number, status: string, xml: string|null, doubt?: string|null}} TransferUnitResult
 * @typedef {{copied: number, model: number, unmatched: number, coverage: number}} TransferCounts
 * @typedef {{job_id: string, doc_id: string, variant: string|null, unit_count: number, batches: number[][], unit_name?: string, transfer?: TransferCounts|null, doubt_batches?: number[][]}} StartResult
 * @typedef {{id: string, label?: string, variant?: string, is_gold_standard?: boolean, status?: string, updated_at?: string}} ArtifactItem
 * @typedef {{artifacts?: ArtifactItem[], doc_metadata?: {title?: string}}} DocumentEntry
 * @typedef {{id: string, label: string}} SourceChoice
 * @typedef {{sourceStableId: string, sourceLabel: string, marks: boolean}} TransferOptions
 */

/**
 * The outcome of a correction run.
 * @typedef {object} CorrectionOutcome
 * @property {Map<number, string>} corrections - Corrected unit XML (for pages: label changes in the line form) by unit index
 * @property {number} processed - Units sent to the model
 * @property {number} invalid - Units whose answer failed the checks
 * @property {number} errors - Units of failed requests
 * @property {number} seconds - Duration of the run
 * @property {Map<number, string>} doubts - Transfer with marks: the model's doubts by unit index
 * @property {'done'|'cancelled'|'failed'} state
 * @property {string|null} error - Why the run failed
 */

/**
 * Send the batches to the server one after the other and collect the corrections. Stops before the
 * next batch if `isCancelled()` returns true, and at the first batch whose request failed.
 * @param {(units: number[]) => Promise<BatchResult>} correctBatch - Corrects the units of one batch
 * @param {number[][]} batches - Unit indices of each batch
 * @param {{isCancelled?: () => boolean, onProgress?: (processed: number, total: number, changed: number) => void, now?: () => number}} [hooks]
 * @returns {Promise<CorrectionOutcome>}
 */
export const runCorrection = async (correctBatch, batches, hooks = {}) => {
  const now = hooks.now ?? (() => Date.now());
  const started = now();
  const total = batches.reduce((sum, batch) => sum + batch.length, 0);
  /** @type {CorrectionOutcome} */
  const outcome = { corrections: new Map(), processed: 0, invalid: 0, errors: 0, seconds: 0, doubts: new Map(), state: 'done', error: null };
  for (const batch of batches) {
    if (hooks.isCancelled?.()) {
      outcome.state = 'cancelled';
      break;
    }
    let result;
    try {
      result = await correctBatch(batch);
    } catch (err) {
      outcome.state = 'failed';
      outcome.error = err instanceof Error ? err.message : String(err);
      break;
    }
    for (const unit of result.units) {
      if (unit.xml !== null) outcome.corrections.set(unit.index, unit.xml);
      const doubt = /** @type {TransferUnitResult} */ (unit).doubt;
      if (doubt) outcome.doubts.set(unit.index, doubt);
      if (INVALID_STATUSES.has(unit.status)) outcome.invalid++;
      if (unit.status === 'error') outcome.errors++;
    }
    outcome.processed += batch.length;
    hooks.onProgress?.(outcome.processed, total, outcome.corrections.size);
    if (result.error) {
      outcome.state = 'failed';
      outcome.error = result.error;
      break;
    }
  }
  outcome.seconds = Math.round((now() - started) / 100) / 10;
  return outcome;
};

/**
 * The versions whose annotation can be transferred to a version: the other versions of the same
 * document and variant.
 * @param {DocumentEntry[]} fileData
 * @param {string} stableId - The version to transfer to
 * @returns {SourceChoice[]}
 */
export function transferSources(fileData, stableId) {
  const doc = (fileData ?? []).find(d => d.artifacts?.some(a => a.id === stableId));
  const target = doc?.artifacts?.find(a => a.id === stableId);
  if (!doc || !target) return [];
  return (doc.artifacts ?? [])
    .filter(a => a.id !== stableId && a.variant === target.variant)
    .map(a => ({ id: a.id, label: `${a.label || a.id}${a.is_gold_standard ? ' (gold)' : ''}` }));
}

/**
 * The version preselected as the source of a transfer: the gold version, else the most recently
 * changed version that is not an extraction, else the first.
 * @param {DocumentEntry[]} fileData
 * @param {string} stableId - The version to transfer to
 * @returns {string|null}
 */
export function preferredSource(fileData, stableId) {
  const ids = new Set(transferSources(fileData, stableId).map(c => c.id));
  const artifacts = (fileData ?? []).flatMap(d => d.artifacts ?? []).filter(a => ids.has(a.id));
  const gold = artifacts.find(a => a.is_gold_standard);
  if (gold) return gold.id;
  const annotated = artifacts.filter(a => a.status !== 'extraction').sort((a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')));
  return annotated[0]?.id ?? artifacts[0]?.id ?? null;
}

/**
 * Escape text for use in a dialog's HTML message.
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export default class AutoAnnotationExtension extends FrontendExtensionPlugin {
  /**
   * @param {PluginContext} context
   */
  constructor(context) {
    super(context, { name: 'auto-annotation', deps: ['xmleditor', 'tools'] });
    /** @type {HTMLElement|undefined} The Tools-menu item, created in start(). */
    this._menuItem = undefined;
    /** @type {HTMLElement|undefined} The Tools-menu item of the transfer, created in start(). */
    this._transferItem = undefined;
    /** @type {Set<string>} Stable ids of the documents being corrected in this tab. */
    this._running = new Set();
    /** @type {number} */
    this._lockRefreshMs = LOCK_REFRESH_SECONDS * 1000;
    /** @type {string|null} Why the open document cannot be corrected: its page layout is missing */
    this._layoutMissing = null;
  }

  /**
   * Add the Tools-menu item.
   * @returns {Promise<void>}
   */
  async start() {
    const item = document.createElement('sl-menu-item');
    item.textContent = 'Correct annotations in new version';
    item.disabled = true;
    item.addEventListener('click', () => this.runCorrection());
    this._menuItem = item;
    const transfer = document.createElement('sl-menu-item');
    transfer.textContent = 'Transfer annotations from another version…';
    transfer.disabled = true;
    transfer.addEventListener('click', () => this.runTransfer());
    this._transferItem = transfer;
    this.getDependency('tools').addMenuItems([item, transfer], 'annotation');
    this.getDependency('xmleditor').on('editorReady', () => this._updateMenuState());
    this._updateMenuState();
    await this._checkLayout();
  }

  /**
   * @param {string} [_doc]
   * @returns {Promise<void>}
   */
  async onXmlChange(_doc) {
    this._layoutMissing = null;
    this._updateMenuState();
    await this._checkLayout();
  }

  /**
   * Find out whether the open document is a zone document without page layout, which cannot be
   * corrected (`GET /api/v1/files/{stable_id}/layout`), and update the menu item.
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
        // the correction reports the problem
      }
    }
    if ((this.state?.xml || null) !== stableId) return;
    this._layoutMissing = missing;
    this._updateMenuState();
  }

  /**
   * @returns {Promise<void>}
   */
  async onUserChange() {
    this._updateMenuState();
  }

  /**
   * Why the open document cannot be corrected now, or '' if it can.
   * @returns {string}
   */
  _disabledReason() {
    const user = this.state?.user;
    if (!user || !this.getDependency('acl-utils').userHasRole(user, ALLOWED_ROLES)) {
      return 'Requires the annotator or reviewer role.';
    }
    const xml = this.state?.xml;
    const tree = this.getDependency('xmleditor').getXmlTree();
    if (!xml || !tree) return 'Requires an open document.';
    if (this._running.has(xml)) return 'The annotations of this document are being corrected.';
    if (this._layoutMissing) return this._layoutMissing;
    const guides = this.getDependency('tei-utils').getEditorialDeclGuides(tree);
    if (!guides.some(g => g.refs.some(r => r.subtype === 'machine'))) {
      return 'Requires a document whose editorialDecl has machine-readable annotation rules (a ref with subtype="machine").';
    }
    return '';
  }

  /**
   * Enable the menu item only if the open document can be corrected; the tooltip says why not.
   * @returns {void}
   */
  _updateMenuState() {
    if (!this._menuItem) return;
    const reason = this._disabledReason();
    this._menuItem.disabled = reason !== '';
    this._menuItem.title = reason;
    if (!this._transferItem) return;
    const transferReason = reason || this._transferDisabledReason();
    this._transferItem.disabled = transferReason !== '';
    this._transferItem.title = transferReason;
  }

  /**
   * Why the annotation of another version cannot be transferred to the open document, beyond the
   * reasons of _disabledReason(); '' if it can.
   * @returns {string}
   */
  _transferDisabledReason() {
    const xml = this.state?.xml ?? '';
    const fileData = this.state?.fileData ?? [];
    const artifact = fileData.flatMap(d => d.artifacts ?? []).find(a => a.id === xml);
    if (artifact?.is_gold_standard) return 'The annotation cannot be transferred to a gold version; open the version to transfer it to.';
    if (!transferSources(fileData, xml).length) return 'Requires another version of the same document and variant to take the annotation from.';
    return '';
  }

  /**
   * The title of the document a version belongs to, or '' if unknown.
   * @param {string} stableId
   * @returns {string}
   */
  _documentTitle(stableId) {
    const doc = (this.state?.fileData ?? []).find(d => d.artifacts?.some(a => a.id === stableId));
    return doc?.doc_metadata?.title || '';
  }

  /**
   * Ask for the version to take the annotation from, then transfer it to the open document (see
   * the file comment).
   * @returns {Promise<void>}
   */
  async runTransfer() {
    const notify = this.getDependency('sl-utils').notify;
    const targetId = this.state?.xml;
    if (!targetId || this._running.has(targetId)) return;
    const model = this.getDependency('inference-settings').getDefaultModel();
    if (!model) {
      notify('No model selected. Choose one via Tools → Inference.', 'warning', 'exclamation-triangle');
      return;
    }
    const label = this.getDependency('inference-settings').getModelLabel(model.providerId, model.modelId) || `${model.providerId}/${model.modelId}`;
    const fileData = this.state?.fileData ?? [];
    const options = await this.openTransferForm(targetId, transferSources(fileData, targetId), preferredSource(fileData, targetId), label);
    if (!options || this.state?.xml !== targetId) return;
    await this.runCorrection(options);
  }

  /**
   * The counts of a transfer, for the dialog.
   * @param {string} targetId
   * @param {string} sourceId
   * @returns {Promise<{unit_count: number, unit_name: string, transfer: TransferCounts}>}
   */
  async _transferPreview(targetId, sourceId) {
    const query = new URLSearchParams({ target: targetId, source: sourceId });
    return /** @type {any} */ (await this.callPluginApi(`/api/plugins/auto-annotation/correct/transfer-preview?${query}`, 'GET'));
  }

  /**
   * Ask for the version to take the annotation from and whether to mark doubts.
   * @param {string} targetId - The open version
   * @param {SourceChoice[]} sources
   * @param {string|null} preferred - The preselected source
   * @param {string} modelLabel
   * @returns {Promise<TransferOptions|null>} null if cancelled
   */
  openTransferForm(targetId, sources, preferred, modelLabel) {
    const selected = Math.max(0, sources.findIndex(c => c.id === preferred));
    const form = document.createElement('sl-dialog');
    form.label = 'Transfer annotations from another version';
    form.dataset.testId = 'auto-annotation-transfer-dialog';
    form.style.setProperty('--width', '34rem');
    form.innerHTML = `
      <style>
        .tr-form { display: flex; flex-direction: column; gap: 0.9rem; }
        .tr-note, .tr-preview, .tr-error { font-size: var(--sl-font-size-small); }
        .tr-error { color: var(--sl-color-danger-600); }
      </style>
      <div class="tr-form">
        <sl-select name="source" label="Take the annotation from" value="v${selected}" hoist>
          ${sources.map((c, i) => `<sl-option value="v${i}">${escapeHtml(c.label)}</sl-option>`).join('')}
        </sl-select>
        <sl-checkbox name="marks">Mark units where the model disagrees with the earlier annotation</sl-checkbox>
        <div class="tr-note">The annotation is applied to the text of the current version by ${escapeHtml(modelLabel)} and saved as a new version. Units with the same text are copied without the model. The current version cannot be edited until the transfer is done.</div>
        <div class="tr-preview" data-name="preview"></div>
        <div class="tr-error" data-name="error"></div>
      </div>
      <sl-button slot="footer" data-name="cancel">Cancel</sl-button>
      <sl-button slot="footer" variant="primary" data-name="start" disabled>Start</sl-button>
    `;
    document.body.appendChild(form);
    /** @param {string} name @returns {any} */
    const field = name => form.querySelector(`[name="${name}"]`);
    const preview = /** @type {HTMLElement} */ (form.querySelector('[data-name="preview"]'));
    const error = /** @type {HTMLElement} */ (form.querySelector('[data-name="error"]'));
    const startButton = /** @type {any} */ (form.querySelector('[data-name="start"]'));
    const current = () => sources[Number(String(field('source').value).slice(1))] ?? null;
    let request = 0;
    const update = async () => {
      const choice = current();
      const mine = ++request;
      startButton.disabled = true;
      error.textContent = '';
      preview.textContent = choice ? 'Comparing the versions…' : '';
      if (!choice) return;
      try {
        const result = await this._transferPreview(targetId, choice.id);
        if (mine !== request) return;
        const { copied, model, unmatched } = result.transfer;
        const unit = result.unit_name === 'pages' ? 'pages' : 'units';
        preview.textContent = `${result.unit_count} ${unit}: ${copied} with the same text (copied without the model), ${model} for the model, ${unmatched} without a counterpart (kept as extracted).`;
        startButton.disabled = false;
      } catch (err) {
        if (mine !== request) return;
        preview.textContent = '';
        error.textContent = err.message;
      }
    };
    form.addEventListener('sl-change', event => {
      if (/** @type {any} */ (event.target)?.getAttribute?.('name') === 'source') update();
    });
    return new Promise(resolve => {
      /** @type {TransferOptions|null} */
      let result = null;
      const close = () => form.hide();
      form.querySelector('[data-name="cancel"]')?.addEventListener('click', close);
      startButton.addEventListener('click', () => {
        const choice = current();
        if (!choice) return;
        result = { sourceStableId: choice.id, sourceLabel: choice.label, marks: Boolean(field('marks').checked) };
        close();
      });
      form.addEventListener('sl-after-hide', event => {
        if (event.target !== form) return;
        form.remove();
        resolve(result);
      });
      Promise.all(['sl-dialog', 'sl-select', 'sl-checkbox'].map(tag => customElements.whenDefined(tag))).then(() => {
        form.show();
        update();
      });
    });
  }

  /**
   * Correct the annotations of the open document with the selected model and save them as a new
   * version, see the file comment. With `transfer`, transfer the annotation of another version
   * instead (runTransfer() asks for it).
   * @param {TransferOptions|null} [transfer]
   * @returns {Promise<void>}
   */
  async runCorrection(transfer = null) {
    const notify = this.getDependency('sl-utils').notify;
    const dialog = this.getDependency('dialog');
    const sourceId = this.state?.xml;
    if (!sourceId || this._running.has(sourceId)) return;
    const model = this.getDependency('inference-settings').getDefaultModel();
    if (!model) {
      notify('No model selected. Choose one via Tools → Inference.', 'warning', 'exclamation-triangle');
      return;
    }
    const label = this.getDependency('inference-settings').getModelLabel(model.providerId, model.modelId) || `${model.providerId}/${model.modelId}`;
    const dialogTitle = transfer ? 'Transfer annotations from another version' : 'Correct annotations in new version';
    const what = transfer ? 'transfer' : 'correction';
    if (!transfer) {
      const confirmed = await dialog.confirm(
        `Correct the annotations of the current document using ${escapeHtml(label)}? The result is saved as a new version. The current version cannot be edited until the correction is done.`,
        dialogTitle
      );
      if (!confirmed || this.state?.xml !== sourceId) return;
    }
    const transferBody = transfer ? { source_stable_id: transfer.sourceStableId, marks: transfer.marks } : {};

    const xmleditor = this.getDependency('xmleditor');
    try {
      await xmleditor.saveIfDirty();
    } catch (err) {
      notify(`The document could not be saved, so its annotations cannot be ${transfer ? 'transferred' : 'corrected'}: ${err.message}`, 'danger', 'exclamation-octagon');
      return;
    }
    const title = this._documentTitle(sourceId);
    this._running.add(sourceId);
    this._updateMenuState();
    // before the server takes over the lock, so that the editor's heartbeat does not report it as lost
    const wasReadOnly = Boolean(this.state?.editorReadOnly);
    if (!wasReadOnly) await this.dispatchStateChange({ editorReadOnly: true });

    /** @type {StartResult|null} */
    let job = null;
    try {
      job = /** @type {StartResult} */ (await this.callPluginApi('/api/plugins/auto-annotation/correct/start', 'POST', {
        stable_id: sourceId, provider_id: model.providerId, model_id: model.modelId, ...transferBody,
      }));
    } catch (err) {
      notify(`The annotations cannot be ${transfer ? 'transferred' : 'corrected'}: ${err.message}`, 'danger', 'exclamation-octagon');
      this._running.delete(sourceId);
      if (!wasReadOnly && this.state?.xml === sourceId) await this.dispatchStateChange({ editorReadOnly: false });
      this._updateMenuState();
      return;
    }

    const jobBody = { job_id: job.job_id, stable_id: sourceId };
    const unitName = job.unit_name ?? 'units';
    const progress = this.getDependency('progress');
    const progressId = `auto-annotation-${job.job_id}`;
    const heading = `${transfer ? 'Transferring' : 'Correcting'} annotations${title ? ` of "${title}"` : ''} using ${label}…`;
    const doubtBatches = job.doubt_batches ?? [];
    const total = job.batches.reduce((sum, b) => sum + b.length, 0) + doubtBatches.reduce((sum, b) => sum + b.length, 0);
    let cancelled = false;
    let lockLost = false;
    progress.show(progressId, { title: transfer ? 'Annotation transfer' : 'Annotation auto-correction', label: `${heading} (0 of ${transfer ? total : job.unit_count} ${unitName})`, value: 0, cancellable: true, onCancel: () => { cancelled = true; } });
    const refresher = setInterval(() => {
      this.callPluginApi('/api/plugins/auto-annotation/correct/heartbeat', 'POST', jobBody).catch(() => { lockLost = true; });
    }, this._lockRefreshMs);

    /** @type {string|null} */
    let savedId = null;
    try {
      /** @param {'transfer'|'doubts'} mode @returns {(units: number[]) => Promise<BatchResult>} */
      const sendBatch = mode => units => /** @type {Promise<BatchResult>} */ (this.callPluginApi('/api/plugins/auto-annotation/correct/batch', 'POST', {
        ...jobBody, provider_id: model.providerId, model_id: model.modelId, units, ...transferBody, ...(transfer ? { mode } : {}),
      }));
      const outcome = await runCorrection(sendBatch('transfer'), job.batches, {
        isCancelled: () => cancelled || lockLost,
        onProgress: (processed, all, changed) => {
          const of = transfer ? total : all;
          progress.setValue(progressId, Math.round((processed / of) * 100));
          progress.setLabel(progressId, `${heading} (${processed} of ${of} ${unitName}, ${changed} changed)`);
        },
      });
      if (transfer && doubtBatches.length && outcome.state === 'done') {
        const done = outcome.processed;
        const check = await runCorrection(sendBatch('doubts'), doubtBatches, {
          isCancelled: () => cancelled || lockLost,
          onProgress: processed => {
            progress.setValue(progressId, Math.round(((done + processed) / total) * 100));
            progress.setLabel(progressId, `${heading} (checking copied ${unitName}: ${done + processed} of ${total})`);
          },
        });
        for (const [index, doubt] of check.doubts) outcome.doubts.set(index, doubt);
        if (check.state !== 'done') {
          outcome.state = check.state;
          outcome.error = check.error;
        }
      }
      if (lockLost && outcome.state === 'cancelled') {
        outcome.state = 'failed';
        outcome.error = 'The document is no longer locked for the correction.';
      }
      clearInterval(refresher);
      if (outcome.state === 'failed' && lockLost) {
        notify(`The correction stopped: ${outcome.error}`, 'danger', 'exclamation-octagon');
        return;
      }
      if (!outcome.corrections.size && !(transfer && (job.transfer?.copied || outcome.doubts.size))) {
        if (outcome.state === 'done') notify(transfer ? 'There was nothing to transfer; no new version was created.' : `${label} found nothing to correct; no new version was created.`, 'success', 'check-circle');
        else if (outcome.state === 'cancelled') notify(`The ${what} was cancelled.`, 'warning', 'exclamation-triangle');
        else notify(`The ${what} failed: ${outcome.error}`, 'danger', 'exclamation-octagon');
        return;
      }
      if (outcome.state !== 'done') {
        const reason = outcome.state === 'cancelled' ? 'was cancelled' : `failed (${escapeHtml(outcome.error ?? '')})`;
        const keep = await dialog.confirm(
          transfer
            ? `The transfer ${reason}. Save the copied ${unitName} and the ${outcome.corrections.size} ${unitName} transferred by the model so far as a new version?`
            : `The correction ${reason} after ${outcome.processed} of ${job.unit_count} ${unitName}. Save the ${outcome.corrections.size} corrected ${unitName === 'pages' ? 'page' : 'unit'}(s) so far as a new version?`,
          dialogTitle
        );
        if (!keep) return;
      }
      progress.setValue(progressId, null);
      progress.setLabel(progressId, transfer ? 'Saving the new version…' : 'Saving the corrected version…');
      const { xml, changed, marks = [] } = /** @type {{xml: string, changed: number, marks?: {fingerprint: string, comment: string}[]}} */ (await this.callPluginApi('/api/plugins/auto-annotation/correct/finish', 'POST', {
        ...jobBody,
        provider_id: model.providerId, model_id: model.modelId, model_label: label,
        corrections: [...outcome.corrections].map(([index, unitXml]) => ({ index, xml: unitXml })),
        processed: outcome.processed, invalid: outcome.invalid, errors: outcome.errors, seconds: outcome.seconds,
        ...(transfer ? { source_stable_id: transfer.sourceStableId, doubts: [...outcome.doubts].map(([index, comment]) => ({ index, comment })) } : {}),
      }));
      if (transfer && !changed && !marks.length) {
        notify('The transfer changed nothing; no new version was created.', 'success', 'check-circle');
        return;
      }
      const client = this.getDependency('client');
      const saved = /** @type {{file_id: string}} */ (await client.saveXml(xml, sourceId, true));
      savedId = saved.file_id;
      await this._release(jobBody);
      await this.getDependency('file-selection').reload({ refresh: true });
      const summary = `${changed} of ${job.unit_count} ${unitName === 'pages' ? 'page' : 'unit'}(s) changed${marks.length ? `, ${marks.length} marked as doubtful` : ''}`;
      // the marks are set in the loaded version, so a transfer with marks always loads it
      if (this.state?.xml === sourceId || marks.length) {
        await this.getDependency('services').load({ xml: savedId });
        const recorded = marks.length ? await this._recordMarks(model, label, marks) : true;
        const savedText = transfer ? 'The new version was saved and loaded' : 'The corrected version was saved and loaded';
        notify(`${savedText} (${summary}).${recorded ? '' : ' The doubts could not be recorded as review marks.'}`, recorded ? 'success' : 'warning', recorded ? 'check-circle' : 'exclamation-triangle');
        return;
      }
      // the save locked the new version for this session; it is locked again when loaded
      await client.releaseLock(savedId).catch(() => {});
      const load = await dialog.confirm(
        `The annotations${title ? ` of "${escapeHtml(title)}"` : ''} were ${transfer ? 'transferred' : 'corrected'} and saved as a new version (${summary}). Load it now?`,
        transfer ? 'Annotations transferred' : 'Annotations corrected'
      );
      if (load) await this.getDependency('services').load({ xml: savedId });
    } catch (err) {
      notify(`The ${what} failed: ${err.message}`, 'danger', 'exclamation-octagon');
    } finally {
      clearInterval(refresher);
      progress.hide(progressId);
      this._running.delete(sourceId);
      if (savedId === null) {
        await this._release(jobBody);
        // the source can be edited again; loading it acquires the editor's lock
        if (this.state?.xml === sourceId) await this.getDependency('services').load({ xml: sourceId });
      }
      this._updateMenuState();
    }
  }

  /**
   * Record the model's doubts as its not-OK marks in the loaded version (unit-review `setMarks()`,
   * reviewer id as the LLM Reviewer's).
   * @param {{providerId: string, modelId: string}} model
   * @param {string} label - The model's label
   * @param {{fingerprint: string, comment: string}[]} marks
   * @returns {Promise<boolean>} False if the marks could not be recorded
   */
  async _recordMarks(model, label, marks) {
    let unitReview;
    try {
      unitReview = this.getDependency('unit-review');
    } catch {
      return false;
    }
    const id = `llm-${model.providerId}-${model.modelId}`.replace(/[^\w.-]/g, '-');
    try {
      return Boolean(await unitReview.setMarks({ id, name: label }, marks.map(m => ({ fingerprint: m.fingerprint, state: 'not-ok', comment: m.comment }))));
    } catch (err) {
      console.warn(`auto-annotation: could not record the marks: ${err.message}`);
      return false;
    }
  }

  /**
   * Release the job's lock; failures are logged only, the lock expires anyway.
   * @param {{job_id: string, stable_id: string}} jobBody
   * @returns {Promise<void>}
   */
  async _release(jobBody) {
    try {
      await this.callPluginApi('/api/plugins/auto-annotation/correct/release', 'POST', jobBody);
    } catch (err) {
      console.warn(`auto-annotation: could not release the lock of ${jobBody.stable_id}: ${err.message}`);
    }
  }
}
